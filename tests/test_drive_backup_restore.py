"""Drive is the long-term copy of prepared media: backup, restore and Drive-backed cleanup."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import drive_storage
import media_retention as retention
import pipeline
from media_lifecycle import StoragePolicy

JOB = "0123456789ab"
MTIME_NS = 1_700_000_000_000_000_000


def build_job(root: Path, *, now: float) -> tuple[Path, dict, dict]:
    """A prepared, analyzed job whose source and segment are backed up on Drive."""
    job = root / JOB
    (job / "segments").mkdir(parents=True)
    (job / "previews").mkdir()
    source = job / "source.mp4"
    source.write_bytes(b"whole source video")
    os.utime(source, ns=(MTIME_NS, MTIME_NS))
    segment = job / "segments" / "segment_000.mp4"
    segment.write_bytes(b"segment zero")
    (job / "previews" / "preview_000.jpg").write_bytes(b"jpg")
    fingerprint = pipeline.file_fingerprint(source)
    manifest = {"version": 2, "status": "ready", "job_id": JOB,
                "source": {"fingerprint": fingerprint, "title": "demo"},
                "segment_seconds": 120, "max_segments": None,
                "segments": [{"index": 0, "filename": "segment_000.mp4", "preview_filename": "preview_000.jpg"}]}
    pipeline.write_json(job / "manifest.json", manifest)
    pipeline.write_json(job / "analysis" / "result.json", {"status": "partial", "job_id": JOB,
                                                            "source_fingerprint": fingerprint})
    state = {"job_id": JOB, "status": "drive_ready", "parent_id": "", "folder_id": "folder_1",
             "source_fingerprint": fingerprint,
             "local_segments": [{"filename": "segment_000.mp4", "size_bytes": segment.stat().st_size,
                                 "sha256": drive_storage._sha256(segment)}],
             "files": [{"file_id": "seg_1", "file_name": "segment_000.mp4", "verified": True,
                        "url": "https://drive.google.com/file/d/seg_1/view"}],
             "source_file": {"file_id": "src_1", "file_name": "source.mp4", "mime_type": "video/mp4",
                             "size_bytes": source.stat().st_size, "sha256": drive_storage._sha256(source),
                             "mtime_ns": MTIME_NS, "verified": True,
                             "url": "https://drive.google.com/file/d/src_1/view"}}
    pipeline.write_json(job / "drive-manifest.json", state)
    policy = StoragePolicy(root, 10**12, 10**12, 7 * 86400, 0)
    policy.mark_managed(JOB, now=now - 2 * 3600)
    return job, manifest, state


class BackupStateTests(unittest.TestCase):
    def test_backup_complete_requires_verified_source_and_every_segment(self):
        with tempfile.TemporaryDirectory() as raw:
            _, manifest, state = build_job(Path(raw) / "mcp_jobs", now=10**9)
            fingerprint = manifest["source"]["fingerprint"]
            self.assertTrue(drive_storage.backup_complete(state, manifest["segments"], fingerprint))
            for broken in (
                {**state, "status": "uploading"},
                {**state, "source_file": {**state["source_file"], "verified": False}},
                {k: v for k, v in state.items() if k != "source_file"},  # pre-source-backup manifest
                {**state, "files": [{**state["files"][0], "verified": False}]},
                {**state, "source_file": {**state["source_file"], "mtime_ns": MTIME_NS + 1}},
            ):
                with self.subTest(broken=sorted(broken)):
                    self.assertFalse(drive_storage.backup_complete(broken, manifest["segments"], fingerprint))
            self.assertFalse(drive_storage.backup_complete(state, manifest["segments"], "other:1"))

    def test_local_matches_backup_allows_missing_but_not_changed_files(self):
        with tempfile.TemporaryDirectory() as raw:
            job, _, state = build_job(Path(raw) / "mcp_jobs", now=10**9)
            self.assertTrue(drive_storage.local_matches_backup(job, state))
            (job / "segments" / "segment_000.mp4").unlink()
            self.assertTrue(drive_storage.local_matches_backup(job, state))
            (job / "source.mp4").write_bytes(b"edited")
            self.assertFalse(drive_storage.local_matches_backup(job, state))


class RestoreTests(unittest.TestCase):
    def test_restore_downloads_missing_files_verifies_and_resets_fingerprint(self):
        with tempfile.TemporaryDirectory() as raw:
            job, manifest, state = build_job(Path(raw) / "mcp_jobs", now=10**9)
            payloads = {"src_1": (job / "source.mp4").read_bytes(),
                        "seg_1": (job / "segments" / "segment_000.mp4").read_bytes()}
            (job / "source.mp4").unlink()
            (job / "segments" / "segment_000.mp4").unlink()
            requested = []

            def fake_urlopen(request, timeout=0):
                file_id = request.full_url.split("/files/")[1].split("?")[0]
                requested.append(file_id)
                return io.BytesIO(payloads[file_id])

            with patch.object(drive_storage, "_access_token", return_value="token"), \
                 patch.object(drive_storage.urllib.request, "urlopen", side_effect=fake_urlopen):
                restored = drive_storage.restore_prepared_media(job, state)
            self.assertEqual(restored, ["segment_000.mp4", "source.mp4"])
            self.assertEqual(requested, ["seg_1", "src_1"])  # source last completes the media set
            self.assertEqual(pipeline.file_fingerprint(job / "source.mp4"), manifest["source"]["fingerprint"])
            self.assertEqual((job / "segments" / "segment_000.mp4").read_bytes(), payloads["seg_1"])

    def test_restore_never_overwrites_a_different_local_file(self):
        with tempfile.TemporaryDirectory() as raw:
            job, _, state = build_job(Path(raw) / "mcp_jobs", now=10**9)
            payloads = {"src_1": (job / "source.mp4").read_bytes(),
                        "seg_1": (job / "segments" / "segment_000.mp4").read_bytes()}
            (job / "segments" / "segment_000.mp4").unlink()
            (job / "source.mp4").write_bytes(b"newer source that was never backed up")

            def backup_bytes(request, timeout=0):  # Drive returns the genuine backup bytes
                return io.BytesIO(payloads[request.full_url.split("/files/")[1].split("?")[0]])

            with patch.object(drive_storage, "_access_token", return_value="token"), \
                 patch.object(drive_storage.urllib.request, "urlopen", side_effect=backup_bytes) as urlopen:
                with self.assertRaises(RuntimeError):
                    drive_storage.restore_prepared_media(job, state)
            self.assertEqual((job / "source.mp4").read_bytes(), b"newer source that was never backed up")
            self.assertNotIn("src_1", [call.args[0].full_url for call in urlopen.call_args_list])

    def test_corrupt_download_is_rejected_without_leaving_a_partial_file(self):
        with tempfile.TemporaryDirectory() as raw:
            job, _, state = build_job(Path(raw) / "mcp_jobs", now=10**9)
            (job / "source.mp4").unlink()
            with patch.object(drive_storage, "_access_token", return_value="token"), \
                 patch.object(drive_storage.urllib.request, "urlopen",
                              side_effect=lambda request, timeout=0: io.BytesIO(b"tampered bytes!!!!")):
                with self.assertRaises(RuntimeError):
                    drive_storage.restore_prepared_media(job, state)
            self.assertFalse((job / "source.mp4").exists())
            self.assertEqual(list(job.glob("*.part")), [])


class SourceUploadTests(unittest.TestCase):
    def _remote(self, file_id, path, parent="folder_1"):
        return {"id": file_id, "mimeType": "video/mp4", "size": path.stat().st_size,
                "sha256Checksum": drive_storage._sha256(path), "parents": [parent],
                "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"}

    def test_upload_keeps_a_verified_copy_of_the_whole_source(self):
        with tempfile.TemporaryDirectory() as raw:
            job, manifest, _ = build_job(Path(raw) / "mcp_jobs", now=10**9)
            (job / "drive-manifest.json").unlink()
            folder = {"id": "folder_1", "mimeType": drive_storage.FOLDER_MIME, "parents": [],
                      "webViewLink": "https://drive.google.com/drive/folders/folder_1"}
            remote = {"folder_1": folder, "seg_1": self._remote("seg_1", job / "segments" / "segment_000.mp4"),
                      "src_1": self._remote("src_1", job / "source.mp4")}
            with patch.dict(drive_storage.os.environ, {"GOOGLE_DRIVE_ACCESS_TOKEN": "token", "GOOGLE_DRIVE_FOLDER_ID": ""}), \
                 patch.object(drive_storage, "_access_token", return_value="token"), \
                 patch.object(drive_storage, "_new_id", side_effect=["folder_1", "seg_1", "src_1"]), \
                 patch.object(drive_storage, "_get_file", side_effect=lambda token, fid: remote.get(fid)), \
                 patch.object(drive_storage, "_create_folder", return_value=folder), \
                 patch.object(drive_storage, "_upload_file") as upload:
                args = (job, JOB, "demo", manifest["source"]["fingerprint"], manifest["segments"])
                first = drive_storage.upload_prepared_segments(*args, source_path=job / "source.mp4")
                second = drive_storage.upload_prepared_segments(*args, source_path=job / "source.mp4")
            self.assertEqual(upload.call_count, 2)  # one segment + the source, never re-uploaded
            self.assertEqual(first["source_file"]["file_id"], "src_1")
            self.assertEqual(first["source_file"]["mtime_ns"], MTIME_NS)
            self.assertEqual(second, first)
            self.assertTrue(drive_storage.backup_complete(first, manifest["segments"], manifest["source"]["fingerprint"]))

    def test_manifest_from_before_source_backups_keeps_its_folder_and_segments(self):
        with tempfile.TemporaryDirectory() as raw:
            job, manifest, state = build_job(Path(raw) / "mcp_jobs", now=10**9)
            legacy = {k: v for k, v in state.items() if k not in ("source_file",)}
            pipeline.write_json(job / "drive-manifest.json", legacy)
            folder = {"id": "folder_1", "mimeType": drive_storage.FOLDER_MIME, "parents": [],
                      "webViewLink": "https://drive.google.com/drive/folders/folder_1"}
            remote = {"folder_1": folder, "seg_1": self._remote("seg_1", job / "segments" / "segment_000.mp4"),
                      "src_9": self._remote("src_9", job / "source.mp4")}
            with patch.dict(drive_storage.os.environ, {"GOOGLE_DRIVE_ACCESS_TOKEN": "token", "GOOGLE_DRIVE_FOLDER_ID": ""}), \
                 patch.object(drive_storage, "_access_token", return_value="token"), \
                 patch.object(drive_storage, "_new_id", side_effect=["src_9"]), \
                 patch.object(drive_storage, "_get_file", side_effect=lambda token, fid: remote.get(fid)), \
                 patch.object(drive_storage, "_create_folder") as create_folder, \
                 patch.object(drive_storage, "_upload_file") as upload:
                result = drive_storage.upload_prepared_segments(
                    job, JOB, "demo", manifest["source"]["fingerprint"], manifest["segments"],
                    source_path=job / "source.mp4")
            create_folder.assert_not_called()
            self.assertEqual([call.args[2] for call in upload.call_args_list], ["source.mp4"])
            self.assertEqual(result["files"][0]["file_id"], "seg_1")
            self.assertEqual(result["source_file"]["file_id"], "src_9")


class LinkExpiryTests(unittest.TestCase):
    def test_playback_links_are_tracked_apart_from_job_protection(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw) / "mcp_jobs"
            (root / JOB).mkdir(parents=True)
            policy = StoragePolicy(root, 10**12, 10**12, 7 * 86400, 0)
            policy.mark_managed(JOB, now=1000)
            policy.protect_until(JOB, 9000)
            policy.protect_until(JOB, 5000, link=True)
            policy.protect_until(JOB, 4000, link=True)
            marker = StoragePolicy._marker(root / JOB)
            self.assertEqual(marker["protected_until"], 9000)
            self.assertEqual(marker["link_until"], 5000)


class DriveBackedCleanupTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0

    def run_cleanup(self, root, *, verified=True, apply=False):
        with patch.object(drive_storage, "verify_remote_backup", return_value=verified):
            return retention.cleanup(root, apply=apply, now=self.now)

    def test_verified_backup_lets_media_go_soon_after_analysis(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, _ = build_job(root, now=self.now)
            report = self.run_cleanup(root)
            self.assertEqual((report["drive_backed_jobs"], report["selected_files"]), (1, 3))
            report = self.run_cleanup(root, apply=True)
            self.assertEqual(report["deleted_files"], 3)
            for name in ("source.mp4", "segments/segment_000.mp4", "previews/preview_000.jpg"):
                self.assertFalse((job / name).exists(), name)
            for name in ("manifest.json", "drive-manifest.json", "analysis/result.json"):
                self.assertTrue((job / name).exists(), name)
            audit = json.loads((job / retention.AUDIT).read_text(encoding="utf-8"))
            self.assertEqual((audit["status"], audit["policy"]), ("cleaned", "drive_backed"))

    def test_media_stays_while_unsafe_to_remove(self):
        cases = {
            "remote_unverified": "drive_unverified",
            "live_link": "retained_or_live_share",
            "recent_use": "younger_than_retention",
            "incomplete_backup": "drive_backup_incomplete",
            "no_analysis": "missing_or_invalid_evidence",
            "local_changed": "source_changed",
        }
        for case, reason in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve() / "mcp_jobs"
                job, _, state = build_job(root, now=self.now)
                policy = StoragePolicy(root, 10**12, 10**12, 7 * 86400, 0)
                if case == "live_link":
                    policy.protect_until(JOB, self.now + 600, link=True)
                elif case == "recent_use":
                    policy.mark_managed(JOB, now=self.now - 60)
                elif case == "incomplete_backup":
                    pipeline.write_json(job / "drive-manifest.json", {k: v for k, v in state.items() if k != "source_file"})
                elif case == "no_analysis":
                    (job / "analysis" / "result.json").unlink()
                elif case == "local_changed":
                    (job / "segments" / "segment_000.mp4").write_bytes(b"different bytes")
                report = self.run_cleanup(root, verified=case != "remote_unverified", apply=True)
                self.assertEqual(report["deleted_files"], 0)
                self.assertEqual(report["skipped"].get(reason), 1, report["skipped"])
                self.assertTrue((job / "source.mp4").exists())

    def test_job_protection_from_queued_work_does_not_block_drive_backed_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            build_job(root, now=self.now)
            policy = StoragePolicy(root, 10**12, 10**12, 7 * 86400, 0)
            policy.protect_until(JOB, self.now - 60, link=True)  # links issued since the upgrade expired
            # analyze/drive submissions protect for days; the queue itself is checked separately.
            policy.protect_until(JOB, self.now + 8 * 86400)
            self.assertEqual(self.run_cleanup(root)["drive_backed_jobs"], 1)

    def test_links_from_before_the_upgrade_still_protect_media(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            build_job(root, now=self.now)
            StoragePolicy(root, 10**12, 10**12, 7 * 86400, 0).protect_until(JOB, self.now + 600)
            report = self.run_cleanup(root, apply=True)
            self.assertEqual(report["skipped"].get("retained_or_live_share"), 1)

    def test_media_the_backup_never_saw_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, _ = build_job(root, now=self.now)
            (job / "source.webm").write_bytes(b"newer download in another container")
            report = self.run_cleanup(root, apply=True)
            self.assertEqual(report["deleted_files"], 0)
            self.assertEqual(report["skipped"].get("unbacked_media"), 1)
            self.assertTrue((job / "source.webm").exists())

    def test_backup_changing_mid_cleanup_keeps_the_source(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, _ = build_job(root, now=self.now)
            with patch.object(drive_storage, "verify_remote_backup", side_effect=[True, False]):
                report = retention.cleanup(root, apply=True, now=self.now)
            self.assertEqual(report["deleted_files"], 2)  # segment and preview only
            self.assertEqual(len(report["errors"]), 1)
            self.assertTrue((job / "source.mp4").exists())
            audit = json.loads((job / retention.AUDIT).read_text(encoding="utf-8"))
            self.assertEqual(audit["status"], "stopped_drive_changed")
            self.assertEqual(sorted(Path(name).as_posix() for name in audit["deleted_files"]),
                             ["previews/preview_000.jpg", "segments/segment_000.mp4"])


if __name__ == "__main__":
    unittest.main()
