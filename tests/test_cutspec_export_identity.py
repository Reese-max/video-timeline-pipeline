"""Real local-media checks for altered evidence and export clip identity."""
import copy
import csv
import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import cutspec


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "local FFmpeg required")
class ExportIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.video = Path(cls.directory.name) / "local.mp4"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i",
            "color=c=black:s=64x64:r=30:d=2", "-c:v", "libx264",
            "-pix_fmt", "yuv420p", str(cls.video),
        ], check=True, timeout=30)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def setUp(self):
        self.spec = cutspec.build_cutspec(cutspec.source_from_video(self.video), [
            cutspec.CutSegment("seg-1", {"startSec": 0.5, "endSec": 1.5}, {},
                               "transcript:local:span-1", "manual", "local evidence", 1.0),
        ])

    def verify(self, text, kind, **kwargs):
        return cutspec.verify_export(self.spec, self.video, text, kind, **kwargs)

    def assert_refused(self, receipt):
        self.assertNotEqual(receipt["status"], "verified")
        self.assertTrue(receipt["stillNeedsHumanFix"])
        self.assertGreater(receipt["mismatches"], 0)

    def test_csv_rejects_changed_evidence_source_and_extra_segments(self):
        original = list(csv.DictReader(io.StringIO(cutspec.cutspec_to_csv(self.spec))))
        for row_index, field, value in [
            (1, "evidenceRef", "transcript:other:span-999"),
            (0, "contentHash", "b" * 64),
            (1, "selection", "suggested"),
            (1, "specHash", "forged-segment-hash"),
            (1, "timelineEndSec", "99.0"),
        ]:
            with self.subTest(field=field):
                rows = [dict(row) for row in original]
                rows[row_index][field] = value
                output = io.StringIO()
                writer = csv.DictWriter(output, fieldnames=cutspec.CSV_COLUMNS)
                writer.writeheader()
                writer.writerows(rows)
                self.assert_refused(self.verify(output.getvalue(), "csv"))
        extra = copy.deepcopy(self.spec.segments[0])
        extra.segmentId = "seg-extra"
        exported = cutspec.build_cutspec(self.spec.source, [extra])
        self.assert_refused(self.verify(cutspec.cutspec_to_csv(exported), "csv"))

    def test_otio_rejects_changed_actual_clip_provenance_and_duplicates(self):
        original = cutspec.cutspec_to_otio_json(self.spec)
        for change in ["evidence", "source", "duplicate", "extra"]:
            with self.subTest(change=change):
                data = json.loads(original)
                clips = data["tracks"]["children"][0]["children"]
                if change == "evidence":
                    clips[0]["metadata"]["cutspec_segment"]["evidenceRef"] = "transcript:other:span-999"
                elif change == "source":
                    clips[0]["media_references"]["DEFAULT_MEDIA"]["target_url"] = "other.mp4"
                else:
                    extra = json.loads(json.dumps(clips[0]))
                    if change == "extra":
                        extra["name"] = "untracked"
                    clips.append(extra)
                self.assert_refused(self.verify(json.dumps(data), "otio"))

    def test_valid_redacted_csv_and_integer_ranges_remain_verified(self):
        for redacted in [False, True]:
            with self.subTest(redacted=redacted):
                self.spec.segments[0].sourceRange = {"startSec": 0, "endSec": 1}
                self.spec = cutspec.build_cutspec(self.spec.source, self.spec.segments)
                receipt = self.verify(cutspec.cutspec_to_csv(self.spec, redact_paths=redacted), "csv")
                self.assertEqual(receipt["status"], "verified")
                self.assertEqual(receipt["externalApiCalls"], 0)

    def test_otio_rejects_nonfinite_and_boolean_actual_timing(self):
        original = cutspec.cutspec_to_otio_json(self.spec)
        for field in ["start_time", "duration"]:
            for value in [float("nan"), float("inf"), True, 10 ** 500]:
                with self.subTest(field=field, value=value):
                    data = json.loads(original)
                    data["tracks"]["children"][0]["children"][0]["source_range"][field]["value"] = value
                    self.assert_refused(self.verify(json.dumps(data), "otio"))

    def test_otio_rejects_unsupported_layout_and_boolean_embedded_ranges(self):
        original = cutspec.cutspec_to_otio_json(self.spec)
        for change in ["gap", "disabled", "effect", "trim", "boolean_range"]:
            with self.subTest(change=change):
                data = json.loads(original)
                track = data["tracks"]["children"][0]
                clip = track["children"][0]
                if change == "gap":
                    track["children"].insert(0, {"OTIO_SCHEMA": "Gap.1", "source_range": clip["source_range"]})
                elif change == "disabled":
                    clip["enabled"] = False
                elif change == "effect":
                    clip["effects"] = [{"OTIO_SCHEMA": "LinearTimeWarp.1", "time_scalar": 2.0}]
                elif change == "trim":
                    track["source_range"] = clip["source_range"]
                else:
                    # Equal dict values do not make JSON booleans numeric ranges.
                    self.spec.segments[0].sourceRange = {"startSec": 0, "endSec": 1}
                    self.spec = cutspec.build_cutspec(self.spec.source, self.spec.segments)
                    data = json.loads(cutspec.cutspec_to_otio_json(self.spec))
                    data["metadata"]["cutspec"]["cutSpec"]["segments"][0]["sourceRange"] = {"startSec": False, "endSec": True}
                self.assert_refused(self.verify(json.dumps(data), "otio"))

    def test_invalid_spec_and_nonfinite_tolerance_are_refused(self):
        for tolerance in [float("nan"), float("inf"), -1.0, True, 10 ** 500]:
            with self.subTest(tolerance=tolerance):
                self.assert_refused(self.verify(cutspec.cutspec_to_csv(self.spec), "csv", tolerance_sec=tolerance))
        self.spec = cutspec.build_cutspec(self.spec.source, [])
        self.assert_refused(self.verify(cutspec.cutspec_to_csv(self.spec), "csv"))


if __name__ == "__main__":
    unittest.main()
