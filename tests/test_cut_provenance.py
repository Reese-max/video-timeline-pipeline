"""Resolver security contracts + actual media/parser/CLI canaries.

Canonical transcript/timeline JSON below are explicit synthetic frozen span
fixtures, not ASR/model output or a human workflow baseline.
"""
import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cut_provenance as provenance
import cutspec
import pipeline

FIXTURES = Path(__file__).parent / "fixtures" / "cutspec"
FROZEN = json.loads((FIXTURES / "manifest.json").read_text())
HAS_FFMPEG = shutil.which("ffprobe") is not None and shutil.which("ffmpeg") is not None
JOB_ID = "0123456789ab"
SELECTIONS = [{"artifact": "transcript", "index": 0, "selection": "manual"},
              {"artifact": "timeline", "index": 1, "selection": "evidence_reel"}]


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def canonical_job(root, video, *, mcp=False):
    """Frozen analysis doubles using native canonical artifact shapes."""
    job = root / "jobs" / JOB_ID
    job.mkdir(parents=True, exist_ok=True)
    transcript = {"provider": "synthetic-frozen-span-fixture", "language": "zh",
                  "segments": [{"start": 0.5, "end": 1.5, "text": '第一段, "證據"\n實際時間', "chunk": 0},
                               {"start": 2.0, "end": 3.0, "text": "第二段", "chunk": 0}]}
    timeline = pipeline.merge_timeline(transcript, [])
    sha = pipeline.file_sha256(video)
    if mcp:
        manifest = {"version": 2, "status": "ready", "job_id": JOB_ID,
                    "source": {"fingerprint": pipeline.file_fingerprint(video)}}
        result = {"status": "complete", "job_id": JOB_ID, "source_sha256": sha,
                  "source_fingerprint": manifest["source"]["fingerprint"],
                  "cache_signature": "b" * 64, "transcript": transcript, "timeline": timeline}
        write_json(job / "analysis/result.json", result)
    else:
        manifest = {"schema_version": 1, "status": "completed", "job_dir": f"jobs/{JOB_ID}",
                    "source": {"resolved_video": str(video.resolve())}, "source_sha256": sha,
                    "artifacts": {"transcript": f"jobs/{JOB_ID}/transcript.json",
                                  "timeline": f"jobs/{JOB_ID}/timeline.json"},
                    "fixture": "synthetic-frozen-span-doubles"}
        write_json(job / "transcript.json", transcript)
        write_json(job / "timeline.json", timeline)
    write_json(job / "manifest.json", manifest)
    return job


