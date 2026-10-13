"""Held source proposal: actual gap/backfill entry with owned files and provider stubs."""
import math
import tempfile
import unittest
from pathlib import Path
from unittest import mock
import pipeline


class TranscriptGapSchemaRegressionTests(unittest.TestCase):
    def test_nonfinite_or_reversed_interval_cannot_hide_existing_hole(self):
        expected = [{"start": 120.0, "end": 149.6}]
        genuine = [{"start": 0.0, "end": 120.0}, {"start": 149.6, "end": 166.0}]
        for bad in [{"start": 0.0, "end": float("inf")},
                    {"start": 0.0, "end": float("nan")},
                    {"start": -100.0, "end": 0.0},
                    {"start": 130.0, "end": 125.0}]:
            with self.subTest(bad=bad):
                self.assertEqual(pipeline.find_transcript_gaps(genuine + [bad]), expected)

    def run_backfill(self, bad, *, twice=False, failure=None):
        with tempfile.TemporaryDirectory(prefix="video65-schema-") as temporary:
            root = Path(temporary)
            chunks = []
            for index, (start, end) in enumerate([(0.0, 120.0), (120.0, 166.0)]):
                audio = root / f"audio_{index:04d}.mp3"
                audio.write_bytes(f"owned-{index}".encode())
                chunks.append({"index": index, "path": str(audio), "source_start": start,
                               "source_end": end, "sha256": pipeline.file_sha256(audio)})
            calls = []
            def extract(source, start, end, dest):
                self.assertEqual((start, end), (105.0, 164.6))
                dest.write_bytes(b"owned gap audio")
                return dest
            def transcribe(audio, *args, **kwargs):
                calls.append(Path(audio).name)
                if Path(audio).name.startswith("gap-"):
                    if failure is not None:
                        raise failure
                    return {"language": "zh", "segments": bad + [
                        {"start": 15.0, "end": 44.6, "text": "valid gap speech"}]}
                if Path(audio).name.endswith("0000.mp3"):
                    return {"language": "zh", "segments": [{"start": 0.0, "end": 120.0, "text": "left"}]}
                return {"language": "zh", "segments": [{"start": 29.6, "end": 46.0, "text": "right"}]}
            with mock.patch.object(pipeline, "_extract_gap_audio", side_effect=extract), \
                 mock.patch.object(pipeline, "groq_transcribe", side_effect=transcribe):
                transcript, _ = pipeline.transcribe_all(chunks, "owned-not-a-credential", "model", "auto", 1,
                    root / "cache", source_path=root / "owned.mp4")
                if twice:
                    transcript, _ = pipeline.transcribe_all(chunks, "owned-not-a-credential", "model", "auto", 1,
                        root / "cache", source_path=root / "owned.mp4")
                return transcript, calls

    def assert_only_valid_speech(self, transcript):
        self.assertEqual([x["text"] for x in transcript["segments"]], ["left", "valid gap speech", "right"])
        self.assertEqual(transcript["gap_check"]["remaining"], [])
        self.assertTrue(all(math.isfinite(x["start"]) and math.isfinite(x["end"]) and
                            0 <= x["start"] < x["end"] for x in transcript["segments"]))

    def test_nan_backfill_is_not_admitted_as_speech(self):
        transcript, calls = self.run_backfill([{"start": float("nan"), "end": float("nan"), "text": "invalid nan"}])
        self.assert_only_valid_speech(transcript)
        self.assertEqual(len(calls), 3)

    def test_reversed_and_zero_width_backfill_are_not_admitted_as_speech(self):
        transcript, _ = self.run_backfill([
            {"start": 20.0, "end": 19.0, "text": "invalid reversed"},
            {"start": 20.0, "end": 20.0, "text": "invalid empty"}])
        self.assert_only_valid_speech(transcript)

    def test_cached_malformed_rows_are_filtered_without_another_provider_call(self):
        transcript, calls = self.run_backfill([
            {"start": float("nan"), "end": float("nan"), "text": "invalid cached nan"}], twice=True)
        self.assert_only_valid_speech(transcript)
        self.assertEqual(len(calls), 3)

    def test_cancel_and_deadline_propagate_exact_original_exception(self):
        for cls in (pipeline.ExecutionCancelled, pipeline.DeadlineExceeded):
            failure = cls("owned interruption")
            with self.subTest(kind=cls.__name__), self.assertRaises(cls) as caught:
                self.run_backfill([], failure=failure)
            self.assertIs(caught.exception, failure)


if __name__ == "__main__":
    unittest.main()
