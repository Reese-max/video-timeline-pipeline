import json
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

    def test_finish_reason_content_filter_raises(self) -> None:
        with self.assertRaises(pipeline.MiniMaxIncompleteError):
            self._chat("content_filter", "{}")


class VisualCacheTests(unittest.TestCase):
    """Truncated/failed visual observations must never enter the reusable cache."""

    def _candidate(self, image: Path) -> dict:
        return {"path": str(image), "timestamp": 1.5, "ocr_text": "", "ocr_confidence": None}

    def _analyze(self, cache_dir: Path, image: Path, finish_reason, content="{}"):
        candidates = [self._candidate(image)]
        with mock.patch.object(pipeline, "uniform_image_observation", return_value=None), \
             mock.patch.object(pipeline, "image_data_url", return_value="data:image/jpeg;base64,AA=="), \
             mock.patch.object(pipeline, "post_json", return_value=_response(finish_reason, content)), \
             mock.patch.object(pipeline, "minimax_endpoint", return_value="https://example.invalid/v1/chat/completions"):
            return pipeline.analyze_images(candidates, "key", "MiniMax-M3", workers=1, cache_dir=cache_dir)

    def test_truncated_observation_not_cached_and_not_marked_done(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "frame.jpg"
            image.write_bytes(b"fake-bytes")
            cache_dir = root / "cache"
            results = self._analyze(cache_dir, image, "length", '{"visual_summary": "截斷')
            self.assertEqual(len(results), 1)
            observation = results[0]
            self.assertEqual(observation["visual_summary"], "尚未執行 MiniMax 視覺理解。")
            self.assertEqual(observation["completion_status"], "incomplete")
            self.assertEqual(observation["finish_reason"], "length")
            self.assertEqual(list(cache_dir.iterdir()) if cache_dir.exists() else [], [])

    def test_raw_fallback_cache_entry_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "frame.jpg"
            image.write_bytes(b"fake-bytes")
            cache_dir = root / "cache"

            calls = []
            def counting_chat(messages, api_key, model, *args, **kwargs):
                calls.append(1)
                return '{"visual_summary": "真正的觀察", "screen_type": "x", "visible_text": [], "important_details": [], "confidence": 0.5}'

            with mock.patch.object(pipeline, "uniform_image_observation", return_value=None), \
                 mock.patch.object(pipeline, "image_data_url", return_value="data:image/jpeg;base64,AA=="), \
                 mock.patch.object(pipeline, "minimax_chat", counting_chat):
                pipeline.analyze_images([self._candidate(image)], "key", "MiniMax-M3", workers=1, cache_dir=cache_dir)
            self.assertEqual(len(list(cache_dir.iterdir())), 1)
            # Overwrite the cache entry with the historical poisoned shape:
            # parse_json_object's {"raw": ...} fallback plus the later-added keys.
            cache_file = next(cache_dir.iterdir())
            cache_file.write_text(
                json.dumps({"raw": "半截輸出", "source_image": str(image), "timestamp": 1.5}),
                encoding="utf-8",
            )

            calls.clear()
            with mock.patch.object(pipeline, "uniform_image_observation", return_value=None), \
                 mock.patch.object(pipeline, "image_data_url", return_value="data:image/jpeg;base64,AA=="), \
                 mock.patch.object(pipeline, "minimax_chat", counting_chat):
                results = pipeline.analyze_images([self._candidate(image)], "key", "MiniMax-M3", workers=1, cache_dir=cache_dir)
            self.assertEqual(calls, [1], "poisoned cache must not be reused")
            self.assertEqual(results[0]["visual_summary"], "真正的觀察")

    def test_incomplete_marker_cache_entry_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "frame.jpg"
            image.write_bytes(b"fake-bytes")
            cache_dir = root / "cache"

            calls = []
            def counting_chat(messages, api_key, model, *args, **kwargs):
                calls.append(1)
                return '{"visual_summary": "ok", "screen_type": "x", "visible_text": [], "important_details": [], "confidence": 0.5}'

            with mock.patch.object(pipeline, "uniform_image_observation", return_value=None), \
                 mock.patch.object(pipeline, "image_data_url", return_value="data:image/jpeg;base64,AA=="), \
                 mock.patch.object(pipeline, "minimax_chat", counting_chat):
                pipeline.analyze_images([self._candidate(image)], "key", "MiniMax-M3", workers=1, cache_dir=cache_dir)
            self.assertEqual(len(list(cache_dir.iterdir())), 1)
            cache_file = next(cache_dir.iterdir())
            poisoned = json.loads(cache_file.read_text(encoding="utf-8"))
            poisoned["completion_status"] = "incomplete"
            cache_file.write_text(json.dumps(poisoned), encoding="utf-8")

            calls.clear()
            with mock.patch.object(pipeline, "uniform_image_observation", return_value=None), \
                 mock.patch.object(pipeline, "image_data_url", return_value="data:image/jpeg;base64,AA=="), \
                 mock.patch.object(pipeline, "minimax_chat", counting_chat):
                pipeline.analyze_images([self._candidate(image)], "key", "MiniMax-M3", workers=1, cache_dir=cache_dir)
            self.assertEqual(calls, [1], "incomplete cache marker must trigger a fresh provider call")

    def test_good_cache_hit_skips_provider(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "frame.jpg"
            image.write_bytes(b"fake-bytes")
            cache_dir = root / "cache"

            calls = []
            def counting_chat(messages, api_key, model, *args, **kwargs):
                calls.append(1)
                return '{"visual_summary": "ok", "screen_type": "x", "visible_text": [], "important_details": [], "confidence": 0.5}'

            with mock.patch.object(pipeline, "uniform_image_observation", return_value=None), \
                 mock.patch.object(pipeline, "image_data_url", return_value="data:image/jpeg;base64,AA=="), \
                 mock.patch.object(pipeline, "minimax_chat", counting_chat):
                pipeline.analyze_images([self._candidate(image)], "key", "MiniMax-M3", workers=1, cache_dir=cache_dir)
                results = pipeline.analyze_images([self._candidate(image)], "key", "MiniMax-M3", workers=1, cache_dir=cache_dir)
            self.assertEqual(len(calls), 1)
            self.assertEqual(results[0]["visual_summary"], "ok")


if __name__ == "__main__":
    unittest.main()
