"""Offline concurrency receipts: real inert workers, OS locks and SQLite."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import cost_tracker
import mcp_server as server
import pipeline
from job_queue import JobQueue, WorkerSlots, parallel_job_limit

WORKER = '''import json,sys,time
from pathlib import Path
p=Path(sys.argv[1]); task=json.loads((p/'input.json').read_text())
args=task['inputs']; start=time.time()
Path(args['marker']).write_text(str(start))
while not Path(args['release']).exists(): time.sleep(.01)
(p/'output.json').write_text(json.dumps({'ok':True,'result':{'start':start,'end':time.time()}}))
'''


class ParallelJobsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.worker = self.root / 'worker.py'
        self.worker.write_text(WORKER, encoding='utf-8')
        self.queues = []
        self.releases = []

    def tearDown(self):
        for release in self.releases: release.touch()
        for queue in self.queues: queue.close()
        self.temp.cleanup()

    def queue(self, limit=2, **kwargs):
        with patch.dict(os.environ, {'MCP_MAX_PARALLEL_JOBS': str(limit)}):
            queue = JobQueue(self.root / ('state-' + str(len(self.queues))), worker=self.worker,
                             autostart=False, timeout_seconds=60, **kwargs)
        self.queues.append(queue)
        return queue

    def submit(self, queue, name, job=None):
        marker, release = self.root / (name + '.started'), self.root / (name + '.release')
        self.releases.append(release)
        value = queue.submit('prepare', {'marker': str(marker), 'release': str(release)}, job)
        return value['operation_id'], marker, release

    def wait(self, predicate):
        deadline = time.monotonic() + 15
        while not predicate():
            if time.monotonic() >= deadline: self.fail('inert worker barrier timed out')
            time.sleep(.02)

    def test_T1_T3_different_jobs_overlap_bounded_fifo_and_queue_position(self):
        q = self.queue()
        a, am, ar = self.submit(q, 'a', 'a' * 12)
        b, bm, br = self.submit(q, 'b', 'b' * 12)
        c, cm, cr = self.submit(q, 'c', 'c' * 12)
        self.assertEqual([q.get(i)['queue_position'] for i in (a, b, c)], [0, 1, 2])
        q.start()
        self.wait(lambda: am.exists() and bm.exists())
        self.assertFalse(cm.exists())
        self.assertEqual(q.get(c)['queue_position'], 0)
        self.assertEqual(q.get(c)['running_count'], 2)
        self.assertEqual(q.get(c)['parallel_limit'], 2)
        self.assertEqual(q.stats()['worker_limit'], 2)
        ar.touch()
        self.wait(cm.exists)
        self.assertFalse(br.exists())
        self.assertLessEqual(q.stats()['running_count'], 2)
        br.touch(); cr.touch()
        self.wait(lambda: all(q.get(i)['status'] == 'completed' for i in (a, b, c)))
        ra, rb, rc = [q.get(i)['result'] for i in (a, b, c)]
        self.assertLess(max(ra['start'], rb['start']), min(ra['end'], rb['end']))
        self.assertGreaterEqual(rc['start'], ra['end'])

    def test_T1_limit_one_serializes_different_jobs(self):
        q = self.queue(1)
        a, am, ar = self.submit(q, 'serial-a', 'a' * 12)
        b, bm, br = self.submit(q, 'serial-b', 'b' * 12)
        q.start(); self.wait(am.exists)
        self.assertEqual(q.get(b)['status'], 'queued')
        self.assertFalse(bm.exists())
        ar.touch(); self.wait(bm.exists); br.touch()
        self.wait(lambda: q.get(b)['status'] == 'completed')
        self.assertLessEqual(q.get(a)['result']['end'], q.get(b)['result']['start'])

    def test_T2_same_job_serializes_and_does_not_block_other_job(self):
        q = self.queue()
        a, am, ar = self.submit(q, 'same-a', 'a' * 12)
        b, bm, br = self.submit(q, 'same-b', 'a' * 12)
        c, cm, cr = self.submit(q, 'other', 'c' * 12)
        q.start(); self.wait(lambda: am.exists() and cm.exists())
        self.assertFalse(bm.exists())
        self.assertEqual(q.get(b)['status'], 'queued')
        ar.touch(); self.wait(bm.exists); br.touch(); cr.touch()
        self.wait(lambda: q.get(b)['status'] == 'completed')
        self.assertLessEqual(q.get(a)['result']['end'], q.get(b)['result']['start'])

    def test_operations_without_job_ids_take_independent_slots(self):
        q = self.queue()
        a, am, _ = self.submit(q, 'unknown-a')
        b, bm, _ = self.submit(q, 'unknown-b')
        c, cm, _ = self.submit(q, 'unknown-c')
        q.start(); self.wait(lambda: am.exists() and bm.exists())
        self.assertFalse(cm.exists())
        self.assertEqual(q.get(c)['running_count'], 2)

    def test_queue_positions_count_serial_operations_but_collapse_pairs(self):
        q = self.queue(paired_same_job=True, writer_path=self.root / 'mcp_jobs/.writer.lock',
                       writer_context=pipeline.file_writer_lock)
        a, _, _ = self.submit(q, 'position-a', 'a' * 12)
        b, _, _ = self.submit(q, 'position-b', 'a' * 12)
        analysis = q.submit('analyze', {'job_id': 'b' * 12}, 'b' * 12)['operation_id']
        drive = q.submit('drive', {'job_id': 'b' * 12}, 'b' * 12)['operation_id']
        c, _, _ = self.submit(q, 'position-c', 'c' * 12)
        self.assertEqual([q.get(i)['queue_position'] for i in (a, b, analysis, drive, c)], [0, 1, 2, 2, 3])

    def test_external_job_lock_skips_only_that_job(self):
        q = self.queue(writer_path=self.root / 'mcp_jobs/.writer.lock', writer_context=pipeline.file_writer_lock)
        a, am, _ = self.submit(q, 'locked', 'a' * 12)
        b, bm, _ = self.submit(q, 'free', 'b' * 12)
        with pipeline.file_writer_lock(pipeline.job_writer_path(self.root / 'mcp_jobs', 'a' * 12)):
            q.start(); self.wait(bm.exists)
            self.assertFalse(am.exists())
            self.assertEqual(q.get(a)['status'], 'queued')
        self.wait(am.exists)

    def test_T4_busy_sync_tools_enqueue_and_drain_refuses(self):
        q = self.queue()
        job = 'a' * 12
        with (patch.object(server, 'get_queue', return_value=q),
              patch.object(server, 'MCP_ROOT', q.root.parent),
              patch.object(server, 'prepared_job_dir', return_value=self.root),
              patch.object(server, 'storage_policy'),
              patch.object(server, 'validate_source_url', side_effect=lambda url: url),
              patch.object(server, 'validate_upload_url'),
              patch.object(server.drive_storage, 'configured', return_value=False),
              patch.object(server, 'run_job', side_effect=server.JobBusyError('capacity'))):
            calls = [lambda: server.prepare_video_for_chatgpt('https://video.example/a.mp4'),
                     lambda: server.prepare_uploaded_video({'file_id': 'file-test', 'download_url': 'https://files.oaiusercontent.com/a.mp4'}),
                     lambda: server.analyze_prepared_video(job),
                     lambda: server.upload_prepared_video_to_drive(job)]
            for call in calls:
                result = asyncio.run(call()).structuredContent
                self.assertEqual(result['status'], 'queued', result)
                self.assertEqual(len(result['operation_id']), 32)
                self.assertGreater(result['retry_after_seconds'], 0)
                self.assertNotEqual(result.get('error_code'), 'MCP_BUSY')
            (q.root / 'deployment-draining').touch()
            for call in calls:
                result = asyncio.run(call()).structuredContent
                self.assertEqual(result['status'], 'busy')
                self.assertEqual(result['error_code'], 'MCP_BUSY')

    def test_sync_and_queue_share_capacity_and_job_exclusion(self):
        slots = WorkerSlots()
        q = self.queue(slots=slots)
        with (patch.object(server, 'JOB_SLOTS', slots), patch.object(server, '_QUEUE', q),
              patch.object(server, 'MCP_ROOT', q.root.parent),
              patch.object(server, 'MCP_JOBS_ROOT', self.root / 'mcp_jobs')):
            def check():
                self.assertIn('a' * 12, slots.snapshot())
                with self.assertRaises(server.JobBusyError):
                    server.run_job(lambda: self.fail('same job started'), _job_id='a' * 12)
                return 'ok'
            self.assertEqual(server.run_job(check, _job_id='a' * 12), 'ok')
            self.assertEqual(slots.snapshot(), set())

    def test_cleanup_failure_retains_unit_locks_and_stops_all_admission(self):
        q = self.queue(writer_path=self.root / 'mcp_jobs/.writer.lock', writer_context=pipeline.file_writer_lock)
        a, am, ar = self.submit(q, 'cleanup', 'a' * 12)
        with patch('job_queue.stop_process_tree', return_value=False):
            q.start(); self.wait(am.exists); ar.touch()
            self.wait(lambda: q._cleanup_failed and not q._thread.is_alive())
        # The fake worker actually exited; only its cleanup receipt was rejected.
        self.assertTrue(all(p.poll() is not None for p in q._pair_processes.values()))
        with self.assertRaisesRegex(RuntimeError, 'ownership lock retained'): q.close()
        self.assertIsNotNone(q._lease)
        self.assertFalse(q.slots.acquire('b' * 12))
        with self.assertRaises(pipeline.WriterBusyError):
            with pipeline.file_writer_lock(pipeline.job_writer_path(self.root / 'mcp_jobs', 'a' * 12)): pass
        # Release only test-owned locks after proving actual process exit.
        for context in q._retained_contexts:
            if context is not None: context.__exit__(None, None, None)
        q._retained_contexts.clear()
        q._cleanup_failed = False
        q.slots.failed = False
        q._reap_units()

    def test_cancelling_one_parallel_job_leaves_other_process_running(self):
        q = self.queue()
        a, am, _ = self.submit(q, 'cancel-a', 'a' * 12)
        b, bm, br = self.submit(q, 'cancel-b', 'b' * 12)
        q.start(); self.wait(lambda: am.exists() and bm.exists())
        q.cancel(a)
        self.wait(lambda: 'a' * 12 not in q.slots.snapshot())
        self.assertEqual(q.get(a)['status'], 'cancelled')
        self.assertEqual(q.get(b)['status'], 'running')
        self.assertIsNone(q._pair_processes[b].poll())
        br.touch(); self.wait(lambda: q.get(b)['status'] == 'completed')

    def test_T6_atomic_budget_reservations_never_overspend(self):
        barrier = threading.Barrier(2)
        def reserve(number):
            barrier.wait()
            return cost_tracker.check_and_reserve_budget(self.root, 'parallel-' + str(number), 600000)
        with patch.dict(os.environ, {'DAILY_BUDGET_USD': '1', 'BUDGET_HARD_STOP': 'true', 'COST_TIMEZONE': 'UTC'}):
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(reserve, range(2)))
        self.assertEqual(sum(result[0] for result in results), 1, results)
        with closing(cost_tracker.get_operations_db(self.root)) as db:
            self.assertEqual(db.execute("SELECT SUM(max_micro_usd) FROM budget_reservations WHERE status='reserved'").fetchone()[0], 600000)
            self.assertGreater(db.execute('PRAGMA busy_timeout').fetchone()[0], 0)

    def test_library_initialization_and_writes_are_serialized(self):
        barrier = threading.Barrier(2)
        def write(number):
            barrier.wait()
            with closing(pipeline.index_connection(self.root)) as db, db:
                db.execute('INSERT INTO videos(job,title) VALUES (?,?)', (str(number), str(number)))
                return db.execute("SELECT value FROM library_metadata WHERE key='library_id'").fetchone()[0]
        with ThreadPoolExecutor(max_workers=2) as workers:
            identities = list(workers.map(write, range(2)))
        self.assertEqual(len(set(identities)), 1)
        with closing(pipeline.index_connection(self.root)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM videos').fetchone()[0], 2)

    def test_invalid_limits_log_and_fall_back(self):
        for raw in ('0', '-1', '2.5', 'garbage', ''):
            with patch.dict(os.environ, {'MCP_MAX_PARALLEL_JOBS': raw}), self.assertLogs('job_queue', level='WARNING'):
                self.assertEqual(parallel_job_limit(), 2)
        with patch.dict(os.environ, {'MCP_MAX_PARALLEL_JOBS': '1'}):
            self.assertEqual(parallel_job_limit(), 1)


if __name__ == '__main__': unittest.main()
