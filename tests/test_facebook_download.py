import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline


class FacebookDownloadTests(unittest.TestCase):
    url = "https://www.facebook.com/reel/4636084203346422/?token=private-query"
    runtime = {
        "yt_dlp_version": "stable@2026.08.19",
        "request_handlers": ["urllib", "curl_cffi"],
        "curl_cffi_version": "0.16.3",
        "chrome_impersonation": True,
    }

    def test_public_success_never_reads_configured_cookies(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)

            def run(command, **kwargs):
                self.assertIn("--impersonate", command)
                self.assertIn("--ignore-config", command)
                self.assertIn("--no-cookies-from-browser", command)
                self.assertNotIn("--cookies", command)
                self.assertFalse(kwargs["stream"])
                (root / "source.mp4").write_bytes(b"video")

            with patch.dict(os.environ, {"YTDLP_FACEBOOK_COOKIE_FILE": str(root / "missing.cookies.txt")}, clear=True), patch.object(pipeline, "instagram_download_diagnostics", return_value=dict(self.runtime)), patch.object(pipeline, "run_external", side_effect=run):
                self.assertEqual(pipeline.download_video(self.url, root), root / "source.mp4")
            report = json.loads((root / "facebook-download.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertFalse(report["cookies_used"])
            self.assertEqual(len(report["attempts"]), 1)

    def test_cookie_fallback_is_scoped_ephemeral_and_preserves_original(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cookies = root / "facebook.cookies.txt"
            original = (
                "# Netscape HTTP Cookie File\n"
                ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tc_user\tPRIVATE_USER\n"
                ".facebook.com\tTRUE\t/\tTRUE\t2147483647\txs\tPRIVATE_XS\n"
                ".example.com\tTRUE\t/\tTRUE\t2147483647\tother\tOTHER_SECRET\n"
            )
            cookies.write_text(original)
            calls, temporary_paths = [], []

            def run(command, **kwargs):
                calls.append(command)
                if len(calls) == 1:
                    raise RuntimeError("Cannot parse data " + self.url)
                cookie_copy = Path(command[command.index("--cookies") + 1])
                temporary_paths.append(cookie_copy)
                self.assertNotEqual(cookie_copy, cookies)
                content = cookie_copy.read_text()
                self.assertIn("PRIVATE_USER", content)
                self.assertIn("PRIVATE_XS", content)
                self.assertNotIn("OTHER_SECRET", content)
                self.assertIn("PRIVATE_USER", kwargs["redact"])
                (root / "source.mp4").write_bytes(b"video")

            with patch.dict(os.environ, {"YTDLP_FACEBOOK_COOKIE_FILE": str(cookies)}, clear=True), patch.object(pipeline, "instagram_download_diagnostics", return_value=dict(self.runtime)), patch.object(pipeline, "run_external", side_effect=run):
                pipeline.download_video(self.url, root, private_url=True)
            report_text = (root / "facebook-download.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertTrue(report["cookies_used"])
            self.assertIn("Cannot parse data", report["attempts"][0]["error"])
            self.assertNotIn("private-query", report_text)
            self.assertEqual(cookies.read_text(), original)
            self.assertTrue(all(not path.exists() for path in temporary_paths))

    def test_unconfigured_cookie_fallback_stops_with_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {}, clear=True), patch.object(pipeline, "instagram_download_diagnostics", return_value=dict(self.runtime)), patch.object(pipeline, "run_external", side_effect=RuntimeError("Cannot parse data")) as run:
                with self.assertRaisesRegex(RuntimeError, "YTDLP_FACEBOOK_COOKIE_FILE"):
                    pipeline.download_video(self.url, root)
            self.assertEqual(run.call_count, 1)
            self.assertFalse((root / ".download-complete").exists())
            self.assertEqual(json.loads((root / "facebook-download.json").read_text(encoding="utf-8"))["status"], "error")

    def test_cookie_values_never_appear_in_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cookies = root / "private.cookies.txt"
            cookies.write_text(
                "# Netscape HTTP Cookie File\n"
                ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tc_user\tPRIVATE_USER\n"
            )
            (root / "source.info.json").write_text(json.dumps({"http_headers": {"Cookie": "PRIVATE_USER"}}))
            errors = [
                RuntimeError("Cannot parse data"),
                RuntimeError("Cookie: c_user=PRIVATE_USER\nAuthorization: Bearer HIDDEN_TOKEN\n" + self.url),
            ]
            with patch.dict(os.environ, {"YTDLP_FACEBOOK_COOKIE_FILE": str(cookies)}, clear=True), patch.object(pipeline, "instagram_download_diagnostics", return_value=dict(self.runtime)), patch.object(pipeline, "run_external", side_effect=errors) as run:
                with self.assertRaises(RuntimeError) as caught:
                    pipeline.download_video(self.url, root)
            text = str(caught.exception) + (root / "facebook-download.json").read_text(encoding="utf-8")
            for secret in ("PRIVATE_USER", "HIDDEN_TOKEN", "private-query", str(cookies)):
                self.assertNotIn(secret, text)
            self.assertNotIn("PRIVATE_USER", (root / "source.info.json").read_text(encoding="utf-8"))
            self.assertEqual(run.call_count, 2)

    def test_other_sites_do_not_use_facebook_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source.mp4").write_bytes(b"video")
            with patch.object(pipeline, "download_facebook") as fb, patch.object(pipeline, "run_external") as run:
                pipeline.download_video("https://www.facebook.com.example.com/video", root)
            fb.assert_not_called()
            self.assertTrue(run.called)

    def test_is_facebook_url_hosts(self):
        self.assertTrue(pipeline.is_facebook_url("https://www.facebook.com/reel/1"))
        self.assertTrue(pipeline.is_facebook_url("https://m.facebook.com/watch/?v=1"))
        self.assertTrue(pipeline.is_facebook_url("https://fb.watch/abc"))
        self.assertFalse(pipeline.is_facebook_url("https://www.instagram.com/reel/1"))


if __name__ == "__main__":
    unittest.main()
