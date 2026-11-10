import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import MagicMock, patch

spec = importlib.util.spec_from_file_location("video_deploy_linux", Path(__file__).resolve().parents[1] / "scripts" / "deploy_linux.py")
deploy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deploy)


SHA = "ab" * 20
TOOLS = [{"name": "lookup", "inputSchema": {"type": "object", "required": ["id"],
          "properties": {"id": {"type": "string", "description": "Old description"}}},
          "annotations": {"readOnlyHint": True}}]


def archive(files):
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as stream:
        for name, value in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            stream.addfile(member, io.BytesIO(value))
    return data.getvalue()


class LinuxDeployTests(unittest.TestCase):
    def test_active_job_lock_defers_before_switch_or_process_signal(self):
        import pipeline
        with tempfile.TemporaryDirectory() as name:
            shared = Path(name).resolve() / "shared"
            path = pipeline.job_writer_path(shared / "mcp_jobs", "a" * 12)
            @contextlib.contextmanager
            def portable_lock(path):
                try:
                    with pipeline.file_writer_lock(path): yield
                except pipeline.WriterBusyError:
                    raise deploy.Deferred("LOCK_BUSY: " + path.name) from None
            with (pipeline.file_writer_lock(path), patch.object(deploy, "locked", portable_lock),
                  patch.object(deploy, "stop_verified") as stop, patch.object(deploy, "atomic_current") as switch):
                with self.assertRaisesRegex(deploy.Deferred, "LOCK_BUSY: a{12}"):
                    deploy.switch_idle(shared, Path(name) / "deploy", Path(name) / "new", shared)
            stop.assert_not_called()
            switch.assert_not_called()

    def test_job_lock_probe_holds_one_job_lock_at_a_time(self):
        # One lock file accumulates per job; holding all of them at once would exhaust fds.
        import pipeline
        with tempfile.TemporaryDirectory() as name:
            shared = Path(name).resolve() / "shared"
            for index in range(300):
                path = pipeline.job_writer_path(shared / "mcp_jobs", f"{index:012x}")
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"0")
            held, probed, peak = set(), [], [0]
            @contextlib.contextmanager
            def counting_lock(path):
                held.add(path)
                if path.parent.name == ".job-locks":
                    probed.append(path.name)
                    peak[0] = max(peak[0], sum(p.parent.name == ".job-locks" for p in held))
                try: yield
                finally: held.discard(path)
            with (patch.object(deploy, "locked", counting_lock),
                  patch.object(deploy, "stop_verified") as stop, patch.object(deploy, "atomic_current") as switch):
                with self.assertRaises(Exception):  # No queue database: stops right after probing.
                    deploy.switch_idle(shared, Path(name) / "deploy", Path(name) / "new", shared)
            self.assertEqual(len(probed), 300)
            self.assertEqual(peak[0], 1)
            stop.assert_not_called()
            switch.assert_not_called()

    def test_runtime_inventory_matches_server(self):
        import mcp_server
        self.assertEqual(set(deploy.RUNTIME_FILES), set(mcp_server.RUNTIME_SHA256))

    def test_clean_environment_preserves_windows_identity_without_secrets(self):
        with patch.dict(os.environ, {"SYSTEMDRIVE": "C:", "USERNAME": "fixture-user", "GROQ_API_KEY": "fixture"}):
            environment = deploy.clean_environment()
        self.assertEqual(environment["SYSTEMDRIVE"], "C:")
        self.assertEqual(environment["USERNAME"], "fixture-user")
        self.assertNotIn("GROQ_API_KEY", environment)

    def test_ci_exact_main_push_and_latest_attempt_are_required(self):
        run = {"id": 100, "run_attempt": 1, "head_sha": SHA, "head_branch": "main",
               "event": "push", "name": "Video Timeline CI", "path": deploy.WORKFLOW,
               "status": "completed", "conclusion": "success", "updated_at": "2026-10-08T10:00:00Z"}
        self.assertEqual(deploy.ci_success([run], SHA)["id"], 100)
        for changed in ({"head_sha": "cd" * 20}, {"head_branch": "dev"},
                        {"event": "pull_request"}, {"name": "Old CI"},
                        {"path": ".github/workflows/another.yml"}):
            with self.subTest(changed=changed), self.assertRaises(deploy.Deferred):
                deploy.ci_success([{**run, **changed}], SHA)
        newer_pr = {**run, "id": 999, "event": "pull_request", "conclusion": "failure"}
        self.assertEqual(deploy.ci_success([run, newer_pr], SHA)["id"], 100)
        for conclusion in ("failure", "cancelled", None):
            latest = {**run, "run_attempt": 2, "conclusion": conclusion}
            with self.subTest(conclusion=conclusion), self.assertRaises(deploy.Deferred):
                deploy.ci_success([run, latest], SHA)

    def test_schema_allows_descriptions_optional_fields_and_new_tools(self):
        newer = copy.deepcopy(TOOLS)
        newer[0]["inputSchema"]["properties"]["id"]["description"] = "New description"
        newer[0]["inputSchema"]["properties"]["extra"] = {"type": "boolean", "default": False}
        newer.append({"name": "additional_tool", "inputSchema": {"type": "object"}})
        deploy.tools_compatible(TOOLS, newer)
        for change in ("required", "type", "enum", "annotations"):
            bad = copy.deepcopy(newer)
            if change == "required":
                bad[0]["inputSchema"]["required"].append("extra")
            elif change in {"type", "enum"}:
                bad[0]["inputSchema"]["properties"]["id"][change] = "integer" if change == "type" else ["x"]
            else:
                bad[0]["annotations"]["readOnlyHint"] = False
            with self.subTest(change=change), self.assertRaises(RuntimeError):
                deploy.tools_compatible(TOOLS, bad)
        with self.assertRaises(RuntimeError):
            deploy.tools_compatible(TOOLS, newer[1:])
        self.assertFalse(deploy.schema_compatible(
            {"type": "object", "required": ["id"]}, {"type": "object"}, output=True))
        closed = {"type": "object", "properties": {"id": {"type": "string"}},
                  "additionalProperties": False}
        expanded = copy.deepcopy(closed)
        expanded["properties"]["optional"] = {"type": "string"}
        self.assertFalse(deploy.schema_compatible(closed, expanded, output=True))

    def test_archive_excludes_runtime_credentials_and_rejects_traversal(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            names = deploy.unpack_code(archive({"pipeline.py": b"code", ".env": b"private",
                ".env.prod": b"private", "mcp_jobs/job.mp4": b"media", "analyses/report.txt": b"data",
                "private.sqlite3": b"database", "tests/fixtures/demo.mp4": b"synthetic"}), root)
            self.assertEqual(names, ["pipeline.py", "tests/fixtures/demo.mp4"])
            self.assertFalse((root / ".env").exists())
            with self.assertRaises(RuntimeError):
                deploy.unpack_code(archive({"../outside.py": b"bad"}), root)

    @unittest.skipUnless(os.name == "posix", "Linux deployment uses POSIX symlinks")
    def test_prepare_tests_before_secrets_and_preserves_dirty_shared_checkout(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            shared, home = root / "dirty-checkout", root / "deploy"
            shared.mkdir(); home.mkdir()
            (shared / "pipeline.py").write_text("valuable uncommitted work")
            (shared / ".env").write_text("GROQ_API_KEY=PRIVATE_FIXTURE\n")
            files = {name: b"fixture source\n" for name in deploy.RUNTIME_FILES}
            files["scripts/check_offline.py"] = b"fixture runner\n"
            tested = []

            def command(args, **kwargs):
                if "archive" in args:
                    return archive(files)
                env = kwargs.get("env", {})
                self.assertNotIn("GROQ_API_KEY", env)
                self.assertNotIn("MCP_ROOT", env)
                if "scripts/check_offline.py" in args:
                    self.assertFalse((kwargs["cwd"] / ".env").exists())
                    tested.append(True)
                return b""

            server = MagicMock()
            server.poll.return_value = 0
            with patch.dict(os.environ, {"GROQ_API_KEY": "INHERITED_SECRET", "MCP_ROOT": str(shared)}), \
                 patch.object(deploy, "command", side_effect=command), \
                 patch.object(deploy.subprocess, "Popen", return_value=server), \
                 patch.object(deploy, "wait_runtime", return_value={"jobs": {"counts": {}}}), \
                 patch.object(deploy, "mcp_tools", return_value=TOOLS):
                release, value = deploy.prepare(root / "mirror", home, shared, SHA, {"id": 100})
            self.assertEqual(tested, [True])
            self.assertEqual((shared / "pipeline.py").read_text(), "valuable uncommitted work")
            self.assertEqual((release / ".env").resolve(), (shared / ".env").resolve())
            self.assertNotIn("PRIVATE_FIXTURE", json.dumps(value))
            self.assertNotIn(".env", value["source_sha256"])

    def test_idle_switch_cannot_signal_a_queued_job(self):
        with tempfile.TemporaryDirectory() as name:
            shared = Path(name) / "shared"
            (shared / ".video-state").mkdir(parents=True)
            import sqlite3
            with contextlib.closing(sqlite3.connect(shared / ".video-state" / "jobs.sqlite3")) as db:
                db.execute("CREATE TABLE jobs(status TEXT)")
                db.execute("INSERT INTO jobs VALUES('queued')")
                db.commit()
            with patch.object(deploy, "locked", side_effect=lambda path: contextlib.nullcontext()), \
                 patch.object(deploy, "http_json", return_value={"writer_busy": False}), \
                 patch.object(deploy, "stop_verified") as stop:
                with self.assertRaises(deploy.Deferred):
                    deploy.switch_idle(shared, Path(name) / "deploy", Path(name) / "new", shared)
            stop.assert_not_called()

    def test_busy_queue_transaction_defers_without_switch_or_signal(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as name:
            shared = Path(name) / "shared"
            (shared / ".video-state").mkdir(parents=True)
            database = shared / ".video-state" / "jobs.sqlite3"
            with contextlib.closing(sqlite3.connect(database)) as busy:
                busy.execute("CREATE TABLE jobs(status TEXT)")
                busy.commit()
                busy.execute("BEGIN IMMEDIATE")
                with patch.object(deploy, "locked", side_effect=lambda path: contextlib.nullcontext()), \
                     patch.object(deploy, "stop_verified") as stop, \
                     patch.object(deploy, "atomic_current") as switch:
                    with self.assertRaisesRegex(deploy.Deferred, "QUEUE_TRANSACTION_BUSY"):
                        deploy.switch_idle(shared, Path(name) / "deploy", Path(name) / "new", shared)
                stop.assert_not_called()
                switch.assert_not_called()

    @unittest.skipUnless(os.name == "posix", "Linux rollback uses POSIX symlinks")
    def test_verification_failure_rolls_back_and_failed_rollback_keeps_drain(self):
        for rollback_fails in (False, True):
            with self.subTest(rollback_fails=rollback_fails), tempfile.TemporaryDirectory() as name:
                root = Path(name)
                shared, home = root / "shared", root / "deploy"
                (shared / "scripts").mkdir(parents=True)
                (shared / ".video-state").mkdir()
                launcher = shared / "scripts" / "start-mcp-supervised.sh"
                launcher.write_text("# VIDEO_GITOPS_PROTOCOL=1\n")
                (shared / "mcp_server.py").write_text("old")
                release = home / "releases" / SHA
                release.mkdir(parents=True)
                (release / "mcp_server.py").write_text("new")
                deploy.atomic_current(home / "current", shared)
                old_hashes = deploy.hashes(shared, ["mcp_server.py"])
                value = {"sha": SHA, "tools": TOOLS, "runtime_sha256": deploy.hashes(release, ["mcp_server.py"])}
                old_health = {"status": "ok", "runtime_sha256": old_hashes,
                              "public_base_url": "https://example.test/video"}
                switches = []
                original_read = Path.read_text

                def read(path, *args, **kwargs):
                    if str(path).replace("\\", "/") == "/tmp/sand-desktop/d1/video-timeline-mcp.json":
                        return json.dumps({"argv": ["/bin/bash", str(launcher), "d1", "--run-server"]})
                    return original_read(path, *args, **kwargs)

                def switch(shared, home, target, code, **kwargs):
                    switches.append(target)
                    if len(switches) == 2 and rollback_fails:
                        raise RuntimeError("rollback identity failed")
                    deploy.atomic_current(home / "current", target)

                def verify(base, expected, sha=None):
                    if expected == value["runtime_sha256"]:
                        raise RuntimeError("injected public hash mismatch")
                    return old_health

                with patch.object(Path, "read_text", new=read), \
                     patch.object(deploy, "http_json", return_value=old_health), \
                     patch.object(deploy, "mcp_tools", return_value=TOOLS), \
                     patch.object(deploy, "switch_idle", side_effect=switch), \
                     patch.object(deploy, "command"), \
                     patch.object(deploy, "wait_runtime", return_value={"deployment_draining": True}), \
                     patch.object(deploy, "verify_runtime", side_effect=verify):
                    with self.assertRaises(RuntimeError):
                        deploy.activate(shared, home, release, value, "https://example.test/video")
                self.assertEqual(switches, [release, shared])
                self.assertEqual((shared / ".video-state" / "deployment-draining").exists(), rollback_fails)
                self.assertEqual((home / "current").resolve(), release if rollback_fails else shared)

    @unittest.skipUnless(os.name == "posix", "Linux rollback uses POSIX symlinks")
    def test_rollback_smoke_is_a_temporary_fixture(self):
        self.assertEqual(deploy.rollback_smoke(), {"status": "ROLLBACK_SMOKE_OK", "production_verified": False})


if __name__ == "__main__":
    unittest.main()
