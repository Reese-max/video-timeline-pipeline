"""Issue #77: degraded MCP analysis must not be stored as a complete cache.

Two failure shapes were stored with ``status=complete`` so the second
``analyze_prepared_sync`` call returned the stale cache without calling the
model again:

1. timestamp-coverage failure: MiniMax returns only early timestamps, the
   pipeline emits one ``completion_status=incomplete`` /
   ``finish_reason=timestamp_coverage`` observation, yet ``visual_status``
   was set to ``complete`` because the visual stage returned a value.
2. transcript backfill failure: Groq raises (e.g. 503), ``gap_check``
   keeps ``remaining`` gaps plus ``backfill_errors``, yet the overall
   status stayed ``complete``.

Each test calls ``analyze_prepared_sync`` twice against a real on-disk
``analysis/result.json`` (no ``write_json``/``reuse_artifact`` mocks) and
requires the second call to reach the model again.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import mcp_server
import pipeline


JOB_ID = "0123456789ab"
DURATION = 166.6

INCOMPLETE_OBSERVATION = {
    "timestamp": 0.0,
    "screen_type": "影片",
    "visible_text": [],
    "visual_summary": pipeline.VISUAL_NOT_ANALYZED,
    "important_details": [],
    "confidence": None,
    "completion_status": "incomplete",
    "finish_reason": "timestamp_coverage",
    "max_observed_timestamp": 6.9,
    "duration_seconds": DURATION,
    "analysis_method": "minimax_video",
}

GOOD_OBSERVATION = {
    "timestamp": 0.0,
    "screen_type": "影片",
    "visible_text": [],
    "visual_summary": "畫面顯示一位講者",
    "important_details": [],
    "confidence": 0.9,
    "completion_status": "complete",
    "analysis_method": "minimax_video",
    "timestamp_reliable": True,
}

CLEAN_TRANSCRIPT = {
    "provider": "groq",
    "language": "zh",
    "segments": [{"start": 0.0, "end": 1.0, "text": "hello"}],
}

GAPPED_TRANSCRIPT = {
    "provider": "groq",
    "language": "zh",
    "segments": [{"start": 0.0, "end": 2.0, "text": "hello"}],
    "gap_check": {"max_gap": 5.0, "remaining": [{"start": 120.0, "end": 149.6}]},
    "backfill_errors": [{"error": "Groq 503"}],
}


class AnalysisCacheRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_root = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_root.name).resolve()
        self.job_dir = self.root / "mcp_jobs" / JOB_ID
        self.job_dir.mkdir(parents=True)
        self.source = self.job_dir / "source.mp4"
        self.source.write_bytes(b"fake-mp4-bytes")
        self.manifest = {
            "status": "ready",
            "segment_seconds": 120,
            "source": {"title": "demo", "url": "https://example.com/video"},
        }
        (self.job_dir / "manifest.json").write_text(
            json.dumps(self.manifest), encoding="utf-8")

    def tearDown(self):
        self.temp_root.cleanup()

    def _run_twice(self, observations, transcript, seed_legacy_bad_cache=False, vision_responses=None):
        """Run analyze_prepared_sync twice with real disk cache; return results + mocks."""
        analyze_mock = (MagicMock(wraps=pipeline.analyze_video_mp4) if vision_responses is not None
                        else MagicMock(return_value=[dict(observations[0])]))
        transcribe_mock = MagicMock(return_value=(dict(transcript), {}))
        env = {"GROQ_API_KEY": "test-key", "ANALYZE_VISUAL_MODE": "video_mp4"}
        patches = [
            patch.object(mcp_server, "MCP_ROOT", self.root),
            patch.object(mcp_server, "MCP_JOBS_ROOT", self.root / "mcp_jobs"),
            patch.dict(mcp_server.os.environ, env, clear=False),
            patch.multiple(mcp_server, check_job_capacity=MagicMock(),
                            storage_policy=MagicMock()),
            patch.object(mcp_server.pipeline, "probe_video", return_value={
                "format": {"duration": str(DURATION)},
                "streams": [{"codec_type": "video"}, {"codec_type": "audio"}],
            }),
            patch.object(mcp_server.pipeline, "minimax_model", return_value="MiniMax-M3"),
            patch.object(mcp_server.pipeline, "minimax_endpoint",
                         return_value="https://api.minimax.io/v1"),
            patch.object(mcp_server.pipeline, "minimax_subscription_key", return_value="key"),
            patch.object(mcp_server.pipeline, "analyze_video_mp4", analyze_mock),
            patch.object(mcp_server.pipeline, "extract_audio", return_value=[{
                "index": 0, "path": str(self.source), "source_start": 0,
                "source_end": 2, "sha256": "audio"}]),
            patch.object(mcp_server.pipeline, "transcribe_all", transcribe_mock),
        ]
        if vision_responses is not None:
            patches.extend([
                patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"),
                patch.object(pipeline, "minimax_chat", side_effect=vision_responses),
            ])
        for patcher in patches:
            patcher.start()
        self.addCleanup(lambda: [patcher.stop() for patcher in reversed(patches)])
        if seed_legacy_bad_cache:
            # A pre-fix result.json: status complete but the only visual frame
            # is an incomplete timestamp-coverage placeholder. The signature is
            # computed under the same env/mocks so only evidence gates the hit.
            legacy = {
                "status": "complete",
                "job_id": JOB_ID,
                "cache_signature": mcp_server.analysis_cache_signature(
                    self.source, self.manifest),
                "source_sha256": pipeline.file_sha256(self.source),
                "source_fingerprint": pipeline.file_fingerprint(self.source),
                "duration_seconds": DURATION,
                "audio_status": "complete",
                "visual_status": "complete",
                "summary": {"status": "pending_chatgpt"},
                "transcript": dict(CLEAN_TRANSCRIPT),
                "timeline": [{"timestamp": 0.0, "type": "visual",
                              "data": dict(INCOMPLETE_OBSERVATION)}],
                "errors": [],
            }
            result_path = self.job_dir / "analysis" / "result.json"
            result_path.parent.mkdir(parents=True, exist_ok=True)
            result_path.write_text(json.dumps(legacy), encoding="utf-8")
            self.assertIsNone(mcp_server.cached_analysis(JOB_ID, self.manifest, self.source))
        first = mcp_server.analyze_prepared_sync(JOB_ID)
        stored = json.loads((self.job_dir / "analysis" / "result.json")
                            .read_text(encoding="utf-8"))
        second = mcp_server.analyze_prepared_sync(JOB_ID)
        return first, stored, second, analyze_mock, transcribe_mock

    def test_timestamp_coverage_failure_is_not_cached_as_complete(self):
        first, stored, second, analyze_mock, _ = self._run_twice(
            [INCOMPLETE_OBSERVATION], CLEAN_TRANSCRIPT)
        self.assertNotEqual(first["visual_status"], "complete")
        self.assertNotEqual(first["status"], "complete")
        self.assertEqual(first["quality"]["status"], "needs_review")
        self.assertNotEqual(stored["status"], "complete")
        # The second call must reach MiniMax again instead of returning cache.
        self.assertEqual(analyze_mock.call_count, 2)
        self.assertNotEqual(second["status"], "complete")

    def test_transcript_gap_with_backfill_error_is_not_cached_as_complete(self):
        first, stored, second, _, transcribe_mock = self._run_twice(
            [GOOD_OBSERVATION], GAPPED_TRANSCRIPT)
        self.assertNotEqual(first["status"], "complete")
        self.assertEqual(first["quality"]["status"], "needs_review")
        self.assertNotEqual(stored["status"], "complete")
        # The second call must reach Groq again instead of returning cache.
        self.assertEqual(transcribe_mock.call_count, 2)
        self.assertNotEqual(second["status"], "complete")

    def test_remaining_gap_without_backfill_error_requires_explicit_retry(self):
        transcript = {**GAPPED_TRANSCRIPT, "backfill_errors": []}
        first, stored, second, _, transcribe_mock = self._run_twice([GOOD_OBSERVATION], transcript)
        self.assertNotEqual(stored["status"], "complete")
        self.assertEqual(first["quality"]["status"], "needs_review")
        self.assertEqual(transcribe_mock.call_count, 2)
        self.assertNotEqual(second["status"], "complete")

    def test_legacy_complete_cache_with_bad_evidence_is_recomputed(self):
        """A pre-fix result.json (status complete + incomplete frame) must not hit cache."""
        first, stored, _, analyze_mock, _ = self._run_twice(
            [GOOD_OBSERVATION], CLEAN_TRANSCRIPT, seed_legacy_bad_cache=True)
        # Cache must be bypassed: the model runs and healthy evidence is stored complete.
        self.assertEqual(analyze_mock.call_count, 1)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(stored["status"], "complete")

    def test_healthy_evidence_still_uses_cache(self):
        first, stored, second, analyze_mock, transcribe_mock = self._run_twice(
            [GOOD_OBSERVATION], CLEAN_TRANSCRIPT)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["visual_status"], "complete")
        self.assertEqual(second["status"], "complete")
        self.assertEqual(analyze_mock.call_count, 1)
        self.assertEqual(transcribe_mock.call_count, 1)

    def test_unanswered_visual_gap_retries_through_the_real_pipeline(self):
        from test_video_event_gap_fill import answer

        first, stored, second, analyze_mock, _ = self._run_twice(
            [GOOD_OBSERVATION], CLEAN_TRANSCRIPT,
            vision_responses=[answer(0, 100), RuntimeError("fill failed"),
                              answer(0, 30, 60, 90, 120, 150, DURATION)])
        self.assertEqual(analyze_mock.call_count, 2)
        self.assertNotEqual(first["visual_status"], "complete")
        self.assertNotEqual(stored["status"], "complete")
        self.assertEqual(first["quality"]["status"], "needs_review")
        self.assertEqual(len([row for row in first["timeline"] if row["type"] == "visual"]), 2)
        self.assertEqual(second["status"], "complete")


class TranscriptGapHelperTests(unittest.TestCase):
    def test_corrupt_gap_check_still_reports_pending_backfill_errors(self):
        self.assertTrue(mcp_server.transcript_gaps_pending(
            {"gap_check": "corrupt", "backfill_errors": [{"error": "Groq 503"}]}))
        self.assertFalse(mcp_server.transcript_gaps_pending(
            {"gap_check": {"max_gap": 5.0, "remaining": []}, "backfill_errors": []}))
        self.assertFalse(mcp_server.transcript_gaps_pending(None))
        self.assertTrue(mcp_server.transcript_gaps_pending(
            {"gap_check": {"remaining": [{"start": 1.0, "end": 2.0}]}}))


if __name__ == "__main__":
    unittest.main()
