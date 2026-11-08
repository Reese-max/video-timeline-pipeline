import datetime
import importlib.util
import io
import json
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "a1-health" / "probe.py"
NOW = datetime.datetime(2026, 10, 22, 12, 0, tzinfo=datetime.timezone.utc)
RUNNING, NEWER = "a" * 40, "b" * 40


def fake_oci(posts):
    def model(**fields):
        return types.SimpleNamespace(**fields)

    def post_metric_data(details):
        posts.append(list(details.metric_data))
        return types.SimpleNamespace(data=types.SimpleNamespace(failed_metrics_count=0))

    client = mock.Mock(post_metric_data=mock.Mock(side_effect=post_metric_data))
    oci = types.ModuleType("oci")
    oci.monitoring = types.SimpleNamespace(
        models=types.SimpleNamespace(MetricDataDetails=model, Datapoint=model, PostMetricDataDetails=model),
        MonitoringClient=mock.Mock(return_value=client))
    oci.auth = types.SimpleNamespace(signers=types.SimpleNamespace(InstancePrincipalsSecurityTokenSigner=mock.Mock()))
    return oci


class A1HealthProbeDeployTests(unittest.TestCase):
    def setUp(self):
        self.posts = []
        with mock.patch.dict(sys.modules, {"oci": fake_oci(self.posts)}):
            spec = importlib.util.spec_from_file_location("a1_health_probe_under_test", SCRIPT)
            self.probe = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(self.probe)
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.status = self.dir / "last.json"
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(self.probe, "AUTODEPLOY_STATUS", self.status).start()
        mock.patch.object(self.probe, "BEHIND_STATE", self.dir / "behind.json").start()
        self.deploying_patch = mock.patch.object(self.probe, "autodeploy_running", return_value=False)
        self.deploying = self.deploying_patch.start()
        self.main_patch = mock.patch.object(self.probe, "main_sha", return_value=RUNNING)
        self.main = self.main_patch.start()

    def problems(self, local=None, last=None, now=NOW):
        if last is not None:
            self.status.write_text(json.dumps(last), encoding="utf-8")
        with redirect_stderr(io.StringIO()):
            return self.probe.deploy_problems(local, now)

    def local(self, **fields):
        return {"status": "ok", "commit_sha": RUNNING, "deployment_draining": False, **fields}

    def test_healthy_deploy_has_no_problems(self):
        self.assertEqual(self.problems(self.local(), {"result": "DEPLOYED"}), [])
        self.assertEqual(self.problems(self.local(), {"result": "DEFERRED", "reason": "CI pending"}), [])

    def test_failed_or_rolled_back_last_run_is_a_problem(self):
        for result in ("FAILED", "ROLLED_BACK"):
            with self.subTest(result=result):
                [problem] = self.problems(self.local(), {"result": result, "error": "candidate tests failed"})
                self.assertIn(result, problem)

    def test_missing_or_odd_status_file_is_skipped(self):
        self.assertEqual(self.problems(self.local()), [])
        for raw in ("{not json", "[1, 2]", json.dumps({"result": ["FAILED"]}), "null"):
            with self.subTest(raw=raw):
                self.status.write_text(raw, encoding="utf-8")
                self.assertEqual(self.problems(self.local()), [])

    def test_draining_without_a_deploy_run_is_a_problem(self):
        [problem] = self.problems(self.local(deployment_draining=True))
        self.assertIn("draining", problem)
        self.deploying.return_value = True
        self.assertEqual(self.problems(self.local(deployment_draining=True)), [])
        self.deploying.side_effect = PermissionError("/proc/locks")  # only this check is skipped
        [problem] = self.problems(self.local(deployment_draining=True), {"result": "FAILED"})
        self.assertIn("FAILED", problem)

    def test_behind_main_alarms_after_eighteen_hours_even_while_main_keeps_moving(self):
        self.main.return_value = NEWER
        self.assertEqual(self.problems(self.local()), [])  # first seen behind now
        self.main.return_value = "c" * 40  # another merge: the clock must not restart
        self.assertEqual(self.problems(self.local(), now=NOW + datetime.timedelta(hours=17)), [])
        [problem] = self.problems(self.local(), now=NOW + datetime.timedelta(hours=19))
        self.assertIn("since 2026-10-22 12:00Z", problem)

    def test_a_deploy_or_catching_up_restarts_the_clock(self):
        self.main.return_value = NEWER
        self.problems(self.local())
        self.assertEqual(self.problems(self.local(commit_sha="d" * 40), now=NOW + datetime.timedelta(hours=19)), [])
        self.main.return_value = RUNNING
        self.assertEqual(self.problems(self.local(), now=NOW + datetime.timedelta(hours=20)), [])
        self.assertFalse((self.dir / "behind.json").exists())

    def test_unreachable_github_or_broken_state_skips_only_the_lag_check(self):
        self.main.side_effect = OSError("rate limited")
        self.assertEqual(self.problems(self.local()), [])
        self.assertEqual(len(self.problems(self.local(), {"result": "FAILED"})), 1)
        self.main.side_effect, self.main.return_value = None, NEWER
        (self.dir / "behind.json").write_text("[]", encoding="utf-8")
        self.assertEqual(self.problems(self.local(), {"result": "DEPLOYED"}), [])
        self.assertEqual(json.loads((self.dir / "behind.json").read_text())["running"], RUNNING)

    def test_service_down_only_checks_the_last_run(self):
        self.assertEqual(len(self.problems(None, {"result": "ROLLED_BACK"})), 1)
        self.main.assert_not_called()
        self.deploying.assert_not_called()

    def test_main_sha_reads_the_sha_media_type_and_rejects_anything_else(self):
        self.main_patch.stop()
        for body, expected in ((NEWER.encode() + b"\n", NEWER), (b'{"message": "API rate limit"}', None)):
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = body
            with self.subTest(body=body), mock.patch.object(self.probe.urllib.request, "urlopen", return_value=response) as urlopen:
                if expected:
                    self.assertEqual(self.probe.main_sha(), expected)
                    self.assertEqual(urlopen.call_args.args[0].get_header("Accept"), "application/vnd.github.sha")
                else:
                    self.assertRaises(ValueError, self.probe.main_sha)

    @unittest.skipUnless(sys.platform.startswith("linux"), "/proc/locks is Linux only")
    def test_lock_holder_is_seen_through_proc_locks_without_taking_the_lock(self):
        import fcntl
        self.deploying_patch.stop()
        lock = self.dir / "deploy.lock"
        mock.patch.object(self.probe, "AUTODEPLOY_LOCK", lock).start()
        self.assertFalse(self.probe.autodeploy_running())
        with lock.open("w") as held:
            self.assertFalse(self.probe.autodeploy_running())
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(self.probe.autodeploy_running())
            fcntl.flock(held, fcntl.LOCK_UN)
            self.assertFalse(self.probe.autodeploy_running())

    def run_main(self, local, deploy_check=None):
        mock.patch.object(self.probe, "fetch_health", return_value=local).start()
        mock.patch.object(self.probe, "public_health", return_value=True).start()
        if deploy_check:
            mock.patch.object(self.probe, "deploy_problems", side_effect=deploy_check).start()
        with redirect_stdout(io.StringIO()) as out, redirect_stderr(io.StringIO()):
            code = self.probe.main()
        return code, out.getvalue(), [[(m.name, m.namespace, tuple(sorted(m.dimensions.items())), m.datapoints[0].value)
                                       for m in post] for post in self.posts]

    def test_service_health_is_posted_before_the_deploy_check_and_separately(self):
        seen = []
        def check(local, now):
            seen.append(len(self.posts))  # how many posts had gone out when the check started
            return ["last auto-deploy ROLLED_BACK"]
        code, out, posts = self.run_main(self.local(public_base_url="https://example.test"), check)
        host = ("host", "oracle-agent-hub")
        self.assertEqual((code, seen), (0, [1]))
        self.assertEqual(posts, [
            [("mcp_health", "video_mcp_health", (("check", "local"), host), 1.0),
             ("mcp_health", "video_mcp_health", (("check", "public"), host), 1.0)],
            [("deploy_health", "video_mcp_health", (host,), 0.0)]])
        self.assertIn("posted deploy=0", out)

    def test_a_broken_deploy_check_never_blocks_the_health_metrics(self):
        def check(local, now):
            raise KeyError("boom")
        code, out, posts = self.run_main(self.local(public_base_url="https://example.test"), check)
        self.assertEqual(code, 0)
        self.assertEqual([[name for name, *_ in post] for post in posts], [["mcp_health", "mcp_health"]])
        self.assertIn("posted deploy=unknown", out)


if __name__ == "__main__":
    unittest.main()
