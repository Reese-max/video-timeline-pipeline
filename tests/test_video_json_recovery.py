"""Synthetic regressions for strict MP4 response parsing and error classification."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


def event(timestamp: float, summary: str = "synthetic frame") -> dict:
    return {
        "timestamp": timestamp,
        "screen_type": "synthetic",
        "visible_text": [],
        "visual_summary": summary,
        "important_details": [],
        "confidence": 0.9,
    }


def response(events: list[dict], *, summary: str = "synthetic summary") -> str:
    return json.dumps(
        {"events": events, "overall_summary": summary},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def break_one_visual_summary_separator(raw: str) -> str:
    token = '"visual_summary":"'
    assert raw.count(token) >= 1
    return raw.replace(token, '"visual_summary="', 1)


class StrictVideoJsonParserTests(unittest.TestCase):
    def test_valid_json_is_unchanged(self) -> None:
        raw = response([event(0, 'caption says "visual_summary=" literally')])
        parsed, diagnostics = pipeline.parse_video_response_json(raw)
        self.assertEqual(parsed, json.loads(raw))
        self.assertEqual(diagnostics, {"json_repaired": False})

    def test_one_known_key_separator_typo_is_repaired(self) -> None:
        raw = break_one_visual_summary_separator(
            response([event(0, 'escaped quote: "hello"')])
        )
        parsed, diagnostics = pipeline.parse_video_response_json(raw)
        self.assertEqual(parsed["events"][0]["visual_summary"], 'escaped quote: "hello"')
        self.assertTrue(diagnostics["json_repaired"])
        self.assertEqual(diagnostics["repair"], pipeline.VIDEO_RESPONSE_REPAIR)
        self.assertEqual(diagnostics["raw"], raw)

    def test_same_text_inside_a_legal_string_is_not_rewritten(self) -> None:
        raw = response([event(0, 'subtitle contains "visual_summary=" text')])
        parsed, diagnostics = pipeline.parse_video_response_json(raw)
        self.assertIsNotNone(parsed)
        self.assertFalse(diagnostics["json_repaired"])

    def test_multiple_known_typos_are_ambiguous_and_rejected(self) -> None:
        raw = response([event(0), event(1)])
        raw = raw.replace('"visual_summary":"', '"visual_summary="')
        parsed, diagnostics = pipeline.parse_video_response_json(raw)
        self.assertIsNone(parsed)
        self.assertEqual(diagnostics["finish_reason"], "json_syntax")

    def test_duplicate_key_is_rejected(self) -> None:
        raw = '{"events":[],"events":[],"overall_summary":"synthetic"}'
        parsed, diagnostics = pipeline.parse_video_response_json(raw)
        self.assertIsNone(parsed)
        self.assertEqual(diagnostics["finish_reason"], "json_duplicate_key")
        self.assertEqual(diagnostics["duplicate_key"], "events")

    def test_repair_that_would_create_a_duplicate_key_is_rejected(self) -> None:
        raw = (
            '{"events":[{"timestamp":0,"screen_type":"synthetic","visible_text":[],'
            '"visual_summary":"first","visual_summary="second","important_details":[],'
            '"confidence":0.9}],"overall_summary":"synthetic"}'
        )
        parsed, diagnostics = pipeline.parse_video_response_json(raw)
        self.assertIsNone(parsed)
        self.assertEqual(diagnostics["finish_reason"], "json_syntax")

    def test_other_key_typo_is_not_guessed(self) -> None:
        raw = response([event(0)]).replace('"screen_type":"', '"screen_type="', 1)
        parsed, diagnostics = pipeline.parse_video_response_json(raw)
        self.assertIsNone(parsed)
        self.assertEqual(diagnostics["finish_reason"], "json_syntax")

    def test_truncated_json_has_its_own_category(self) -> None:
        raw = response([event(0)])[:-8]
        parsed, diagnostics = pipeline.parse_video_response_json(raw)
        self.assertIsNone(parsed)
        self.assertEqual(diagnostics["error_category"], "truncation")
        self.assertEqual(diagnostics["finish_reason"], "json_truncated")

    def test_trailing_garbage_is_not_substring_parsed(self) -> None:
        raw = response([event(0)]) + " trailing"
        parsed, diagnostics = pipeline.parse_video_response_json(raw)
        self.assertIsNone(parsed)
        self.assertEqual(diagnostics["finish_reason"], "json_syntax")

    def test_non_object_top_level_is_schema_error(self) -> None:
        parsed, diagnostics = pipeline.parse_video_response_json("[]")
        self.assertIsNone(parsed)
        self.assertEqual(diagnostics["finish_reason"], "response_schema")

    def test_375_event_synthetic_response_repairs_then_fully_validates(self) -> None:
        marks = [round(index * 536.2 / 374, 3) for index in range(375)]
        target = min(range(len(marks)), key=lambda index: abs(marks[index] - 512.0))
        marks[target] = 512.0
        events = [event(mark, f"synthetic-{index}") for index, mark in enumerate(marks)]
        events[target]["visual_summary"] = "target-512"
        raw = response(events).replace(
            '"visual_summary":"target-512"',
            '"visual_summary="target-512"',
            1,
        )
        parsed, diagnostics = pipeline.parse_video_response_json(raw)
        self.assertTrue(diagnostics["json_repaired"])
        self.assertIsNone(pipeline.video_response_validation_issue(parsed, 536.22))
        observations = pipeline._parse_video_mp4_events(parsed)
        self.assertEqual(len(observations), 375)
        self.assertTrue(pipeline.video_timestamps_cover_duration(observations, 536.22))


class VideoResponseIntegrationTests(unittest.TestCase):
    def run_response(self, responses, duration: float = 8.0, *, cache: bool = True):
        temporary = tempfile.TemporaryDirectory(prefix="video-json-recovery-")
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        video = root / "synthetic.mp4"
        video.write_bytes(b"synthetic provider-stub fixture")
        cache_dir = root / "cache" if cache else None
        patches = [
            mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="synthetic-file"),
            mock.patch.object(pipeline, "minimax_chat", side_effect=responses),
            mock.patch.object(pipeline, "compress_video_for_minimax_upload"),
        ]
        mocks = [patch.start() for patch in patches]
        self.addCleanup(lambda: [patch.stop() for patch in patches])
        observations = pipeline.analyze_video_mp4(
            video,
            "synthetic-not-a-credential",
            "MiniMax-M3",
            duration=duration,
            cache_dir=cache_dir,
            work_dir=root / "work",
        )
        cache_files = list(cache_dir.glob("*.json")) if cache_dir and cache_dir.exists() else []
        return observations, mocks[1], cache_files

    def test_repaired_response_is_validated_marked_and_raw_is_cached(self) -> None:
        raw = break_one_visual_summary_separator(response([event(0), event(4), event(8)]))
        observations, chat, cache_files = self.run_response([raw])
        self.assertEqual(chat.call_count, 1)
        self.assertEqual([row["timestamp"] for row in observations], [0.0, 4.0, 8.0])
        self.assertTrue(all(pipeline.usable_visual_observation(row) for row in observations))
        self.assertTrue(all(row["response_repaired"] for row in observations))
        self.assertTrue(all(row["response_repair"] == pipeline.VIDEO_RESPONSE_REPAIR for row in observations))
        self.assertEqual(len(cache_files), 1)
        cached = json.loads(cache_files[0].read_text(encoding="utf-8"))
        self.assertEqual(cached["response_diagnostics"]["raw"], raw)
        self.assertEqual(cached["response_diagnostics"]["repair"], pipeline.VIDEO_RESPONSE_REPAIR)

    def test_json_syntax_failure_is_not_mislabeled_or_retried(self) -> None:
        raw = response([event(0)]).replace('"screen_type":"', '"screen_type="', 1)
        observations, chat, cache_files = self.run_response([raw])
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(observations[0]["finish_reason"], "json_syntax")
        self.assertEqual(observations[0]["error_category"], "json_syntax")
        self.assertEqual(observations[0]["raw"], raw)
        self.assertEqual(cache_files, [])

    def test_missing_required_field_is_schema_error_without_retry(self) -> None:
        invalid = event(0)
        del invalid["visual_summary"]
        observations, chat, _ = self.run_response([response([invalid])])
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(observations[0]["finish_reason"], "response_schema")
        self.assertEqual(observations[0]["error_category"], "schema")

    def test_wrong_field_type_is_schema_error(self) -> None:
        invalid = event(0)
        invalid["visible_text"] = "not-an-array"
        observations, chat, _ = self.run_response([response([invalid])])
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(observations[0]["finish_reason"], "response_schema")

    def test_out_of_bounds_timestamp_is_not_complete(self) -> None:
        observations, chat, _ = self.run_response([response([event(0), event(9)])])
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(observations[0]["finish_reason"], "timestamp_bounds")
        self.assertEqual(observations[0]["error_category"], "timestamp")

    def test_non_monotonic_timestamp_is_not_complete(self) -> None:
        observations, chat, _ = self.run_response(
            [response([event(0), event(6), event(4), event(8)])]
        )
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(observations[0]["finish_reason"], "timestamp_order")

    def test_recovered_but_grossly_gapped_timeline_still_fails_coverage(self) -> None:
        raw = break_one_visual_summary_separator(
            response([event(0, "start"), event(512, "middle"), event(536.2, "end")])
        )
        observations, chat, _ = self.run_response([raw, raw], duration=536.22)
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(observations[0]["finish_reason"], "timestamp_coverage")
        self.assertEqual(observations[0]["error_category"], "timestamp")
        self.assertEqual(observations[0]["raw"], raw)

    def test_provider_length_finish_reason_wins_over_parseability(self) -> None:
        error = pipeline.MiniMaxIncompleteError("length", "MiniMax-M3")
        observations, chat, _ = self.run_response([error])
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(observations[0]["finish_reason"], "length")
        self.assertEqual(observations[0]["error_category"], "truncation")


if __name__ == "__main__":
    unittest.main()
