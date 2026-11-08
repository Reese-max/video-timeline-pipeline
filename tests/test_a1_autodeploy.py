import http.client
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "a1-autodeploy" / "autodeploy.py"


@unittest.skipUnless(sys.platform.startswith("linux"), "A1 auto-deploy needs Linux (pwd, fcntl)")
class A1AutodeployNetworkTests(unittest.TestCase):
    def setUp(self):
        with mock.patch("pwd.getpwnam", return_value=mock.Mock(pw_uid=1000, pw_gid=1000)):
            spec = importlib.util.spec_from_file_location("a1_autodeploy_under_test", SCRIPT)
            self.m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.m)
        self.addCleanup(mock.patch.stopall)
        self.sleep = mock.patch.object(self.m.time, "sleep").start()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tmp = Path(folder.name)

    def git(self, answers, order=None):
        def fake(*args, **kwargs):
            if order is not None:
                order.append(args[0])
            value = answers[args[0]].pop(0) if isinstance(answers[args[0]], list) else answers[args[0]]
            if isinstance(value, Exception):
                raise value
            return value
        return mock.patch.object(self.m, "git", side_effect=fake).start()

    def waits(self):
        return [call.args[0] for call in self.sleep.call_args_list]

    def test_retry_rides_out_transient_errors_with_growing_waits(self):
        action = mock.Mock(side_effect=[RuntimeError("Could not resolve host: github.com"), OSError("dns"),
                                        http.client.IncompleteRead(b""), subprocess.TimeoutExpired(["git"], 30), "ok"])
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.m.retry_network(action, "git ls-remote", attempts=5), "ok")
        self.assertEqual(self.waits(), [30, 60, 90, 120])
        self.assertIn("git ls-remote failed (attempt 1/5)", out.getvalue())

    def test_lasting_failure_raises_and_other_errors_are_not_retried(self):
        down = mock.Mock(side_effect=RuntimeError("down"))
        with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            self.m.retry_network(down, "x")
        self.assertEqual((down.call_count, self.waits()), (4, [30, 60, 90]))
        for error in (ValueError("bad json"), KeyError("x"), self.m.Deferred("later")):
            broken = mock.Mock(side_effect=error)
            with self.subTest(error=type(error).__name__), self.assertRaises(type(error)):
                self.m.retry_network(broken, "x")
            self.assertEqual(broken.call_count, 1)

    def test_switch_and_rollback_never_fetch(self):
        mock.patch.object(self.m, "run", return_value="").start()
        mock.patch.object(self.m, "DROPIN", self.tmp / "dropin" / "release.conf").start()
        mock.patch.object(self.m, "FREEZE", self.tmp / "freeze.txt").start()
        mock.patch.object(self.m, "file_hash", return_value="same").start()
        for rollback in (True, False):
            order = []
            self.git({"fetch": RuntimeError("network down"), "checkout": ""}, order)
            with self.subTest(rollback=rollback), redirect_stdout(io.StringIO()):
                self.m.switch_to("a" * 40, restore_freeze=rollback)
                self.assertEqual(order, ["checkout"])

    def deploy_mocks(self, order):
        mock.patch.object(self.m, "http_json", return_value={}).start()
        mock.patch.object(self.m, "mcp_tool_names", return_value={"tool"}).start()
        mock.patch.object(self.m, "drain", side_effect=lambda: order.append("drain")).start()
        mock.patch.object(self.m, "switch_to", side_effect=lambda *a, **k: order.append("switch")).start()
        mock.patch.object(self.m, "wait_health", return_value={}).start()
        mock.patch.object(self.m, "verify_public").start()
        mock.patch.object(self.m, "STATE_DIR", self.tmp / "state").start()
        mock.patch.object(self.m, "FREEZE", self.tmp / "state" / "freeze.txt").start()
        mock.patch.object(self.m, "DRAIN_FLAG", self.tmp / "draining").start()

    def test_deploy_fetches_with_retry_before_draining(self):
        order = []
        self.deploy_mocks(order)
        self.git({"fetch": [RuntimeError("Could not resolve host"), ""]}, order)
        with redirect_stdout(io.StringIO()):
            self.m.deploy("a" * 40, "b" * 40)
        self.assertEqual(order, ["fetch", "fetch", "drain", "switch"])

    def test_lasting_fetch_failure_stops_before_draining(self):
        order = []
        self.deploy_mocks(order)
        self.git({"fetch": RuntimeError("Could not resolve host")}, order)
        with redirect_stdout(io.StringIO()), self.assertRaises(RuntimeError):
            self.m.deploy("a" * 40, "b" * 40)
        self.assertEqual(order, ["fetch"] * 4)

    def test_candidate_fetch_is_retried(self):
        sha = "c" * 40
        (self.tmp / "candidate" / ".git").mkdir(parents=True)
        (self.tmp / "venv" / "bin").mkdir(parents=True)
        (self.tmp / "venv" / "bin" / "python").touch()
        mock.patch.object(self.m, "CANDIDATE", self.tmp / "candidate").start()
        mock.patch.object(self.m, "TEST_VENV", self.tmp / "venv").start()
        mock.patch.object(self.m, "run", return_value="").start()
        mock.patch.object(self.m.os, "chown").start()
        order = []
        self.git({"fetch": [OSError("dns"), ""], "checkout": "", "clean": "", "rev-parse": sha}, order)
        with redirect_stdout(io.StringIO()):
            self.m.prepare_candidate(sha)
        self.assertEqual(order, ["fetch", "fetch", "checkout", "clean", "rev-parse"])

    def run_main(self, answers, ci=True):
        self.git(answers)
        ci_passed = (mock.patch.object(self.m, "ci_passed", return_value=ci).start() if isinstance(ci, bool)
                     else mock.patch.object(self.m, "http_json", side_effect=ci).start())
        prepare = mock.patch.object(self.m, "prepare_candidate").start()
        mock.patch.object(self.m, "deploy").start()
        mock.patch.object(self.m, "DRAIN_FLAG", self.tmp / "draining").start()
        mock.patch.object(self.m, "STATE_DIR", self.tmp / "state").start()
        real_open = open
        mock.patch("builtins.open", side_effect=lambda path, *a, **k: real_open(
            self.tmp / "lock" if path == "/run/video-mcp-autodeploy.lock" else path, *a, **k)).start()
        mock.patch.object(sys, "argv", ["autodeploy.py"]).start()
        with redirect_stdout(io.StringIO()) as out:
            code = self.m.main()
        last = self.tmp / "state" / "last.json"  # deploy() is mocked, so a deployment writes no status
        return code, out.getvalue(), last.read_text() if last.exists() else "", ci_passed, prepare

    def test_main_rides_out_an_ls_remote_blip(self):
        sha = "b" * 40
        code, out, status, ci_passed, _ = self.run_main(
            {"status": "", "rev-parse": sha, "ls-remote": [RuntimeError("Could not resolve host: github.com"),
                                                          f"{sha}\trefs/heads/main"]})
        self.assertEqual(code, 0)
        self.assertIn('"UP_TO_DATE"', status)
        ci_passed.assert_not_called()

    def test_truncated_ci_response_is_retried(self):
        sha = "a" * 40
        runs = {"workflow_runs": [{"name": "Video Timeline CI", "head_sha": sha, "id": 7, "run_number": 3,
                                   "run_attempt": 1, "status": "completed", "conclusion": "success"}]}
        code, out, status, http_json, prepare = self.run_main(
            {"status": "", "rev-parse": "b" * 40, "ls-remote": sha + "\trefs/heads/main"},
            ci=[http.client.IncompleteRead(b"{"), runs])
        self.assertEqual((code, http_json.call_count, self.waits()), (0, 2, [30]))
        prepare.assert_called_once_with(sha)
        self.m.deploy.assert_called_once_with(sha, "b" * 40)

    def test_unfinished_ci_defers_without_retrying(self):
        code, out, status, ci_passed, prepare = self.run_main(
            {"status": "", "rev-parse": "b" * 40, "ls-remote": "a" * 40 + "\trefs/heads/main"}, ci=False)
        self.assertEqual(code, 0)
        self.assertIn('"DEFERRED"', status)
        self.assertEqual((ci_passed.call_count, self.waits()), (1, []))
        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()
