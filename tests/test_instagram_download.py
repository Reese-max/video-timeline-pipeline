import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline


class InstagramDownloadTests(unittest.TestCase):
    url = "https://www.instagram.com/reel/DZuris5SuxT/?token=private-query"
    runtime = {
        "yt_dlp_version": "stable@2026.08.19",
        "request_handlers": ["urllib", "curl_cffi"],
        "curl_cffi_version": "0.16.3",
        "chrome_impersonation": True,
    }

    def test_runtime_diagnostics_only_keep_safe_fields(self):
        output = "\n".join([
            "[debug] yt-dlp version stable@2026.08.19 from yt-dlp/yt-dlp",
            "[debug] Optional libraries: curl_cffi-0.16.3, sqlite3-3.45.1",
            "[debug] Request Handlers: urllib, curl_cffi",
            "[debug] Proxy map: {'https': 'https://user:SECRET@proxy'}",
            "Chrome-150 Macos-26 curl_cffi",
        ])
        with patch.object(pipeline.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, output, "")):
            result = pipeline.instagram_download_diagnostics(["yt-dlp"])
        self.assertEqual(result["request_handlers"], ["urllib", "curl_cffi"])
        self.assertEqual(result["curl_cffi_version"], "0.16.3")
        self.assertTrue(result["chrome_impersonation"])
        self.assertNotIn("SECRET", json.dumps(result))

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
            with patch.dict(os.environ, {"YTDLP_INSTAGRAM_COOKIE_FILE": str(root / "missing.cookies.txt")}, clear=True), patch.object(pipeline, "instagram_download_diagnostics", return_value=dict(self.runtime)), patch.object(pipeline, "run_external", side_effect=run):
                self.assertEqual(pipeline.download_video(self.url, root), root / "source.mp4")
            report = json.loads((root / "instagram-download.json").read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertFalse(report["cookies_used"])
            self.assertEqual(len(report["attempts"]), 1)

    def test_cookie_fallback_is_scoped_ephemeral_and_preserves_first_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cookies = root / "instagram.cookies.txt"
            original = "# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tPRIVATE_SESSION\n.example.com\tTRUE\t/\tTRUE\t2147483647\tother\tOTHER_SECRET\n"
            cookies.write_text(original)
            calls, temporary_paths = [], []
            def run(command, **kwargs):
                calls.append(command)
                if len(calls) == 1:
                    raise RuntimeError("Instagram sent an empty media response " + self.url)
                cookie_copy = Path(command[command.index("--cookies") + 1])
                temporary_paths.append(cookie_copy)
                self.assertNotEqual(cookie_copy, cookies)
                content = cookie_copy.read_text()
                self.assertIn("PRIVATE_SESSION", content)
                self.assertNotIn("OTHER_SECRET", content)
                self.assertIn("PRIVATE_SESSION", kwargs["redact"])
                (root / "source.mp4").write_bytes(b"video")
            with patch.dict(os.environ, {"YTDLP_INSTAGRAM_COOKIE_FILE": str(cookies)}, clear=True), patch.object(pipeline, "instagram_download_diagnostics", return_value=dict(self.runtime)), patch.object(pipeline, "run_external", side_effect=run):
                pipeline.download_video(self.url, root, private_url=True)
            report_text = (root / "instagram-download.json").read_text(encoding="utf-8")
            report = json.loads(report_text)
            self.assertTrue(report["cookies_used"])
            self.assertIn("empty media response", report["attempts"][0]["error"])
            self.assertNotIn("private-query", report_text)
            self.assertEqual(cookies.read_text(), original)
            self.assertTrue(all(not path.exists() for path in temporary_paths))

    def test_unconfigured_cookie_fallback_stops_with_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.dict(os.environ, {}, clear=True), patch.object(pipeline, "instagram_download_diagnostics", return_value=dict(self.runtime)), patch.object(pipeline, "run_external", side_effect=RuntimeError("empty media response")) as run:
                with self.assertRaisesRegex(RuntimeError, "YTDLP_INSTAGRAM_COOKIE_FILE"):
                    pipeline.download_video(self.url, root)
            self.assertEqual(run.call_count, 1)
            self.assertFalse((root / ".download-complete").exists())
            self.assertEqual(json.loads((root / "instagram-download.json").read_text(encoding="utf-8"))["status"], "error")

    def test_cookie_values_and_header_values_never_appear_in_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cookies = root / "private.cookies.txt"
            cookies.write_text("# Netscape HTTP Cookie File\n.instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\tPRIVATE_SESSION\n")
            (root / "source.info.json").write_text(json.dumps({"http_headers": {"Cookie": "PRIVATE_SESSION"}}))
            errors = [RuntimeError("empty media response"), RuntimeError("Cookie: sessionid=PRIVATE_SESSION\nAuthorization: Bearer HIDDEN_TOKEN\n" + self.url)]
            with patch.dict(os.environ, {"YTDLP_INSTAGRAM_COOKIE_FILE": str(cookies)}, clear=True), patch.object(pipeline, "instagram_download_diagnostics", return_value=dict(self.runtime)), patch.object(pipeline, "run_external", side_effect=errors) as run:
                with self.assertRaises(RuntimeError) as caught:
                    pipeline.download_video(self.url, root)
            text = str(caught.exception) + (root / "instagram-download.json").read_text(encoding="utf-8")
            for secret in ("PRIVATE_SESSION", "HIDDEN_TOKEN", "private-query", str(cookies)):
                self.assertNotIn(secret, text)
            self.assertNotIn("PRIVATE_SESSION", (root / "source.info.json").read_text(encoding="utf-8"))
            self.assertEqual(run.call_count, 2)
            self.assertFalse((root / ".download-complete").exists())

    def test_other_sites_do_not_use_instagram_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source.mp4").write_bytes(b"video")
            with patch.object(pipeline, "instagram_download_diagnostics") as diagnose, patch.object(pipeline, "run_external") as run:
                pipeline.download_video("https://www.instagram.com.example.com/video", root)
            diagnose.assert_not_called()
            self.assertNotIn("--impersonate", run.call_args.args[0])

    def test_missing_curl_is_reported_without_forcing_unsupported_target(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source.mp4").write_bytes(b"video")
            runtime = dict(self.runtime, chrome_impersonation=False, curl_cffi_version=None)
            with patch.dict(os.environ, {}, clear=True), patch.object(pipeline, "instagram_download_diagnostics", return_value=runtime), patch.object(pipeline, "run_external") as run:
                pipeline.download_video(self.url, root)
            self.assertNotIn("--impersonate", run.call_args.args[0])

    def test_invalid_cookie_file_is_not_used_or_disclosed(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            cookies = root / "bad.cookies.txt"
            cookies.write_text("not a cookie file: DO_NOT_DISCLOSE")
            with patch.dict(os.environ, {"YTDLP_INSTAGRAM_COOKIE_FILE": str(cookies)}, clear=True), patch.object(pipeline, "instagram_download_diagnostics", return_value=dict(self.runtime)), patch.object(pipeline, "run_external", side_effect=RuntimeError("empty media response")) as run:
                with self.assertRaises(RuntimeError) as error:
                    pipeline.download_video(self.url, root)
            self.assertNotIn("DO_NOT_DISCLOSE", str(error.exception))
            self.assertEqual(run.call_count, 1)

    def test_success_without_media_is_not_marked_complete(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "source.f123.mp4").write_bytes(b"unfinished track")
            with patch.dict(os.environ, {}, clear=True), patch.object(pipeline, "instagram_download_diagnostics", return_value=dict(self.runtime)), patch.object(pipeline, "run_external"):
                with self.assertRaises(RuntimeError):
                    pipeline.download_video(self.url, root, max_filesize=100)
            self.assertFalse((root / ".download-complete").exists())

    def test_sanitized_sidecar_drops_authentication_headers(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "source.info.json"
            path.write_text(json.dumps({"title": "keep", "formats": [{"http_headers": {"Cookie": "SECRET", "Authorization": "SECRET", "User-Agent": "browser"}}]}))
            pipeline.sanitize_download_info(path)
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("SECRET", text)
            self.assertIn("keep", text)

    def test_virtualenv_package_takes_precedence_over_path_binary(self):
        with patch.object(pipeline.sys, "prefix", "venv"), patch.object(pipeline.sys, "base_prefix", "base"), patch.object(pipeline.importlib.util, "find_spec", return_value=object()), patch.object(pipeline.shutil, "which", return_value="old-yt-dlp"):
            self.assertEqual(pipeline.yt_dlp_command(), [pipeline.sys.executable, "-m", "yt_dlp"])


if __name__ == "__main__":
    unittest.main()
