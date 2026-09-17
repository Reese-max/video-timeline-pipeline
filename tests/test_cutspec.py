import json
import shutil
import subprocess
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import cutspec
import pipeline


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "cutspec"
MANIFEST = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def fixture_source(name: str) -> cutspec.SourceInfo:
    path = FIXTURE_DIR / name
    return cutspec.source_from_video(path)


def make_spec(name: str = "cfr_30.mp4", ranges=((0.5, 1.5), (2.0, 3.0))) -> cutspec.CutSpec:
    source = fixture_source(name)
    segments = [
        cutspec.CutSegment(
            segmentId=f"seg-{i}",
            sourceRange={"startSec": s, "endSec": e},
            timelineRange={"startSec": 0.0, "endSec": 0.0},
            evidenceRef=f"transcript:{name}:span-{i}",
            selection="manual",
            reason="fixture clip",
            confidence=1.0,
            note="",
        )
        for i, (s, e) in enumerate(ranges)
    ]
    return cutspec.build_cutspec(source, segments)


def codes(issues):
    return {i.code for i in issues}


def rejects(issues):
    return {i.code for i in issues if i.severity == "reject"}


class SchemaTests(unittest.TestCase):
    def test_spec_hash_deterministic_and_self_excluding(self) -> None:
        spec = make_spec()
        again = make_spec()
        self.assertEqual(spec.specHash, again.specHash)
        self.assertTrue(spec.specHash)
        loaded = cutspec.CutSpec.from_json(spec.to_json())
        self.assertEqual(loaded.specHash, spec.specHash)
        self.assertEqual(loaded.compute_spec_hash(), spec.specHash)
        mutated = cutspec.CutSpec.from_json(spec.to_json())
        mutated.specHash = "tampered"
        self.assertEqual(mutated.compute_spec_hash(), spec.specHash)

    def test_spec_hash_stable_across_path_and_mtime_relocation(self) -> None:
        spec = make_spec()
        relocated = cutspec.CutSpec.from_json(spec.to_json())
        relocated.source.videoPath = "/elsewhere/renamed copy.mp4"
        relocated.source.fingerprint = "99999:1"
        self.assertEqual(relocated.compute_spec_hash(), spec.specHash)

    def test_timeline_ranges_derived_cumulatively(self) -> None:
        spec = make_spec(ranges=((0.5, 1.5), (2.0, 4.0)))
        self.assertEqual(spec.segments[0].timelineRange, {"startSec": 0.0, "endSec": 1.0})
        self.assertEqual(spec.segments[1].timelineRange, {"startSec": 1.0, "endSec": 3.0})
        self.assertAlmostEqual(spec.intendedDurationSec, 3.0)

    def test_every_segment_carries_provenance(self) -> None:
        spec = make_spec()
        for seg in spec.segments:
            self.assertTrue(seg.evidenceRef)
            self.assertIn(seg.selection, cutspec.SELECTION_REASONS)
            self.assertGreater(seg.sourceRange["endSec"], seg.sourceRange["startSec"])
        self.assertTrue(spec.source.contentHash)
        self.assertTrue(spec.source.fingerprint)


