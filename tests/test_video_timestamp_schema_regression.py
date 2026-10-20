"""Held source proposal: exercise the actual MP4 entry, with owned provider stubs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import pipeline


class VideoTimestampSchemaRegressionTests(unittest.TestCase):
    def run_response(self, events):
        with tempfile.TemporaryDirectory(prefix="video62-schema-") as temporary:
            root = Path(temporary)
            video = root / "owned.mp4"
            video.write_bytes(b"owned synthetic media; provider is stubbed")
            response = json.dumps({"events": events, "overall_summary": "Synthetic summary"})
            with mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="owned-file") as upload, \
                 mock.patch.object(pipeline, "minimax_chat", return_value=response) as chat, \
                 mock.patch.object(pipeline, "compress_video_for_minimax_upload") as compress, \
                 mock.patch.object(pipeline, "_fill_video_event_gaps", side_effect=lambda rows, **_: rows):  # gap filling is tested separately
                observations = pipeline.analyze_video_mp4(
                    video, "owned-not-a-credential", "MiniMax-M3", duration=166.6,
                    work_dir=root / "work", cache_dir=root / "cache")
                cache_files = list((root / "cache").glob("*.json"))
                return observations, upload.call_count, chat.call_count, compress.call_count, cache_files

    @staticmethod
    def event(stamp):
        return {"timestamp": stamp, "screen_type": "Owned frame", "visible_text": [],
                "visual_summary": "Synthetic frame", "important_details": [], "confidence": 0.9}

    def assert_incomplete(self, result):
        observations, uploads, chats, compression, cache = result
        self.assertEqual([x for x in observations if pipeline.usable_visual_observation(x)], [])
        self.assertEqual(observations[0]["finish_reason"], "timestamp_coverage")
        self.assertEqual((uploads, chats, compression), (1, 2, 0))
        self.assertEqual(cache, [])

    def test_missing_timestamp_is_not_fabricated_beside_valid_endpoint(self):
        missing = self.event(0.0)
        del missing["timestamp"]
        result = self.run_response([missing, self.event(137.0)])
        self.assertEqual([x["timestamp"] for x in result[0]], [137.0])
        self.assertEqual((result[1], result[2]), (1, 1))

    def test_all_invalid_timestamps_cannot_become_cached_summary_at_zero(self):
        self.assert_incomplete(self.run_response([self.event("not-a-timestamp")]))

    def test_all_missing_timestamps_cannot_become_cached_summary_at_zero(self):
        missing = self.event(0.0)
        del missing["timestamp"]
        self.assert_incomplete(self.run_response([missing]))

    def test_explicit_empty_events_preserve_existing_summary_compatibility(self):
        result = self.run_response([])
        self.assertEqual([x["timestamp"] for x in result[0]], [0.0])
        self.assertTrue(pipeline.usable_visual_observation(result[0][0]))
        self.assertEqual((result[1], result[2]), (1, 1))

    def test_clock_timestamp_preserves_one_upload_and_one_chat(self):
        result = self.run_response([self.event("02:17")])
        self.assertEqual([x["timestamp"] for x in result[0]], [137.0])
        self.assertEqual((result[1], result[2]), (1, 1))


if __name__ == "__main__":
    unittest.main()
