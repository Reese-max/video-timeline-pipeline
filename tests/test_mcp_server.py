import unittest

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


if __name__ == "__main__":
    unittest.main()
