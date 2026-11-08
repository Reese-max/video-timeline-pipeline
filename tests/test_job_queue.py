import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from job_queue import JobQueue, TERMINAL

WORKER = '''import json,sys,time,os
from pathlib import Path
p=Path(sys.argv[1]); task=json.loads((p/'input.json').read_text())
time.sleep(task['inputs'].get('delay', 0))
(p/'output.json').write_text(json.dumps({'ok':True,'result':{'job_id':'0123456789ab','observed':task['inputs']}}))
'''


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.worker = self.root / 'fixture_worker.py'
        self.worker.write_text(WORKER, encoding='utf-8')
        self.queues = []

    def tearDown(self):
        for queue in self.queues: queue.close()
        self.temp.cleanup()

    def queue(self, **kwargs):
        q = JobQueue(self.root / 'state', worker=self.worker, **kwargs)
        self.queues.append(q)
        return q

    def wait(self, q, ident, states=TERMINAL):
        until = time.time() + 60
        while time.time() < until:
            result = q.get(ident)
            if result['status'] in states: return result
            time.sleep(.05)
        self.fail('operation did not reach expected state')

    def test_deduplicates_active_and_persists_results(self):
        q = self.queue(autostart=False)
        first = q.submit('prepare', {'delay': .1})
        self.assertEqual(first['operation_id'], q.submit('prepare', {'delay': .1})['operation_id'])
        q.start()
        result = self.wait(q, first['operation_id'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['result']['job_id'], '0123456789ab')
        q.close()
        reopened = self.queue()
        self.assertEqual(reopened.get(first['operation_id'])['result'], result['result'])

    def test_cancel_running_then_resume(self):
        q = self.queue()
        first = q.submit('analyze', {'delay': .5}, '0123456789ab')
        self.wait(q, first['operation_id'], {'running'})
        self.assertEqual(q.cancel(first['operation_id'])['status'], 'cancelled')
        resumed = q.resume(first['operation_id'])
        self.assertNotEqual(resumed['operation_id'], first['operation_id'])
        self.assertEqual(self.wait(q, resumed['operation_id'])['status'], 'completed')
        self.assertEqual(q.get(first['operation_id'])['status'], 'cancelled')

    def test_deadline_releases_worker_for_next_job(self):
        q = self.queue(timeout_seconds=1)
        long = q.submit('prepare', {'delay': 30})
        self.assertEqual(self.wait(q, long['operation_id'])['status'], 'expired')
        q.timeout_seconds = 60  # OS/Python startup latency is not part of this deadline assertion.
        short = q.submit('prepare', {'delay': 0})
        self.assertEqual(self.wait(q, short['operation_id'])['status'], 'completed')

    def test_restart_recovers_reads_but_does_not_replay_uncertain_drive_write(self):
        q = self.queue(autostart=False)
        prepare = q.submit('prepare', {})
        drive = q.submit('drive', {'job_id': '0123456789ab'}, '0123456789ab')
        with q.connect() as db: db.execute("UPDATE jobs SET status='running'")
        q.start()
        self.assertEqual(q.get(drive['operation_id'])['status'], 'interrupted')
        self.assertEqual(self.wait(q, prepare['operation_id'])['status'], 'completed')
        self.assertEqual(self.wait(q, q.resume(drive['operation_id'])['operation_id'])['status'], 'completed')

    def test_queue_limit_and_cancel_before_start(self):
        q = self.queue(autostart=False, max_queued=1)
        first = q.submit('prepare', {'url': 'https://example.test/a'})
        with self.assertRaisesRegex(RuntimeError, 'full'): q.submit('prepare', {'url': 'https://example.test/b'})
        q.cancel(first['operation_id'])
        second = q.submit('prepare', {'url': 'https://example.test/b'})
        q.start()
        self.assertEqual(self.wait(q, second['operation_id'])['status'], 'completed')
        self.assertEqual(q.get(first['operation_id'])['attempts'], 0)

    def test_exclusive_scheduler(self):
        self.queue()
        with self.assertRaises(RuntimeError): self.queue()

    def test_cancel_kills_descendants(self):
        child_marker = self.root / 'child_survived'
        ready_marker = self.root / 'child_pid'
        self.worker.write_text("import subprocess,sys,time\nfrom pathlib import Path\nchild=subprocess.Popen([sys.executable,'-c'," +
                               repr("import time;from pathlib import Path;time.sleep(2);Path(" + repr(str(child_marker)) + ").touch()") +
                               "])\nPath(" + repr(str(ready_marker)) + ").write_text(str(child.pid))\ntime.sleep(30)\n", encoding='utf-8')
        q = self.queue()
        job = q.submit('analyze', {})
        self.wait(q, job['operation_id'], {'running'})
        until = time.time() + 60
        while not ready_marker.exists() and time.time() < until: time.sleep(.05)
        self.assertTrue(ready_marker.exists(), 'Descendant must actually be launched before cancellation')
        q.cancel(job['operation_id'])
        time.sleep(2.5)
        self.assertFalse(child_marker.exists())


if __name__ == '__main__': unittest.main()
