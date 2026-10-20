import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


def _response(finish_reason, content="{}"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


class MinimaxCompletionTests(unittest.TestCase):
    """minimax_chat must surface the provider completion state, not just content."""

    def _chat(self, finish_reason, content="{}"):
        with mock.patch.object(pipeline, "post_json", return_value=_response(finish_reason, content)), \
             mock.patch.object(pipeline, "minimax_endpoint", return_value="https://example.invalid/v1/chat/completions"):
            return pipeline.minimax_chat([{"role": "user", "content": "test"}], "key", "MiniMax-M3")

    def test_finish_reason_length_raises_even_when_content_parses(self) -> None:
        with self.assertRaises(pipeline.MiniMaxIncompleteError) as ctx:
            self._chat("length", '{"visual_summary": "完整但不被信任"}')
        self.assertEqual(ctx.exception.finish_reason, "length")

    def test_finish_reason_length_with_broken_json_raises(self) -> None:
        with self.assertRaises(pipeline.MiniMaxIncompleteError):
            self._chat("length", '{"visual_summary": "半截')

    def test_missing_finish_reason_fails_closed(self) -> None:
        with mock.patch.object(pipeline, "post_json", return_value={"choices": [{"message": {"content": "{}"}}]}), \
             mock.patch.object(pipeline, "minimax_endpoint", return_value="https://example.invalid/v1/chat/completions"):
            with self.assertRaises(pipeline.MiniMaxIncompleteError):
                pipeline.minimax_chat([{"role": "user", "content": "t"}], "key", "MiniMax-M3")

    def test_finish_reason_stop_returns_content(self) -> None:
        self.assertEqual(self._chat("stop", "純文字答案"), "純文字答案")

    def test_finish_reason_normalization_accepts_mixed_case(self) -> None:
        self.assertEqual(self._chat(" Stop ", "答案"), "答案")

    def test_finish_reason_content_filter_raises(self) -> None:
        with self.assertRaises(pipeline.MiniMaxIncompleteError):
            self._chat("content_filter", "{}")


class VisualSchemaTests(unittest.TestCase):
    """Both cache layers and the user-visible status require complete evidence."""

    def _good(self) -> dict:
        return {
            "screen_type": "slide",
            "visible_text": [],
            "visual_summary": "投影片內容",
            "important_details": [],
            "confidence": 0.8,
            "timestamp": 1.5,
            "source_image": "frame.jpg",
            "analysis_method": "minimax",
            "visual_schema_version": pipeline.VISUAL_OBSERVATION_SCHEMA_VERSION,
            "completion_status": "complete",
        }

    def test_schema_rejects_partial_values_and_unknown_version(self) -> None:
        good = self._good()
        self.assertTrue(pipeline.usable_visual_observation(good))
        for changes in (
            {"screen_type": ""},
            {"visible_text": "text"},
            {"important_details": [7]},
            {"visual_summary": ""},
            {"visual_summary": pipeline.VISUAL_NOT_ANALYZED},
            {"confidence": None},
            {"confidence": 1.5},
            {"visual_schema_version": 999},
            {"completion_status": "incomplete"},
            {"raw": "partial"},
        ):
            with self.subTest(changes=changes):
                self.assertFalse(pipeline.usable_visual_observation({**good, **changes}))

    def test_knowledge_requires_all_observations_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            good = self._good()
            incomplete = {
                **good,
                "visual_summary": pipeline.VISUAL_NOT_ANALYZED,
                "confidence": None,
                "completion_status": "incomplete",
            }
            path = pipeline.render_knowledge_markdown(
                root, {"title": "video"}, root / "source.mp4",
                {"segments": []}, [good, incomplete], [], None,
            )
            self.assertIn("畫面分析：未完成", path.read_text(encoding="utf-8"))
            path = pipeline.render_knowledge_markdown(
                root, {"title": "video"}, root / "source.mp4",
                {"segments": []}, [good], [], None,
            )
            self.assertIn("畫面分析：已執行 MiniMax", path.read_text(encoding="utf-8"))

    def test_local_uniformity_is_not_reported_as_minimax(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            uniform = {**self._good(), "analysis_method": "local_uniformity_check"}
            path = pipeline.render_knowledge_markdown(
                root, {"title": "video"}, root / "source.mp4",
                {"segments": []}, [uniform], [], None,
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn("已完成本地純色畫面檢查", content)
            self.assertNotIn("已執行 MiniMax 視覺核對", content)

    def test_tool_calls_is_not_text_completion(self) -> None:
        with mock.patch.object(pipeline, "post_json", return_value=_response("tool_calls", "{}")), \
             mock.patch.object(pipeline, "minimax_endpoint", return_value="https://example.invalid/v1/chat/completions"):
            with self.assertRaises(pipeline.MiniMaxIncompleteError):
                pipeline.minimax_chat([{"role": "user", "content": "test"}], "key", "MiniMax-M3")


if __name__ == "__main__":
    unittest.main()
