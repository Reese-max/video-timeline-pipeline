"""Windows atomic-replace regressions; no real jobs or providers are invoked."""

from pathlib import Path
import tempfile
import unittest
from unittest import mock

import job_queue


def windows_replace_error(winerror: int) -> PermissionError:
    error = PermissionError(f"synthetic Windows replace error {winerror}")
    error.winerror = winerror
    return error


class AtomicJsonWindowsRetryTests(unittest.TestCase):
    def test_transient_windows_sharing_violation_is_retried(self) -> None:
        for winerror in (5, 32):
            with self.subTest(winerror=winerror), tempfile.TemporaryDirectory() as temporary:
                target = Path(temporary) / "state.json"
                original_replace = job_queue.os.replace
                calls = 0

                def replace(source, destination):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise windows_replace_error(winerror)
                    return original_replace(source, destination)

                with mock.patch.object(job_queue.os, "replace", side_effect=replace), \
                     mock.patch.object(job_queue.time, "sleep") as sleep:
                    job_queue.atomic_json(target, {"status": "complete"})

                self.assertEqual(calls, 2)
                sleep.assert_called_once_with(job_queue.ATOMIC_REPLACE_RETRY_SECONDS)
                self.assertEqual(target.read_text(encoding="utf-8"), '{"status": "complete"}')

    def test_persistent_sharing_violation_still_fails_and_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "state.json"
            error = windows_replace_error(32)
            with mock.patch.object(job_queue.os, "replace", side_effect=error) as replace, \
                 mock.patch.object(job_queue.time, "sleep"):
                with self.assertRaises(PermissionError) as caught:
                    job_queue.atomic_json(target, {"status": "complete"})

            self.assertIs(caught.exception, error)
            self.assertEqual(replace.call_count, job_queue.ATOMIC_REPLACE_ATTEMPTS)
            self.assertEqual(list(target.parent.glob("*.tmp")), [])
            self.assertFalse(target.exists())

    def test_unrelated_replace_error_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "state.json"
            error = windows_replace_error(123)
            with mock.patch.object(job_queue.os, "replace", side_effect=error) as replace, \
                 mock.patch.object(job_queue.time, "sleep") as sleep:
                with self.assertRaises(PermissionError) as caught:
                    job_queue.atomic_json(target, {"status": "complete"})

            self.assertIs(caught.exception, error)
            replace.assert_called_once()
            sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
