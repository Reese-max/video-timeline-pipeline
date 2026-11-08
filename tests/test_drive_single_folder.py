"""One shared Drive folder holds every video: segments, source and the A report side by side."""
from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import analysis_report
import drive_storage
import mcp_server as server
import pipeline

ENV = {"GOOGLE_DRIVE_ACCESS_TOKEN": "token", "GOOGLE_DRIVE_FOLDER_ID": "parent_1"}
JOB_A = "0123456789ab"
JOB_B = "fedcba987654"


class FakeDrive:
    """Just enough Drive: folders, files and pre-allocated IDs."""

    def __init__(self, ids: list[str]):
        self.ids = list(ids)
        self.remote: dict[str, dict] = {}
        self.folders: list[tuple[str, str, str]] = []
        self.uploads: list[dict] = []

    def new_id(self, token: str) -> str:
        return self.ids.pop(0)

    def get_file(self, token: str, file_id: str):
        return self.remote.get(file_id)

    def create_folder(self, token: str, name: str, parent_id: str, file_id: str | None = None):
        self.folders.append((file_id, name, parent_id))
        folder = {"id": file_id, "name": name, "mimeType": drive_storage.FOLDER_MIME,
                  "parents": [parent_id] if parent_id else [], "trashed": False,
                  "createdTime": "2026-10-01T00:00:00.000Z",
                  "webViewLink": f"https://drive.google.com/drive/folders/{file_id}"}
        self.remote[file_id] = folder
        return folder

    def add_folder(self, file_id: str, name: str, parent_id: str, created: str):
        self.remote[file_id] = {"id": file_id, "name": name, "mimeType": drive_storage.FOLDER_MIME,
                                "parents": [parent_id] if parent_id else [], "trashed": False,
                                "createdTime": created,
                                "webViewLink": f"https://drive.google.com/drive/folders/{file_id}"}
        return file_id

    def list_children(self, token: str, parent_id: str) -> list[dict]:
        return [dict(item) for item in self.remote.values()
                if not item.get("trashed") and parent_id in (item.get("parents") or [])]

    def upload(self):
        def upload_file(token, path, name, parent_id, mime_type="video/mp4", *, file_id=None):
            data = path.read_bytes()
            self.uploads.append({"file_id": file_id, "name": name, "parent": parent_id,
                                  "mime_type": mime_type})
            self.remote[file_id] = {"id": file_id, "name": name, "mimeType": mime_type,
                                    "size": str(len(data)),
                                    "sha256Checksum": hashlib.sha256(data).hexdigest(),
                                    "parents": [parent_id], "trashed": False,
                                    "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"}
            return self.remote[file_id]
        return upload_file

    def patches(self):
        return (patch.dict(drive_storage.os.environ, ENV),
                patch.object(drive_storage, "_access_token", return_value="token"),
                patch.object(drive_storage, "_new_id", side_effect=self.new_id),
                patch.object(drive_storage, "_get_file", side_effect=self.get_file),
                patch.object(drive_storage, "_create_folder", side_effect=self.create_folder),
                patch.object(drive_storage, "_list_children", side_effect=self.list_children),
                patch.object(drive_storage, "_upload_file", side_effect=self.upload()))


def build_job(root: Path, job_id: str, title: str) -> tuple[Path, dict, Path]:
    job = root / job_id
    (job / "segments").mkdir(parents=True)
    source = job / "source.mp4"
    source.write_bytes(f"whole source of {job_id}".encode("utf-8"))
    (job / "segments" / "segment_000.mp4").write_bytes(f"segment of {job_id}".encode("utf-8"))
    manifest = {"version": 2, "status": "ready", "job_id": job_id,
                "source": {"fingerprint": pipeline.file_fingerprint(source), "title": title},
                "segment_seconds": 120, "max_segments": None,
                "segments": [{"index": 0, "filename": "segment_000.mp4", "start_seconds": 0,
                              "end_seconds": 30}]}
    pipeline.write_json(job / "manifest.json", manifest)
    return job, manifest, source


def upload(drive: FakeDrive, job: Path, manifest: dict, source: Path, title: str):
    with ExitStack() as stack:
        for patcher in drive.patches():
            stack.enter_context(patcher)
        return drive_storage.upload_prepared_segments(
            job, manifest["job_id"], title, manifest["source"]["fingerprint"],
            manifest["segments"], source_path=source)


