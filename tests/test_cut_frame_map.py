"""Actual decoded frame boundaries plus refusal and interchange contracts.

Canonical transcript/timeline inputs are explicitly frozen synthetic doubles;
the frame PTS/timebase/durations below are decoded from unchanged real media.
"""
import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

import cut_frame_map as frames
import cutspec
import pipeline
from test_cut_provenance import FIXTURES, FROZEN, SELECTIONS, canonical_job, hashes, write_json


def manual_spec(video, ranges=((0.5, 1.5), (2.0, 3.0))):
    return cutspec.build_cutspec(cutspec.source_from_video(video), [
        cutspec.CutSegment(f"seg-{i}", {"startSec": start, "endSec": end}, {},
                           "manual-frozen-test", "manual", "measured boundary test", 1.0)
        for i, (start, end) in enumerate(ranges)])


@unittest.skipUnless(shutil.which("ffprobe"), "ffprobe unavailable")
class FrameMapTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.video = FIXTURES / "cfr_30.mp4"
        cls.spec = manual_spec(cls.video)
        cls.manifest = frames.build_frame_map(cls.spec, cls.video)
        cls.otio = cutspec.cutspec_to_otio_json(cls.spec, frame_map=cls.manifest)
        cls.csv = cutspec.cutspec_to_csv(cls.spec, frame_map=cls.manifest)

    def changed(self, modify):
        result = copy.deepcopy(self.manifest)
        modify(result)
        result["manifestHash"] = frames.manifest_hash(result)
        return result

    def test_source_duration_includes_positive_presentation_origin(self):
        stream = {"codec_type": "video", "avg_frame_rate": "30/1", "r_frame_rate": "30/1",
                  "time_base": "1/100", "start_pts": 25, "duration_ts": 100}
        probe = {"format": {"duration": "1"}, "streams": [stream]}
        self.assertEqual(cutspec.source_from_video(self.video, probe).durationSec, 1.25)
        for invalid in ({"start_pts": -25}, {"start_pts": True}, {"duration_ts": None},
                        {"duration_ts": -1}, {"time_base": "0/1"}, {"time_base": "bad"}):
            with self.subTest(invalid=invalid):
                probe["streams"] = [{**stream, **invalid}]
                self.assertEqual(cutspec.source_from_video(self.video, probe).durationSec, 1.0)

    def receipt(self, *, manifest=None, text=None, fmt="otio", tolerance=0.15):
        return cutspec.verify_export(self.spec, self.video, text or self.otio, fmt,
                                     tolerance_sec=tolerance, frame_map=self.manifest if manifest is None else manifest)

    def test_measured_vfr_pts_and_terminal_duration_do_not_use_nominal_fps(self):
        video = FIXTURES / "vfr_30.mp4"
        spec = manual_spec(video)
        mapping = frames.build_frame_map(spec, video)
        raw = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=time_base:frame=pts,pkt_duration,duration", "-of", "json", str(video)], text=True))
        last = raw["frames"][-1]
        self.assertEqual(mapping["timeBase"], "1/15360")
        self.assertEqual(mapping["timeBase"], raw["streams"][0]["time_base"])
        self.assertEqual(mapping["boundaryPts"][0], 507)
        # This fixture has a 33 ms empty edit. FFmpeg 7 clipped its final
        # duration to 5 ticks; newer demuxers report the complete 512 ticks.
        self.assertEqual(mapping["boundaryPts"][-1], last["pts"] + last.get("duration", last.get("pkt_duration")))
        self.assertEqual(mapping["decodedFrames"], 96)
        self.assertEqual(len(raw["frames"]), 96)
        self.assertGreater(len({b - a for a, b in zip(mapping["boundaryPts"], mapping["boundaryPts"][1:-1])}), 1)
        selected = mapping["entries"][0]["startBoundary"]
        self.assertNotEqual(float(Fraction(selected["pts"]) * Fraction(mapping["timeBase"])),
                            cutspec.frames_to_seconds(selected["frameIndex"], spec.source.timebase))
        self.assertEqual(spec.segments[0].sourceRange, {"startSec": 0.5, "endSec": 1.5})

    def test_nearest_measured_boundary_ties_later_and_end_is_exclusive(self):
        mapping = {"timeBase": "1/10", "boundaryPts": [0, 10, 20]}
        self.assertEqual(frames.boundary_index(mapping, 0.49), 0)
        self.assertEqual(frames.boundary_index(mapping, 0.5), 1)
        self.assertEqual(frames.boundary_index(mapping, 0.51), 1)
        self.assertEqual(frames.boundary_index(mapping, 1.5), 2)
        with self.assertRaisesRegex(frames.FrameMapError, "outside"):
            frames.boundary_index(mapping, 2.01)

    def test_actual_vfr_final_frame_uses_measured_terminal_duration(self):
        video = FIXTURES / "vfr_30.mp4"
        actual = frames.decoded_map(video)
        tick = Fraction(actual["timeBase"])
        start = math.nextafter(float(actual["boundaryPts"][-2] * tick), math.inf)
        end = math.nextafter(float(actual["boundaryPts"][-1] * tick), -math.inf)
        spec = manual_spec(video, [(start, end)])
        manifest = frames.build_frame_map(spec, video)
        text = cutspec.cutspec_to_otio_json(spec, frame_map=manifest)
        receipt = cutspec.verify_export(spec, video, text, "otio", frame_map=manifest)
        self.assertEqual(receipt["status"], "verified", receipt)
        self.assertEqual(receipt["segments"][0]["decodedFrames"], 1)
        raw = json.loads(subprocess.check_output([
            "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
            "frame=pts,pkt_duration,duration", "-of", "json", str(video)], text=True))
        last = raw["frames"][-1]
        entry = manifest["entries"][0]
        self.assertEqual(entry["startBoundary"]["pts"], last["pts"])
        self.assertEqual(entry["endBoundaryExclusive"]["pts"], last["pts"] + last.get("duration", last.get("pkt_duration")))

    def test_companion_hash_is_self_excluding_and_binds_exact_spec(self):
        manifest = copy.deepcopy(self.manifest)
        self.assertEqual(manifest["manifestHash"], frames.manifest_hash(manifest))
        manifest["manifestHash"] = "altered"
        self.assertEqual(self.manifest["manifestHash"], frames.manifest_hash(manifest))
        manifest["specHash"] = "a" * 64
        manifest["manifestHash"] = frames.manifest_hash(manifest)
        self.assertEqual(self.receipt(manifest=manifest)["status"], "FRAME_MAP_SPEC_MISMATCH")

    def test_changed_companion_without_rehash_is_rejected_before_probe(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["boundaryPts"][2] += 1
        with mock.patch.object(pipeline, "run_external") as external:
            receipt = self.receipt(manifest=manifest)
        self.assertEqual(receipt["status"], "FRAME_MAP_HASH_MISMATCH")
        self.assertEqual(receipt["localSubprocessCalls"], 0)
        external.assert_not_called()

    def test_rehashed_forged_pts_entries_policy_or_count_rejected_against_actual_media(self):
        for modify in [lambda m: m["boundaryPts"].__setitem__(2, m["boundaryPts"][2] + 1),
                       lambda m: m["entries"][0]["startBoundary"].__setitem__("frameIndex", 99),
                       lambda m: m.__setitem__("normalization", "nominal-fps"),
                       lambda m: m.__setitem__("decodedFrames", 99),
                       lambda m: m["entries"].reverse()]:
            with self.subTest(modify=modify):
                self.assertEqual(self.receipt(manifest=self.changed(modify))["status"], "FRAME_MAP_SOURCE_MISMATCH")

    def test_stale_actual_source_refused_before_decode(self):
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "source.mp4"
            video.write_bytes(self.video.read_bytes() + b"changed-content")
            with mock.patch.object(pipeline, "run_external") as external:
                receipt = cutspec.verify_export(self.spec, video, self.otio, "otio", frame_map=self.manifest)
            self.assertEqual(receipt["status"], "STALE_SOURCE")
            self.assertEqual(receipt["localSubprocessCalls"], 0)
            external.assert_not_called()

    def test_media_drift_during_full_decode_cannot_publish_companion(self):
        raw = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                       "stream=index,time_base:frame=pts,pkt_duration,duration", "-of", "json", str(self.video)], text=True)
        with tempfile.TemporaryDirectory() as temp:
            video = Path(temp) / "source.mp4"
            shutil.copyfile(self.video, video)
            spec = copy.deepcopy(self.spec)
            spec.source.videoPath = str(video)
            def decode(*args, **kwargs):
                video.write_bytes(video.read_bytes() + b"mid-decode-drift")
                return raw
            with mock.patch.object(pipeline, "run_external", side_effect=decode):
                with self.assertRaisesRegex(frames.FrameMapError, "changed while decoding"):
                    frames.build_frame_map(spec, video)

    def test_missing_nonmonotone_negative_gap_overlap_or_unmeasured_duration_failclosed(self):
        good = {"streams": [{"index": 0, "time_base": "1/100"}],
                "frames": [{"pts": 0, "duration": 10}, {"pts": 10, "duration": 10}]}
        bad = []
        for modify in [lambda d: d["frames"][0].pop("pts"), lambda d: d["frames"][-1].pop("duration"),
                       lambda d: d["frames"][0].__setitem__("pts", -1), lambda d: d["frames"][1].__setitem__("pts", 0),
                       lambda d: d["frames"][0].__setitem__("duration", 9), lambda d: d["frames"][0].__setitem__("duration", 11),
                       lambda d: d["frames"][0].__setitem__("pts", True), lambda d: d["streams"][0].__setitem__("time_base", "0/1"),
                       lambda d: d["streams"][0].__setitem__("time_base", "1001/30000"),
                       lambda d: d.__setitem__("frames", [])]:
            data = copy.deepcopy(good)
            modify(data)
            bad.append(data)
        for data in bad:
            with self.subTest(data=data), mock.patch.object(pipeline, "run_external", return_value=json.dumps(data)):
                with self.assertRaises(frames.FrameMapError):
                    frames.decoded_map(self.video)

    def test_outside_pts_domain_or_collapsed_normalization_refused_without_clamp(self):
        for start, end, code in [(0.501, 0.502, "EMPTY_NORMALIZED_FRAME_RANGE")]:
            spec = manual_spec(self.video, [(start, end)])
            with self.assertRaises(frames.FrameMapError) as error:
                frames.build_frame_map(spec, self.video)
            self.assertEqual(error.exception.code, code)
        video = FIXTURES / "vfr_30.mp4"
        spec = manual_spec(video, [(0.0, 1.0)])
        with self.assertRaises(frames.FrameMapError) as error:
            frames.build_frame_map(spec, video)
        self.assertEqual(error.exception.code, "FRAME_RANGE_OUTSIDE_DECODED_SOURCE")

    def test_one_otio_tick_drift_fails_even_with_large_seconds_tolerance(self):
        data = json.loads(self.otio)
        data["tracks"]["children"][0]["children"][0]["source_range"]["start_time"]["value"] += 1
        receipt = self.receipt(text=json.dumps(data), tolerance=100.0)
        self.assertEqual(receipt["status"], "FRAME_EXPORT_BOUNDARY_MISMATCH")

    def test_removed_or_changed_otio_binding_refused(self):
        for marker in [None, {"schema": frames.SCHEMA, "manifestHash": "a" * 64}]:
            data = json.loads(self.otio)
            if marker is None:
                data["metadata"]["cutspec"].pop("frameMap")
            else:
                data["metadata"]["cutspec"]["frameMap"] = marker
            self.assertEqual(self.receipt(text=json.dumps(data))["status"], "FRAME_EXPORT_BINDING_MISMATCH")

    def test_otio_available_range_must_match_actual_measured_stream(self):
        data = json.loads(self.otio)
        data["tracks"]["children"][0]["children"][0]["media_references"]["DEFAULT_MEDIA"]["available_range"]["duration"]["value"] -= 1
        self.assertEqual(self.receipt(text=json.dumps(data))["status"], "FRAME_EXPORT_BOUNDARY_MISMATCH")

    def test_frame_otio_without_companion_rejected_before_processes(self):
        with mock.patch.object(pipeline, "run_external") as external:
            receipt = cutspec.verify_export(self.spec, self.video, self.otio, "otio")
        self.assertEqual(receipt["status"], "MISSING_FRAME_MAP")
        external.assert_not_called()

    def test_csv_companion_receipt_binds_frames_export_and_immutable_requested_ranges(self):
        receipt = self.receipt(fmt="csv", text=self.csv)
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["frameMapping"]["manifestHash"], self.manifest["manifestHash"])
        self.assertEqual(receipt["exportHash"], hashlib.sha256(self.csv.encode()).hexdigest())
        self.assertEqual(cutspec.cutspec_from_csv(self.csv).canonical_payload(), self.spec.canonical_payload())
        self.assertEqual(receipt["evidence"]["provenanceCoverage"], 0.0)
        self.assertEqual(receipt["nleImport"], "NLE_IMPORT_UNVERIFIED")

    def test_legacy_exports_explicitly_have_unresolved_frame_boundaries(self):
        text = cutspec.cutspec_to_csv(self.spec)
        receipt = cutspec.verify_export(self.spec, self.video, text, "csv")
        self.assertEqual(receipt["frameMapping"], {"status": "UNRESOLVED_FRAME_BOUNDARIES"})

    def test_preview_export_refuse_invalid_frame_map(self):
        manifest = self.changed(lambda m: m.__setitem__("decodedFrames", 1))
        self.assertEqual(cutspec.preview_cutspec(self.spec, frame_map=manifest)["status"], "rejected")
        for exporter in (cutspec.cutspec_to_csv, cutspec.cutspec_to_otio_json):
            with self.assertRaisesRegex(ValueError, "FRAME_MAP_SOURCE_MISMATCH"):
                exporter(self.spec, frame_map=manifest)

    def test_cli_malformed_missing_or_duplicate_map_emits_failed_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec, export, companion, receipt = (root / name for name in ("spec.json", "export.otio", "frames.json", "receipt.json"))
            spec.write_text(self.spec.to_json())
            export.write_text(self.otio)
            for content in [None, "not JSON", '{"schema":"cut-frame-map/1","schema":"cut-frame-map/1"}', '{"x":NaN}']:
                if content is not None:
                    companion.write_text(content)
                self.assertEqual(cutspec.main(["verify", "--spec", str(spec), "--video", str(self.video),
                                              "--export-file", str(export), "--format", "otio", "--frame-map", str(companion),
                                              "--receipt", str(receipt)]), 2)
                self.assertEqual(json.loads(receipt.read_text())["status"], "INVALID_FRAME_MAP")

    def test_cli_frame_outputs_cannot_replace_inputs_or_canonical_job(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec = root / "spec.json"
            spec.write_text(self.spec.to_json())
            before = hashes([self.video, spec])
            for output in (self.video, spec):
                self.assertEqual(cutspec.main(["map-frames", "--spec", str(spec), "--video", str(self.video), "--out", str(output)]), 2)
            self.assertEqual(before, hashes([self.video, spec]))

    def test_cli_copy_locator_cannot_overwrite_original_spec_media_or_via_temporary_symlink(self):
        for command, temporary in (("map-frames", False), ("map-frames", True), ("verify", False), ("verify", True)):
            with self.subTest(command=command, temporary=temporary), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                original, video = root / "original.mp4", root / "copy.mp4"
                shutil.copyfile(self.video, original)
                shutil.copyfile(self.video, video)
                spec, companion, export = root / "spec.json", root / "frames.json", root / "cut.otio"
                source_spec = copy.deepcopy(self.spec)
                source_spec.source.videoPath = str(original)
                spec.write_text(source_spec.to_json())
                write_json(companion, self.manifest)
                data = json.loads(self.otio)
                data["metadata"]["cutspec"]["cutSpec"]["source"]["videoPath"] = str(original)
                for clip in data["tracks"]["children"][0]["children"]:
                    clip["media_references"]["DEFAULT_MEDIA"]["target_url"] = str(original)
                export.write_text(json.dumps(data))
                output = original
                if temporary:
                    output = root / "output.json"
                    output.with_name(output.name + ".tmp").symlink_to(original)
                protected = [original, video, spec, companion, export]
                before = hashes(protected)
                if command == "map-frames":
                    args = [command, "--spec", spec, "--video", video, "--out", output]
                else:
                    args = [command, "--spec", spec, "--video", video, "--frame-map", companion,
                            "--export-file", export, "--format", "otio", "--receipt", output]
                self.assertEqual(cutspec.main(list(map(str, args))), 2)
                self.assertEqual(before, hashes(protected))

    def test_actual_lf_and_crlf_csv_cli_receipts_bind_distinct_raw_utf8_bytes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            spec, companion = root / "spec.json", root / "frames.json"
            spec.write_bytes(self.spec.to_json().encode("utf-8"))
            companion.write_bytes(json.dumps(self.manifest).encode("utf-8"))
            receipts = []
            for ending in ("lf", "crlf"):
                export, output = root / f"{ending}.csv", root / f"receipt-{ending}.json"
                raw = self.csv.encode("utf-8")
                if ending == "crlf":
                    raw = raw.replace(b"\n", b"\r\n")
                export.write_bytes(raw)
                self.assertEqual(cutspec.main(["verify", "--spec", str(spec), "--video", str(self.video),
                                              "--frame-map", str(companion), "--export-file", str(export),
                                              "--format", "csv", "--receipt", str(output)]), 0)
                receipt = json.loads(output.read_bytes())
                self.assertEqual(receipt["status"], "verified")
                self.assertEqual(receipt["exportHash"], hashlib.sha256(export.read_bytes()).hexdigest())
                self.assertEqual(export.read_bytes(), raw)
                receipts.append(receipt)
            self.assertNotEqual(receipts[0]["exportHash"], receipts[1]["exportHash"])
            self.assertNotEqual(receipts[0]["receiptId"], receipts[1]["receiptId"])

    def test_atomic_utf8_writes_preserve_exact_lf_crlf_bytes_on_windows_translation_model(self):
        text = '證據, "UTF-8"\nline two\r\nline three\n'
        def windows_text_write(path, value, *, encoding=None, newline=None, **kwargs):
            # Model the documented text-mode Windows newline conversion on
            # this Linux host; this is not a claim of a Windows runtime test.
            translated = value.replace("\n", "\r\n") if newline is None else value
            return path.write_bytes(translated.encode(encoding or "utf-8"))
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "artifact.csv"
            with mock.patch.object(Path, "write_text", autospec=True, side_effect=windows_text_write):
                cutspec._write_atomic(output, text)
            self.assertEqual(output.read_bytes(), text.encode("utf-8"))

    def test_actual_six_media_both_formats_cli_official_parser_and_decoded_pts(self):
        self.assertIsNotNone(cutspec._otio_module(), "official OTIO parser required")
        self.assertEqual(cutspec._otio_module().__version__, "0.18.1")
        for frozen in FROZEN["fixtures"]:
            with self.subTest(fixture=frozen["file"]), tempfile.TemporaryDirectory() as temp:
                root, video = Path(temp), FIXTURES / frozen["file"]
                job = canonical_job(root, video)
                protected = [video, *[p for p in job.rglob("*") if p.is_file()]]
                before = hashes(protected)
                self.assertEqual(pipeline.file_sha256(video), frozen["sha256"])
                selections, spec, evidence, companion = (root / name for name in ("selections.json", "spec.json", "evidence.json", "frames.json"))
                write_json(selections, SELECTIONS)
                def command(*args):
                    result = subprocess.run([sys.executable, str(Path(cutspec.__file__)), *map(str, args)],
                                            capture_output=True, text=True, env=os.environ.copy())
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return result.stdout
                command("build-evidence", "--video", video, "--job-dir", job, "--selections", selections,
                        "--out", spec, "--evidence-manifest", evidence)
                strict = ["--job-dir", job, "--evidence-manifest", evidence]
                command("map-frames", "--spec", spec, "--video", video, "--out", companion, *strict)
                measured = json.loads(companion.read_text())
                actual = frames.decoded_map(video)
                self.assertEqual(measured["boundaryPts"], actual["boundaryPts"])
                self.assertEqual(measured["timeBase"], actual["timeBase"])
                preview = json.loads(command("preview", "--spec", spec, "--frame-map", companion, *strict))
                self.assertEqual(preview["frameMapping"]["status"], "resolved")
                for fmt in ("csv", "otio"):
                    output, receipt_path = root / f"cut.{fmt}", root / f"receipt-{fmt}.json"
                    command("export", "--spec", spec, "--format", fmt, "--out", output, "--frame-map", companion, *strict)
                    command("verify", "--spec", spec, "--video", video, "--export-file", output, "--format", fmt,
                            "--receipt", receipt_path, "--frame-map", companion, *strict)
                    receipt = json.loads(receipt_path.read_text())
                    self.assertEqual(receipt["status"], "verified", receipt)
                    self.assertEqual(receipt["evidence"]["provenanceCoverage"], 1.0)
                    self.assertEqual(receipt["frameMapping"]["manifestHash"], measured["manifestHash"])
                    self.assertEqual(receipt["externalApiCalls"], 0)
                    self.assertEqual(receipt["localSubprocessCalls"], 2)
                    self.assertTrue(all(x["decodedFrames"] > 0 for x in receipt["segments"]))
                    self.assertEqual(receipt["nleImport"], "NLE_IMPORT_UNVERIFIED")
                    print("FRAME_MAP_CANARY " + json.dumps({"fixture": frozen["file"], "format": fmt,
                          "frameMap": measured, "receipt": receipt, "analysisFixture": "synthetic-frozen-span-doubles"}, sort_keys=True))
                self.assertEqual(before, hashes(protected))


if __name__ == "__main__":
    unittest.main()
