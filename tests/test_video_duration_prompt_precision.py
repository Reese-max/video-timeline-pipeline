"""Synthetic regressions for exact source-duration prompt boundaries."""

from __future__ import annotations

import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import pipeline


def event(timestamp: float, label: str = "synthetic") -> dict:
    return {
        "timestamp": timestamp,
        "screen_type": "test",
        "visible_text": [],
        "visual_summary": label,
        "important_details": [],
        "confidence": 0.9,
    }


def response(*timestamps: float) -> str:
    return json.dumps(
        {
            "events": [event(timestamp, f"event-{index}") for index, timestamp in enumerate(timestamps)],
            "overall_summary": "synthetic summary",
        },
        ensure_ascii=False,
    )


def prompt_from_call(call: mock._Call) -> str:
    return call.args[0][0]["content"][0]["text"]


class VideoDurationPromptPrecisionTests(unittest.TestCase):
    def test_initial_prompt_uses_round_trip_duration_endpoint(self) -> None:
        prompt = pipeline._video_mp4_prompt(8.06)
        self.assertIn("約 8.06 秒", prompt)
        self.assertNotIn("約 8.1 秒", prompt)

    def test_duration_endpoint_formatter_rejects_unknown_bounds(self) -> None:
        self.assertEqual(pipeline._format_video_duration_endpoint(536.26), "536.26")
        for value in (0, -1, math.inf, -math.inf, math.nan, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                pipeline._format_video_duration_endpoint(value)
            prompt = pipeline._video_mp4_prompt(value)
            self.assertNotIn("影片長度約", prompt)
            self.assertIn("覆蓋全片直到片尾", prompt)

    def test_correction_prompt_preserves_exact_duration_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            video = Path(temporary) / "synthetic.mp4"
            video.write_bytes(b"synthetic")
            chat = mock.MagicMock(side_effect=[response(0, 1), response(0, 268.13, 536.26)])
            with mock.patch.object(pipeline, "minimax_reusable_upload", return_value=("fixture-id", False)), \
                 mock.patch.object(pipeline, "minimax_chat", chat), \
                 mock.patch.object(pipeline, "_fill_video_event_gaps", side_effect=lambda rows, **_: rows):
                rows, _, _ = pipeline._analyze_video_mp4_core(
                    video,
                    "fixture-key",
                    "fixture-model",
                    prompt="base prompt",
                    fps_value=1,
                    detail_value="default",
                    timeout=1,
                    analysis_method="minimax_video",
                    source_video=video,
                    duration_hint=536.26,
                )

        self.assertEqual(chat.call_count, 2)
        correction = prompt_from_call(chat.call_args_list[1])
        self.assertIn("0 到 536.26", correction)
        self.assertNotIn("0 到 536.3", correction)
        self.assertEqual([row["timestamp"] for row in rows], [0.0, 268.13, 536.26])

    def test_chunk_prompt_uses_exact_relative_duration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.mp4"
            chunk_path = root / "chunk.mp4"
            source.write_bytes(b"source")
            chunk_path.write_bytes(b"chunk")
            chunk = pipeline.VideoMp4Chunk(chunk_path, 100.0, 108.06)
            plan = pipeline.VideoMp4ChunkPlan(
                True, 60, 1, 1.0, "default", "default", False, None
            )
            chat = mock.MagicMock(return_value=response(0, 8.06))
            with mock.patch.object(pipeline, "split_video_mp4_into_chunks", return_value=[chunk]), \
                 mock.patch.object(pipeline, "minimax_reusable_upload", return_value=("fixture-id", False)), \
                 mock.patch.object(pipeline, "minimax_chat", chat), \
                 mock.patch.object(pipeline, "check_video_mp4_chunk_budget"):
                rows = pipeline._analyze_video_mp4_chunked(
                    source,
                    "fixture-key",
                    "fixture-model",
                    duration=108.06,
                    cache_dir=None,
                    fps_value=1.0,
                    detail_value="default",
                    timeout=1,
                    work_dir=root / "work",
                    plan=plan,
                    max_completion_tokens=1000,
                )

        prompt = prompt_from_call(chat.call_args)
        self.assertIn("約 8.06 秒", prompt)
        self.assertNotIn("約 8.1 秒", prompt)
        self.assertEqual([row["timestamp"] for row in rows], [100.0, 108.06])

    def test_strict_validator_accepts_exact_endpoint_but_rejects_real_overshoot(self) -> None:
        exact = {"events": [event(0), event(8.06)], "overall_summary": "synthetic"}
        overshoot = {"events": [event(0), event(8.1)], "overall_summary": "synthetic"}
        self.assertIsNone(pipeline.video_response_validation_issue(exact, 8.06))
        issue = pipeline.video_response_validation_issue(overshoot, 8.06)
        self.assertIsNotNone(issue)
        self.assertEqual(issue["finish_reason"], "timestamp_bounds")
        self.assertEqual(issue["timestamp_issue"], "outside_source_duration")


if __name__ == "__main__":
    unittest.main()