class ValidationTests(unittest.TestCase):
    def test_missing_range_rejected(self) -> None:
        spec = make_spec()
        spec.segments[0].sourceRange = {"startSec": float("nan"), "endSec": 1.0}
        self.assertIn("MISSING_RANGE", rejects(cutspec.validate_cutspec(spec)))

    def test_negative_duration_rejected(self) -> None:
        spec = make_spec(ranges=((1.5, 0.5),))
        self.assertIn("NEGATIVE_DURATION", rejects(cutspec.validate_cutspec(spec)))

    def test_missing_evidence_rejected(self) -> None:
        spec = make_spec()
        spec.segments[0].evidenceRef = ""
        self.assertIn("MISSING_EVIDENCE", rejects(cutspec.validate_cutspec(spec)))

    def test_unknown_selection_rejected(self) -> None:
        spec = make_spec()
        spec.segments[0].selection = "vibes"
        self.assertIn("BAD_SELECTION", rejects(cutspec.validate_cutspec(spec)))

    def test_out_of_range_rejected_not_clamped(self) -> None:
        spec = make_spec(ranges=((0.5, 1.5), (2.0, 99.0)))
        issues = cutspec.validate_cutspec(spec)
        self.assertIn("OUT_OF_RANGE", rejects(issues))
        self.assertEqual(spec.segments[1].sourceRange["endSec"], 99.0)

    def test_missing_source_rejected(self) -> None:
        spec = make_spec()
        spec.source.videoPath = ""
        spec.source.contentHash = ""
        self.assertIn("MISSING_SOURCE", rejects(cutspec.validate_cutspec(spec)))

    def test_unparseable_timebase_rejected(self) -> None:
        spec = make_spec()
        spec.source.timebase = "not-a-rate"
        self.assertIn("UNSUPPORTED_TIMEBASE", rejects(cutspec.validate_cutspec(spec)))

    def test_untested_timebase_degraded_not_rejected(self) -> None:
        spec = make_spec()
        spec.source.timebase = "60000/1001"
        spec.source.fps = float(Fraction(60000, 1001))
        issues = cutspec.validate_cutspec(spec)
        self.assertIn("UNTESTED_TIMEBASE", codes(issues))
        self.assertNotIn("UNTESTED_TIMEBASE", rejects(issues))
        self.assertTrue(cutspec.is_exportable(issues))

    def test_vfr_source_flagged_degraded(self) -> None:
        spec = make_spec("vfr_30.mp4")
        issues = cutspec.validate_cutspec(spec)
        self.assertIn("VFR_SOURCE", codes(issues))
        self.assertNotIn("VFR_SOURCE", rejects(issues))

    def test_source_overlap_flagged(self) -> None:
        spec = make_spec(ranges=((0.5, 2.0), (1.0, 3.0)))
        issues = cutspec.validate_cutspec(spec)
        self.assertIn("OVERLAP", codes(issues))
        self.assertNotIn("OVERLAP", rejects(issues))

    def test_stale_source_rejects_and_does_not_rebind(self) -> None:
        spec = make_spec("cfr_30.mp4")
        other = fixture_source("cfr_25.mp4")
        issues = cutspec.validate_cutspec(spec, current_source=other)
        self.assertIn("STALE_SOURCE", rejects(issues))
        self.assertFalse(cutspec.is_exportable(issues))
        self.assertNotEqual(spec.source.contentHash, other.contentHash)

    def test_clean_spec_exportable(self) -> None:
        spec = make_spec()
        issues = cutspec.validate_cutspec(spec)
        self.assertTrue(cutspec.is_exportable(issues), [str(i) for i in issues])


class PreviewTests(unittest.TestCase):
    def test_preview_lists_clips_totals_and_gaps(self) -> None:
        spec = make_spec(ranges=((0.5, 1.5), (2.5, 3.5)))
        preview = cutspec.preview_cutspec(spec)
        self.assertEqual(preview["clipCount"], 2)
        self.assertAlmostEqual(preview["intendedDurationSec"], 2.0)
        self.assertEqual(len(preview["gaps"]), 1)
        self.assertAlmostEqual(preview["gaps"][0]["durationSec"], 1.0)
        self.assertEqual(preview["status"], "ok")

    def test_preview_reflects_rejections(self) -> None:
        spec = make_spec(ranges=((1.5, 0.5),))
        preview = cutspec.preview_cutspec(spec)
        self.assertEqual(preview["status"], "rejected")
        self.assertIn("NEGATIVE_DURATION", preview["rejectCodes"])

    def test_preview_and_export_make_no_external_calls(self) -> None:
        spec = make_spec()
        with mock.patch.object(pipeline.subprocess, "run", side_effect=AssertionError("subprocess")), \
             mock.patch.object(pipeline.urllib.request, "urlopen", side_effect=AssertionError("network")):
            cutspec.preview_cutspec(spec)
            cutspec.cutspec_to_csv(spec)
            cutspec.cutspec_to_otio_json(spec)


