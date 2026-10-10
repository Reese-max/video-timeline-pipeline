"""Actual queue/owned Python workers/OS writer lock; all operation bodies inert."""
import inspect
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import pipeline
from job_queue import JobQueue, TERMINAL

JOB = "0123456789ab"
WORKER = r'''import json,sys,time
from pathlib import Path
import mcp_server as server
import job_worker
work=Path(sys.argv[1]); task=json.loads((work/'input.json').read_text(encoding='utf-8'))
args=task['inputs']; marker=Path(args['marker'])
if args.get('wrong_lease') and 'parent_writer_lease' in task:
    task['parent_writer_lease']['token']='0'*32
    (work/'input.json').write_text(json.dumps(task),encoding='utf-8')
def inert(**unused):
    marker.write_text(json.dumps({'start':time.time(),'operation':task['operation']}),encoding='utf-8')
    release=args.get('release')
    while release and not Path(release).exists(): time.sleep(.02)
    return {'job_id':args.get('job_id','0123456789ab'),'fixture':True}
server.prepare_video_sync=inert
server.analyze_prepared_sync=inert
server.upload_prepared_drive_sync=inert
server.tool_payload=lambda result:result
job_worker.main()
'''

class PairedQueueExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.worker = self.root / "inert_worker.py"
        self.worker.write_text(WORKER, encoding="utf-8")
        self.environment = patch.dict(os.environ, {"MCP_ROOT": str(self.root / "mcp"), "PYTHONPATH": str(Path(__file__).resolve().parents[1])})
        self.environment.start()
        self.queues = []
        self.releases = []

    def tearDown(self):
        # Release owned test barriers independently of the assertions; this is
        # fixture cleanup, not evidence of product operation completion.
        for file in self.releases:
            file.touch()
        try:
            for queue in self.queues:
                queue.close()
        finally:
            self.environment.stop()
            self.temp.cleanup()

    def queue(self, paired=True):
        kwargs = {"worker": self.worker, "autostart": False, "timeout_seconds": 60}
        # Same source reaches old genuine worker paths instead of failing on a
        # candidate-only constructor parameter or import.
        if paired and "paired_same_job" in inspect.signature(JobQueue).parameters:
            writer = self.root / "mcp/mcp_jobs/.writer.lock"
            kwargs.update(paired_same_job=True, writer_path=writer,
                          writer_context=lambda: pipeline.file_writer_lock(writer))
        queue = JobQueue(self.root / ("paired" if paired else "serial"), **kwargs)
        self.queues.append(queue)
        return queue

    def submit(self, queue, kind, name, *, hold=False, job=JOB, wrong_lease=False):
        marker = self.root / (name + ".started.json")
        args = {"marker": str(marker), "job_id": job, "wrong_lease": wrong_lease}
        if hold:
            release = self.root / (name + ".release")
            self.releases.append(release)
            args["release"] = str(release)
        else:
            release = None
        return queue.submit(kind, args, job), marker, release

    def await_marker(self, file, seconds=10):
        until = time.monotonic() + seconds
        while time.monotonic() < until and not file.exists():
            time.sleep(.02)
        self.assertTrue(file.exists(), "Actual inert worker must reach its body within the original <10s start-gap context")
        return json.loads(file.read_text(encoding="utf-8"))

    def await_state(self, queue, ident, states=TERMINAL):
        until = time.monotonic() + 60
        while time.monotonic() < until:
            result = queue.get(ident)
            if result["status"] in states:
                return result
            time.sleep(.02)
        self.fail("Operation did not reach expected state within original queue test bound")

    def test_actual_drive_completes_while_analysis_body_is_still_blocked(self):
        queue = self.queue()
        analysis, amarker, arelease = self.submit(queue, "analyze", "analysis", hold=True)
        drive, dmarker, _ = self.submit(queue, "drive", "drive")
        queue.start()
        a = self.await_marker(amarker)
        d = self.await_marker(dmarker)
        self.assertLess(abs(d["start"] - a["start"]), 10)
        self.assertEqual(self.await_state(queue, drive["operation_id"])["status"], "completed")
        self.assertEqual(queue.get(analysis["operation_id"])["status"], "running")
        self.assertFalse(arelease.exists())
        with self.assertRaises(pipeline.WriterBusyError):
            with pipeline.file_writer_lock(self.root / "mcp/mcp_jobs/.writer.lock"):
                pass
        arelease.touch()
        self.assertEqual(self.await_state(queue, analysis["operation_id"])["status"], "completed")

    def test_both_actual_roles_start_before_either_barrier_is_released(self):
        queue = self.queue()
        drive, dmarker, drelease = self.submit(queue, "drive", "drive", hold=True)
        analysis, amarker, arelease = self.submit(queue, "analyze", "analysis", hold=True)
        queue.start()
        d = self.await_marker(dmarker)
        a = self.await_marker(amarker)
        self.assertLess(abs(a["start"] - d["start"]), 10)
        self.assertEqual(queue.get(drive["operation_id"])["status"], "running")
        self.assertEqual(queue.get(analysis["operation_id"])["status"], "running")
        self.assertFalse(drelease.exists()); self.assertFalse(arelease.exists())

    def test_cancelling_either_owned_role_does_not_cancel_its_companion(self):
        for cancelled_kind in ("analyze", "drive"):
            with self.subTest(cancelled=cancelled_kind):
                # Independent queue state and source-owned barriers for each case.
                previous = self.root
                self.root = previous / cancelled_kind
                self.root.mkdir()
                try:
                    with patch.dict(os.environ, {"MCP_ROOT": str(self.root / "mcp")}):
                        queue = self.queue()
                        cancelled, cmarker, _ = self.submit(queue, cancelled_kind, "cancelled", hold=True)
                        other_kind = "drive" if cancelled_kind == "analyze" else "analyze"
                        other, omarker, release = self.submit(queue, other_kind, "other", hold=True)
                        queue.start(); self.await_marker(cmarker); self.await_marker(omarker)
                        queue.cancel(cancelled["operation_id"])
                        self.assertEqual(queue.get(cancelled["operation_id"])["status"], "cancelled")
                        self.assertEqual(queue.get(other["operation_id"])["status"], "running")
                        release.touch()
                        self.assertEqual(self.await_state(queue, other["operation_id"])["status"], "completed")
                        queue.close(); self.queues.remove(queue)
                finally:
                    self.root = previous

    def test_prepare_and_other_job_wait_until_pair_finishes_then_keep_primary_fifo(self):
        queue = self.queue()
        analysis, amarker, release = self.submit(queue, "analyze", "analysis", hold=True)
        self.submit(queue, "drive", "drive")
        prepare, pmarker, _ = self.submit(queue, "prepare", "prepare")
        other, omarker, _ = self.submit(queue, "drive", "other-job", job="abcdef012345")
        queue.start(); self.await_marker(amarker); self.await_marker(self.root / "drive.started.json")
        self.assertEqual(queue.get(prepare["operation_id"])["status"], "queued")
        self.assertEqual(queue.get(other["operation_id"])["status"], "queued")
        self.assertFalse(pmarker.exists()); self.assertFalse(omarker.exists())
        release.touch(); p = self.await_marker(pmarker); o = self.await_marker(omarker)
        self.assertLessEqual(p["start"], o["start"])

    def test_default_queue_retains_single_worker_behavior(self):
        queue = self.queue(paired=False)
        analysis, marker, release = self.submit(queue, "analyze", "analysis", hold=True)
        drive, dmarker, _ = self.submit(queue, "drive", "drive")
        queue.start(); self.await_marker(marker)
        self.assertEqual(queue.stats()["worker_limit"], 1)
        self.assertEqual(queue.get(drive["operation_id"])["status"], "queued")
        self.assertFalse(dmarker.exists())
        release.touch(); self.await_marker(dmarker)
        self.assertEqual(self.await_state(queue, analysis["operation_id"])["status"], "completed")

    def test_wrong_private_lease_rejects_before_inert_operation_effect(self):
        queue = self.queue()
        job, marker, _ = self.submit(queue, "analyze", "invalid-lease", wrong_lease=True)
        queue.start()
        self.assertEqual(self.await_state(queue, job["operation_id"])["status"], "failed")
        self.assertFalse(marker.exists(), "A forged internal lease must never reach the operation body")

if __name__ == "__main__":
    unittest.main()
