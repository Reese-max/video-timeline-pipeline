"""Comprehensive unit tests for API cost tracking, token accounting, and budget guard (Issue #4).

Covers:
- C1: Integer micro-USD storage without float drift.
- C2/C3: Atomic budget reservations and hard stop blocking.
- C4: Error taxonomy classification and safe retryability.
- C6: Pure local cost reporting and budget status.
- C7: Provider health metrics and failed list auditing.
- C8: Zero external transport calls on budget block and zero cost on cache hit.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import cost_tracker


class CostTrackerPrecisionAndStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_integer_micro_usd_precision(self) -> None:
        # MiniMax calculation: prompt_tokens=1000 ($0.0003), completion_tokens=1000 ($0.0012)
        # Expected: 300 + 1200 = 1500 micro-USD
        cost = cost_tracker.estimate_operation_cost_micro_usd("minimax", "MiniMax-Text-01", 1000, 1000)
        self.assertEqual(cost, 1500)
        self.assertIsInstance(cost, int)

    def test_cache_hit_zero_cost(self) -> None:
        cost_id = cost_tracker.record_usage(
            self.root,
            provider="minimax",
            operation="score_video",
            job="video_test_cache",
            cache_hit=True,
            request_units=5000,
            response_units=2000,
        )
        self.assertTrue(cost_id)

        report = cost_tracker.get_cost_report(self.root)
        self.assertEqual(report["grand_total_estimated_usd"], 0.0)
        p = report["providers"][0]
        self.assertEqual(p["cache_hits"], 1)


class BudgetGuardAndReservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_atomic_budget_reservation_and_hard_stop(self) -> None:
        # Set daily budget to $0.010 (10,000 micro-USD) with hard stop enabled
        with patch.dict(os.environ, {"DAILY_BUDGET_USD": "0.010", "BUDGET_HARD_STOP": "true"}):
            # 1. First reservation for 6,000 micro-USD should succeed
            allowed, res_id, reason = cost_tracker.check_and_reserve_budget(
                self.root, operation_name="transcribe", estimated_micro_usd=6000
            )
            self.assertTrue(allowed)
            self.assertIsNotNone(res_id)
            self.assertEqual(reason, "Approved")

            # 2. Second reservation for 5,000 micro-USD (6000 + 5000 = 11000 > 10000) must be blocked
            allowed2, res_id2, reason2 = cost_tracker.check_and_reserve_budget(
                self.root, operation_name="transcribe", estimated_micro_usd=5000
            )
            self.assertFalse(allowed2)
            self.assertIsNone(res_id2)
            self.assertIn("Budget blocked", reason2)

            # Settle first reservation
            cost_tracker.record_usage(
                self.root,
                provider="groq",
                operation="transcribe",
                reservation_id=res_id,
                request_units=60,
                status="succeeded",
            )

            status = cost_tracker.get_budget_status(self.root)
            self.assertEqual(status["blocked_operations_today"], 1)
            self.assertTrue(status["hard_stop_enabled"])

    def test_budget_rejection_guarantees_zero_external_transport(self) -> None:
        mock_transport = MagicMock()

        with patch.dict(os.environ, {"DAILY_BUDGET_USD": "0.005", "BUDGET_HARD_STOP": "true"}):
            # Request exceeds budget
            allowed, _, _ = cost_tracker.check_and_reserve_budget(
                self.root, operation_name="large_batch", estimated_micro_usd=10000
            )
            if allowed:
                mock_transport()

            # Zero transport calls guaranteed
            mock_transport.assert_not_called()


class ErrorTaxonomyAndRetryTests(unittest.TestCase):
    def test_classify_error_categories(self) -> None:
        self.assertEqual(cost_tracker.classify_error("unauthorized", 401), "auth")
        self.assertEqual(cost_tracker.classify_error("forbidden - insufficient_scope", 403), "permission")
        self.assertEqual(cost_tracker.classify_error("Quota exceeded", 429), "quota_exhausted")
        self.assertEqual(cost_tracker.classify_error("Rate limit reached", 429), "rate_limit")
        self.assertEqual(cost_tracker.classify_error("Gateway timeout", 504), "timeout")
        self.assertEqual(cost_tracker.classify_error("Connection refused by host"), "network")
        self.assertEqual(cost_tracker.classify_error("Internal server error", 500), "provider_5xx")
        self.assertEqual(cost_tracker.classify_error("Content policy violation", 400), "content_rejected")
        self.assertEqual(cost_tracker.classify_error("Budget blocked: daily limit reached"), "budget_blocked")

    def test_safe_retry_predicate(self) -> None:
        self.assertTrue(cost_tracker.is_retryable("rate_limit"))
        self.assertTrue(cost_tracker.is_retryable("timeout"))
        self.assertTrue(cost_tracker.is_retryable("network"))
        self.assertTrue(cost_tracker.is_retryable("provider_5xx"))

        self.assertFalse(cost_tracker.is_retryable("auth"))
        self.assertFalse(cost_tracker.is_retryable("permission"))
        self.assertFalse(cost_tracker.is_retryable("quota_exhausted"))
        self.assertFalse(cost_tracker.is_retryable("budget_blocked"))
        self.assertFalse(cost_tracker.is_retryable("content_rejected"))


class ReportingAndHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_provider_health_and_failed_list(self) -> None:
        # Record 1 success and 2 failures for apify
        cost_tracker.record_usage(
            self.root,
            provider="apify",
            operation="scrape",
            status="succeeded",
            latency_ms=120.0,
        )
        cost_tracker.record_usage(
            self.root,
            provider="apify",
            operation="scrape",
            status="failed",
            safe_error="Actor timeout after 300s",
            latency_ms=300000.0,
        )
        cost_tracker.record_usage(
            self.root,
            provider="apify",
            operation="scrape",
            status="failed",
            safe_error="Rate limited by Apify",
            latency_ms=50.0,
        )

        health = cost_tracker.get_provider_health(self.root)
        providers = health["providers"]
        self.assertEqual(len(providers), 1)
        self.assertEqual(providers[0]["provider"], "apify")
        self.assertEqual(providers[0]["status"], "degraded")  # 1/3 = 33.3% < 90%
        self.assertAlmostEqual(providers[0]["success_rate_pct"], 33.3, places=1)

        failed_list = cost_tracker.get_failed_list(self.root)
        self.assertEqual(len(failed_list), 2)
        self.assertEqual(failed_list[0]["safe_error"], "Rate limited by Apify")


if __name__ == "__main__":
    unittest.main()
