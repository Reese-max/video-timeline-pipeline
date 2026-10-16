import io
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts import check_offline


class OfflineCheckTests(unittest.TestCase):
    def test_missing_ffmpeg_fails_before_tests(self):
        with patch.object(check_offline.shutil, "which", return_value=None), \
             patch.object(check_offline.subprocess, "run") as run, \
             patch.object(check_offline.sys, "stderr", io.StringIO()):
            self.assertEqual(check_offline.main(), 1)
            run.assert_not_called()

    def test_failed_command_blocks_release_and_stops_checks(self):
        with patch.object(check_offline.shutil, "which", return_value="available"), \
             patch.object(check_offline.importlib.util, "find_spec", return_value=True), \
             patch.object(check_offline.subprocess, "run") as run:
            run.return_value.returncode = 7
            self.assertEqual(check_offline.main(), 7)
            run.assert_called_once()
            self.assertIn("MCP_ROOT", run.call_args.kwargs["env"])

        for index, script in ((5, "tests/test_parallel_submission.cjs"),
                              (6, "tests/test_widget_analysis_report.cjs"),
                              (7, "tests/test_widget_library_experiment.cjs"),
                              (8, "tests/test_dashboard_ui.cjs")):
            with self.subTest(command=script), \
                 patch.object(check_offline.shutil, "which", return_value="available"), \
                 patch.object(check_offline.importlib.util, "find_spec", return_value=True), \
                 patch.object(check_offline.subprocess, "run") as run:
                run.side_effect = [SimpleNamespace(returncode=0)] * index + [SimpleNamespace(returncode=7)]
                self.assertEqual(check_offline.main(), 7)
                self.assertEqual(run.call_count, index + 1)
                self.assertEqual(run.call_args.args[0], ["node", script])

    def test_archive_checks_do_not_inspect_a_parent_checkout(self):
        parent = Path(check_offline.__file__).resolve().parents[2]
        outcomes = [SimpleNamespace(returncode=0)] * 9 + [SimpleNamespace(returncode=0, stdout=str(parent))]
        with patch.object(check_offline.shutil, "which", return_value="available"), \
             patch.object(check_offline.importlib.util, "find_spec", return_value=True), \
             patch.object(check_offline.subprocess, "run", side_effect=outcomes) as run:
            self.assertEqual(check_offline.main(), 0)
            self.assertEqual(run.call_count, 10)
            expected = [
                ["ffmpeg", "-version"], ["ffprobe", "-version"], ["node", "--version"],
                [check_offline.sys.executable, "-X", "utf8", "-B", "-m", "unittest",
                 "discover", "-s", "tests", "-p", "test_*.py", "-v"],
                ["node", "tests/test_widget_handoff.cjs"],
                ["node", "tests/test_parallel_submission.cjs"],
                ["node", "tests/test_widget_analysis_report.cjs"],
                ["node", "tests/test_widget_library_experiment.cjs"],
                ["node", "tests/test_dashboard_ui.cjs"],
            ]
            self.assertEqual([call.args[0] for call in run.call_args_list[:9]], expected)
            self.assertTrue(all(call.kwargs["cwd"] == parent / Path(check_offline.__file__).resolve().parents[1].name
                                for call in run.call_args_list))
            self.assertEqual(run.call_args.args[0], ["git", "rev-parse", "--show-toplevel"])


if __name__ == "__main__":
    unittest.main()
