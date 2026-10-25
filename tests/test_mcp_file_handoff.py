"""Offline file-reference receipts never substitute for a model reading the bytes."""
import asyncio
import unittest
from unittest.mock import patch

import mcp_server as server


class FileHandoffContractTests(unittest.TestCase):
    def files(self):
        return [
            {"file_id": "file-video", "download_url": "https://files.example/video?sig=temporary",
             "mime_type": "video/mp4", "file_name": "segment_000.mp4"},
            {"file_id": "file-preview", "download_url": "https://files.example/preview?sig=temporary",
             "mime_type": "image/jpeg", "file_name": "preview_000.jpg"},
        ]

    def test_schema_preserves_official_file_object_and_declares_ranges(self):
        tool = next(tool for tool in asyncio.run(server.mcp.list_tools())
                    if tool.name == "ingest_video_segments")
        self.assertEqual(tool.meta["openai/fileParams"], ["files"])
        file_schema = tool.inputSchema["$defs"]["OpenAIFileReference"]
        self.assertEqual(set(file_schema["properties"]),
                         {"file_id", "download_url", "mime_type", "file_name"})
        self.assertEqual(set(file_schema["required"]), {"file_id", "download_url"})
        self.assertIn("segment_ranges", tool.inputSchema["properties"])
        self.assertEqual(tool.inputSchema["required"], ["files"])
        self.assertIn("start_seconds", tool.outputSchema["$defs"]["VerifiedFileSummary"]["properties"])
        self.assertIn("end_seconds", tool.outputSchema["$defs"]["VerifiedFileSummary"]["properties"])
        self.assertIn("/v20-", server.UI_URI)

    def test_ingestion_retains_identity_mime_names_and_fractional_times(self):
        files = self.files()
        ranges = [{"start_seconds": 0.25, "end_seconds": 119.75}] * 2
        with patch.object(server, "normalize_file_references", wraps=server.normalize_file_references) as receive, \
                patch.object(server.pipeline, "create_summary") as summarize, \
                patch.object(server.requests, "get") as download:
            _, payload = asyncio.run(server.mcp._tool_manager.call_tool(
                "ingest_video_segments", {"files": files, "segment_ranges": ranges}, convert_result=True))
        receive.assert_called_once_with(files)
        summarize.assert_not_called()
        download.assert_not_called()
        self.assertEqual(payload["status"], "files_received")
        self.assertEqual(payload["files_received"], 2)
        for actual, original, time_range in zip(payload["files"], files, ranges):
            self.assertEqual(actual, {key: value for key, value in original.items() if key != "download_url"}
                             | time_range)
        self.assertIn("未驗證", payload["limitation"])
        self.assertIn("metadata", payload["limitation"])
        self.assertNotIn("sig=temporary", str(payload))
        server.FileHandoffResult.model_validate(payload)

    def test_invalid_or_misaligned_ranges_reject_the_entire_handoff(self):
        valid = {"start_seconds": 0, "end_seconds": 120}
        cases = [[], [valid], [valid, valid, valid]]
        for invalid in (
            {"start_seconds": -1, "end_seconds": 120},
            {"start_seconds": 120, "end_seconds": 120},
            {"start_seconds": 121, "end_seconds": 120},
            {"start_seconds": 0},
            {"start_seconds": float("nan"), "end_seconds": 120},
            {"start_seconds": 0, "end_seconds": float("inf")},
            {"start_seconds": True, "end_seconds": 120},
            {**valid, "unexpected": 1},
        ):
            cases.append([valid, invalid])
        for ranges in cases:
            with self.subTest(ranges=ranges), self.assertRaises(ValueError):
                asyncio.run(server.ingest_video_segments(self.files(), segment_ranges=ranges))

    def test_prepare_busy_is_a_schema_valid_state(self):
        with patch.object(server, "prepare_video_locked", side_effect=server.JobBusyError("busy")):
            result = asyncio.run(server.prepare_video_for_chatgpt("https://video.example/test.mp4"))
        self.assertFalse(result.isError)
        payload = server.PreparedVideoResult.model_validate(result.structuredContent)
        self.assertEqual(payload.status, "busy")
        self.assertEqual(payload.error_code, "MCP_BUSY")
        self.assertEqual(payload.retry_after_seconds, 5)


if __name__ == "__main__":
    unittest.main()
