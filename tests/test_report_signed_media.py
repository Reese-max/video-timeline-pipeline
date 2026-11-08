"""Signed delivery of generated reports; all files and requests stay in owned fixtures."""
import hashlib
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

import analysis_report
import mcp_server as server
from media_lifecycle import MediaSigner


JOB = "0123456789ab"
OTHER_JOB = "abcdef012345"


class ReportSignerContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.signer = MediaSigner(Path(self.temp.name).resolve() / "private" / "media.key", max_ttl_seconds=100)

    def test_canonical_report_signature_binds_job_digest_and_expiry_without_changing_media(self):
        report = "reports/" + "a" * 64 + "/A_server_analysis.md"
        signature = self.signer.sign(JOB, report, 1050, now=1000)
        self.assertTrue(self.signer.verify(JOB, report, "1050", signature, now=1001))
        for job, path, expiry in ((OTHER_JOB, report, 1050),
                                  (JOB, report.replace("a" * 64, "b" * 64), 1050),
                                  (JOB, report, 1051)):
            with self.subTest(job=job, path=path, expiry=expiry):
                self.assertFalse(self.signer.verify(job, path, expiry, signature, now=1001))
        self.assertFalse(self.signer.verify(JOB, report, 1050, signature, now=1050))
        with self.assertRaises(ValueError):
            self.signer.sign(JOB, report, 1101, now=1000)
        for path in ("segments/segment_000.mp4", "previews/preview_000.jpg"):
            with self.subTest(existing_media=path):
                value = self.signer.sign(JOB, path, 1050, now=1000)
                self.assertTrue(self.signer.verify(JOB, path, 1050, value, now=1001))

    def test_report_allowlist_rejects_other_files_traversal_encodings_and_noncanonical_digests(self):
        folder = "reports/" + "a" * 64 + "/"
        paths = (folder + "other.md", folder + "A_server_analysis.json", folder + "drive-state.json",
                 folder + "analysis-report.json", "A_server_analysis.md", "reports/A_server_analysis.md",
                 "reports/" + "A" * 64 + "/A_server_analysis.md",
                 "reports/" + "g" * 64 + "/A_server_analysis.md",
                 "reports/" + "a" * 63 + "/A_server_analysis.md",
                 "reports/" + "a" * 65 + "/A_server_analysis.md",
                 folder + "../A_server_analysis.md", "/" + folder + "A_server_analysis.md",
                 folder.replace("/", "\\") + "A_server_analysis.md",
                 "reports/%2e%2e/A_server_analysis.md", folder + "A_server_analysis.md?x=1",
                 folder + "A_server_analysis.md#fragment", folder + "A_server_analysis.md\n",
                 folder + "nested/A_server_analysis.md", "segments/source.mp4.txt", "previews/x.png")
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError):
                self.signer.sign(JOB, path, 1050, now=1000)
        for job in ("not-a-job", JOB.upper(), "../" + JOB):
            with self.subTest(job=job), self.assertRaises(ValueError):
                self.signer.sign(job, folder + "A_server_analysis.md", 1050, now=1000)


class GeneratedReportRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.job = self.root / "mcp_jobs" / JOB
        (self.job / "analysis").mkdir(parents=True)
        self.analysis = {"job_id": JOB, "cache_signature": "a" * 64, "source_sha256": "b" * 64,
                         "source_fingerprint": "owned-fixture", "status": "complete",
                         "transcript": {"segments": [{"start": 0, "end": 1, "text": "untrusted <source>"}]},
                         "timeline": [{"timestamp": 0, "visual_summary": "owned evidence"}]}
        (self.job / "analysis" / "result.json").write_text(json.dumps(self.analysis), encoding="utf-8")
        self.report = analysis_report.record(self.job, self.analysis)
        self.path = self.job / self.report["relative_path"]
        self.raw = self.path.read_bytes()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for patcher in (patch.object(server, "MCP_ROOT", self.root),
                        patch.object(server, "MCP_JOBS_ROOT", self.root / "mcp_jobs"),
                        patch.object(server, "MCP_LEGACY_MEDIA_UNTIL", 0),
                        patch.object(server, "MCP_MEDIA_TTL", 60),
                        patch("urllib.request.urlopen", side_effect=AssertionError("No network permitted")),
                        patch.object(server.drive_storage, "_access_token", side_effect=AssertionError("No Drive access permitted"))):
            self.stack.enter_context(patcher)
        app = Starlette(routes=[Route("/media/{job_id}/{filename:path}", server.media, methods=["GET", "HEAD"])])
        self.client = self.stack.enter_context(TestClient(app))

    def target(self, relative_path=None):
        url = urlsplit(server.media_url(JOB, relative_path or self.report["relative_path"]))
        return url.path + "?" + url.query

    def test_generated_report_snapshot_downloads_exact_utf8_attachment_by_get_and_head(self):
        self.assertEqual(hashlib.sha256(self.raw).hexdigest(), self.report["report_sha256"])
        self.assertIn(b"pending_chatgpt", self.raw)
        snapshot = server.report_snapshot(JOB, self.analysis)
        self.assertEqual(snapshot["status"], "summary_pending")
        url = urlsplit(snapshot["download_url"])
        target = url.path + "?" + url.query
        response = self.client.get(target)
        self.assertEqual((response.status_code, response.content), (200, self.raw))
        self.assertEqual(response.headers["content-type"].split(";", 1)[0], "text/markdown")
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="A_server_analysis.md"')
        self.assertTrue(response.headers["cache-control"].startswith("private, max-age="))
        self.assertEqual(response.headers["cross-origin-resource-policy"], "cross-origin")
        head = self.client.head(target)
        self.assertEqual((head.status_code, head.content), (200, b""))
        self.assertEqual(int(head.headers["content-length"]), len(self.raw))
        self.assertEqual(self.path.read_bytes(), self.raw)

    def test_report_route_rejects_unsigned_tampered_duplicate_and_expired_authorization(self):
        target = self.target()
        url = urlsplit(target)
        expires = int(parse_qs(url.query)["expires"][0])
        requests = (url.path, target.replace(JOB, OTHER_JOB, 1),
                    target.replace(self.report["report_sha256"], "c" * 64, 1),
                    target + "&expires=1", target + "&sig=" + "0" * 64,
                    url.path + "?expires=" + str(expires) + "&sig=" + "0" * 64)
        for request in requests:
            with self.subTest(request_kind=request.split("?", 1)[0]):
                self.assertEqual(self.client.get(request).status_code, 403)
        with patch("media_lifecycle.time.time", return_value=expires):
            self.assertEqual(self.client.get(target).status_code, 403)

    def test_signed_report_with_changed_bytes_is_not_served(self):
        target = self.target()
        self.path.write_bytes(b"changed report bytes")
        self.assertEqual(self.client.get(target).status_code, 404)
        self.assertEqual(self.client.head(target).status_code, 404)

    def test_canonical_but_wrong_digest_or_missing_report_is_not_served(self):
        fake = "reports/" + "0" * 64 + "/A_server_analysis.md"
        wrong = self.job / fake
        wrong.parent.mkdir()
        wrong.write_bytes(self.raw)
        self.assertNotEqual(hashlib.sha256(self.raw).hexdigest(), "0" * 64)
        self.assertEqual(self.client.get(self.target(fake)).status_code, 404)
        missing = "reports/" + "1" * 64 + "/A_server_analysis.md"
        self.assertEqual(self.client.get(self.target(missing)).status_code, 404)

    def test_legacy_window_does_not_serve_other_report_files_or_traversal(self):
        folder = self.path.parent
        for name in ("other.md", "drive-state.json", "A_server_analysis.txt"):
            (folder / name).write_bytes(self.raw)
        with patch.object(server, "MCP_LEGACY_MEDIA_UNTIL", int(time.time()) + 60):
            for path in (str(folder.relative_to(self.job).as_posix()) + "/other.md",
                         str(folder.relative_to(self.job).as_posix()) + "/drive-state.json",
                         str(folder.relative_to(self.job).as_posix()) + "/A_server_analysis.txt",
                         "analysis/result.json", "reports/%2e%2e/analysis/result.json"):
                with self.subTest(path=path):
                    self.assertEqual(self.client.get("/media/" + JOB + "/" + path).status_code, 404)

    def test_signed_report_refuses_each_symlink_component_even_when_target_bytes_match(self):
        target = self.target()
        for original in (self.path, self.path.parent, self.path.parent.parent, self.job):
            with self.subTest(component=original.name):
                backup = original.with_name(original.name + ".owned-backup")
                is_directory = original.is_dir()
                original.rename(backup)
                try:
                    try:
                        original.symlink_to(backup, target_is_directory=is_directory)
                    except (OSError, NotImplementedError) as error:
                        self.skipTest("Symlinks unavailable: " + type(error).__name__)
                    self.assertEqual(self.client.get(target).status_code, 404)
                finally:
                    if original.is_symlink():
                        original.unlink()
                    backup.rename(original)


if __name__ == "__main__":
    unittest.main()