class TimebaseTests(unittest.TestCase):
    def test_parse_timebase(self) -> None:
        self.assertEqual(cutspec.parse_timebase("30000/1001"), Fraction(30000, 1001))
        self.assertEqual(cutspec.parse_timebase("25/1"), Fraction(25, 1))
        with self.assertRaises(ValueError):
            cutspec.parse_timebase("nope")

    def test_seconds_frames_contract_per_tested_timebase(self) -> None:
        for tb in sorted(cutspec.TESTED_TIMEBASES):
            fps = cutspec.parse_timebase(tb)
            with self.subTest(timebase=tb):
                self.assertEqual(cutspec.seconds_to_frames(1.0, tb), round(float(fps)))
                for sec in (0.0, 0.5, 1.234, 3.999):
                    frames = cutspec.seconds_to_frames(sec, tb)
                    back = cutspec.frames_to_seconds(frames, tb)
                    self.assertAlmostEqual(back, sec, delta=0.5 / float(fps) + 1e-9)

    def test_fixture_matrix_matches_manifest_timebases(self) -> None:
        for entry in MANIFEST["fixtures"]:
            name = entry["file"]
            if not name.startswith("cfr_"):
                continue
            with self.subTest(fixture=name):
                source = fixture_source(name)
                self.assertEqual(source.timebase, entry["probe"]["avg_frame_rate"])
                self.assertIn(source.timebase, cutspec.TESTED_TIMEBASES)
                self.assertFalse(source.vfr)

    def test_vfr_fixture_detected(self) -> None:
        source = fixture_source("vfr_30.mp4")
        self.assertTrue(source.vfr)


