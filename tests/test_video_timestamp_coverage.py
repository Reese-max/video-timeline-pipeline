"""Issue #56: MiniMax video timestamps must span the real video duration.

Job 16fae7864ca8 returned 17 visual events stamped 0.0-6.9s for a 166.6s
video. The model-visible contract (seconds on the original timeline) was
underspecified, the parser silently coerced garbage to 0.0, and nothing
validated coverage — so compressed evidence was cached as if it were fine.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


def _compressed_response() -> str:
    """Mirror of the issue #56 evidence: 17 events squeezed into 0-6.9s."""
    marks = [0.3, 0.9, 1.2, 1.8, 2.4, 2.7, 3.1, 3.6, 4.1, 4.4, 4.9, 5.3, 5.7, 6.0, 6.2, 6.6, 6.9]
    return json.dumps(
        {
            "events": [
                {
                    "timestamp": mark,
                    "screen_type": "影片片段",
                    "visible_text": [],
                    "visual_summary": f"畫面摘要 {index}",
                    "important_details": [],
                    "confidence": 0.8,
                }
                for index, mark in enumerate(marks)
            ],
            "overall_summary": "整片摘要",
        },
        ensure_ascii=False,
    )


def _spread_response() -> str:
    return json.dumps(
        {
            "events": [
                {
                    "timestamp": 8.0,
                    "screen_type": "開場",
                    "visible_text": [],
                    "visual_summary": "開場畫面",
                    "important_details": [],
                    "confidence": 0.9,
                },
                {
                    "timestamp": 137.0,
                    "screen_type": "結尾",
                    "visible_text": [],
                    "visual_summary": "結尾畫面",
                    "important_details": [],
                    "confidence": 0.9,
                },
            ],
            "overall_summary": "整片摘要",
        },
        ensure_ascii=False,
    )


class ParseVideoTimestampTests(unittest.TestCase):
    def test_plain_seconds(self) -> None:
        self.assertEqual(pipeline.parse_video_timestamp(6.9), 6.9)
        self.assertEqual(pipeline.parse_video_timestamp(8), 8.0)
        self.assertEqual(pipeline.parse_video_timestamp("137.0"), 137.0)

    def test_clock_strings_become_seconds(self) -> None:
        self.assertAlmostEqual(pipeline.parse_video_timestamp("02:17"), 137.0)
        self.assertAlmostEqual(pipeline.parse_video_timestamp("1:04:05"), 3845.0)
        self.assertAlmostEqual(pipeline.parse_video_timestamp("56s"), 56.0)
        self.assertAlmostEqual(pipeline.parse_video_timestamp("56secs"), 56.0)
        self.assertAlmostEqual(pipeline.parse_video_timestamp("56seconds"), 56.0)
        self.assertAlmostEqual(pipeline.parse_video_timestamp("2分30秒"), 150.0)

    def test_garbage_returns_none_instead_of_zero(self) -> None:
        self.assertIsNone(pipeline.parse_video_timestamp(None))
        self.assertIsNone(pipeline.parse_video_timestamp("看不清楚"))
        self.assertIsNone(pipeline.parse_video_timestamp(float("nan")))
        self.assertIsNone(pipeline.parse_video_timestamp(-3.0))


class TimestampCoverageTests(unittest.TestCase):
    def _run_mp4(self, responses: list[str], duration: float):
        root = Path(tempfile.mkdtemp(prefix="issue-56-"))
        video = root / "clip.mp4"
        video.write_bytes(b"fake-mp4-bytes")
        patches = [
            mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid-56"),
            mock.patch.object(pipeline, "minimax_chat", side_effect=responses),
            mock.patch.object(pipeline, "compress_video_for_minimax_upload"),
            mock.patch.object(pipeline, "_fill_video_event_gaps", side_effect=lambda rows, **_: rows),  # gap filling is tested separately
        ]
        for patch in patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in patches])
        observations = pipeline.analyze_video_mp4(
            video, "key", "MiniMax-M3", duration=duration, work_dir=root / "work"
        )
        return observations, pipeline.minimax_chat

    def test_compressed_timestamps_are_not_accepted_as_evidence(self) -> None:
        observations, _ = self._run_mp4([_compressed_response(), _compressed_response()], 166.6)
        usable = [item for item in observations if pipeline.usable_visual_observation(item)]
        self.assertEqual(usable, [])

    def test_compressed_response_triggers_one_repair_retry(self) -> None:
        _, chat = self._run_mp4([_compressed_response(), _compressed_response()], 166.6)
        self.assertEqual(chat.call_count, 2)

    def test_repaired_response_is_kept(self) -> None:
        observations, chat = self._run_mp4([_compressed_response(), _spread_response()], 166.6)
        self.assertEqual(chat.call_count, 2)
        stamps = sorted(item["timestamp"] for item in observations)
        self.assertEqual(stamps, [8.0, 137.0])

    def test_healthy_response_needs_no_retry(self) -> None:
        observations, chat = self._run_mp4([_spread_response()], 166.6)
        self.assertEqual(chat.call_count, 1)
        self.assertEqual(sorted(item["timestamp"] for item in observations), [8.0, 137.0])

    def test_short_video_without_duration_pressure_stays_single_call(self) -> None:
        payload = json.dumps(
            {
                "events": [
                    {
                        "timestamp": 1.0,
                        "screen_type": "開場",
                        "visible_text": [],
                        "visual_summary": "短片畫面",
                        "important_details": [],
                        "confidence": 0.9,
                    }
                ],
                "overall_summary": "整片摘要",
            },
            ensure_ascii=False,
        )
        observations, chat = self._run_mp4([payload], 5.0)
        self.assertEqual(chat.call_count, 1)
        self.assertTrue(all(pipeline.usable_visual_observation(item) for item in observations))

    def test_timestamps_past_the_end_of_video_are_rejected(self) -> None:
        payload = json.dumps(
            {
                "events": [
                    {
                        "timestamp": 8.0,
                        "screen_type": "開場",
                        "visible_text": [],
                        "visual_summary": "開場畫面",
                        "important_details": [],
                        "confidence": 0.9,
                    },
                    {
                        "timestamp": 500.0,
                        "screen_type": "結尾",
                        "visible_text": [],
                        "visual_summary": "超片長畫面",
                        "important_details": [],
                        "confidence": 0.9,
                    },
                ],
                "overall_summary": "整片摘要",
            },
            ensure_ascii=False,
        )
        observations, chat = self._run_mp4([payload, payload], 166.6)
        self.assertEqual(chat.call_count, 2)
        usable = [item for item in observations if pipeline.usable_visual_observation(item)]
        self.assertEqual(usable, [])

    def test_clock_string_event_is_placed_on_timeline(self) -> None:
        payload = json.dumps(
            {
                "events": [
                    {
                        "timestamp": "02:17",
                        "screen_type": "結尾",
                        "visible_text": [],
                        "visual_summary": "結尾畫面",
                        "important_details": [],
                        "confidence": 0.9,
                    }
                ],
                "overall_summary": "整片摘要",
            },
            ensure_ascii=False,
        )
        observations, _ = self._run_mp4([payload], 166.6)
        self.assertEqual([item["timestamp"] for item in observations], [137.0])


if __name__ == "__main__":
    unittest.main()
