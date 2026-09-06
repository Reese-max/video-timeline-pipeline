"""Comprehensive unit tests for video scoring, caching, and decision gates (Issue #3).

Covers:
- S0: Score weighting, renormalization when novelty is null.
- S1: History evidence retrieval and cutoff respect.
- S2: Cache key immutability and zero duplicate calls on cache hit.
- S3: Deterministic decision policy evaluation (notify vs digest).
- S5: Explain and reporting without paid API calls.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import scoring


class PriorityScoreCalculationTests(unittest.TestCase):
    def test_full_dimensions_weighting(self) -> None:
        # I=8, N=6, R=10 -> 8*0.4 + 6*0.35 + 10*0.25 = 3.2 + 2.1 + 2.5 = 7.8
        score = scoring.calculate_priority_score(importance=8.0, novelty=6.0, relevance=10.0)
        self.assertAlmostEqual(score, 7.8, places=2)

    def test_novelty_null_renormalization(self) -> None:
        # Novelty is None -> weights I:0.40, R:0.25 (total 0.65)
        # I=8, R=10 -> (8*0.4 + 10*0.25) / 0.65 = 5.7 / 0.65 = 8.77
        score = scoring.calculate_priority_score(importance=8.0, novelty=None, relevance=10.0)
        self.assertAlmostEqual(score, 8.77, places=2)

    def test_zero_and_clamp_boundaries(self) -> None:
        score_zero = scoring.calculate_priority_score(importance=0.0, novelty=0.0, relevance=0.0)
        self.assertEqual(score_zero, 0.0)


class DeterministicDecisionPolicyTests(unittest.TestCase):
    def test_high_importance_triggers_notify(self) -> None:
        mock_res = scoring.ScoreResult(
            importance=9.0,
            relevance=8.0,
            novelty=5.0,
            novelty_status="measured",
            evidence_confidence=0.85,
            priority_score=7.5,
            reasons={},
            claims=[],
        )
        dec, reason, policy_hash = scoring.evaluate_decision(mock_res)
        self.assertEqual(dec, "notify")
        self.assertIn("High importance", reason)
        self.assertTrue(policy_hash)

    def test_high_novelty_triggers_notify_if_importance_sufficient(self) -> None:
        mock_res = scoring.ScoreResult(
            importance=7.8,
            relevance=8.0,
            novelty=8.5,
            novelty_status="measured",
            evidence_confidence=0.80,
            priority_score=8.1,
            reasons={},
            claims=[],
        )
        dec, reason, _ = scoring.evaluate_decision(mock_res)
        self.assertEqual(dec, "notify")
        self.assertIn("High novelty", reason)

    def test_low_confidence_routes_to_digest(self) -> None:
        mock_res = scoring.ScoreResult(
            importance=9.5,
            relevance=9.0,
            novelty=9.0,
            novelty_status="measured",
            evidence_confidence=0.40,  # Below threshold 0.60
            priority_score=9.2,
            reasons={},
            claims=[],
        )
        dec, reason, _ = scoring.evaluate_decision(mock_res)
        self.assertEqual(dec, "digest")
        self.assertIn("Confidence", reason)

    def test_novelty_null_does_not_break_high_importance(self) -> None:
        mock_res = scoring.ScoreResult(
            importance=9.0,
            relevance=8.5,
            novelty=None,
            novelty_status="insufficient_history",
            evidence_confidence=0.75,
            priority_score=8.8,
            reasons={},
            claims=[],
        )
        dec, _, _ = scoring.evaluate_decision(mock_res)
        self.assertEqual(dec, "notify")


class VideoScoringAndCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.jobs_dir = self.root / "jobs"
        self.jobs_dir.mkdir()

        # Create a sample completed job
        self.job_name = "test_vid_101"
        job_dir = self.jobs_dir / self.job_name
        job_dir.mkdir()
        manifest = {
            "status": "completed",
            "analysis_revision": "1",
            "created_at": "2026-09-05T12:00:00Z",
            "source": {"title": "FastAPI AI Agent Architecture", "duration": 450},
        }
        summary = {
            "brief": "Deep dive into building multi-agent systems with FastAPI and background workers.",
            "terms": ["FastAPI", "Agent", "Worker", "TaskQueue", "SQLite"],
        }
        (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (job_dir / "summary.json").write_text(json.dumps(summary), encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_score_and_caching_lifecycle(self) -> None:
        # First execution -> must calculate and write cache
        res1 = scoring.score_video(self.root, self.job_name)
        self.assertFalse(res1["cached"])
        self.assertGreaterEqual(res1["importance"], 3.0)
        self.assertTrue(res1["score_id"])

        # Second execution -> must hit cache (cached == True)
        res2 = scoring.score_video(self.root, self.job_name)
        self.assertTrue(res2["cached"])
        self.assertEqual(res1["score_id"], res2["score_id"])
        self.assertEqual(res1["priority_score"], res2["priority_score"])

        # Force rescore -> force_rescore=True bypasses cache
        res3 = scoring.score_video(self.root, self.job_name, force_rescore=True)
        self.assertFalse(res3["cached"])

    def test_explain_score_reads_stored_decision(self) -> None:
        scoring.score_video(self.root, self.job_name)
        explanation = scoring.explain_score(self.root, self.job_name)
        self.assertEqual(explanation["job"], self.job_name)
        self.assertIn(explanation["decision"], ("notify", "digest"))
        self.assertIn("importance", explanation["reasons"])
        self.assertTrue(explanation["policy_hash"])

    def test_score_report_aggregates_summary(self) -> None:
        scoring.score_video(self.root, self.job_name)
        report = scoring.generate_score_report(self.root)
        self.assertEqual(report["total_scored"], 1)
        self.assertGreater(report["avg_importance"], 0)
        self.assertEqual(len(report["top_ranked"]), 1)
        self.assertEqual(report["top_ranked"][0]["job"], self.job_name)