class SharedVideoFolderTests(unittest.TestCase):
    def test_two_videos_share_one_folder_with_prefixed_names(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job_a, manifest_a, source_a = build_job(root, JOB_A, "第一支")
            job_b, manifest_b, source_b = build_job(root, JOB_B, "第二支")
            drive = FakeDrive(["folder_1", "file_a", "src_a", "file_b", "src_b"])
            state_a = upload(drive, job_a, manifest_a, source_a, "第一支")
            state_b = upload(drive, job_b, manifest_b, source_b, "第二支")
            self.assertEqual((state_a["folder_id"], state_b["folder_id"]), ("folder_1", "folder_1"))
            self.assertEqual(drive.folders, [("folder_1", "Video Timeline", "parent_1")])
            self.assertEqual(sorted(item["name"] for item in drive.uploads),
                             ["第一支 - 0123456789ab - segment_000.mp4",
                              "第一支 - 0123456789ab - source.mp4",
                              "第二支 - fedcba987654 - segment_000.mp4",
                              "第二支 - fedcba987654 - source.mp4"])
            self.assertTrue(all(item["parent"] == "folder_1" for item in drive.uploads))
            self.assertEqual(state_a["drive_prefix"], "第一支 - 0123456789ab")
            self.assertEqual(state_a["files"][0]["file_name"], "segment_000.mp4")  # local name
            self.assertEqual(state_a["files"][0]["drive_name"], "第一支 - 0123456789ab - segment_000.mp4")
            self.assertEqual(state_a["source_file"]["drive_name"], "第一支 - 0123456789ab - source.mp4")
            folder_state = json.loads((root / "drive-video-folder.json").read_text(encoding="utf-8"))
            self.assertEqual((folder_state["folder_id"], folder_state["parent_id"]),
                             ("folder_1", "parent_1"))
            self.assertEqual(folder_state["name"], "Video Timeline")

    def test_a_trashed_shared_folder_gets_a_new_id_and_is_never_written_into(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job_a, manifest_a, source_a = build_job(root, JOB_A, "第一支")
            job_b, manifest_b, source_b = build_job(root, JOB_B, "第二支")
            drive = FakeDrive(["folder_1", "file_a", "src_a", "folder_2", "file_b", "src_b"])
            upload(drive, job_a, manifest_a, source_a, "第一支")
            drive.remote["folder_1"]["trashed"] = True
            state_b = upload(drive, job_b, manifest_b, source_b, "第二支")
            self.assertEqual(state_b["folder_id"], "folder_2")
            self.assertEqual(drive.folders[-1], ("folder_2", "Video Timeline", "parent_1"))
            self.assertTrue(all(item["parent"] == "folder_2" for item in drive.uploads[2:]))
            self.assertNotIn("folder_1", [item["parent"] for item in drive.uploads[2:]])

    def test_a_missing_shared_folder_is_recreated_with_a_new_id(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job_a, manifest_a, source_a = build_job(root, JOB_A, "第一支")
            job_b, manifest_b, source_b = build_job(root, JOB_B, "第二支")
            drive = FakeDrive(["folder_1", "file_a", "src_a", "folder_2", "file_b", "src_b"])
            upload(drive, job_a, manifest_a, source_a, "第一支")
            drive.remote.pop("folder_1")  # 404: the folder is gone
            state_b = upload(drive, job_b, manifest_b, source_b, "第二支")
            self.assertEqual(state_b["folder_id"], "folder_2")

    def test_a_reserved_but_uncreated_folder_id_survives_a_crash(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job, manifest, source = build_job(root, JOB_A, "第一支")
            pipeline.write_json(root / "drive-video-folder.json",
                                {"folder_id": "reserved_1", "parent_id": "parent_1",
                                 "name": "Video Timeline", "created": "", "folder_url": ""})
            drive = FakeDrive(["file_a", "src_a"])  # no new folder ID is allocated
            state = upload(drive, job, manifest, source, "第一支")
            self.assertEqual(state["folder_id"], "reserved_1")
            self.assertEqual(drive.folders, [("reserved_1", "Video Timeline", "parent_1")])

    def test_a_per_job_folder_manifest_is_uploaded_again_into_the_shared_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job, manifest, source = build_job(root, JOB_A, "第一支")
            segment = job / "segments" / "segment_000.mp4"
            legacy = {"job_id": JOB_A, "status": "drive_ready", "parent_id": "parent_1",
                      "folder_id": "old_per_job_folder", "source_fingerprint": manifest["source"]["fingerprint"],
                      "local_segments": [{"filename": "segment_000.mp4", "size_bytes": segment.stat().st_size,
                                          "sha256": drive_storage._sha256(segment)}],
                      "files": [{"file_id": "old_file", "file_name": "segment_000.mp4", "verified": True,
                                 "url": "https://drive.google.com/file/d/old_file/view"}],
                      "source_file": {"file_id": "old_source", "file_name": "source.mp4", "verified": True,
                                      "url": "https://drive.google.com/file/d/old_source/view"}}
            pipeline.write_json(job / "drive-manifest.json", legacy)
            drive = FakeDrive(["folder_1", "file_a", "src_a"])
            state = upload(drive, job, manifest, source, "第一支")
            self.assertEqual(state["folder_id"], "folder_1")
            self.assertEqual(state["files"][0]["file_id"], "file_a")  # uploaded again
            self.assertEqual(state["source_file"]["file_id"], "src_a")
            self.assertTrue(all(item["parent"] == "folder_1" for item in drive.uploads))

    def test_a_reused_upload_keeps_the_name_it_already_has_on_drive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job, manifest, source = build_job(root, JOB_A, "第一支")
            drive = FakeDrive(["folder_1", "file_a", "src_a"])
            first = upload(drive, job, manifest, source, "第一支")
            # The title changes, but the files stay where they are: nothing is uploaded again.
            again = upload(drive, job, manifest, source, "改過的標題")
            self.assertEqual(len(drive.uploads), 2)
            self.assertEqual(again["drive_prefix"], first["drive_prefix"])
            self.assertEqual(again["files"][0]["drive_name"], first["files"][0]["drive_name"])
            self.assertEqual(again["source_file"]["drive_name"], first["source_file"]["drive_name"])

    def test_a_broken_folder_state_file_reserves_a_new_id(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job, manifest, source = build_job(root, JOB_A, "第一支")
            (root / "drive-video-folder.json").write_text("{not json", encoding="utf-8")
            drive = FakeDrive(["folder_1", "file_a", "src_a"])
            state = upload(drive, job, manifest, source, "第一支")
            self.assertEqual(state["folder_id"], "folder_1")
            self.assertEqual(drive.folders, [("folder_1", "Video Timeline", "parent_1")])


class SharedFolderRecoveryTests(unittest.TestCase):
    """F4: the shared folder on Drive is reused instead of creating a second one."""

    def test_a_lost_state_file_reuses_the_shared_folder_on_drive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job, manifest, source = build_job(root, JOB_A, "第一支")
            drive = FakeDrive(["file_a", "src_a"])
            drive.add_folder("existing_1", "Video Timeline", "parent_1", "2026-09-01T00:00:00.000Z")
            state = upload(drive, job, manifest, source, "第一支")
            self.assertEqual(state["folder_id"], "existing_1")
            self.assertEqual(drive.folders, [])  # no second shared folder
            folder_state = json.loads((root / "drive-video-folder.json").read_text(encoding="utf-8"))
            self.assertEqual(folder_state["folder_id"], "existing_1")

    def test_the_oldest_shared_folder_wins(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job, manifest, source = build_job(root, JOB_A, "第一支")
            drive = FakeDrive(["file_a", "src_a"])
            drive.add_folder("newer_1", "Video Timeline", "parent_1", "2026-10-01T00:00:00.000Z")
            drive.add_folder("older_1", "Video Timeline", "parent_1", "2026-09-01T00:00:00.000Z")
            state = upload(drive, job, manifest, source, "第一支")
            self.assertEqual(state["folder_id"], "older_1")
            self.assertEqual(drive.folders, [])

    def test_a_trashed_shared_folder_is_not_reused(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job, manifest, source = build_job(root, JOB_A, "第一支")
            drive = FakeDrive(["folder_1", "file_a", "src_a"])
            drive.add_folder("gone_1", "Video Timeline", "parent_1", "2026-09-01T00:00:00.000Z")
            drive.remote["gone_1"]["trashed"] = True
            state = upload(drive, job, manifest, source, "第一支")
            self.assertEqual(state["folder_id"], "folder_1")
            self.assertEqual(drive.folders, [("folder_1", "Video Timeline", "parent_1")])

    def test_a_deleted_shared_folder_is_reused_when_the_state_file_is_lost(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job, manifest, source = build_job(root, JOB_A, "第一支")
            drive = FakeDrive(["file_a", "src_a"])
            drive.add_folder("existing_1", "Video Timeline", "parent_1", "2026-09-01T00:00:00.000Z")
            pipeline.write_json(root / "drive-video-folder.json",
                                {"folder_id": "vanished_1", "parent_id": "parent_1",
                                 "name": "Video Timeline", "created": "2026-09-01T00:00:00.000Z",
                                 "folder_url": "https://drive.google.com/drive/folders/vanished_1"})
            state = upload(drive, job, manifest, source, "第一支")
            self.assertEqual(state["folder_id"], "existing_1")
            self.assertEqual(drive.folders, [])


class ListChildrenTests(unittest.TestCase):
    """_list_children follows Drive's page token."""

    def test_list_children_reads_every_page(self):
        calls: list[str] = []

        def fake_api_json(method, url, token, payload=None):
            calls.append(url)
            if "pageToken=" not in url:
                return {"files": [{"id": "f1", "name": "one"}], "nextPageToken": "page_2"}
            return {"files": [{"id": "f2", "name": "two"}]}

        with patch.object(drive_storage, "_api_json", side_effect=fake_api_json):
            children = drive_storage._list_children("token", "parent_1")
        self.assertEqual([item["id"] for item in children], ["f1", "f2"])
        self.assertEqual(len(calls), 2)
        self.assertIn("pageToken=page_2", calls[1])


class ReportNameTests(unittest.TestCase):
    def report_on_disk(self, job: Path, fingerprint: str) -> dict:
        body = b"# A server analysis\nfixture report\n"
        digest = hashlib.sha256(body).hexdigest()
        folder = job / "reports" / digest
        folder.mkdir(parents=True)
        (folder / analysis_report.NAME).write_bytes(body)
        return {"job_id": JOB_A, "analysis_sha256": "a" * 64, "report_sha256": digest,
                "source_sha256": "b" * 64, "source_fingerprint": fingerprint,
                "size_bytes": len(body), "summary_status": "caller_submitted"}

    def test_the_report_uses_the_job_prefix_and_is_checked_by_name(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job, manifest, source = build_job(root, JOB_A, "第一支")
            drive = FakeDrive(["folder_1", "file_a", "src_a", "report_1"])
            state = upload(drive, job, manifest, source, "第一支")
            report = self.report_on_disk(job, manifest["source"]["fingerprint"])
            with ExitStack() as stack:
                for patcher in drive.patches():
                    stack.enter_context(patcher)
                uploaded = drive_storage.upload_analysis_report(job, report, state)
            self.assertEqual(uploaded["file_name"], "第一支 - 0123456789ab - A_server_analysis.md")
            self.assertEqual(drive.uploads[-1]["name"], "第一支 - 0123456789ab - A_server_analysis.md")
            self.assertEqual(drive.uploads[-1]["parent"], "folder_1")
            self.assertTrue(uploaded["verified"])
            self.assertEqual(uploaded["status"], "report_ready")

    def test_a_renamed_report_is_never_matched_or_overwritten(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job, manifest, source = build_job(root, JOB_A, "第一支")
            drive = FakeDrive(["folder_1", "file_a", "src_a", "report_1"])
            state = upload(drive, job, manifest, source, "第一支")
            report = self.report_on_disk(job, manifest["source"]["fingerprint"])
            with ExitStack() as stack:
                for patcher in drive.patches():
                    stack.enter_context(patcher)
                drive_storage.upload_analysis_report(job, report, state)
                drive.remote["report_1"]["name"] = "someone renamed this"
                with self.assertRaises(ValueError):
                    drive_storage.upload_analysis_report(job, report, state)

    def test_legacy_segments_keep_the_plain_report_name(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            job, manifest, source = build_job(root, JOB_A, "第一支")
            drive = FakeDrive(["folder_1", "file_a", "src_a", "report_1"])
            state = upload(drive, job, manifest, source, "第一支")
            legacy = {k: v for k, v in state.items() if k != "drive_prefix"}
            report = self.report_on_disk(job, manifest["source"]["fingerprint"])
            with ExitStack() as stack:
                for patcher in drive.patches():
                    stack.enter_context(patcher)
                uploaded = drive_storage.upload_analysis_report(job, report, legacy)
            self.assertEqual(uploaded["file_name"], "A_server_analysis.md")
            self.assertEqual(drive.uploads[-1]["name"], "A_server_analysis.md")


class HandoffTextTests(unittest.TestCase):
    def drive_state(self, **extra) -> dict:
        return {"job_id": JOB_A, "folder_id": "folder_1",
                "files": [{"file_id": "file_a", "url": "https://drive.google.com/file/d/file_a/view",
                           "file_name": "segment_000.mp4", "size_bytes": 8}] , **extra}

    def test_the_handoff_names_the_full_drive_file(self):
        prepared = {"job_id": JOB_A, "title": "第一支",
                    "segments": [{"filename": "segment_000.mp4", "start_seconds": 0, "end_seconds": 30}]}
        text = server.build_drive_handoff_text(
            prepared, self.drive_state(drive_prefix="第一支 - 0123456789ab"))
        self.assertIn("檔名為「第一支 - 0123456789ab - A_server_analysis.md」", text)
        self.assertIn("同名檔有多份時用最新的一份", text)
        self.assertIn("A 尚未就緒", text)
        self.assertLess(text.index("限制說明"), text.index("A_server_analysis.md"))

    def test_the_legacy_handoff_keeps_its_wording(self):
        prepared = {"job_id": JOB_A, "title": "第一支", "segments": []}
        text = server.build_drive_handoff_text(prepared, self.drive_state())
        self.assertIn("讀取同一個 Drive 資料夾裡的 A_server_analysis.md", text)
        self.assertNotIn("檔名為「", text)

    def test_merge_text_names_the_drive_file(self):
        text = analysis_report.merge_text({"file_id": "report-1", "report_sha256": "c" * 64,
                                            "file_name": "第一支 - 0123456789ab - A_server_analysis.md"})
        self.assertIn("第一支 - 0123456789ab - A_server_analysis.md", text)
        legacy = analysis_report.merge_text({"file_id": "report-1", "report_sha256": "c" * 64})
        self.assertTrue(legacy.startswith("合併：讀取同資料夾的 A_server_analysis.md"))

    def test_the_handoff_shows_the_drive_name_next_to_the_local_name(self):
        prepared = {"job_id": JOB_A, "title": "第一支",
                    "segments": [{"filename": "segment_000.mp4", "start_seconds": 0, "end_seconds": 30}]}
        state = {"job_id": JOB_A, "folder_id": "folder_1", "drive_prefix": "第一支 - 0123456789ab",
                 "files": [{"file_id": "file_a", "url": "https://drive.google.com/file/d/file_a/view",
                            "file_name": "segment_000.mp4",
                            "drive_name": "第一支 - 0123456789ab - segment_000.mp4",
                            "size_bytes": 8}]}
        text = server.build_drive_handoff_text(prepared, state)
        self.assertIn("第一支 - 0123456789ab - segment_000.mp4（segment_000.mp4）", text)
        # The segment table still uses the local file name.
        self.assertIn("segment_000.mp4: 0–30 秒", text)

    def test_the_handoff_keeps_the_plain_name_when_drive_has_no_other_name(self):
        prepared = {"job_id": JOB_A, "title": "第一支", "segments": []}
        state = {"job_id": JOB_A, "folder_id": "folder_1",
                 "files": [{"file_id": "file_a", "url": "https://drive.google.com/file/d/file_a/view",
                            "file_name": "segment_000.mp4", "size_bytes": 8}]}
        text = server.build_drive_handoff_text(prepared, state)
        self.assertIn("1. segment_000.mp4", text)
        self.assertNotIn("（segment_000.mp4）", text)


if __name__ == "__main__":
    unittest.main()