def hashes(paths):
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe unavailable")
class ProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = cutspec.source_from_video(FIXTURES / "cfr_30.mp4")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = FIXTURES / "cfr_30.mp4"
        self.job = canonical_job(self.root, self.video)
        # Resolver reads the real source SHA; only redundant probing is avoided
        # in shape/drift negatives. The matrix below uses actual probes and CLI.
        patch = mock.patch.object(cutspec, "source_from_video", return_value=copy.deepcopy(self.source))
        patch.start()
        self.addCleanup(patch.stop)
        self.spec, self.companion = provenance.build_evidence_cutspec(self.job, self.video, SELECTIONS)
        self.csv = cutspec.cutspec_to_csv(self.spec, evidence_manifest=self.companion, job_dir=self.job)

    def change(self, file, modify):
        path = self.job / file
        data = json.loads(path.read_text())
        modify(data)
        write_json(path, data)

    def rehash(self, companion):
        companion["manifestHash"] = provenance.manifest_hash(companion)
        return companion

    def assert_build_error(self, code, selections=SELECTIONS):
        with self.assertRaises(provenance.EvidenceError) as caught:
            provenance.build_evidence_cutspec(self.job, self.video, selections)
        self.assertEqual(caught.exception.code, code)

    def assert_receipt_error(self, code, companion=None):
        receipt = cutspec.verify_export(self.spec, self.video, self.csv, "csv",
                                        evidence_manifest=self.companion if companion is None else companion,
                                        job_dir=self.job)
        self.assertEqual(receipt["status"], code)
        self.assertTrue(receipt["stillNeedsHumanFix"])
        self.assertEqual(receipt["localSubprocessCalls"], 0)
        self.assertEqual(receipt["evidence"]["provenanceCoverage"], 0.0)
        return receipt

    def test_exact_artifact_span_job_and_revision_are_bound(self):
        again, manifest = provenance.build_evidence_cutspec(self.job, self.video, SELECTIONS)
        self.assertEqual(self.spec.specHash, again.specHash)
        self.assertEqual(self.companion, manifest)
        self.assertEqual([s.sourceRange for s in self.spec.segments],
                         [{"startSec": 0.5, "endSec": 1.5}, {"startSec": 2.0, "endSec": 3.0}])
        resolved = provenance.validate_evidence(self.spec, manifest, self.job)
        self.assertEqual(resolved["sourceJob"], JOB_ID)
        self.assertEqual(resolved["provenanceCoverage"], 1.0)
        self.assertEqual([e["pointer"] for e in manifest["entries"]], ["/segments/0", "/1"])
        self.assertIn(manifest["analysisRevision"], self.spec.segments[0].evidenceRef)
        self.assertIn(manifest["entries"][0]["spanHash"], self.spec.segments[0].evidenceRef)

    def test_missing_persistent_source_hash_fails_without_inventing_one(self):
        self.change("manifest.json", lambda d: d.pop("source_sha256"))
        self.assert_build_error("MISSING_SOURCE_HASH")

    def test_wrong_media_revision_build_fails(self):
        self.change("manifest.json", lambda d: d.update(source_sha256="a" * 64))
        self.assert_build_error("STALE_SOURCE")

    def test_stale_media_receipt_and_export_failclosed(self):
        self.change("manifest.json", lambda d: d.update(source_sha256="a" * 64))
        self.assert_receipt_error("STALE_SOURCE")
        with self.assertRaisesRegex(ValueError, "STALE_SOURCE"):
            cutspec.cutspec_to_otio_json(self.spec, evidence_manifest=self.companion, job_dir=self.job)
        self.assertEqual(cutspec.preview_cutspec(self.spec, evidence_manifest=self.companion, job_dir=self.job)["status"], "rejected")

    def test_modified_artifact_same_ranges_is_stale_analysis(self):
        self.change("transcript.json", lambda d: d["segments"][0].update(text="different analysis"))
        self.assert_receipt_error("STALE_ANALYSIS")

    def test_modified_unselected_artifact_is_stale_analysis(self):
        self.change("timeline.json", lambda d: d.append({"timestamp": 3.5, "type": "visual", "data": {}}))
        self.assert_receipt_error("STALE_ANALYSIS")

    def test_missing_artifact_fails_export_and_receipt(self):
        (self.job / "transcript.json").unlink()
        self.assert_receipt_error("MISSING_EVIDENCE")
        with self.assertRaisesRegex(ValueError, "MISSING_EVIDENCE"):
            cutspec.cutspec_to_csv(self.spec, evidence_manifest=self.companion, job_dir=self.job)

    def test_invalid_json_failsclosed(self):
        (self.job / "timeline.json").write_text('{"broken"')
        self.assert_receipt_error("MALFORMED_EVIDENCE")

    def test_duplicate_json_fields_rejected(self):
        (self.job / "transcript.json").write_text('{"segments":[],"segments":[]}')
        self.assert_build_error("MALFORMED_EVIDENCE")

    def test_actual_media_bytes_drift_after_selection_failsclosed(self):
        video = self.root / "copy.mp4"
        shutil.copyfile(self.video, video)
        source = copy.deepcopy(self.source)
        source.videoPath = str(video)
        with mock.patch.object(cutspec, "source_from_video", return_value=source):
            job = canonical_job(self.root, video)
            spec, companion = provenance.build_evidence_cutspec(job, video, SELECTIONS)
            export = cutspec.cutspec_to_csv(spec, evidence_manifest=companion, job_dir=job)
            with video.open("ab") as media:
                media.write(b"actual-byte-revision-drift")
            receipt = cutspec.verify_export(spec, video, export, "csv", evidence_manifest=companion, job_dir=job)
            self.assertEqual(receipt["status"], "STALE_SOURCE")
            self.assertEqual(receipt["localSubprocessCalls"], 0)
            self.assertEqual(receipt["evidence"]["provenanceCoverage"], 0.0)

    def test_nonfinite_json_rejected(self):
        (self.job / "timeline.json").write_text('[{"timestamp": NaN}]')
        self.assert_build_error("MALFORMED_EVIDENCE")

    def test_crossjob_declarations_rejected(self):
        self.change("manifest.json", lambda d: d["artifacts"].update(transcript="jobs/abcdefabcdef/transcript.json"))
        self.assert_build_error("MALFORMED_EVIDENCE")

    def test_crossjob_manifest_rejected(self):
        self.change("manifest.json", lambda d: d.update(job_dir="jobs/abcdefabcdef"))
        self.assert_build_error("INVALID_SOURCE_JOB")

    def test_native_manifest_paths_match_pipeline_output(self):
        self.change("manifest.json", lambda d: d.update(
            job_dir=str(Path("jobs") / JOB_ID),
            artifacts={name: str(Path("jobs") / JOB_ID / f"{name}.json") for name in ("transcript", "timeline")}))
        spec, companion = provenance.build_evidence_cutspec(self.job, self.video, SELECTIONS)
        self.assertEqual(provenance.validate_evidence(spec, companion, self.job)["provenanceCoverage"], 1.0)

    def test_native_path_compatibility_does_not_normalize_escapes(self):
        original = json.loads((self.job / "manifest.json").read_text())
        for value in (str(Path("jobs") / ".." / "jobs" / JOB_ID), str(self.root / "outside" / JOB_ID)):
            with self.subTest(value=value):
                write_json(self.job / "manifest.json", {**original, "job_dir": value})
                self.assert_build_error("INVALID_SOURCE_JOB")
        write_json(self.job / "manifest.json", original)
        for value in (str(Path("jobs") / JOB_ID / ".." / JOB_ID / "transcript.json"), str(self.root / "transcript.json")):
            with self.subTest(value=value):
                self.change("manifest.json", lambda d: d["artifacts"].update(transcript=value))
                self.assert_build_error("MALFORMED_EVIDENCE")

    def test_source_locator_must_match_actual_media(self):
        self.change("manifest.json", lambda d: d["source"].update(resolved_video=str(FIXTURES / "cfr_25.mp4")))
        self.assert_build_error("STALE_SOURCE")

    def test_symlink_artifact_escape_rejected(self):
        path = self.job / "transcript.json"
        outside = self.root / "outside.json"
        shutil.copyfile(path, outside)
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError as exc:
            if os.name == "nt" and exc.winerror == 1314:
                self.skipTest("Windows account lacks symlink creation privilege")
            raise
        self.assert_build_error("MALFORMED_EVIDENCE")

    def test_point_visual_and_summary_cannot_generate_range(self):
        for row in ({"timestamp": 1, "type": "visual", "data": {"visual_summary": "summary"}},
                    {"start": 1, "end": 2, "summary": "summary"}):
            write_json(self.job / "timeline.json", [row])
            self.assert_build_error("UNTIMED_EVIDENCE", [{"artifact": "timeline", "index": 0}])

    def test_caller_ranges_and_summary_fields_rejected(self):
        for extra in ({"sourceRange": {"startSec": 0, "endSec": 1}}, {"summary": "guess"}):
            self.assert_build_error("INVALID_EVIDENCE_LOCATOR", [{"artifact": "transcript", "index": 0, **extra}])

    def test_malformed_selection_metadata_rejected_before_export(self):
        for metadata in ({"confidence": "0.5"}, {"confidence": True}, {"confidence": float("nan")},
                         {"confidence": 1.1}, {"reason": []}, {"note": {}}, {"selection": "unknown"}):
            self.assert_build_error("INVALID_SELECTION", [{"artifact": "transcript", "index": 0, **metadata}])

    def test_nonexact_numeric_ranges_rejected(self):
        for start, end, code in [(True, 1.5, "UNTIMED_EVIDENCE"), ("0.5", 1.5, "UNTIMED_EVIDENCE"),
                                 (0.5, None, "UNTIMED_EVIDENCE"), (0.5, 0.5, "INVALID_EVIDENCE_RANGE"),
                                 (-0.5, 1.5, "INVALID_EVIDENCE_RANGE"), (0.5, 50, "INVALID_EVIDENCE_RANGE")]:
            with self.subTest(start=start, end=end):
                self.change("transcript.json", lambda d: d["segments"][0].update(start=start, end=end))
                self.assert_build_error(code, [SELECTIONS[0]])

    def test_missing_or_invalid_span_index_rejected(self):
        for value, code in [(50, "MISSING_EVIDENCE_SPAN"), (-1, "INVALID_EVIDENCE_LOCATOR"),
                             (True, "INVALID_EVIDENCE_LOCATOR"), ("0", "INVALID_EVIDENCE_LOCATOR")]:
            self.assert_build_error(code, [{"artifact": "transcript", "index": value}])
        self.assert_build_error("INVALID_EVIDENCE_LOCATOR", [{"artifact": "summary", "index": 0}])

    def test_companion_tamper_detected(self):
        companion = copy.deepcopy(self.companion)
        companion["entries"][0]["spanHash"] = "a" * 64
        self.assert_receipt_error("EVIDENCE_MANIFEST_HASH_MISMATCH", companion)

    def test_rehashed_forged_span_or_range_still_fails_resolution(self):
        for field, value in (("spanHash", "a" * 64), ("pointer", "/segments/1"),
                             ("sourceRange", {"startSec": 0.6, "endSec": 1.4})):
            companion = copy.deepcopy(self.companion)
            companion["entries"][0][field] = value
            self.assert_receipt_error("EVIDENCE_SPAN_MISMATCH", self.rehash(companion))

    def test_companion_bound_to_exact_spec_and_entry_order(self):
        companion = copy.deepcopy(self.companion)
        companion["specHash"] = "a" * 64
        self.assert_receipt_error("EVIDENCE_SPEC_MISMATCH", self.rehash(companion))
        companion = copy.deepcopy(self.companion)
        companion["entries"].reverse()
        self.assert_receipt_error("EVIDENCE_SPAN_MISMATCH", self.rehash(companion))

    def test_strict_spec_cannot_export_or_verify_without_companion(self):
        with self.assertRaisesRegex(ValueError, "MISSING_EVIDENCE_MANIFEST"):
            cutspec.cutspec_to_csv(self.spec)
        receipt = cutspec.verify_export(self.spec, self.video, self.csv, "csv")
        self.assertEqual(receipt["status"], "MISSING_EVIDENCE_MANIFEST")
        self.assertEqual(cutspec.preview_cutspec(self.spec)["status"], "rejected")

    def test_receipt_binds_exact_export_and_manifest_hashes(self):
        receipt = cutspec.verify_export(self.spec, self.video, self.csv, "csv",
                                       evidence_manifest=self.companion, job_dir=self.job)
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["exportHash"], hashlib.sha256(self.csv.encode()).hexdigest())
        self.assertEqual(receipt["evidence"]["manifestHash"], self.companion["manifestHash"])
        original = receipt.pop("receiptId")
        self.assertEqual(original, hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()[:16])

    def test_legacy_strings_are_explicitly_unresolved(self):
        legacy = copy.deepcopy(self.spec)
        for segment in legacy.segments:
            segment.evidenceRef = "opaque-category"
        legacy.update_hash()
        receipt = cutspec.verify_export(legacy, self.video, cutspec.cutspec_to_csv(legacy), "csv")
        self.assertEqual(receipt["status"], "verified")
        self.assertEqual(receipt["evidence"], {"status": "UNRESOLVED_EVIDENCE", "provenanceCoverage": 0.0})

    def test_mcp_embedded_artifacts_resolve_and_content_edit_is_stale(self):
        job = canonical_job(self.root, self.video, mcp=True)
        spec, companion = provenance.build_evidence_cutspec(job, self.video, SELECTIONS)
        self.assertEqual([e["pointer"] for e in companion["entries"]], ["/transcript/segments/0", "/timeline/1"])
        self.assertEqual(companion["nativeAnalysisRevision"], "b" * 64)
        self.change("analysis/result.json", lambda d: d["transcript"]["segments"][0].update(text="changed under same cache signature"))
        with self.assertRaises(provenance.EvidenceError) as caught:
            provenance.validate_evidence(spec, companion, job, self.video)
        self.assertEqual(caught.exception.code, "STALE_ANALYSIS")

    def test_mcp_source_job_revision_and_completion_failclosed(self):
        for changes, code in [({"job_id": "abcdefabcdef"}, "INVALID_ANALYSIS"),
                              ({"source_sha256": "a" * 64}, "STALE_SOURCE"),
                              ({"cache_signature": "missing"}, "INVALID_ANALYSIS"),
                              ({"source_fingerprint": "stale"}, "STALE_ANALYSIS"),
                              ({"status": "partial"}, "INVALID_ANALYSIS")]:
            canonical_job(self.root, self.video, mcp=True)
            self.change("analysis/result.json", lambda d: d.update(changes))
            self.assert_build_error(code)

    def test_preview_and_export_resolver_make_no_external_calls_or_writes(self):
        paths = [self.video, *self.job.glob("*.json")]
        before = hashes(paths)
        with mock.patch("pipeline.run_external", side_effect=AssertionError("external process")), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            preview = cutspec.preview_cutspec(self.spec, evidence_manifest=self.companion, job_dir=self.job)
            cutspec.cutspec_to_csv(self.spec, evidence_manifest=self.companion, job_dir=self.job)
            cutspec.cutspec_to_otio_json(self.spec, evidence_manifest=self.companion, job_dir=self.job)
        self.assertEqual(preview["evidence"]["provenanceCoverage"], 1.0)
        self.assertEqual(before, hashes(paths))

    def test_cli_missing_or_malformed_companion_emits_failed_receipt(self):
        spec_path, export_path, companion_path = self.root / "spec.json", self.root / "cut.csv", self.root / "evidence.json"
        spec_path.write_text(self.spec.to_json())
        export_path.write_text(self.csv)
        for contents in (None, "{bad"):
            if contents is not None:
                companion_path.write_text(contents)
            receipt_path = self.root / "failed-receipt.json"
            self.assertEqual(cutspec.main(["verify", "--spec", str(spec_path), "--video", str(self.video),
                                          "--export-file", str(export_path), "--format", "csv", "--receipt", str(receipt_path),
                                          "--job-dir", str(self.job), "--evidence-manifest", str(companion_path)]), 2)
            receipt = json.loads(receipt_path.read_text())
            self.assertEqual(receipt["status"], "INVALID_EVIDENCE_MANIFEST")
            self.assertTrue(receipt["stillNeedsHumanFix"])
            self.assertEqual(receipt["localSubprocessCalls"], 0)

    def test_output_guard_protects_media_and_canonical_artifacts(self):
        for target in (self.video, self.job / "manifest.json", self.job / "transcript.json",
                       self.job / "timeline.json", self.job / "analysis/result.json"):
            with self.assertRaisesRegex(ValueError, "must not replace"):
                cutspec._check_evidence_outputs([target.resolve()], self.job, self.video)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe unavailable")
class PipelineSourceBindingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.video = self.root / "local.mp4"
        shutil.copyfile(FIXTURES / "cfr_30.mp4", self.video)
        with mock.patch.object(sys, "argv", ["pipeline.py", "--video", str(self.video),
                                             "--root", str(self.root), "--local-only",
                                             "--max-vision-frames", "2"]):
            self.args = pipeline.parse_args()
        patch = mock.patch.object(pipeline, "load_dotenv")
        patch.start()
        self.addCleanup(patch.stop)
        # Current main binds a provider ledger root per pipeline run. Restore
        # that process-global context so this fixture cannot affect later tests.
        import cost_tracker
        scope = cost_tracker.cost_root_scope(None)
        scope.__enter__()
        self.addCleanup(scope.__exit__, None, None, None)

    def test_actual_local_pipeline_persists_source_bytes_before_evidence(self):
        expected = pipeline.file_sha256(self.video)
        with mock.patch("pipeline.request_json", side_effect=AssertionError("provider call")), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            path = pipeline.run_pipeline(self.args)
        manifest = json.loads(path.read_text())
        self.assertEqual(manifest["status"], "local-only")
        self.assertEqual(manifest["source_sha256"], expected)
        self.assertEqual(pipeline.file_sha256(self.video), expected)
        self.assertTrue((path.parent / "timeline.json").is_file())
        # Actual local-only workflow has sampled visual points, not speech spans.
        with self.assertRaises(provenance.EvidenceError) as caught:
            provenance.build_evidence_cutspec(path.parent, self.video, [{"artifact": "timeline", "index": 0}])
        self.assertEqual(caught.exception.code, "UNTIMED_EVIDENCE")

    def test_mid_analysis_source_change_cannot_publish_completed_evidence(self):
        write = pipeline.write_json
        def drift_at_publish(path, data):
            if Path(path).name == "source.json":
                with self.video.open("ab") as media:
                    media.write(b"injected-source-revision-change")
            return write(path, data)
        with mock.patch.object(pipeline, "write_json", side_effect=drift_at_publish), \
             mock.patch("pipeline.request_json", side_effect=AssertionError("provider call")):
            with self.assertRaisesRegex(RuntimeError, "STALE_SOURCE"):
                pipeline.run_pipeline(self.args)
        job = self.root / "jobs" / pipeline.job_key(str(self.video.resolve()))
        manifest = json.loads((job / "manifest.json").read_text())
        self.assertEqual(manifest["status"], "failed")
        self.assertNotEqual(manifest["source_sha256"], pipeline.file_sha256(self.video))

    def test_cli_keeps_transcript_when_video_cannot_reach_minimax(self):
        self.args.local_only = False
        transcript = {"provider": "groq", "language": "zh",
                      "segments": [{"start": 0.0, "end": 0.5, "text": "保留的逐字稿"}]}
        unavailable = pipeline.VideoMp4UnavailableError("video_mp4 無法交給 MiniMax 直接讀取（抽幀備援已移除）：fixture")
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "offline-test-placeholder", "MINIMAX_SUBSCRIPTION_KEY": "offline-test-placeholder"}), \
             mock.patch.object(pipeline, "minimax_endpoint", return_value="offline"), \
             mock.patch.object(pipeline, "minimax_model", return_value="offline"), \
             mock.patch.object(pipeline, "transcribe_all", return_value=(transcript, {"provider": "groq"})), \
             mock.patch.object(pipeline, "analyze_video_mp4", side_effect=unavailable) as vision, \
             mock.patch.object(pipeline, "create_summary", return_value={"status": "skipped", "reason": "test"}), \
             mock.patch.object(pipeline, "request_json", side_effect=AssertionError("provider call")), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            path = pipeline.run_pipeline(self.args)
        vision.assert_called_once()
        job = path.parent
        saved = json.loads((job / "transcript.json").read_text(encoding="utf-8"))
        self.assertEqual([segment["text"] for segment in saved["segments"]], ["保留的逐字稿"])
        visual = json.loads((job / "visual.json").read_text(encoding="utf-8"))
        self.assertEqual(visual[0]["completion_status"], "incomplete")
        self.assertEqual(visual[0]["finish_reason"], "video_unavailable")

    def test_legacy_or_stale_cloud_cache_refused_on_consecutive_retries_without_canonical_writes(self):
        self.args.local_only = False
        job = self.root / "jobs" / pipeline.job_key(str(self.video.resolve()))
        for previous in ({"status": "completed"}, {"status": "completed", "source_sha256": "a" * 64}):
            write_json(job / "manifest.json", previous)
            write_json(job / "transcript.json", {"segments": [{"start": 0.5, "end": 1.5, "text": "saved canonical evidence"}]})
            write_json(job / "timeline.json", [{"timestamp": 0.5, "end": 1.5, "type": "speech", "text": "saved canonical evidence"}])
            write_json(job / "source.json", {"source_sha256": previous.get("source_sha256")})
            protected = [self.video, *job.glob("*.json")]
            before = hashes(protected)
            with mock.patch.dict(os.environ, {"GROQ_API_KEY": "offline-test-placeholder", "MINIMAX_SUBSCRIPTION_KEY": "offline-test-placeholder"}), \
                 mock.patch.object(pipeline, "minimax_endpoint", return_value="offline"), \
                 mock.patch.object(pipeline, "minimax_model", return_value="offline"), \
                 mock.patch.object(pipeline, "transcribe_all", side_effect=AssertionError("paid ASR")) as asr, \
                 mock.patch.object(pipeline, "analyze_video_mp4", side_effect=AssertionError("paid vision")) as vision, \
                 mock.patch.object(pipeline, "create_summary", side_effect=AssertionError("paid summary")) as summary, \
                 mock.patch.object(pipeline, "request_json", side_effect=AssertionError("provider call")) as transport, \
                 mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")) as network:
                for attempt in range(2):
                    with self.subTest(previous=previous, attempt=attempt):
                        with self.assertRaisesRegex(RuntimeError, "STALE_SOURCE.*--refresh"):
                            pipeline.run_pipeline(self.args)
                        self.assertEqual(hashes(protected), before, "refusal must preserve exact canonical bytes for the next retry")
                for provider in (asr, vision, summary, transport, network):
                    provider.assert_not_called()


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg/ffprobe unavailable")
class ActualFixtureCliMatrix(unittest.TestCase):
    def command(self, *args, expected=0):
        result = subprocess.run([sys.executable, str(Path(cutspec.__file__)), *map(str, args)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stderr + result.stdout)
        return result

    def test_six_frozen_media_both_formats_actual_cli_parser_and_ffprobe(self):
        self.assertIsNotNone(cutspec._otio_module(), "official OTIO dependency is required for the acceptance matrix")
        for frozen in FROZEN["fixtures"]:
            with self.subTest(fixture=frozen["file"]), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                video = FIXTURES / frozen["file"]
                job = canonical_job(root, video)
                protected = [video, *job.glob("*.json")]
                before = hashes(protected)
                self.assertEqual(pipeline.file_sha256(video), frozen["sha256"])
                write_json(root / "selections.json", SELECTIONS)
                spec_path, manifest_path = root / "spec.json", root / "evidence.json"
                self.command("build-evidence", "--video", video, "--job-dir", job,
                             "--selections", root / "selections.json", "--out", spec_path,
                             "--evidence-manifest", manifest_path)
                strict = ["--job-dir", job, "--evidence-manifest", manifest_path]
                preview = json.loads(self.command("preview", "--spec", spec_path, *strict).stdout)
                self.assertEqual(preview["evidence"]["provenanceCoverage"], 1.0)
                for fmt in ("csv", "otio"):
                    export_path = root / f"export.{fmt}"
                    receipt_path = root / f"receipt-{fmt}.json"
                    self.command("export", "--spec", spec_path, "--format", fmt, "--out", export_path, *strict)
                    self.command("verify", "--spec", spec_path, "--video", video,
                                 "--export-file", export_path, "--format", fmt, "--receipt", receipt_path, *strict)
                    receipt = json.loads(receipt_path.read_text())
                    self.assertEqual(receipt["status"], "verified")
                    self.assertEqual(receipt["mismatches"], 0)
                    self.assertEqual(receipt["nleImport"], "NLE_IMPORT_UNVERIFIED")
                    self.assertEqual(receipt["externalApiCalls"], 0)
                    self.assertEqual(receipt["localSubprocessCalls"], 3)
                    self.assertEqual(receipt["evidence"]["provenanceCoverage"], 1.0)
                    self.assertEqual(receipt["expectedContentHash"], frozen["sha256"])
                    self.assertEqual(receipt["exportHash"], hashlib.sha256(export_path.read_bytes()).hexdigest())
                    self.assertTrue(all(row["ffprobe"]["frames"] > 0 for row in receipt["segments"]))
                    if fmt == "otio":
                        self.assertEqual(receipt["parser"], "opentimelineio-0.18.1")
                    print("PROVENANCE_CANARY " + json.dumps({"fixture": frozen["file"], "format": fmt,
                           "analysisFixture": "synthetic-frozen-span-doubles", "receipt": receipt}, sort_keys=True))
                self.command("export", "--spec", spec_path, "--format", "csv", expected=2)
                (job / "transcript.json").unlink()
                self.command("verify", "--spec", spec_path, "--video", video,
                             "--export-file", root / "export.csv", "--format", "csv", *strict, expected=2)
                self.assertEqual({k: v for k, v in before.items() if not k.endswith("transcript.json")},
                                 hashes([p for p in protected if p.name != "transcript.json"]))


if __name__ == "__main__":
    unittest.main()
