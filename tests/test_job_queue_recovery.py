"""Recovery regressions; fixtures never invoke media providers or real jobs."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from job_queue import JobQueue


class JobQueueRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.queue = JobQueue(self.root / "state", autostart=False)
        self.addCleanup(self.queue.close)

    def test_input_write_failure_after_claim_is_terminal_and_does_not_launch_worker(self):
        submitted = self.queue.submit("prepare", {"url": "fixture://video"}, "0123456789ab")
        operation_id = submitted["operation_id"]
        row = self.queue._row(operation_id)

        def disk_full_after_claim(*_args, **_kwargs):
            self.assertEqual(self.queue.get(operation_id)["status"], "running")
            raise OSError("fixture: no space left")

        with (patch("job_queue.atomic_json", side_effect=disk_full_after_claim),
              patch("job_queue.subprocess.Popen") as launch):
            self.queue._execute(row)

        launch.assert_not_called()
        result = self.queue.get(operation_id)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["attempts"], 1)
        self.assertIn("OSError", result["error"])
        self.assertEqual(self.queue.stats()["counts"].get("running", 0), 0)
        self.assertIsNone(self.queue._process)
        self.assertEqual(self.queue.protected_job_ids(), set())

    def test_close_retains_lease_if_scheduler_has_not_stopped(self):
        lease = (self.queue.root / "fixture-ownership.lock").open("a+b")
        scheduler = Mock(spec=["join", "is_alive"])
        scheduler.is_alive.return_value = True
        self.queue._lease = lease
        self.queue._thread = scheduler

        try:
            with self.assertRaisesRegex(RuntimeError, "ownership lock retained"):
                self.queue.close()
            scheduler.join.assert_called_once_with(timeout=20)
            self.assertFalse(lease.closed, "A second scheduler must not gain ownership yet")
            self.assertIs(self.queue._lease, lease)
            self.assertTrue(self.queue._stop.is_set())
        finally:
            scheduler.is_alive.return_value = False
            self.queue.close()

        self.assertTrue(lease.closed)
        self.assertIsNone(self.queue._lease)

    @unittest.skipUnless(os.name == "posix", "Real worker process-group death is verified on POSIX")
    def test_real_worker_and_descendant_stop_when_parent_process_dies(self):
        """Kill the actual pipe owner, then observe real job_worker.py exit.

        Only the provider-facing mcp_server module is replaced with a sleeping
        fixture. The worker main, stdin watchdog and process-group kill are real.
        A single parent/worker/descendant tree runs for this test.
        """
        repository = Path(__file__).resolve().parents[1]
        fixture = self.root / "provider-fixture"
        fixture.mkdir()
        work = self.root / "worker-job"
        work.mkdir()
        (work / "input.json").write_text(json.dumps({"operation": "prepare", "inputs": {}}))
        (fixture / "mcp_server.py").write_text(
            "import os,subprocess,sys,time,pipeline\n"
            "from pathlib import Path\n"
            "def prepare_video_sync(**kwargs):\n"
            "    script = \"import os,time; from pathlib import Path; Path(os.environ['FIXTURE_DESCENDANT_PID']).write_text(str(os.getpid())); time.sleep(90)\"\n"
            "    return pipeline.run_external([sys.executable, '-c', script], stream=False)\n"
            "def tool_payload(value): return value\n"
            "def run_job(operation, *args): return operation(*args)\n"
            "def analysis_error(error): return str(error)\n",
            encoding="utf-8",
        )
        launcher = (
            "import runpy,sys; "
            "sys.path[:0]=[sys.argv[1],sys.argv[2]]; "
            "worker=sys.argv[3]; sys.argv=[worker,sys.argv[4]]; "
            "runpy.run_path(worker,run_name='__main__')"
        )
        worker_command = [sys.executable, "-c", launcher, str(fixture), str(repository),
                          str(repository / "job_worker.py"), str(work)]
        specification = self.root / "launch.json"
        specification.write_text(json.dumps(worker_command))
        worker_pid_file = self.root / "worker.pid"
        descendant_pid_file = self.root / "descendant.pid"
        parent_script = (
            "import json,subprocess,sys,time; from pathlib import Path; "
            "command=json.loads(Path(sys.argv[1]).read_text()); "
            "worker=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,"
            "stderr=subprocess.DEVNULL,start_new_session=True); "
            "Path(sys.argv[2]).write_text(str(worker.pid)); time.sleep(90)"
        )
        env = {**os.environ, "VIDEO_JOB_DEADLINE": str(time.time() + 90),
               "VIDEO_JOB_PROGRESS_FILE": str(work / "progress.json"),
               "VIDEO_JOB_WORKER": "1", "FIXTURE_DESCENDANT_PID": str(descendant_pid_file)}
        parent = subprocess.Popen([sys.executable, "-c", parent_script, str(specification), str(worker_pid_file)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, env=env)
        worker_pid = None
        descendant_pid = None
        try:
            deadline = time.monotonic() + 20
            while not descendant_pid_file.exists():
                if parent.poll() is not None:
                    self.fail("Parent fixture exited before the real worker started")
                if time.monotonic() >= deadline:
                    self.fail("The real worker did not reach the sleeping provider fixture")
                time.sleep(.02)
            worker_pid = int(worker_pid_file.read_text())
            descendant_pid = int(descendant_pid_file.read_text())
            self.assertEqual(os.getpgid(worker_pid), worker_pid)
            self.assertEqual(os.getpgid(descendant_pid), worker_pid)
            self.assertTrue(self._running(worker_pid))
            self.assertTrue(self._running(descendant_pid))

            parent.kill()  # Closing the parent's pipe write-end must trigger watch_parent.
            parent.wait(timeout=5)
            deadline = time.monotonic() + 8
            while self._running(worker_pid) or self._running(descendant_pid):
                if time.monotonic() >= deadline:
                    self.fail("Parent death left the worker or its descendant running")
                time.sleep(.02)
            self.assertFalse((work / "output.json").exists())
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            if worker_pid is None and worker_pid_file.exists():
                worker_pid = int(worker_pid_file.read_text())
            if descendant_pid is None and descendant_pid_file.exists():
                descendant_pid = int(descendant_pid_file.read_text())
            if worker_pid is not None:
                for member in (worker_pid, descendant_pid):
                    if member is None:
                        continue
                    try:
                        if os.getpgid(member) == worker_pid:
                            os.killpg(worker_pid, signal.SIGKILL)
                        else:
                            os.kill(member, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    @staticmethod
    def _running(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        # Minimal container PID 1 may reap orphans slowly. A zombie has already
        # stopped and cannot perform provider calls or keep descendants running.
        process_stat = Path(f"/proc/{pid}/stat")
        try:
            if process_stat.exists():
                state = process_stat.read_text().rsplit(")", 1)[1].split()[0]
                if state in {"Z", "X"}:
                    return False
        except FileNotFoundError:
            return False
        return True


if __name__ == "__main__":
    unittest.main()
