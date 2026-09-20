import asyncio
import unittest
from unittest.mock import patch

import mcp_server


class MCPValidationTests(unittest.TestCase):
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

    def test_media_path_rejects_traversal(self) -> None:
        self.assertIsNone(mcp_server.safe_media_path("0123456789ab", "segments/../source.mp4"))
        self.assertIsNone(mcp_server.safe_media_path("not-a-job", "segments/segment_000.mp4"))

    def test_media_urls_use_media_base(self) -> None:
        self.assertEqual(
            mcp_server.media_url("0123456789ab", "segments/segment_000.mp4"),
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

    def test_prepare_tool_structured_result_matches_schema(self) -> None:
        manifest = {
            "job_id": "job-1",
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


if __name__ == "__main__":
    unittest.main()
