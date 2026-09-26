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

    def test_finish_reason_normalization_accepts_mixed_case(self) -> None:
        self.assertEqual(self._chat(" Stop ", "答案"), "答案")

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

    def test_unparseable_stop_response_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "frame.jpg"
            image.write_bytes(b"frame")
            cache_dir = root / "cache"
            results = self._analyze(cache_dir, image, "stop", "這不是 JSON")
            self.assertEqual(results[0]["completion_status"], "incomplete")
            self.assertEqual(results[0]["raw"], "這不是 JSON")
            self.assertEqual(list(cache_dir.iterdir()), [])

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

    def test_aggregate_cache_checks_every_frame_and_metadata(self) -> None:
        good = self._good()
        candidate = {"timestamp": 1.5, "path": "frame.jpg"}
        self.assertTrue(pipeline.reusable_visual_artifact([good], [candidate]))
        self.assertFalse(pipeline.reusable_visual_artifact([good], [candidate, candidate]))
        self.assertFalse(pipeline.reusable_visual_artifact([{**good, "source_image": ""}], [candidate]))
        self.assertFalse(pipeline.reusable_visual_artifact([{**good, "source_image": "other.jpg"}], [candidate]))
        self.assertFalse(pipeline.reusable_visual_artifact([{**good, "timestamp": 2.0}], [candidate]))
        self.assertFalse(pipeline.reusable_visual_artifact([{**good, "visible_text": "bad"}], [candidate]))

    def test_stop_with_partial_schema_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "frame.jpg"
            image.write_bytes(b"frame")
            with mock.patch.object(pipeline, "uniform_image_observation", return_value=None), \
                 mock.patch.object(pipeline, "image_data_url", return_value="data:image/jpeg;base64,AA=="), \
                 mock.patch.object(pipeline, "post_json", return_value=_response("stop", '{"screen_type":"slide","visual_summary":"只有摘要"}')), \
                 mock.patch.object(pipeline, "minimax_endpoint", return_value="https://example.invalid/v1/chat/completions"):
                observations = pipeline.analyze_images(
                    [{"path": str(image), "timestamp": 1.5}], "key", "MiniMax-M3",
                    workers=1, cache_dir=root / "cache",
                )
            self.assertEqual(observations[0]["completion_status"], "incomplete")
            self.assertEqual(list((root / "cache").iterdir()), [])

    def test_length_with_parseable_schema_is_not_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "frame.jpg"
            image.write_bytes(b"frame")
            complete_json = json.dumps({
                key: value for key, value in self._good().items()
                if key in {"screen_type", "visible_text", "visual_summary", "important_details", "confidence"}
            })
            with mock.patch.object(pipeline, "uniform_image_observation", return_value=None), \
                 mock.patch.object(pipeline, "image_data_url", return_value="data:image/jpeg;base64,AA=="), \
                 mock.patch.object(pipeline, "post_json", return_value=_response("length", complete_json)), \
                 mock.patch.object(pipeline, "minimax_endpoint", return_value="https://example.invalid/v1/chat/completions"):
                observations = pipeline.analyze_images(
                    [{"path": str(image), "timestamp": 1.5}], "key", "MiniMax-M3",
                    workers=1, cache_dir=root / "cache",
                )
            self.assertEqual(observations[0]["completion_status"], "incomplete")
            self.assertEqual(list((root / "cache").iterdir()), [])

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

    def test_stop_with_full_schema_reuses_cache_without_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "frame.jpg"
            image.write_bytes(b"frame")
            content = json.dumps({
                key: value for key, value in self._good().items()
                if key in {"screen_type", "visible_text", "visual_summary", "important_details", "confidence"}
            })
            provider = mock.Mock(return_value=_response("stop", content))
            with mock.patch.object(pipeline, "uniform_image_observation", return_value=None), \
                 mock.patch.object(pipeline, "image_data_url", return_value="data:image/jpeg;base64,AA=="), \
                 mock.patch.object(pipeline, "post_json", provider), \
                 mock.patch.object(pipeline, "minimax_endpoint", return_value="https://example.invalid/v1/chat/completions"):
                first = pipeline.analyze_images(
                    [{"path": str(image), "timestamp": 1.5}], "key", "MiniMax-M3",
                    workers=1, cache_dir=root / "cache",
                )
                second = pipeline.analyze_images(
                    [{"path": str(image), "timestamp": 1.5}], "key", "MiniMax-M3",
                    workers=1, cache_dir=root / "cache",
                )
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(first, second)
            self.assertEqual(first[0]["analysis_method"], "minimax")
            self.assertTrue(pipeline.reusable_visual_artifact(first, [{"timestamp": 1.5, "path": str(image)}]))

    def test_valid_legacy_cache_hit_needs_no_provider_call(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / "frame.jpg"
            image.write_bytes(b"frame")
            cache_dir = root / "cache"
            valid_json = json.dumps({
                key: value for key, value in self._good().items()
                if key in {"screen_type", "visible_text", "visual_summary", "important_details", "confidence"}
            })
            with mock.patch.object(pipeline, "uniform_image_observation", return_value=None), \
                 mock.patch.object(pipeline, "image_data_url", return_value="data:image/jpeg;base64,AA=="), \
                 mock.patch.object(pipeline, "minimax_chat", return_value=valid_json):
                pipeline.analyze_images(
                    [{"path": str(image), "timestamp": 1.5}], "key", "MiniMax-M3",
                    workers=1, cache_dir=cache_dir,
                )
            cache_file = next(cache_dir.iterdir())
            legacy = json.loads(cache_file.read_text(encoding="utf-8"))
            legacy.pop("visual_schema_version")
            legacy.pop("completion_status")
            legacy.pop("analysis_method")
            cache_file.write_text(json.dumps(legacy), encoding="utf-8")
            with mock.patch.object(pipeline, "minimax_chat", side_effect=AssertionError("unexpected provider call")):
                observations = pipeline.analyze_images(
                    [{"path": str(image), "timestamp": 1.5}], "key", "MiniMax-M3",
                    workers=1, cache_dir=cache_dir,
                )
            self.assertEqual(observations[0]["visual_summary"], "投影片內容")

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
