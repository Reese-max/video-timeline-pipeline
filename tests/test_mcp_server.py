import asyncio
import ipaddress
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import drive_storage
import mcp_server


class MCPValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = tempfile.TemporaryDirectory()
        root = Path(self.temp_root.name)
        (root / "mcp_jobs" / "0123456789ab").mkdir(parents=True)
        self.patchers = [patch.object(mcp_server, "MCP_ROOT", root),
                         patch.object(mcp_server, "MCP_JOBS_ROOT", root / "mcp_jobs")]
        for patcher in self.patchers: patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers): patcher.stop()
        self.temp_root.cleanup()

    def test_drive_upload_recovers_502_in_same_session(self) -> None:
        completed = {"id": "file_1", "webViewLink": "https://drive.google.com/file/d/file_1/view"}
        for statuses, ranges, expected_bodies in [
            ([502, 308, 200], [None, "bytes=0-2", None], [b"fake-mp4", b"", b"e-mp4"]),
            ([502, 200], [None, None], [b"fake-mp4", b""]),
            ([502, 503, 308, 200], [None, None, None, None], [b"fake-mp4", b"", b"", b"fake-mp4"]),
        ]:
            with self.subTest(statuses=statuses), tempfile.TemporaryDirectory() as raw_dir:
                path = Path(raw_dir) / "video.mp4"
                path.write_bytes(b"fake-mp4")
                init = MagicMock()
                init.__enter__.return_value.headers = {"Location": "https://www.googleapis.com/upload/session"}
                connections = []
                for status, received in zip(statuses, ranges):
                    connection = MagicMock()
                    response = connection.getresponse.return_value
                    response.status = status
                    response.getheader.return_value = received
                    response.read.return_value = json.dumps(completed).encode()
                    connections.append(connection)
                with (
                    patch.object(drive_storage.urllib.request, "urlopen", return_value=init) as initialize,
                    patch.object(drive_storage.http.client, "HTTPSConnection", side_effect=connections),
                    patch("time.sleep"),
                ):
                    self.assertEqual(drive_storage._upload_file("token", path, path.name, "folder"), completed)
                initialize.assert_called_once()
                for connection, body in zip(connections, expected_bodies):
                    self.assertEqual(b"".join(c.args[0] for c in connection.send.call_args_list), body)
                connections[1].putheader.assert_any_call("Content-Range", "bytes */8")
                if ranges[1]:
                    connections[2].putheader.assert_any_call("Content-Range", "bytes 3-7/8")

    def test_drive_upload_retries_are_bounded(self) -> None:
        for status, attempts in [(502, 4), (429, 4), (403, 1)]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as raw_dir:
                path = Path(raw_dir) / "video.mp4"
                path.write_bytes(b"fake-mp4")
                init, connection = MagicMock(), MagicMock()
                init.__enter__.return_value.headers = {"Location": "https://www.googleapis.com/upload/session"}
                connection.getresponse.return_value.status = status
                with (
                    patch.object(drive_storage.urllib.request, "urlopen", return_value=init) as initialize,
                    patch.object(drive_storage.http.client, "HTTPSConnection", return_value=connection) as connect,
                    patch("time.sleep") as sleep,
                ):
                    with self.assertRaisesRegex(RuntimeError, f"HTTP {status}"):
                        drive_storage._upload_file("token", path, path.name, "folder")
                initialize.assert_called_once()
                self.assertEqual(connect.call_count, attempts)
                self.assertEqual(sleep.call_count, attempts - 1)

    def test_segment_bounds_allow_unlimited_segments(self) -> None:
        self.assertEqual(
            mcp_server.segment_bounds(301, 120, 4),
            [(0, 120), (120, 240), (240, 301)],
        )
        self.assertEqual(len(mcp_server.segment_bounds(120 * 9, 120, None)), 9)
        with self.assertRaisesRegex(ValueError, "max_segments"):
            mcp_server.segment_bounds(601, 120, 4)
        with self.assertRaisesRegex(ValueError, "正整數"):
            mcp_server.segment_bounds(120, 120, 0)

    def test_rejects_local_source_urls(self) -> None:
        with self.assertRaises(ValueError):
            mcp_server.validate_source_url("http://127.0.0.1/video.mp4")
        with self.assertRaises(ValueError):
            mcp_server.validate_source_url("https://user:pass@example.com/video.mp4")

    def test_validate_source_url_facebook_share_accepts_with_fake_ip_and_doh(self) -> None:
        url = "https://www.facebook.com/share/v/1ExtCtwPgg/?mibextid=wwXIfr"
        mock_response = MagicMock(status_code=200)
        with (
            patch.object(mcp_server.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", 443))]),
            patch.object(mcp_server, "public_dns_addresses", return_value={ipaddress.ip_address("57.145.2.1")}),
            patch.object(mcp_server.requests, "get", return_value=mock_response),
        ):
            self.assertEqual(mcp_server.validate_source_url(url), url)

    def test_validate_source_url_facebook_share_reel_follows_public_redirect(self) -> None:
        url = "https://www.facebook.com/share/r/1FZQnH1JoV/?mibextid=wwXIfr"
        hop = MagicMock(status_code=302, headers={"Location": "https://www.facebook.com/reel/1234567890/"})
        final = MagicMock(status_code=200, headers={})
        with (
            patch.object(mcp_server.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", 443))]),
            patch.object(mcp_server, "public_dns_addresses", return_value={ipaddress.ip_address("57.145.2.1")}),
            patch.object(mcp_server.requests, "get", side_effect=[hop, final]) as get,
        ):
            self.assertEqual(mcp_server.validate_source_url(url), url)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[1].args[0], "https://www.facebook.com/reel/1234567890/")

    def test_validate_source_url_fake_ip_empty_doh_known_vs_unknown_host(self) -> None:
        fb_url = "https://www.facebook.com/share/v/1ExtCtwPgg/?mibextid=wwXIfr"
        mock_response = MagicMock(status_code=200)
        with (
            patch.object(mcp_server.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", 443))]),
            patch.object(mcp_server, "public_dns_addresses", return_value=set()),
            patch.object(mcp_server.requests, "get", return_value=mock_response),
        ):
            with self.assertRaisesRegex(ValueError, "無法確認"):
                mcp_server.validate_source_url(fb_url)

        evil_url = "https://evil.example/video.mp4"
        with (
            patch.object(mcp_server.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", 443))]),
            patch.object(mcp_server, "public_dns_addresses", return_value=set()),
        ):
            with self.assertRaises(ValueError):
                mcp_server.validate_source_url(evil_url)

    def test_validate_source_url_fake_ip_doh_private_ip_rejected(self) -> None:
        url = "https://www.facebook.com/share/v/1ExtCtwPgg/?mibextid=wwXIfr"
        with (
            patch.object(mcp_server.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", 443))]),
            patch.object(mcp_server, "public_dns_addresses", return_value={ipaddress.ip_address("10.0.0.5")}),
        ):
            with self.assertRaises(ValueError):
                mcp_server.validate_source_url(url)

    def test_validate_source_url_ip_literals_rejected(self) -> None:
        urls = [
            "http://127.0.0.1/",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.100.100.100/",
            "http://198.18.0.1/",
            "http://[::1]/",
        ]
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    mcp_server.validate_source_url(url)

    def test_validate_source_url_resolving_to_link_local_rejected(self) -> None:
        url = "https://metadata.internal.example/video.mp4"
        with patch.object(
            mcp_server.socket,
            "getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443))],
        ):
            with self.assertRaises(ValueError):
                mcp_server.validate_source_url(url)

    def test_validate_source_url_share_redirect_hop_to_link_local_rejected(self) -> None:
        url = "https://www.facebook.com/share/v/1ExtCtwPgg/?mibextid=wwXIfr"
        mock_response = MagicMock()
        mock_response.status_code = 302
        mock_response.headers = {"Location": "http://169.254.169.254/"}
        with (
            patch.object(mcp_server.socket, "getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.18.0.1", 443))]),
            patch.object(mcp_server, "public_dns_addresses", return_value={ipaddress.ip_address("57.145.2.1")}),
            patch.object(mcp_server.requests, "get", return_value=mock_response),
        ):
            with self.assertRaises(ValueError):
                mcp_server.validate_source_url(url)

    def test_public_dns_addresses_parsing_and_fallback(self) -> None:
        bad_response = MagicMock(status_code=500)
        good_a = MagicMock(status_code=200)
        good_a.json.return_value = {
            "Status": 0,
            "Question": [{"name": "example.com.", "type": 1}],
            "Answer": [
                {"name": "example.com", "type": 5, "data": "cname.example.com."},
                {"name": "cname.example.com", "type": 1, "data": "93.184.216.34"},
            ],
        }
        good_aaaa = MagicMock(status_code=200)
        good_aaaa.json.return_value = {
            "Status": 0,
            "Question": [{"name": "example.com.", "type": 28}],
            "Answer": [
                {"name": "example.com", "type": 5, "data": "cname.example.com."},
                {"name": "cname.example.com", "type": 28, "data": "2606:2800:220:1:248:1893:25c8:1946"},
            ],
        }
        with patch.object(mcp_server.requests, "get", side_effect=[bad_response, good_a, good_aaaa]):
            addrs = mcp_server.public_dns_addresses("example.com")
            self.assertEqual(
                addrs,
                {
                    ipaddress.ip_address("93.184.216.34"),
                    ipaddress.ip_address("2606:2800:220:1:248:1893:25c8:1946"),
                },
            )

    def test_media_path_rejects_traversal(self) -> None:
        self.assertIsNone(mcp_server.safe_media_path("0123456789ab", "segments/../source.mp4"))
        self.assertIsNone(mcp_server.safe_media_path("not-a-job", "segments/segment_000.mp4"))

    def test_media_urls_use_media_base(self) -> None:
        self.assertEqual(
            mcp_server.media_url("0123456789ab", "segments/segment_000.mp4").split("?", 1)[0],
            f"{mcp_server.MCP_MEDIA_BASE_URL}/media/0123456789ab/segments/segment_000.mp4",
        )

    def test_tool_errors_are_explicit(self) -> None:
        result = mcp_server.tool_error_result(RuntimeError("download failed"))
        self.assertTrue(result.isError)
        self.assertIn("download failed", result.content[0].text)

    def test_file_references_require_verified_ids_and_https_urls(self) -> None:
        with self.assertRaisesRegex(ValueError, "file_id"):
            mcp_server.normalize_file_references(
                [{"download_url": "https://files.example/video.mp4"}]
            )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            mcp_server.normalize_file_references(
                [{"file_id": "file_1", "download_url": "http://files.example/video.mp4"}]
            )

    def test_file_handoff_returns_verified_file_summary(self) -> None:
        result = asyncio.run(
            mcp_server.ingest_video_segments(
                [
                    {
                        "file_id": "file_1",
                        "download_url": "https://files.example/video.mp4",
                        "mime_type": "video/mp4",
                        "file_name": "segment_000.mp4",
                    }
                ]
            )
        )
        self.assertEqual(result.status, "files_received")
        self.assertEqual(result.files_received, 1)
        self.assertEqual(result.files[0].file_id, "file_1")
        self.assertEqual(result.files[0].file_name, "segment_000.mp4")

    def test_file_handoff_rejects_url_without_file_id(self) -> None:
        with self.assertRaises(ValueError):
            asyncio.run(
                mcp_server.ingest_video_segments(
                    ["https://files.example/segment_000.mp4"]
                )
            )

    def test_file_handoff_declares_complete_openai_file_schema(self) -> None:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        tool = next(item for item in tools if item.name == "ingest_video_segments")
        self.assertEqual(tool.meta["openai/fileParams"], ["files"])
        file_schema = tool.inputSchema["$defs"]["OpenAIFileReference"]
        self.assertEqual(
            set(file_schema["properties"]),
            {"download_url", "file_id", "mime_type", "file_name"},
        )
        self.assertEqual(file_schema["required"], ["download_url", "file_id"])
        item_schema = tool.inputSchema["properties"]["files"]["items"]
        self.assertEqual(item_schema["$ref"], "#/$defs/OpenAIFileReference")

    def test_file_handoff_protocol_returns_structured_content(self) -> None:
        result = asyncio.run(
            mcp_server.mcp._tool_manager.call_tool(
                "ingest_video_segments",
                {"files": [{"file_id": "file_1", "download_url": "https://files.example/video.mp4"}]},
                convert_result=True,
            )
        )
        self.assertEqual(result[1]["status"], "files_received")
        self.assertEqual(result[1]["files_received"], 1)

    def test_prepare_tool_exposes_output_schema(self) -> None:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        tool = next(item for item in tools if item.name == "prepare_video_for_chatgpt")
        self.assertIsNotNone(tool.outputSchema)

    def test_analysis_tool_exposes_output_schema_and_widget_fallback(self) -> None:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        tool = next(item for item in tools if item.name == "analyze_prepared_video")
        self.assertIsNotNone(tool.outputSchema)
        self.assertTrue(tool.meta["openai/widgetAccessible"])
        self.assertNotIn("const openai = window.openai", mcp_server.WIDGET_HTML)
        self.assertIn("window.openai", mcp_server.WIDGET_HTML)
        self.assertIn('request("ui/initialize"', mcp_server.WIDGET_HTML)
        self.assertIn("ui/notifications/initialized", mcp_server.WIDGET_HTML)
        self.assertIn('"tools/call"', mcp_server.WIDGET_HTML)
        self.assertIn('"ui/message"', mcp_server.WIDGET_HTML)
        self.assertNotIn("queueMicrotask(startAnalysis)", mcp_server.WIDGET_HTML)
        self.assertIn("get_video_job", mcp_server.WIDGET_HTML)
        self.assertNotIn("uploadFile(", mcp_server.WIDGET_HTML)

    def test_drive_tool_exposes_output_schema_and_widget_path(self) -> None:
        tools = asyncio.run(mcp_server.mcp.list_tools())
        tool = next(item for item in tools if item.name == "upload_prepared_video_to_drive")
        self.assertIsNotNone(tool.outputSchema)
        self.assertTrue(tool.meta["openai/widgetAccessible"])
        self.assertIn("upload_prepared_video_to_drive", mcp_server.WIDGET_HTML)
        self.assertIn("Google Drive", mcp_server.WIDGET_HTML)
        self.assertNotIn("analyzeOnServer", mcp_server.WIDGET_HTML)

    def test_drive_view_urls_are_metadata_not_materializable_attachments(self) -> None:
        payload = {
            "status": "drive_ready",
            "job_id": "0123456789ab",
            "files": [{
                "file_id": "file_1",
                "url": "https://drive.google.com/file/d/file_1/view",
                "file_name": "segment_000.mp4",
                "mime_type": "video/mp4",
                "size_bytes": 8,
                "sha256": "a" * 64,
            }],
        }
        result = mcp_server.drive_tool_result(payload)
        self.assertEqual([item.type for item in result.content], ["text"])
        self.assertEqual(result.structuredContent, payload)
        mcp_server.DriveUploadResult.model_validate(result.structuredContent)
        self.assertIn("download_raw_file=true", result.content[0].text)
        self.assertIn("include_base64=false", result.content[0].text)
        self.assertIn("一般 ChatGPT 對話", result.content[0].text)
        self.assertIn("本 developer MCP 對話不可使用官方 Drive", result.content[0].text)

    def test_analysis_tool_returns_clean_error_payload(self) -> None:
        result = asyncio.run(mcp_server.analyze_prepared_video("not-a-job"))
        self.assertTrue(result.isError)
        self.assertEqual(result.structuredContent["status"], "error")
        self.assertIn("job_id", result.structuredContent["error"])

    def test_prepared_analysis_reuses_pipeline_stages(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            video_path = Path(raw_dir) / "source.mp4"
            video_path.write_bytes(b"fake-mp4")
            manifest = {
                "status": "ready",
                "segment_seconds": 120,
                "source": {"title": "demo", "url": "https://example.com/video"},
            }
            with (
                patch.object(mcp_server, "prepared_job_dir", return_value=Path(raw_dir)),
                patch.object(mcp_server, "read_manifest", return_value=manifest),
                patch.multiple(mcp_server, check_job_capacity=MagicMock(), storage_policy=MagicMock()),
                patch.object(mcp_server.pipeline, "locate_source_video", return_value=video_path),
                patch.object(mcp_server.pipeline, "file_fingerprint", return_value="7:1"),
                patch.object(mcp_server.pipeline, "reuse_artifact", return_value=None),
                patch.object(
                    mcp_server.pipeline,
                    "probe_video",
                    return_value={
                        "format": {"duration": "2"},
                        "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
                    },
                ),
                patch.object(mcp_server.pipeline, "minimax_model", return_value="MiniMax-M3"),
                patch.object(mcp_server.pipeline, "minimax_endpoint", return_value="https://api.minimax.io/v1"),
                patch.object(mcp_server.pipeline, "minimax_subscription_key", return_value="key"),
                patch.object(
                    mcp_server.pipeline,
                    "extract_budgeted_candidates",
                    return_value=(
                        [{"timestamp": 0, "path": str(video_path), "ocr_text": ""}],
                        {},
                    ),
                ) as extract_candidates_mock,
                patch.object(mcp_server.pipeline, "analyze_images", return_value=[{
                    "timestamp": 0, "visual_summary": "A visible sign", "confidence": 0.9}]),
                patch.object(
                    mcp_server.pipeline,
                    "extract_audio",
                    return_value=[
                        {
                            "index": 0,
                            "path": str(video_path),
                            "source_start": 0,
                            "source_end": 2,
                            "sha256": "audio",
                        }
                    ],
                ),
                patch.object(
                    mcp_server.pipeline,
                    "transcribe_all",
                    return_value=(
                        {
                            "provider": "groq",
                            "segments": [{"start": 0, "end": 1, "text": "hello"}],
                        },
                        {},
                    ),
                ),
                patch.object(mcp_server.pipeline, "create_summary") as create_summary_mock,
                patch.object(mcp_server.pipeline, "file_sha256", return_value="sha"),
                patch.object(mcp_server.pipeline, "write_json"),
                patch.dict(mcp_server.os.environ, {"GROQ_API_KEY": "groq", "ANALYZE_VISUAL_MODE": "frames"}, clear=False),
            ):
                result = mcp_server.analyze_prepared_sync("0123456789ab")

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["source_sha256"], "sha")
        self.assertEqual(result["audio_status"], "complete")
        self.assertEqual(result["visual_status"], "complete")
        self.assertEqual(result["summary"]["status"], "pending_chatgpt")
        self.assertEqual(result["quality"]["status"], "evidence_available")
        create_summary_mock.assert_not_called()
        self.assertEqual(extract_candidates_mock.call_args.kwargs["candidate_budget"], 240)
        self.assertEqual(extract_candidates_mock.call_args.args[2], 2)

    def test_drive_upload_caches_verified_segment_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as raw_dir:
            job_dir = Path(raw_dir)
            segment_dir = job_dir / "segments"
            segment_dir.mkdir()
            segment_path = segment_dir / "segment_000.mp4"
            segment_path.write_bytes(b"fake-mp4")
            segment_rows = [{"filename": segment_path.name}]
            folder = {
                "id": "folder_1",
                "mimeType": drive_storage.FOLDER_MIME,
                "parents": ["parent"],
                "webViewLink": "https://drive.google.com/drive/folders/folder_1",
            }
            uploaded = {
                "id": "file_1",
                "name": "segment_000.mp4",
                "mimeType": "video/mp4",
                "size": 8,
                "sha256Checksum": drive_storage._sha256(segment_path),
                "parents": ["folder_1"],
                "webViewLink": "https://drive.google.com/file/d/file_1/view",
            }
            with (
                patch.dict(
                    drive_storage.os.environ,
                    {"GOOGLE_DRIVE_ACCESS_TOKEN": "token", "GOOGLE_DRIVE_FOLDER_ID": "parent"},
                    clear=False,
                ),
                patch.object(drive_storage, "_access_token", return_value="token"),
                patch.object(drive_storage, "_new_id", side_effect=["folder_1", "file_1"]),
                patch.object(drive_storage, "_get_file", side_effect=lambda token, fid: folder if fid == "folder_1" else uploaded),
                patch.object(drive_storage, "_create_folder", return_value=folder) as create_folder,
                patch.object(drive_storage, "_upload_file", return_value=uploaded) as upload_file,
            ):
                first = drive_storage.upload_prepared_segments(
                    job_dir,
                    "0123456789ab",
                    "demo",
                    "source-fingerprint",
                    segment_rows,
                )
                second = drive_storage.upload_prepared_segments(
                    job_dir,
                    "0123456789ab",
                    "demo",
                    "source-fingerprint",
                    segment_rows,
                )

        self.assertEqual(first["status"], "drive_ready")
        self.assertEqual(first["files"][0]["file_id"], "file_1")
        self.assertEqual(second, first)
        create_folder.assert_called_once()
        upload_file.assert_called_once()

    def test_prepare_tool_structured_result_matches_schema(self) -> None:
        manifest = {
            "job_id": "0123456789ab",
            "source": {"title": "demo", "url": "https://example.com/video", "duration_seconds": 1.0},
            "duration_seconds": 1.0,
            "segment_seconds": 60,
            "segments": [
                {
                    "index": 0,
                    "filename": "segment_000.mp4",
                    "preview_filename": None,
                    "start_seconds": 0,
                    "end_seconds": 1,
                    "size_bytes": 10,
                    "mime_type": "video/mp4",
                }
            ],
        }
        with patch.object(mcp_server, "prepare_video_locked", return_value=manifest):
            result = asyncio.run(
                mcp_server.mcp._tool_manager.call_tool(
                    "prepare_video_for_chatgpt",
                    {"url": "https://example.com/video"},
                    convert_result=True,
                )
            )
        self.assertEqual(result.structuredContent["status"], "ready")
        self.assertEqual(result.structuredContent["segments"][0]["filename"], "segment_000.mp4")
        self.assertIn("analyze_prepared_video", result.content[0].text)
        self.assertNotIn("先完成檔案交接", result.content[0].text)


if __name__ == "__main__":
    unittest.main()
