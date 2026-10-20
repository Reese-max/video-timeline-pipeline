import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mcp_server
from job_queue import JobQueue, JobBusyError


class DeploymentDrainTests(unittest.TestCase):
    def test_drain_refuses_new_work_and_keeps_accepted_work(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            queue = JobQueue(root / '.video-state', autostart=False)
            accepted = queue.submit('prepare', {'url': 'fixture'})
            (root / '.video-state' / 'deployment-draining').touch()
            try:
                with self.assertRaises(JobBusyError):
                    queue.submit('prepare', {'url': 'new-fixture'})
                self.assertEqual(queue.get(accepted['operation_id'])['status'], 'queued')
                with patch.object(mcp_server, 'MCP_ROOT', root), patch.object(mcp_server, 'MCP_JOBS_ROOT', root / 'mcp_jobs'):
                    with patch.dict('os.environ', {'VIDEO_JOB_WORKER': '0'}):
                        with self.assertRaises(JobBusyError):
                            mcp_server.run_job(lambda: self.fail('new work must not run'))
                    with patch.dict('os.environ', {'VIDEO_JOB_WORKER': '1'}):
                        self.assertEqual(mcp_server.run_job(lambda: 'accepted-work'), 'accepted-work')
                self.assertEqual(mcp_server.tool_error_result(JobBusyError('fixture')).structuredContent['status'], 'busy')
            finally:
                queue.close()


if __name__ == '__main__':
    unittest.main()
