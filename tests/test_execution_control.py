import io
import os
import subprocess
import sys
import threading
import time
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
import execution_control
import pipeline
from execution_control import DeadlineExceeded, ExecutionCancelled, OperationBudget


class ExecutionControlTests(unittest.TestCase):
    def tearDown(self):
        execution_control.reset_execution_cancellation()

    def test_job_deadline_caps_operation_budget(self):
        with patch.dict(os.environ, {"VIDEO_JOB_DEADLINE": "1005"}, clear=True), \
             patch("execution_control.time.time", return_value=1000), \
             patch("execution_control.time.monotonic", return_value=20):
            budget = OperationBudget("test", 900, "UNSET")
            self.assertEqual(budget.remaining(), 5)

    def test_expired_or_invalid_deadlines_fail_before_start(self):
        with patch.dict(os.environ, {"VIDEO_JOB_DEADLINE": "1"}, clear=True):
            with self.assertRaises(DeadlineExceeded):
                OperationBudget("test", 900, "UNSET")
        for value in ("nan", "inf", "-2", "garbage"):
            with patch.dict(os.environ, {"VIDEO_PROCESS_TIMEOUT_SECONDS": value}, clear=True):
                with self.assertRaises(ValueError):
                    pipeline.run_external([sys.executable, "-c", "pass"])

    def test_real_child_is_killed_on_timeout(self):
        children = []
        original = subprocess.Popen
        def start(*args, **kwargs):
            child = original(*args, **kwargs)
            children.append(child)
            return child
        with patch.dict(os.environ, {"VIDEO_PROCESS_TIMEOUT_SECONDS": "0.15"}, clear=True), \
             patch("pipeline.subprocess.Popen", side_effect=start):
            with self.assertRaises(DeadlineExceeded):
                pipeline.run_external([sys.executable, "-c", "import time; time.sleep(30)"])
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].poll())

    def test_cooperative_cancellation_stops_child(self):
        timer = threading.Timer(0.2, execution_control.cancel_execution)
        with patch.dict(os.environ, {}, clear=True):
            timer.start()
            try:
                with self.assertRaises(ExecutionCancelled):
                    pipeline.run_external([sys.executable, "-c", "import time; time.sleep(30)"])
            finally:
                timer.cancel()

    def test_retry_after_cannot_reset_total_api_budget(self):
        error = urllib.error.HTTPError("https://example.invalid", 429, "busy", {"Retry-After": "120"}, io.BytesIO(b"{}"))
        with patch.dict(os.environ, {"VIDEO_API_TIMEOUT_SECONDS": "1"}, clear=True), \
             patch("pipeline.urllib.request.urlopen", side_effect=error) as opener, \
             patch("execution_control.time.sleep") as sleep:
            with self.assertRaises(DeadlineExceeded):
                pipeline.request_json("https://example.invalid", {}, method="GET", timeout=30)
        opener.assert_called_once()
        sleep.assert_not_called()

    def test_api_attempt_timeout_is_capped_by_job_deadline(self):
        class Response(io.BytesIO):
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                self.close()
        with patch.dict(os.environ, {"VIDEO_JOB_DEADLINE": str(time.time() + 10)}, clear=True), \
             patch("pipeline.urllib.request.urlopen", return_value=Response(b'{"ok":true}')) as opener:
            self.assertEqual(pipeline.request_json("https://example.invalid", {}, method="GET", timeout=180), {"ok": True})
        self.assertGreater(opener.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(opener.call_args.kwargs["timeout"], 10)

    def test_http_error_redacts_auth_and_url_query(self):
        error = urllib.error.HTTPError("https://example.invalid", 401, "denied", {}, io.BytesIO(b"token=private-secret"))
        with patch.dict(os.environ, {}, clear=True), patch("pipeline.urllib.request.urlopen", side_effect=error):
            with self.assertRaises(RuntimeError) as caught:
                pipeline.request_json("https://example.invalid?token=private-secret", {"Authorization": "Bearer private-secret"}, method="GET", timeout=30)
        self.assertNotIn("private-secret", str(caught.exception))

    def test_chunked_response_checks_total_budget_between_reads(self):
        clock = [0.0]
        class SlowStream:
            def read1(self, _size):
                clock[0] += 1.0
                return b"partial"
        with patch.dict(os.environ, {}, clear=True), patch("execution_control.time.monotonic", side_effect=lambda: clock[0]):
            budget = OperationBudget("test", 2, "UNSET")
            with self.assertRaises(DeadlineExceeded):
                execution_control.read_response(SlowStream(), budget)

    def test_legacy_sampling_does_not_swallow_deadline_in_fallback(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            with patch("pipeline.require_tool", return_value="ffmpeg"), \
                 patch("pipeline.run_external", side_effect=DeadlineExceeded("test deadline")) as run:
                with self.assertRaises(DeadlineExceeded):
                    pipeline.extract_candidates(source, root / "job", 1, 1, 40, False, 0.75)
            run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