class InterchangeTests(unittest.TestCase):
    def test_csv_roundtrip_with_hostile_fields(self) -> None:
        spec = make_spec()
        spec.source.videoPath = "/tmp/dir,with,commas/vid,eo.mp4"
        spec.segments[0].reason = 'quoted "reason", with comma'
        spec.segments[0].note = "多行\n備註"
        loaded = cutspec.cutspec_from_csv(cutspec.cutspec_to_csv(spec))
        self.assertEqual(loaded.specHash, spec.specHash)
        self.assertEqual(loaded.source.videoPath, spec.source.videoPath)
        self.assertEqual(loaded.segments[0].reason, spec.segments[0].reason)
        self.assertEqual(loaded.segments[0].note, spec.segments[0].note)
        self.assertEqual(len(loaded.segments), len(spec.segments))

    def test_otio_roundtrip_ranges(self) -> None:
        spec = make_spec()
        text = cutspec.cutspec_to_otio_json(spec)
        parsed = cutspec.otio_clip_ranges(text)
        self.assertEqual(len(parsed), len(spec.segments))
        fps = float(cutspec.parse_timebase(spec.source.timebase))
        for seg, got in zip(spec.segments, parsed):
            self.assertEqual(got["segmentId"], seg.segmentId)
            self.assertAlmostEqual(got["sourceStartSec"], seg.sourceRange["startSec"], delta=1.5 / fps)
            self.assertAlmostEqual(got["sourceEndSec"], seg.sourceRange["endSec"], delta=1.5 / fps)
            self.assertAlmostEqual(got["timelineStartSec"], seg.timelineRange["startSec"], delta=1.5 / fps)
        loaded = cutspec.cutspec_from_otio_json(text)
        self.assertEqual(loaded.specHash, spec.specHash)
        self.assertEqual(loaded.source.contentHash, spec.source.contentHash)

    def test_redacted_export_hides_local_path(self) -> None:
        spec = make_spec()
        spec.source.videoPath = "/home/box/private/secret_video.mp4"
        text = cutspec.cutspec_to_csv(spec, redact_paths=True)
        self.assertNotIn("/home/box/private", text)
        self.assertIn("secret_video.mp4", text)
        self.assertIn(spec.source.contentHash, text)
        otio_text = cutspec.cutspec_to_otio_json(spec, redact_paths=True)
        self.assertNotIn("/home/box/private", otio_text)
        loaded = cutspec.cutspec_from_otio_json(otio_text)
        self.assertEqual(loaded.specHash, spec.specHash)
        self.assertEqual(loaded.compute_spec_hash(), spec.specHash)

    @unittest.skipUnless(HAS_FFMPEG, "ffprobe required for verify")
    def test_csv_export_tamper_detected(self) -> None:
        spec = make_spec()
        rows = cutspec.cutspec_to_csv(spec).splitlines()
        rows[2] = rows[2].replace("0.5", "0.9", 1)
        parsed = cutspec.cutspec_from_csv("\n".join(rows))
        self.assertNotEqual(
            parsed.segments[0].sourceRange["startSec"],
            spec.segments[0].sourceRange["startSec"],
        )
        receipt = cutspec.verify_export(
            spec, FIXTURE_DIR / "cfr_30.mp4", "\n".join(rows), "csv"
        )
        self.assertGreater(receipt["mismatches"], 0)
        self.assertTrue(receipt["stillNeedsHumanFix"])


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe required for runtime verification")
class VerificationTests(unittest.TestCase):
    def test_frozen_fixtures_match_manifest(self) -> None:
        for entry in MANIFEST["fixtures"]:
            path = FIXTURE_DIR / entry["file"]
            with self.subTest(fixture=entry["file"]):
                self.assertEqual(pipeline.file_sha256(path), entry["sha256"])

    def test_verify_clean_roundtrip_receipt(self) -> None:
        spec = make_spec()
        export = cutspec.cutspec_to_otio_json(spec)
        receipt = cutspec.verify_export(spec, FIXTURE_DIR / "cfr_30.mp4", export, "otio")
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["mismatches"], 0)
        self.assertEqual(receipt["nleImport"], "NLE_IMPORT_UNVERIFIED")
        self.assertEqual(receipt["externalApiCalls"], 0)
        self.assertEqual(receipt["specHash"], spec.specHash)
        for seg in receipt["segments"]:
            self.assertEqual(seg["status"], "ok")
            self.assertLessEqual(abs(seg["deltaImportSec"]), receipt["toleranceSec"])

    def test_verify_csv_roundtrip_receipt(self) -> None:
        spec = make_spec()
        export = cutspec.cutspec_to_csv(spec)
        receipt = cutspec.verify_export(spec, FIXTURE_DIR / "cfr_30.mp4", export, "csv")
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["mismatches"], 0)

    def test_verify_stale_source_no_rebind(self) -> None:
        spec = make_spec("cfr_30.mp4")
        export = cutspec.cutspec_to_otio_json(spec)
        receipt = cutspec.verify_export(spec, FIXTURE_DIR / "cfr_25.mp4", export, "otio")
        self.assertEqual(receipt["status"], "STALE_SOURCE")
        self.assertEqual(receipt["specHash"], spec.specHash)
        self.assertEqual(spec.source.contentHash, receipt["expectedContentHash"])
        self.assertNotEqual(receipt["expectedContentHash"], receipt["observedContentHash"])

    def test_verify_missing_source(self) -> None:
        spec = make_spec()
        receipt = cutspec.verify_export(spec, FIXTURE_DIR / "does-not-exist.mp4", "x", "otio")
        self.assertEqual(receipt["status"], "MISSING_SOURCE")

    def test_verify_forged_spechash_rejected(self) -> None:
        spec = make_spec()
        forged = cutspec.CutSpec.from_json(spec.to_json())
        forged.segments[0].sourceRange = {"startSec": 0.1, "endSec": 1.1}
        self.assertNotEqual(forged.compute_spec_hash(), forged.specHash)
        receipt = cutspec.verify_export(
            forged, FIXTURE_DIR / "cfr_30.mp4", cutspec.cutspec_to_otio_json(spec), "otio"
        )
        self.assertEqual(receipt["status"], "SPEC_HASH_MISMATCH")
        self.assertTrue(receipt["stillNeedsHumanFix"])

    def test_verify_flags_imported_range_drift(self) -> None:
        spec = make_spec()
        export = cutspec.cutspec_to_otio_json(spec)
        tampered = json.loads(export)
        clip = tampered["tracks"]["children"][0]["children"][0]
        clip["source_range"]["start_time"]["value"] += 200
        receipt = cutspec.verify_export(
            spec, FIXTURE_DIR / "cfr_30.mp4", json.dumps(tampered), "otio"
        )
        self.assertGreater(receipt["mismatches"], 0)
        self.assertTrue(receipt["stillNeedsHumanFix"])

    def test_verify_does_not_modify_source(self) -> None:
        path = FIXTURE_DIR / "cfr_30.mp4"
        before = pipeline.file_sha256(path)
        spec = make_spec()
        cutspec.verify_export(spec, path, cutspec.cutspec_to_otio_json(spec), "otio")
        self.assertEqual(pipeline.file_sha256(path), before)


class SourceProbeTests(unittest.TestCase):
    @unittest.skipUnless(HAS_FFMPEG, "ffprobe required")
    def test_source_from_video_derives_probe_fields(self) -> None:
        source = fixture_source("cfr_2997.mp4")
        self.assertEqual(source.timebase, "30000/1001")
        self.assertAlmostEqual(source.fps, 30000 / 1001, places=6)
        self.assertFalse(source.vfr)
        self.assertAlmostEqual(source.durationSec, 4.004, places=3)
        self.assertTrue(source.contentHash)
        self.assertTrue(source.fingerprint)


if __name__ == "__main__":
    unittest.main()
