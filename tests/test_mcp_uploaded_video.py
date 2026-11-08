"""Uploaded MP4 preparation and analysis contracts; no network or paid APIs."""
import asyncio
from contextlib import ExitStack
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import job_worker
from job_queue import JobQueue
import mcp_server as server


class UploadedVideoTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.jobs = self.root / "mcp_jobs"
        self.stack.enter_context(patch.multiple(server, MCP_ROOT=self.root, MCP_JOBS_ROOT=self.jobs,
                                                MCP_MIN_FREE_BYTES=0, MCP_MAX_SEGMENTS=0))
        self.stack.enter_context(patch.dict(os.environ, {
            "UPLOAD_ALLOWED_HOSTS": "files.oaiusercontent.com", "GROQ_API_KEY": "offline-test",
            "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
            "MINIMAX_MODEL": "offline-test-model",
            "ANALYZE_VISUAL_MODE": "video_mp4", "TRANSCRIBE_BACKEND": "groq"}))
        self.reference = {"file_id": "file-upload", "file_name": "私人影片.mp4", "mime_type": "video/mp4",
                          "download_url": "https://files.oaiusercontent.com/upload?sig=private-test"}
        self.data = b"offline-mp4-bytes"
        self.probe = {"format": {"duration": "2"},
                      "streams": [{"codec_type": "video", "codec_name": "h264"}, {"codec_type": "audio"}]}
        self.response = self.http_response()
        self.sessions = []

        def get(session, url, **kwargs):
            self.sessions.append((session.trust_env, list(session.cookies), url, kwargs))
            return self.response

        self.get = self.stack.enter_context(patch.object(server.requests.Session, "get", autospec=True,
                                                        side_effect=get))
        self.external = self.stack.enter_context(patch.object(server.pipeline, "run_external",
                                                              side_effect=lambda *a, **k: json.dumps(self.probe)))
        self.stack.enter_context(patch.object(server.pipeline, "require_tool", side_effect=lambda name: name))

        def split(source, directory, seconds, duration):
            directory.mkdir(parents=True, exist_ok=True)
            segment = directory / "segment_000.mp4"
            segment.write_bytes(self.data)
            return [(segment, 0.0, duration)]

        self.split = self.stack.enter_context(patch.object(server, "split_video", side_effect=split))

        def preview(source, target):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"offline-jpeg")
            return True

        self.preview = self.stack.enter_context(patch.object(server, "extract_preview", side_effect=preview))
        self.queue = JobQueue(self.root / "queue", autostart=False)
        self.addCleanup(self.queue.close)
        self.stack.enter_context(patch.object(server, "get_queue", return_value=self.queue))
        self.stack.enter_context(patch.object(server.drive_storage, "configured", return_value=False))

    def http_response(self, status=200, headers=None, chunks=None):
        response = MagicMock(status_code=status, headers=headers or {})
        response.__enter__.return_value = response
        response.iter_content.return_value = chunks if chunks is not None else [self.data[:4], b"", self.data[4:]]
        return response

    def prepare(self, **kwargs):
        return asyncio.run(server.prepare_uploaded_video(self.reference, **kwargs))

    def assert_rejected(self, text, **kwargs):
        result = self.prepare(**kwargs)
        self.assertTrue(result.isError)
        self.assertIn(text, result.structuredContent["error"])
        self.assertNotIn("private-test", str(result))
        self.assertFalse(list(self.jobs.glob(".upload-*")))
        self.assertFalse(list(self.jobs.glob("*/manifest.json")))

    def test_tool_schema_and_file_metadata(self):
        tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "prepare_uploaded_video")
        self.assertEqual(tool.meta["openai/fileParams"], ["file"])
        self.assertEqual(tool.meta["openai/outputTemplate"], server.UI_URI)
        self.assertEqual(tool.inputSchema["required"], ["file"])
        self.assertEqual(set(tool.inputSchema["$defs"]["OpenAIFileReference"]["required"]),
                         {"file_id", "download_url"})
        self.assertIn("analyze_prepared_video", tool.description)

    def test_happy_path_analyze_and_restore_use_real_prepared_job(self):
        result = self.prepare()
        self.assertFalse(result.isError)
        payload = server.PreparedVideoResult.model_validate(result.structuredContent)
        sha256 = hashlib.sha256(self.data).hexdigest()
        self.assertEqual(payload.job_id, server.pipeline.job_key(f"upload://{sha256}"))
        self.assertEqual(payload.source_url, f"upload://{sha256}")
        job = self.jobs / payload.job_id
        self.assertEqual((job / "source.mp4").read_bytes(), self.data)
        self.assertTrue((job / "source.probe.json").is_file())
        info = json.loads((job / "source.info.json").read_text(encoding="utf-8"))
        for key, value in {"source_type": "upload", "file_name": "私人影片.mp4",
                           "file_id": "file-upload", "sha256": sha256}.items():
            self.assertEqual(info[key], value)
            self.assertEqual(server.read_manifest(job)["source"][key], value)
        self.assertNotIn("private-test", (job / "manifest.json").read_text(encoding="utf-8"))
        command = self.external.call_args_list[0].args[0]
        self.assertIn("-protocol_whitelist", command)
        self.assertEqual(command[command.index("-format_whitelist") + 1], "mov")
        trust_env, cookies, _, options = self.sessions[0]
        self.assertFalse(trust_env)
        self.assertEqual(cookies, [])
        self.assertTrue(options["stream"])
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(all(0 < timeout <= 30 for timeout in options["timeout"]))
        with patch.object(server.pipeline, "minimax_subscription_key", return_value="offline-test"), \
                patch.object(server.pipeline, "analyze_video_mp4", return_value=[{
                    "timestamp": 0, "visual_summary": "測試畫面", "confidence": 0.9,
                    "analysis_method": "minimax_video"}]) as visual, \
                patch.object(server.pipeline, "extract_audio", return_value=[]) as audio, \
                patch.object(server.pipeline, "transcribe_all", return_value=({
                    "provider": "groq", "segments": [{"start": 0, "end": 2, "text": "測試語音"}]}, {})) as asr:
            analysis = asyncio.run(server.analyze_prepared_video(payload.job_id))
        self.assertFalse(analysis.isError, analysis.structuredContent)
        self.assertEqual(analysis.structuredContent["status"], "complete")
        self.assertEqual(analysis.structuredContent["source_sha256"], sha256)
        self.assertEqual(visual.call_args.args[0], job / "source.mp4")
        audio.assert_called_once()
        asr.assert_called_once()
        restored = asyncio.run(server.get_prepared_video(payload.job_id))
        self.assertEqual(restored["prepared"]["job_id"], payload.job_id)
        self.assertEqual(restored["analysis"]["source_sha256"], sha256)

    def test_same_bytes_new_reference_reuses_completed_preparation(self):
        first = self.prepare().structuredContent
        source = self.jobs / first["job_id"] / "source.mp4"
        manifest = server.read_manifest(source.parent)
        fingerprint = server.pipeline.file_fingerprint(source)
        self.reference.update(file_id="file-upload-again", file_name="不同名稱.mp4")
        second = self.prepare().structuredContent
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertEqual(first["source_url"], second["source_url"])
        self.assertEqual(server.read_manifest(source.parent), manifest)
        self.assertEqual(server.pipeline.file_fingerprint(source), fingerprint)
        self.split.assert_called_once()
        self.preview.assert_called_once()
        self.assertEqual(self.get.call_count, 2)  # Bytes must be fetched to establish identity.

    def test_disallowed_hosts_scheme_credentials_and_port_fail_before_network(self):
        for url in ("http://files.oaiusercontent.com/upload", "https://evil.example/video",
                    "https://files.oaiusercontent.com.evil.example/video", "https://127.0.0.1/video",
                    "https://user:password@files.oaiusercontent.com/video", "https://files.oaiusercontent.com:444/video"):
            with self.subTest(url=url):
                self.reference["download_url"] = url
                self.assertTrue(self.prepare().isError)
                self.assertTrue(self.prepare(background=True).isError)
        self.get.assert_not_called()
        self.assertEqual(self.queue.stats()["counts"], {})

    def test_malformed_urls_return_localized_errors_before_network(self):
        for url in ("https://files.oaiusercontent.com:abc/x", "https://[bad/"):
            with self.subTest(url=url):
                with self.assertRaisesRegex(ValueError, "上傳檔案下載網址"):
                    server.validate_upload_url(url)
                self.reference["download_url"] = url
                for background in (False, True):
                    result = self.prepare(background=background)
                    self.assertTrue(result.isError)
                    self.assertIn("網址", result.structuredContent["error"])
        self.get.assert_not_called()
        self.assertEqual(self.queue.stats()["counts"], {})

    def test_expired_budget_blocks_initial_and_redirect_requests(self):
        for expire_before_first_request in (True, False):
            with self.subTest(initial=expire_before_first_request):
                budget = server.OperationBudget("上傳影片下載", 900, "VIDEO_API_TIMEOUT_SECONDS")

                def redirect(*args, **kwargs):
                    self.assertTrue(all(value > 0 for value in kwargs["timeout"]))
                    budget.deadline = -1
                    return self.http_response(302, {"Location": "/redirected"})

                self.get.reset_mock()
                self.get.side_effect = redirect
                if expire_before_first_request:
                    budget.deadline = -1
                with patch.object(server, "OperationBudget", return_value=budget):
                    with self.assertRaises(server.DeadlineExceeded):
                        server.download_uploaded_video(self.reference["download_url"], self.root / "unused.mp4")
                self.assertEqual(self.get.call_count, 0 if expire_before_first_request else 1)

    def test_capacity_preflight_does_not_reserve_synthetic_job(self):
        job_id = server.pipeline.job_key("upload-staging")
        size = server.MCP_MAX_SOURCE_BYTES
        with patch.multiple(server, MCP_MAX_STORAGE_BYTES=size * 4, MCP_MAX_JOB_BYTES=size * 4):
            first = server.check_job_capacity(job_id, size)
            second = server.check_job_capacity(job_id, size)
        self.assertEqual(first["total_bytes"], second["total_bytes"])
        self.assertEqual(second["total_bytes"], 0)
        self.assertFalse((self.jobs / job_id).exists())

    def test_stale_staging_is_cleaned_before_capacity_preflight(self):
        stale = self.jobs / ".upload-abandoned"
        fresh = self.jobs / ".upload-recent"
        unrelated = self.jobs / "unrelated"
        for path in (stale, fresh, unrelated):
            path.mkdir(parents=True)
            (path / "source.mp4").write_bytes(self.data)
        os.utime(stale, (1, 1))
        os.utime(unrelated, (1, 1))

        def capacity(*args):
            self.assertFalse(stale.exists())
            raise RuntimeError("測試容量不足")

        with patch.object(server, "check_job_capacity", side_effect=capacity):
            result = self.prepare()
        self.assertTrue(result.isError)
        self.assertIn("測試容量不足", result.structuredContent["error"])
        self.assertTrue((fresh / "source.mp4").is_file())
        self.assertTrue((unrelated / "source.mp4").is_file())
        self.get.assert_not_called()

    def test_staging_cleanup_does_not_follow_directory_links(self):
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.mp4").write_bytes(self.data)
        os.utime(outside, (1, 1))
        self.jobs.mkdir()
        link = self.jobs / ".upload-link"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Directory symlinks are unavailable")
        self.assertFalse(self.prepare().isError)
        self.assertTrue(link.is_symlink())
        self.assertEqual((outside / "keep.mp4").read_bytes(), self.data)

    def test_failed_capacity_or_move_leaves_no_empty_job(self):
        job_id = server.pipeline.job_key(f"upload://{hashlib.sha256(self.data).hexdigest()}")

        def capacity(key, *args):
            if key == job_id:
                raise RuntimeError("測試容量不足")

        with patch.object(server, "check_job_capacity", side_effect=capacity):
            self.assert_rejected("測試容量不足")
        self.assertFalse((self.jobs / job_id).exists())
        with patch.object(Path, "replace", side_effect=OSError("測試搬移失敗")):
            self.assert_rejected("測試搬移失敗")
        self.assertFalse((self.jobs / job_id).exists())

    def test_redirect_to_non_allowlisted_host_or_http_never_followed(self):
        for location in ("https://evil.example/video", "http://files.oaiusercontent.com/video"):
            with self.subTest(location=location):
                self.response = self.http_response(302, {"Location": location})
                before = self.get.call_count
                self.assert_rejected("允許清單" if location.startswith("https") else "HTTPS")
                self.assertEqual(self.get.call_count, before + 1)

    def test_allowed_redirect_clears_response_cookies_and_honors_env(self):
        with patch.dict(os.environ, {"UPLOAD_ALLOWED_HOSTS": " files.oaiusercontent.com, uploads.example "}):
            final = self.http_response()

            def get(session, url, **kwargs):
                self.assertFalse(session.trust_env)
                self.assertEqual(list(session.cookies), [])
                if "uploads.example" in url:
                    return final
                session.cookies.set("secret", "must-not-forward")
                return self.http_response(307, {"Location": "https://uploads.example/video"})

            self.get.side_effect = get
            self.assertFalse(self.prepare().isError)
        self.assertEqual(self.get.call_count, 2)

    def test_oversize_stream_and_declared_length_are_rejected_and_cleaned(self):
        with patch.object(server, "MCP_MAX_SOURCE_BYTES", 4):
            self.assert_rejected("大小上限")
            self.response = self.http_response(headers={"Content-Length": "5"})
            self.assert_rejected("大小上限")
        self.split.assert_not_called()

    def test_empty_invalid_and_audio_only_files_are_rejected(self):
        self.response = self.http_response(chunks=[])
        self.assert_rejected("空的")
        self.response = self.http_response()
        self.external.side_effect = RuntimeError("ffprobe invalid media")
        self.assert_rejected("不是可讀取的 MP4")
        self.external.side_effect = lambda *a, **k: json.dumps({"streams": [{"codec_type": "audio"}]})
        self.assert_rejected("沒有視訊串流")
        self.split.assert_not_called()

    def test_silent_video_is_accepted(self):
        self.probe["streams"] = [{"codec_type": "video"}]
        self.assertFalse(self.prepare().isError)

    def test_non_object_probe_is_rejected_as_invalid_mp4(self):
        for probe in ([], None, "invalid", {"streams": None}, {"streams": [None]}):
            with self.subTest(probe=probe):
                self.external.side_effect = lambda *a, **k: json.dumps(probe)
                self.assert_rejected("不是可讀取的 MP4")
        self.split.assert_not_called()

    def test_http_timeout_does_not_expose_signed_url(self):
        self.get.side_effect = server.requests.Timeout(self.reference["download_url"])
        self.assert_rejected("下載失敗或逾時")

    def test_background_uses_prepare_queue_and_worker_dispatch(self):
        result = self.prepare(background=True)
        self.assertFalse(result.isError)
        operation = result.structuredContent
        self.assertEqual(operation["operation"], "prepare")
        self.assertEqual(operation["status"], "queued")
        self.get.assert_not_called()
        row = self.queue._row(operation["operation_id"])
        work = self.queue.root / operation["operation_id"]
        work.mkdir()
        (work / "input.json").write_text(json.dumps({"operation": "prepare", "inputs": json.loads(row["inputs"])}), encoding="utf-8")
        with patch.object(sys, "argv", ["job_worker.py", str(work)]), \
                patch.object(job_worker.threading, "Thread"):
            job_worker.main()
        outcome = json.loads((work / "output.json").read_text(encoding="utf-8"))
        self.assertTrue(outcome["ok"], outcome)
        self.assertEqual(outcome["result"]["status"], "ready")
        self.assertTrue(outcome["result"]["source_url"].startswith("upload://"))
        server.PreparedVideoResult.model_validate(outcome["result"])


if __name__ == "__main__":
    unittest.main()
