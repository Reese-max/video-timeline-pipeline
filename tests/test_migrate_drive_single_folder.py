"""One-off migration: per-job Drive folders move into the shared "Video Timeline" folder."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import tempfile
import unittest
import urllib.parse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import drive_storage
import mcp_server as server
import pipeline

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "migrate_drive_single_folder", ROOT / "scripts" / "migrate_drive_single_folder.py")
script = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(script)

ENV = {"GOOGLE_DRIVE_ACCESS_TOKEN": "token", "GOOGLE_DRIVE_FOLDER_ID": "parent_1"}
JOB = "0123456789ab"
MTIME_NS = 1_700_000_000_000_000_000
SEGMENT_BYTES = b"segment"
SOURCE_BYTES = b"source"


class FakeDriveApi:
    """An in-memory Drive: files by ID with parents, names, trash state and created time."""

    def __init__(self):
        self.files: dict[str, dict] = {}
        self.requests: list[tuple[str, str, object]] = []
        self._counter = 0

    def add(self, name: str, mime_type: str = "video/mp4", parents=(), size: int = 8,
            sha: str = "a" * 64, created: str = "2026-10-01T00:00:00.000Z") -> str:
        self._counter += 1
        file_id = f"f{self._counter:03d}"
        self.files[file_id] = {"id": file_id, "name": name, "mimeType": mime_type,
                               "size": str(size), "sha256Checksum": sha, "trashed": False,
                               "parents": list(parents), "createdTime": created,
                               "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"}
        return file_id

    def api_json(self, method, url, token, payload=None):
        self.requests.append((method, url, payload))
        parsed = urllib.parse.urlsplit(url)
        path, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if path.endswith("/generateIds"):
            self._counter += 1
            return {"ids": [f"gen{self._counter:03d}"]}
        if path.endswith("/files"):
            if method == "GET":
                return self._list(query)
            if method == "POST":
                return self._create(payload or {})
        file_id = path.rsplit("/", 1)[-1]
        if method == "GET":
            item = self.files.get(file_id)
            if item is None:
                raise drive_storage.DriveAPIError(404)
            return dict(item)
        if method == "PATCH":
            return self._patch(file_id, query, payload or {})
        raise AssertionError(f"unexpected Drive call: {method} {url}")

    def _list(self, query):
        raw = (query.get("q") or [""])[0]
        parent = raw.split("'")[1] if "'" in raw else "root"
        matches = [dict(item) for item in self.files.values()
                   if not item["trashed"] and parent in item["parents"]]
        return {"files": matches}

    def _create(self, payload):
        file_id = payload.get("id") or f"new{len(self.files) + 1}"
        self.files[file_id] = {"id": file_id, "name": payload.get("name"),
                               "mimeType": payload.get("mimeType"), "trashed": False,
                               "parents": list(payload.get("parents") or []),
                               "createdTime": "2026-10-22T00:00:00.000Z",
                               "webViewLink": f"https://drive.google.com/drive/folders/{file_id}"}
        return dict(self.files[file_id])

    def _patch(self, file_id, query, payload):
        item = self.files[file_id]
        remove = (query.get("removeParents") or [""])[0]
        add = (query.get("addParents") or [""])[0]
        if remove:
            item["parents"] = [parent for parent in item["parents"] if parent != remove]
        if add and add not in item["parents"]:
            item["parents"].append(add)
        if payload.get("name"):
            item["name"] = payload["name"]
        if "trashed" in payload:
            item["trashed"] = bool(payload["trashed"])
        return dict(item)

    def writes(self) -> list[tuple[str, str, object]]:
        return [item for item in self.requests
                if item[0] in {"POST", "PATCH"} or "generateIds" in item[1]]


def report_key(job_id: str, analysis_sha: str, report_sha: str, folder_id: str) -> str:
    return hashlib.sha256(json.dumps({"job_id": job_id, "analysis_sha256": analysis_sha,
                                      "report_sha256": report_sha}, sort_keys=True).encode()
                          + folder_id.encode()).hexdigest()


class MigrationFixture:
    """A jobs root plus a Drive that still uses the old per-job folder layout."""

    def __init__(self, root: Path):
        self.root = root
        self.drive = FakeDriveApi()
        self.last_code = 0
        self.shared = self.drive.add("Video Timeline", drive_storage.FOLDER_MIME, ["parent_1"])
        self.old_folder = self.drive.add(f"Video Timeline - 第一支 - {JOB}", drive_storage.FOLDER_MIME,
                                         ["parent_1"], created="2026-09-01T10:20:00.000Z")
        self.segment = self.drive.add("segment_000.mp4", parents=[self.old_folder], size=len(SEGMENT_BYTES),
                                      sha=hashlib.sha256(SEGMENT_BYTES).hexdigest())
        self.source = self.drive.add("source.mp4", parents=[self.old_folder], size=len(SOURCE_BYTES),
                                     sha=hashlib.sha256(SOURCE_BYTES).hexdigest())
        self.report = self.drive.add("A_server_analysis.md", "text/markdown", [self.old_folder],
                                     size=5, sha="c" * 64)
        self.job = root / JOB
        (self.job / "segments").mkdir(parents=True)
        (self.job / "segments" / "segment_000.mp4").write_bytes(SEGMENT_BYTES)
        source_path = self.job / "source.mp4"
        source_path.write_bytes(SOURCE_BYTES)
        os.utime(source_path, ns=(MTIME_NS, MTIME_NS))
        self.fingerprint = pipeline.file_fingerprint(source_path)
        pipeline.write_json(self.job / "manifest.json", {
            "version": 2, "status": "ready", "job_id": JOB,
            "source": {"fingerprint": self.fingerprint, "title": "第一支"},
            "segments": [{"index": 0, "filename": "segment_000.mp4", "start_seconds": 0,
                          "end_seconds": 30}]})
        self.drive_manifest = {
            "job_id": JOB, "status": "drive_ready", "parent_id": "parent_1",
            "folder_id": self.old_folder, "folder_url": f"https://drive.google.com/drive/folders/{self.old_folder}",
            "source_fingerprint": self.fingerprint,
            "local_segments": [{"filename": "segment_000.mp4", "size_bytes": len(SEGMENT_BYTES),
                                "sha256": hashlib.sha256(SEGMENT_BYTES).hexdigest()}],
            "files": [{"file_id": self.segment, "file_name": "segment_000.mp4", "verified": True,
                       "url": f"https://drive.google.com/file/d/{self.segment}/view",
                       "mime_type": "video/mp4", "size_bytes": len(SEGMENT_BYTES),
                       "sha256": hashlib.sha256(SEGMENT_BYTES).hexdigest()}],
            "local_source": {"filename": "source.mp4", "size_bytes": len(SOURCE_BYTES),
                             "sha256": hashlib.sha256(SOURCE_BYTES).hexdigest(), "mtime_ns": MTIME_NS},
            "source_file": {"file_id": self.source, "file_name": "source.mp4", "verified": True,
                            "url": f"https://drive.google.com/file/d/{self.source}/view",
                            "mime_type": "video/mp4", "size_bytes": len(SOURCE_BYTES),
                            "sha256": hashlib.sha256(SOURCE_BYTES).hexdigest(), "mtime_ns": MTIME_NS}}
        pipeline.write_json(self.job / "drive-manifest.json", self.drive_manifest)
        report_folder = self.job / "reports" / ("d" * 64)
        report_folder.mkdir(parents=True)
        (report_folder / "A_server_analysis.md").write_bytes(b"report")
        self.report_state = {"job_id": JOB, "analysis_sha256": "a" * 64, "report_sha256": "c" * 64,
                             "source_sha256": "b" * 64, "source_fingerprint": self.fingerprint,
                             "size_bytes": 5, "folder_id": self.old_folder,
                             "file_name": "A_server_analysis.md", "mime_type": "text/markdown",
                             "file_id": self.report, "status": "report_ready", "verified": True,
                             "url": f"https://drive.google.com/file/d/{self.report}/view"}
        pipeline.write_json(report_folder / f"drive-{report_key(JOB, 'a' * 64, 'c' * 64, self.old_folder)}.json",
                            self.report_state)

    def run(self, *, apply: bool = False, trash_jobs=()) -> list[dict]:
        buffer = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, ENV))
            stack.enter_context(patch.object(drive_storage, "_access_token", return_value="token"))
            stack.enter_context(patch.object(drive_storage, "_api_json", side_effect=self.drive.api_json))
            with redirect_stdout(buffer):
                self.last_code = script.Migration(
                    self.root, {str(job) for job in trash_jobs}, apply).run()
        return [json.loads(line) for line in buffer.getvalue().splitlines() if line.strip()]

    def manifest(self) -> dict:
        return json.loads((self.job / "drive-manifest.json").read_text(encoding="utf-8"))

    def add_job(self, job_id: str, title: str = "第二支") -> dict:
        """Another job with its own old folder in the same Drive and jobs root."""
        old_folder = self.drive.add(f"Video Timeline - {title} - {job_id}", drive_storage.FOLDER_MIME,
                                    ["parent_1"], created="2026-09-03T00:00:00.000Z")
        segment = self.drive.add("segment_000.mp4", parents=[old_folder], size=len(SEGMENT_BYTES),
                                 sha=hashlib.sha256(SEGMENT_BYTES).hexdigest())
        source = self.drive.add("source.mp4", parents=[old_folder], size=len(SOURCE_BYTES),
                                sha=hashlib.sha256(SOURCE_BYTES).hexdigest())
        job = self.root / job_id
        job.mkdir(parents=True, exist_ok=True)
        pipeline.write_json(job / "drive-manifest.json", {
            "job_id": job_id, "status": "drive_ready", "parent_id": "parent_1",
            "folder_id": old_folder, "drive_prefix": f"{title} - {job_id}",
            "files": [{"file_id": segment, "file_name": "segment_000.mp4",
                       "sha256": hashlib.sha256(SEGMENT_BYTES).hexdigest()}],
            "source_file": {"file_id": source, "file_name": "source.mp4",
                            "sha256": hashlib.sha256(SOURCE_BYTES).hexdigest()}})
        return {"job_id": job_id, "old_folder": old_folder, "segment": segment, "source": source}

    def report_state_paths(self) -> list[Path]:
        return sorted((self.job / "reports").glob("*/drive-*.json"))

    def move_to_shared(self, prefix: str) -> None:
        """Simulate the crash window: the files are in the shared folder already."""
        for file_id, name in ((self.segment, "segment_000.mp4"), (self.source, "source.mp4"),
                              (self.report, "A_server_analysis.md")):
            item = self.drive.files[file_id]
            item["parents"] = [self.shared]
            item["name"] = f"{prefix} - {name}"


class MigrationTests(unittest.TestCase):
    def test_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            before = fixture.manifest()
            events = fixture.run(apply=False)
            self.assertEqual(fixture.drive.writes(), [])
            self.assertEqual(fixture.manifest(), before)
            self.assertEqual(list(fixture.job.glob("*.bak-*")), [])
            self.assertEqual(list(fixture.job.glob("reports/*/*.bak-*")), [])
            steps = {event["step"] for event in events}
            self.assertIn("plan_move", steps)
            self.assertIn("plan_trash_folder", steps)
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.drive.files[fixture.segment]["parents"], [fixture.old_folder])

    def test_apply_moves_files_renames_them_and_trashes_the_old_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.run(apply=True)
            prefix = f"第一支 - {JOB}"
            self.assertEqual(fixture.drive.files[fixture.segment]["parents"], [fixture.shared])
            self.assertEqual(fixture.drive.files[fixture.segment]["name"], f"{prefix} - segment_000.mp4")
            self.assertEqual(fixture.drive.files[fixture.source]["name"], f"{prefix} - source.mp4")
            self.assertEqual(fixture.drive.files[fixture.report]["name"], f"{prefix} - A_server_analysis.md")
            self.assertEqual(fixture.drive.files[fixture.report]["sha256Checksum"], "c" * 64)
            self.assertTrue(fixture.drive.files[fixture.old_folder]["trashed"])

    def test_apply_rewrites_the_manifest_and_the_report_state_with_a_backup(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.run(apply=True)
            manifest = fixture.manifest()
            self.assertEqual(manifest["folder_id"], fixture.shared)
            self.assertEqual(manifest["parent_id"], "parent_1")
            self.assertEqual(manifest["drive_prefix"], f"第一支 - {JOB}")
            self.assertEqual(manifest["status"], "drive_ready")
            self.assertEqual(manifest["files"][0]["file_id"], fixture.segment)
            self.assertEqual(manifest["files"][0]["sha256"], hashlib.sha256(SEGMENT_BYTES).hexdigest())
            self.assertEqual(manifest["files"][0]["drive_name"], f"第一支 - {JOB} - segment_000.mp4")
            self.assertEqual(manifest["source_file"]["drive_name"], f"第一支 - {JOB} - source.mp4")
            self.assertEqual(manifest["source_file"]["file_id"], fixture.source)
            backups = list(fixture.job.glob("drive-manifest.json.bak-*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(json.loads(backups[0].read_text(encoding="utf-8"))["folder_id"],
                             fixture.old_folder)
            new_key = report_key(JOB, "a" * 64, "c" * 64, fixture.shared)
            new_state = json.loads((fixture.job / "reports" / ("d" * 64) / f"drive-{new_key}.json")
                                   .read_text(encoding="utf-8"))
            self.assertEqual(new_state["folder_id"], fixture.shared)
            self.assertEqual(new_state["file_name"], f"第一支 - {JOB} - A_server_analysis.md")
            self.assertEqual(new_state["file_id"], fixture.report)
            self.assertTrue(new_state["verified"])
            # The old report state stays readable.
            self.assertTrue(list((fixture.job / "reports" / ("d" * 64)).glob("drive-*.json")))

    def test_the_migrated_job_still_verifies_backs_up_and_uploads_nothing(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.run(apply=True)
            manifest = json.loads((fixture.job / "manifest.json").read_text(encoding="utf-8"))
            state = fixture.manifest()
            with ExitStack() as stack:
                stack.enter_context(patch.dict(os.environ, ENV))
                stack.enter_context(patch.object(drive_storage, "_access_token", return_value="token"))
                stack.enter_context(patch.object(drive_storage, "_api_json", side_effect=fixture.drive.api_json))
                stack.enter_context(patch.object(drive_storage, "_upload_file",
                                                 side_effect=AssertionError("must not upload again")))
                self.assertTrue(drive_storage.backup_complete(state, manifest["segments"],
                                                              fixture.fingerprint))
                self.assertTrue(drive_storage.verify_remote_backup(state))
                completed = server.completed_drive_state(fixture.job, manifest)
                self.assertEqual(completed["folder_id"], fixture.shared)
                self.assertEqual(completed["drive_prefix"], f"第一支 - {JOB}")
                # The handoff shows the real Drive file name next to the local name.
                self.assertEqual(completed["files"][0]["drive_name"],
                                 f"第一支 - {JOB} - segment_000.mp4")
                self.assertIn(f"第一支 - {JOB} - segment_000.mp4（segment_000.mp4）",
                              completed["handoff_text"])
                again = drive_storage.upload_prepared_segments(
                    fixture.job, JOB, "第一支", fixture.fingerprint, manifest["segments"],
                    source_path=fixture.job / "source.mp4")
            self.assertEqual(again["files"][0]["file_id"], fixture.segment)
            self.assertEqual(again["status"], "drive_ready")

    def test_a_second_apply_writes_nothing_to_drive(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.run(apply=True)
            fixture.drive.requests.clear()
            events = fixture.run(apply=True)
            self.assertEqual(fixture.drive.writes(), [])
            self.assertEqual(events[-1]["step"], "done")
            self.assertEqual(events[-1]["old_folders"], 0)

    def test_a_trash_job_drops_the_whole_folder_and_marks_the_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.run(apply=True, trash_jobs=[JOB])
            self.assertTrue(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.drive.files[fixture.segment]["parents"], [fixture.old_folder])
            manifest = fixture.manifest()
            self.assertEqual(manifest["status"], "drive_trashed")
            self.assertEqual(manifest["folder_id"], fixture.old_folder)
            self.assertEqual(len(list(fixture.job.glob("drive-manifest.json.bak-*"))), 1)
            self.assertFalse(drive_storage.backup_complete(manifest, [{"filename": "segment_000.mp4"}],
                                                           fixture.fingerprint))
            self.assertIsNone(server.completed_drive_state(
                fixture.job, json.loads((fixture.job / "manifest.json").read_text(encoding="utf-8"))))

    def test_duplicate_job_folders_get_an_old_version_suffix(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            duplicate = fixture.drive.add(f"Video Timeline - 第一支 - {JOB}", drive_storage.FOLDER_MIME,
                                          ["parent_1"], created="2026-09-05T06:07:00.000Z")
            extra = fixture.drive.add("segment_001.mp4", parents=[duplicate], size=len(SEGMENT_BYTES),
                                      sha=hashlib.sha256(SEGMENT_BYTES).hexdigest())
            fixture.run(apply=True)
            self.assertEqual(fixture.drive.files[fixture.segment]["name"], f"第一支 - {JOB} - segment_000.mp4")
            # The folder the manifest does not point at keeps an old-version marker.
            self.assertEqual(fixture.drive.files[extra]["name"],
                             f"第一支 - {JOB} - 舊版 20260905-0607 - segment_001.mp4")
            self.assertEqual(fixture.drive.files[extra]["parents"], [fixture.shared])
            self.assertTrue(fixture.drive.files[duplicate]["trashed"])
            # The manifest keeps the plain prefix because it points at the first folder.
            self.assertEqual(fixture.manifest()["drive_prefix"], f"第一支 - {JOB}")

    def test_a_name_conflict_keeps_the_old_folder_and_its_files(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            clash = fixture.drive.add(f"第一支 - {JOB} - segment_000.mp4", parents=[fixture.shared],
                                      size=len(SEGMENT_BYTES), sha=hashlib.sha256(SEGMENT_BYTES).hexdigest())
            events = fixture.run(apply=True)
            self.assertIn("name_conflict", {event["step"] for event in events})
            self.assertEqual(fixture.drive.files[fixture.segment]["parents"], [fixture.old_folder])
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertNotEqual(fixture.drive.files[clash]["id"], fixture.segment)

    def test_a_subfolder_stops_the_migration_of_that_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.drive.add("nested", drive_storage.FOLDER_MIME, [fixture.old_folder])
            events = fixture.run(apply=True)
            self.assertIn("skipped_subfolders", {event["step"] for event in events})
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.drive.files[fixture.segment]["parents"], [fixture.old_folder])


class CrashRecoveryTests(unittest.TestCase):
    """F1: an interrupted migration converges on the next run."""

    def test_a_run_interrupted_before_the_manifest_rewrite_converges(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            prefix = f"第一支 - {JOB}"
            fixture.move_to_shared(prefix)  # the files moved, the manifest did not follow
            events = fixture.run(apply=True)
            steps = {event["step"] for event in events}
            self.assertIn("manifest_repaired", steps)
            manifest = fixture.manifest()
            self.assertEqual(manifest["folder_id"], fixture.shared)
            self.assertEqual(manifest["drive_prefix"], prefix)
            self.assertEqual(manifest["files"][0]["drive_name"], f"{prefix} - segment_000.mp4")
            self.assertTrue(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.last_code, 0)
            # A second apply writes nothing to Drive.
            fixture.drive.requests.clear()
            fixture.run(apply=True)
            self.assertEqual(fixture.drive.writes(), [])

    def test_a_run_interrupted_before_the_report_state_converges(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.run(apply=True)
            new_key = report_key(JOB, "a" * 64, "c" * 64, fixture.shared)
            new_state = fixture.job / "reports" / ("d" * 64) / f"drive-{new_key}.json"
            new_state.unlink()  # the manifest was rewritten, the report state was not
            fixture.run(apply=True)
            self.assertTrue(new_state.exists())
            written = json.loads(new_state.read_text(encoding="utf-8"))
            self.assertEqual(written["folder_id"], fixture.shared)
            self.assertEqual(written["file_name"], f"第一支 - {JOB} - A_server_analysis.md")
            fixture.drive.requests.clear()
            fixture.run(apply=True)
            self.assertEqual(fixture.drive.writes(), [])

    def test_a_run_interrupted_before_the_folder_goes_to_the_trash_converges(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.run(apply=True)
            # The empty old folder never made it into the trash.
            fixture.drive.files[fixture.old_folder]["trashed"] = False
            fixture.drive.requests.clear()
            events = fixture.run(apply=True)
            self.assertIn("trashed_folder", {event["step"] for event in events})
            self.assertTrue(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.last_code, 0)
            fixture.drive.requests.clear()
            fixture.run(apply=True)
            self.assertEqual(fixture.drive.writes(), [])

    def test_a_failed_manifest_rewrite_keeps_the_folder_and_fails(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            manifest = fixture.job / "drive-manifest.json"
            real_write = pipeline.write_json

            def failing(path, payload):
                if Path(path) == manifest:
                    raise OSError("no space left on device")
                return real_write(path, payload)

            with patch.object(script.pipeline, "write_json", side_effect=failing):
                events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("manifest_rewrite_failed", {event["step"] for event in events})
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertIn("folder_kept", {event["step"] for event in events})
            # The next run repairs what the failed run left behind.
            fixture.run(apply=True)
            self.assertEqual(fixture.manifest()["folder_id"], fixture.shared)
            self.assertEqual(fixture.last_code, 0)

    def test_a_missing_manifest_file_is_a_problem(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            # The manifest points at a file that is no longer on Drive.
            fixture.drive.files[fixture.segment]["trashed"] = True
            events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("repair_problem", {event["step"] for event in events})

    def test_a_conflicting_report_state_is_reported_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.run(apply=True)
            new_key = report_key(JOB, "a" * 64, "c" * 64, fixture.shared)
            new_state = fixture.job / "reports" / ("d" * 64) / f"drive-{new_key}.json"
            payload = json.loads(new_state.read_text(encoding="utf-8"))
            payload["url"] = "https://drive.google.com/file/d/somebody_else/view"
            pipeline.write_json(new_state, payload)
            events = fixture.run(apply=True)
            self.assertIn("report_state_conflict", {event["step"] for event in events})
            self.assertEqual(fixture.last_code, 1)
            # The other file is never overwritten.
            self.assertEqual(json.loads(new_state.read_text(encoding="utf-8"))["url"],
                             "https://drive.google.com/file/d/somebody_else/view")

    def test_an_orphan_folder_is_not_a_problem(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            orphan_job = "a" * 12
            orphan = fixture.drive.add(f"Video Timeline - 孤兒 - {orphan_job}", drive_storage.FOLDER_MIME,
                                       ["parent_1"], created="2026-09-02T00:00:00.000Z")
            segment = fixture.drive.add("segment_000.mp4", parents=[orphan], size=len(SEGMENT_BYTES),
                                        sha=hashlib.sha256(SEGMENT_BYTES).hexdigest())
            events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 0)
            self.assertIn("manifest_missing", {event["step"] for event in events})
            self.assertEqual(fixture.drive.files[segment]["parents"], [fixture.shared])
            self.assertTrue(fixture.drive.files[orphan]["trashed"])


class ConflictTests(unittest.TestCase):
    """F2/F3: a conflict keeps the whole folder; only reports may share a name."""

    def test_one_conflict_keeps_every_file_of_the_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.drive.add(f"第一支 - {JOB} - segment_000.mp4", parents=[fixture.shared],
                              size=len(SEGMENT_BYTES), sha="e" * 64)
            events = fixture.run(apply=True)
            self.assertIn("name_conflict", {event["step"] for event in events})
            self.assertEqual(fixture.last_code, 1)
            for file_id in (fixture.segment, fixture.source, fixture.report):
                self.assertEqual(fixture.drive.files[file_id]["parents"], [fixture.old_folder])
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])

    def test_a_same_named_report_is_not_a_conflict(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.drive.add(f"第一支 - {JOB} - A_server_analysis.md", "text/markdown",
                              [fixture.shared], size=5, sha="e" * 64)
            events = fixture.run(apply=True)
            self.assertNotIn("name_conflict", {event["step"] for event in events})
            self.assertEqual(fixture.drive.files[fixture.report]["parents"], [fixture.shared])
            self.assertEqual(fixture.drive.files[fixture.report]["name"],
                             f"第一支 - {JOB} - A_server_analysis.md")
            self.assertTrue(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.last_code, 0)

    def test_a_report_already_moved_is_moved_again_on_a_rerun(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            prefix = f"第一支 - {JOB}"
            # The first report version is already in the shared folder; a second one
            # with the same name is still in the old folder.
            fixture.drive.files[fixture.report]["parents"] = [fixture.shared]
            fixture.drive.files[fixture.report]["name"] = f"{prefix} - A_server_analysis.md"
            fixture.run(apply=True)
            self.assertEqual(fixture.drive.files[fixture.report]["parents"], [fixture.shared])
            self.assertEqual(fixture.drive.files[fixture.report]["name"],
                             f"{prefix} - A_server_analysis.md")
            self.assertEqual(fixture.last_code, 0)

    def test_an_identical_duplicate_is_trashed_and_does_not_block_the_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            orphan_job = "b" * 12
            orphan = fixture.drive.add(f"Video Timeline - 重複 - {orphan_job}", drive_storage.FOLDER_MIME,
                                       ["parent_1"], created="2026-09-02T00:00:00.000Z")
            digest = hashlib.sha256(SEGMENT_BYTES).hexdigest()
            duplicate = fixture.drive.add("segment_000.mp4", parents=[orphan], size=len(SEGMENT_BYTES),
                                          sha=digest)
            fixture.drive.add(f"重複 - {orphan_job} - segment_000.mp4", parents=[fixture.shared],
                              size=len(SEGMENT_BYTES), sha=digest)
            events = fixture.run(apply=True)
            self.assertIn("trashed_identical_duplicate", {event["step"] for event in events})
            self.assertEqual(fixture.last_code, 0)
            self.assertTrue(fixture.drive.files[duplicate]["trashed"])
            self.assertTrue(fixture.drive.files[orphan]["trashed"])

    def test_a_duplicate_of_a_manifest_folder_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            digest = hashlib.sha256(SEGMENT_BYTES).hexdigest()
            fixture.drive.add(f"第一支 - {JOB} - segment_000.mp4", parents=[fixture.shared],
                              size=len(SEGMENT_BYTES), sha=digest)
            events = fixture.run(apply=True)
            # The manifest points at this folder: the copy stays, the folder is kept.
            self.assertIn("name_conflict", {event["step"] for event in events})
            self.assertEqual(fixture.drive.files[fixture.segment]["parents"], [fixture.old_folder])
            self.assertEqual(fixture.last_code, 1)


class TrashJobTests(unittest.TestCase):
    """F5: a --trash-job that matches nothing is reported before any early exit."""

    def test_a_missing_trash_job_is_reported_without_old_folders(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.drive.files[fixture.old_folder]["trashed"] = True
            events = fixture.run(apply=True, trash_jobs=["f" * 12])
            self.assertIn("trash_job_not_found", {event["step"] for event in events})
            self.assertEqual(fixture.last_code, 1)

    def test_a_trash_job_that_was_already_trashed_is_not_a_problem(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.run(apply=True, trash_jobs=[JOB])
            fixture.drive.requests.clear()
            events = fixture.run(apply=True, trash_jobs=[JOB])
            self.assertIn("trash_job_already_trashed", {event["step"] for event in events})
            self.assertEqual(fixture.last_code, 0)


class PrefixTests(unittest.TestCase):
    """F6: an old-version folder keeps the title of its own name."""

    def test_an_old_version_folder_uses_its_own_title(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            duplicate = fixture.drive.add(f"Video Timeline - 舊標題 - {JOB}", drive_storage.FOLDER_MIME,
                                          ["parent_1"], created="2026-09-05T06:07:00.000Z")
            extra = fixture.drive.add("segment_001.mp4", parents=[duplicate], size=len(SEGMENT_BYTES),
                                      sha=hashlib.sha256(SEGMENT_BYTES).hexdigest())
            fixture.run(apply=True)
            self.assertEqual(fixture.drive.files[extra]["name"],
                             f"舊標題 - {JOB} - 舊版 20260905-0607 - segment_001.mp4")
            self.assertEqual(fixture.drive.files[fixture.segment]["name"],
                             f"第一支 - {JOB} - segment_000.mp4")
            self.assertEqual(fixture.last_code, 0)


class RepairBlockedTests(unittest.TestCase):
    """G1: a job blocked in the repair phase is not rewritten in the folder phase."""

    def test_a_manifest_file_missing_on_drive_keeps_the_folder_and_fails_twice(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            del fixture.drive.files[fixture.segment]  # the manifest points at a file that is gone
            events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("repair_problem", {event["step"] for event in events})
            kept = [event for event in events if event["step"] == "folder_kept"]
            self.assertTrue(kept)
            self.assertEqual(kept[0]["reason"], "repair_blocked")
            # Nothing moved, the manifest still points at the old folder, the folder is still there.
            self.assertEqual(fixture.drive.files[fixture.source]["parents"], [fixture.old_folder])
            self.assertEqual(fixture.manifest()["folder_id"], fixture.old_folder)
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])
            # The next run reports the same thing instead of quietly finishing the job.
            events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("repair_problem", {event["step"] for event in events})
            self.assertEqual(fixture.manifest()["folder_id"], fixture.old_folder)
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.drive.files[fixture.source]["parents"], [fixture.old_folder])

    def test_a_manifest_that_does_not_match_the_shared_folder_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            manifest = fixture.manifest()
            manifest["status"] = "drive_pending"  # the repair phase does not look at this one
            manifest["files"].append({"file_id": "gone_1", "file_name": "segment_001.mp4",
                                      "sha256": "d" * 64})
            pipeline.write_json(fixture.job / "drive-manifest.json", manifest)
            events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("manifest_verify_failed", {event["step"] for event in events})
            # The manifest still points at the old folder and the old folder is still there.
            self.assertEqual(fixture.manifest()["folder_id"], fixture.old_folder)
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])


class ProtectedFileTests(unittest.TestCase):
    """G2: a file that is still referenced by a local record is never trashed."""

    def test_a_duplicate_of_a_protected_manifest_file_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            digest = hashlib.sha256(SEGMENT_BYTES).hexdigest()
            # The manifest points at a folder that is gone, but the file it records
            # really sits in the old folder.
            manifest = fixture.manifest()
            manifest["folder_id"] = "gone_folder"
            pipeline.write_json(fixture.job / "drive-manifest.json", manifest)
            fixture.drive.add(f"第一支 - {JOB} - segment_000.mp4", parents=[fixture.shared],
                              size=len(SEGMENT_BYTES), sha=digest)
            events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            conflicts = [event for event in events if event["step"] == "name_conflict"]
            self.assertTrue(conflicts)
            self.assertEqual(conflicts[0]["reason"], "protected_file_id")
            # The duplicate is still there and the folder is kept.
            self.assertFalse(fixture.drive.files[fixture.segment]["trashed"])
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.drive.files[fixture.segment]["parents"], [fixture.old_folder])

    def test_a_duplicate_referenced_by_a_report_state_is_a_conflict(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            digest = hashlib.sha256(SEGMENT_BYTES).hexdigest()
            orphan_job = "c" * 12
            orphan = fixture.drive.add(f"Video Timeline - 孤兒 - {orphan_job}",
                                       drive_storage.FOLDER_MIME, ["parent_1"],
                                       created="2026-09-02T00:00:00.000Z")
            duplicate = fixture.drive.add("segment_000.mp4", parents=[orphan],
                                          size=len(SEGMENT_BYTES), sha=digest)
            fixture.drive.add(f"孤兒 - {orphan_job} - segment_000.mp4", parents=[fixture.shared],
                              size=len(SEGMENT_BYTES), sha=digest)
            # A local report state still refers to that file.
            state_folder = fixture.job / "reports" / ("e" * 64)
            state_folder.mkdir(parents=True)
            pipeline.write_json(state_folder / f"drive-{'f' * 64}.json",
                                {"job_id": orphan_job, "analysis_sha256": "a" * 64,
                                 "report_sha256": "e" * 64, "folder_id": orphan,
                                 "file_id": duplicate, "file_name": "segment_000.mp4",
                                 "status": "report_ready", "verified": True})
            events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            conflicts = [event for event in events if event["step"] == "name_conflict"]
            self.assertTrue(conflicts)
            self.assertEqual(conflicts[0]["reason"], "protected_file_id")
            self.assertFalse(fixture.drive.files[duplicate]["trashed"])
            self.assertFalse(fixture.drive.files[orphan]["trashed"])

    def test_an_unreferenced_duplicate_is_still_trashed(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            orphan_job = "b" * 12
            orphan = fixture.drive.add(f"Video Timeline - 重複 - {orphan_job}",
                                       drive_storage.FOLDER_MIME, ["parent_1"],
                                       created="2026-09-02T00:00:00.000Z")
            digest = hashlib.sha256(SEGMENT_BYTES).hexdigest()
            duplicate = fixture.drive.add("segment_000.mp4", parents=[orphan],
                                          size=len(SEGMENT_BYTES), sha=digest)
            fixture.drive.add(f"重複 - {orphan_job} - segment_000.mp4", parents=[fixture.shared],
                              size=len(SEGMENT_BYTES), sha=digest)
            events = fixture.run(apply=True)
            self.assertIn("trashed_identical_duplicate", {event["step"] for event in events})
            self.assertTrue(fixture.drive.files[duplicate]["trashed"])

    def test_a_trash_job_reports_the_protected_files_it_drops(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            events = fixture.run(apply=True, trash_jobs=[JOB])
            infos = [event for event in events if event["step"] == "trash_job_protected_files"]
            self.assertTrue(infos)
            self.assertIn(fixture.segment, infos[0]["file_ids"])
            self.assertIn(fixture.source, infos[0]["file_ids"])
            self.assertIn(fixture.report, infos[0]["file_ids"])
            self.assertTrue(fixture.drive.files[fixture.old_folder]["trashed"])


class LocalWriteFailureTests(unittest.TestCase):
    """G3: a local write failure is contained; the other jobs still finish."""

    def test_a_failed_manifest_write_keeps_one_folder_and_finishes_the_others(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            other = fixture.add_job("fedcba987654", "第二支")
            manifest = fixture.job / "drive-manifest.json"
            real_write = pipeline.write_json

            def failing(path, payload):
                if Path(path) == manifest:
                    raise OSError("no space left on device")
                return real_write(path, payload)

            with patch.object(script.pipeline, "write_json", side_effect=failing):
                events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("manifest_rewrite_failed", {event["step"] for event in events})
            # The failing job keeps its folder and its manifest.
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.manifest()["folder_id"], fixture.old_folder)
            # The other job is done.
            self.assertTrue(fixture.drive.files[other["old_folder"]]["trashed"])
            self.assertEqual(fixture.drive.files[other["segment"]]["parents"], [fixture.shared])

    def test_a_failed_repair_write_does_not_stop_the_other_jobs(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            other = fixture.add_job("fedcba987654", "第二支")
            # The media of the first job is already in the shared folder: the repair
            # phase is the one that writes that manifest.
            fixture.move_to_shared(f"第一支 - {JOB}")
            manifest = fixture.job / "drive-manifest.json"
            real_write = pipeline.write_json

            def failing(path, payload):
                if Path(path) == manifest:
                    raise OSError("no space left on device")
                return real_write(path, payload)

            with patch.object(script.pipeline, "write_json", side_effect=failing):
                events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("manifest_rewrite_failed", {event["step"] for event in events})
            # The repair-phase failure keeps that job's folder...
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.manifest()["folder_id"], fixture.old_folder)
            # ...and the other job still finishes.
            self.assertTrue(fixture.drive.files[other["old_folder"]]["trashed"])
            self.assertEqual(fixture.drive.files[other["segment"]]["parents"], [fixture.shared])

    def test_a_failed_backup_does_not_rewrite_the_manifest(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            real_write_bytes = Path.write_bytes

            def failing(path, data):
                if ".bak-" in str(path):
                    raise OSError("read-only file system")
                return real_write_bytes(path, data)

            with patch.object(Path, "write_bytes", failing):
                events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("backup_failed", {event["step"] for event in events})
            # No backup means no rewrite: the manifest and the folder stay as they are.
            self.assertEqual(fixture.manifest()["folder_id"], fixture.old_folder)
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])

    def test_a_failed_report_state_write_keeps_the_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            new_key = report_key(JOB, "a" * 64, "c" * 64, fixture.shared)
            real_write = pipeline.write_json

            def failing(path, payload):
                if Path(path).name == f"drive-{new_key}.json":
                    raise OSError("no space left on device")
                return real_write(path, payload)

            with patch.object(script.pipeline, "write_json", side_effect=failing):
                events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("report_state_write_failed", {event["step"] for event in events})
            kept = [event for event in events if event["step"] == "folder_kept"]
            self.assertTrue(kept)
            self.assertEqual(kept[0]["reason"], "report_state_write_failed")
            # The manifest is rewritten but the folder waits for the report state.
            self.assertEqual(fixture.manifest()["folder_id"], fixture.shared)
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])
            # The next run writes the missing report state and finishes the job.
            events = fixture.run(apply=True)
            self.assertIn("report_state_written", {event["step"] for event in events})
            self.assertTrue(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.last_code, 0)


class ReportStateRecoveryTests(unittest.TestCase):
    """G4: the media is in the shared folder but the report state is not."""

    def test_a_report_left_in_the_old_folder_is_completed_in_one_apply(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            prefix = f"第一支 - {JOB}"
            # The media is already in the shared folder, the report is still in the old one.
            for file_id, name in ((fixture.segment, "segment_000.mp4"),
                                  (fixture.source, "source.mp4")):
                item = fixture.drive.files[file_id]
                item["parents"] = [fixture.shared]
                item["name"] = f"{prefix} - {name}"
            events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 0)
            new_key = report_key(JOB, "a" * 64, "c" * 64, fixture.shared)
            new_state = fixture.job / "reports" / ("d" * 64) / f"drive-{new_key}.json"
            self.assertTrue(new_state.exists())
            written = json.loads(new_state.read_text(encoding="utf-8"))
            self.assertEqual(written["folder_id"], fixture.shared)
            self.assertEqual(written["file_name"], f"{prefix} - A_server_analysis.md")
            # The report itself made it into the shared folder.
            self.assertEqual(fixture.drive.files[fixture.report]["parents"], [fixture.shared])
            self.assertTrue(fixture.drive.files[fixture.old_folder]["trashed"])

    def test_several_reports_with_the_same_name_in_one_old_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            prefix = f"第一支 - {JOB}"
            second_report = fixture.drive.add("A_server_analysis.md", "text/markdown",
                                              [fixture.old_folder], size=7, sha="d" * 64)
            folder = fixture.job / "reports" / ("d" * 64)
            pipeline.write_json(
                folder / f"drive-{report_key(JOB, 'a' * 64, 'd' * 64, fixture.old_folder)}.json",
                {"job_id": JOB, "analysis_sha256": "a" * 64, "report_sha256": "d" * 64,
                 "source_sha256": "b" * 64, "source_fingerprint": fixture.fingerprint,
                 "size_bytes": 7, "folder_id": fixture.old_folder,
                 "file_name": "A_server_analysis.md", "mime_type": "text/markdown",
                 "file_id": second_report, "status": "report_ready", "verified": True,
                 "url": f"https://drive.google.com/file/d/{second_report}/view"})
            events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 0)
            # Both report versions get their own new key file in the shared folder.
            for report_sha in ("c" * 64, "d" * 64):
                key = report_key(JOB, "a" * 64, report_sha, fixture.shared)
                path = fixture.job / "reports" / ("d" * 64) / f"drive-{key}.json"
                self.assertTrue(path.exists())
                state = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(state["folder_id"], fixture.shared)
                self.assertEqual(state["file_name"], f"{prefix} - A_server_analysis.md")
            # Both report files sit in the shared folder under the same name.
            self.assertEqual(fixture.drive.files[fixture.report]["parents"], [fixture.shared])
            self.assertEqual(fixture.drive.files[second_report]["parents"], [fixture.shared])
            self.assertEqual(fixture.drive.files[fixture.report]["name"],
                             f"{prefix} - A_server_analysis.md")


class InvalidManifestTests(unittest.TestCase):
    """G5: a broken manifest is a problem, an orphan folder is not."""

    def test_a_broken_manifest_is_a_problem_and_keeps_the_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            (fixture.job / "drive-manifest.json").write_text("{not json", encoding="utf-8")
            events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("manifest_invalid", {event["step"] for event in events})
            kept = [event for event in events if event["step"] == "folder_kept"]
            self.assertTrue(kept)
            self.assertEqual(kept[0]["reason"], "manifest_invalid")
            # Nothing is moved and the folder is not dropped.
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])
            self.assertEqual(fixture.drive.files[fixture.segment]["parents"], [fixture.old_folder])

    def test_a_manifest_that_is_not_an_object_is_a_problem(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            (fixture.job / "drive-manifest.json").write_text("[]", encoding="utf-8")
            events = fixture.run(apply=True)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("manifest_invalid", {event["step"] for event in events})
            self.assertFalse(fixture.drive.files[fixture.old_folder]["trashed"])


class SharedCandidateTests(unittest.TestCase):
    """G6: several folders with the shared name are an ambiguity that gets reported."""

    def test_two_shared_folders_are_reported_and_the_pinned_one_is_used(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            fixture.drive.add("Video Timeline", drive_storage.FOLDER_MIME, ["parent_1"],
                              created="2026-09-01T00:00:00.000Z")
            # The local state file pins one of the two.
            pipeline.write_json(fixture.root / "drive-video-folder.json",
                                {"folder_id": fixture.shared, "parent_id": "parent_1",
                                 "name": "Video Timeline"})
            events = fixture.run(apply=False)
            self.assertEqual(fixture.last_code, 1)
            self.assertIn("multiple_shared_candidates", {event["step"] for event in events})
            shared = [event for event in events if event["step"] == "shared_folder"]
            self.assertEqual(shared[0]["folder_id"], fixture.shared)

    def test_drive_storage_prefers_the_pinned_shared_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            fixture = MigrationFixture(Path(raw) / "mcp_jobs")
            older = fixture.drive.add("Video Timeline", drive_storage.FOLDER_MIME, ["parent_1"],
                                      created="2026-09-01T00:00:00.000Z")
            with patch.object(drive_storage, "_api_json", side_effect=fixture.drive.api_json):
                chosen = drive_storage._find_shared_video_folder("token", "parent_1",
                                                                 [fixture.shared])
            self.assertEqual(chosen["id"], fixture.shared)
            # Without a pinned id the oldest folder wins and the ambiguity is warned about.
            stderr = io.StringIO()
            with patch.object(drive_storage, "_api_json", side_effect=fixture.drive.api_json):
                with redirect_stderr(stderr):
                    chosen = drive_storage._find_shared_video_folder("token", "parent_1")
            self.assertEqual(chosen["id"], older)
            self.assertIn("Video Timeline", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
