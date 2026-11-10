"""Drive is the long-term copy of prepared media: backup, restore and Drive-backed cleanup."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
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
                 patch.object(drive_storage, "_list_children", return_value=[]), \
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
            root = Path(raw) / "mcp_jobs"
            job, manifest, state = build_job(root, now=10**9)
            legacy = {k: v for k, v in state.items() if k not in ("source_file",)}
            pipeline.write_json(job / "drive-manifest.json", legacy)
            # The shared folder already exists; its ID is durable before anything is uploaded.
            pipeline.write_json(root / "drive-video-folder.json", {"folder_id": "folder_1", "parent_id": "",
                                                                  "name": "Video Timeline", "created": "",
                                                                  "folder_url": ""})
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
            self.assertEqual([call.args[2] for call in upload.call_args_list],
                             ["demo - 0123456789ab - source.mp4"])
            self.assertEqual(result["files"][0]["file_id"], "seg_1")
            self.assertEqual(result["source_file"]["file_id"], "src_9")
            self.assertEqual(result["folder_id"], "folder_1")


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


class DriveVideoRetentionTests(unittest.TestCase):
    """MCP_DRIVE_VIDEO_RETENTION_DAYS: old videos go to the Drive trash, the A report stays."""

    def setUp(self):
        self.now = 1_800_000_000.0

    def listing(self, state, *, age_days=20.0, source_age_days=None, source_sha=None):
        def created(days):
            return datetime.fromtimestamp(self.now - days * 86400, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        rows = [{"id": "seg_1", "sha256Checksum": state["local_segments"][0]["sha256"],
                 "createdTime": created(age_days)},
                {"id": "report_1", "sha256Checksum": "f" * 64, "createdTime": created(age_days)}]
        if "source_file" in state:
            rows.append({"id": "src_1", "sha256Checksum": source_sha or state["source_file"]["sha256"],
                         "createdTime": created(age_days if source_age_days is None else source_age_days)})
        return rows

    def run_cleanup(self, root, listing, *, days="14", apply=True, trash_error_on=None):
        trashed = []

        def trash(_token, file_id):
            if file_id == trash_error_on:
                raise drive_storage.DriveAPIError(500)
            trashed.append(file_id)
            return {}

        # A tuple gives one listing per Drive call; an exception makes the call fail.
        listed = listing if isinstance(listing, (Exception, tuple)) else None
        with (patch.dict(os.environ, {"MCP_DRIVE_VIDEO_RETENTION_DAYS": days,
                                      "GOOGLE_DRIVE_ACCESS_TOKEN": "token"}),
              patch.object(drive_storage, "verify_remote_backup", return_value=False),
              patch.object(drive_storage, "_list_children", side_effect=listed,
                           return_value=listing) as list_children,
              patch.object(drive_storage, "_trash_file", side_effect=trash)):
            report = retention.cleanup(root, apply=apply, now=self.now)
        return report, trashed, list_children

    def status(self, job):
        return json.loads((job / "drive-manifest.json").read_text(encoding="utf-8"))["status"]

    def test_off_by_default_never_asks_drive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, state = build_job(root, now=self.now)
            report, trashed, list_children = self.run_cleanup(root, self.listing(state), days="")
            list_children.assert_not_called()
            self.assertEqual((report["drive_retention_days"], trashed, self.status(job)), (0, [], "drive_ready"))

    def test_dry_run_counts_and_touches_nothing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, state = build_job(root, now=self.now)
            report, trashed, _ = self.run_cleanup(root, self.listing(state), apply=False)
            self.assertEqual((report["drive_expired_jobs"], report["drive_trashed_files"]), (1, 0))
            self.assertEqual((trashed, self.status(job)), ([], "drive_ready"))

    def test_expired_videos_are_trashed_and_the_report_stays(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, manifest, state = build_job(root, now=self.now)
            report, trashed, _ = self.run_cleanup(root, self.listing(state))
            self.assertEqual(trashed, ["seg_1", "src_1"])  # report_1 is not in the manifest
            self.assertEqual((report["drive_expired_jobs"], report["drive_trashed_files"], report["errors"]),
                             (1, 2, []))
            saved = json.loads((job / "drive-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual((saved["status"], saved["drive_trashed_at"]), ("drive_trashed", self.now))
            self.assertFalse(drive_storage.backup_complete(saved, manifest["segments"],
                                                           manifest["source"]["fingerprint"]))
            # Trashing the Drive copy never removes a local one.
            self.assertTrue((job / "source.mp4").exists())

    def test_videos_stay_until_the_newest_upload_is_old_enough(self):
        cases = {"young": {"age_days": 13.9}, "source_uploaded_later": {"source_age_days": 1.0}}
        for case, ages in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw).resolve() / "mcp_jobs"
                job, _, state = build_job(root, now=self.now)
                report, trashed, _ = self.run_cleanup(root, self.listing(state, **ages))
                self.assertEqual((report["drive_expired_jobs"], trashed, self.status(job)), (0, [], "drive_ready"))

    def test_a_file_the_user_replaced_is_left_alone(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, state = build_job(root, now=self.now)
            _, trashed, _ = self.run_cleanup(root, self.listing(state, source_sha="0" * 64))
            self.assertEqual((trashed, self.status(job)), (["seg_1"], "drive_trashed"))

    def test_manifest_from_before_source_backups_trashes_its_segments(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, state = build_job(root, now=self.now)
            state = {key: value for key, value in state.items() if key != "source_file"}
            pipeline.write_json(job / "drive-manifest.json", state)
            _, trashed, _ = self.run_cleanup(root, self.listing(state))
            self.assertEqual((trashed, self.status(job)), (["seg_1"], "drive_trashed"))

    def test_queued_job_keeps_its_drive_videos(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, state = build_job(root, now=self.now)
            from job_queue import JobQueue
            queue = JobQueue(root.parent / ".video-state", autostart=False)
            try:
                queue.submit("analyze", {"job_id": JOB}, JOB)
                report, trashed, _ = self.run_cleanup(root, self.listing(state))
            finally:
                queue.close()
            self.assertEqual((report["drive_expired_jobs"], trashed, self.status(job)), (0, [], "drive_ready"))

    def test_interrupted_run_is_finished_by_the_next_one(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, state = build_job(root, now=self.now)
            report, trashed, _ = self.run_cleanup(root, self.listing(state), trash_error_on="src_1")
            self.assertEqual((trashed, self.status(job)), (["seg_1"], "drive_trashing"))
            self.assertEqual([error["job_id"] for error in report["errors"]], [JOB])
            # Drive no longer lists the trashed segment; a fresh timestamp must not stop the resume.
            left = [row for row in self.listing(state, age_days=0.0) if row["id"] != "seg_1"]
            report, trashed, _ = self.run_cleanup(root, left)
            self.assertEqual((trashed, self.status(job), report["errors"]), (["src_1"], "drive_trashed", []))

    def test_unknown_drive_state_trashes_nothing_and_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, _ = build_job(root, now=self.now)
            report, trashed, _ = self.run_cleanup(root, drive_storage.DriveAPIError(503))
            self.assertEqual((trashed, self.status(job), report["errors"]), ([], "drive_ready", []))
            self.assertEqual(report["skipped"].get("drive_listing_failed"), 1)

    def test_broken_manifest_is_skipped_without_failing_the_run(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, state = build_job(root, now=self.now)
            pipeline.write_json(job / "drive-manifest.json", {**state, "files": [{"verified": True}]})
            report, trashed, _ = self.run_cleanup(root, self.listing(state))
            self.assertEqual((trashed, self.status(job), report["errors"]), ([], "drive_ready", []))

    def test_job_busy_with_a_direct_analysis_keeps_its_drive_videos(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, state = build_job(root, now=self.now)
            # A direct (not queued) analysis or restore holds only the job writer lock.
            with pipeline.file_writer_lock(pipeline.job_writer_path(root, JOB)):
                report, trashed, _ = self.run_cleanup(root, self.listing(state))
            self.assertEqual((trashed, self.status(job), report["errors"]), ([], "drive_ready", []))

    def test_upload_finished_after_the_shared_listing_is_seen_before_trashing(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, state = build_job(root, now=self.now)
            stale, fresh = self.listing(state), self.listing(state, source_age_days=1.0)
            report, trashed, list_children = self.run_cleanup(root, (stale, fresh))
            self.assertEqual((trashed, self.status(job), report["drive_expired_jobs"]), ([], "drive_ready", 0))
            self.assertEqual(list_children.call_count, 2)

    def test_unreadable_setting_turns_drive_retention_off_without_failing_the_run(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve() / "mcp_jobs"
            job, _, state = build_job(root, now=self.now)
            report, trashed, list_children = self.run_cleanup(root, self.listing(state), days="fourteen")
            list_children.assert_not_called()
            self.assertEqual((trashed, self.status(job), report["errors"]), ([], "drive_ready", []))
            self.assertEqual(report["skipped"].get("invalid_drive_retention_days"), 1)


if __name__ == "__main__":
    unittest.main()
