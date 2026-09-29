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
from contextlib import contextmanager
from datetime import datetime as RealDateTime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import cost_tracker


@contextmanager
def freeze_utc(utc_iso: str):
    fixed = RealDateTime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    if fixed.tzinfo is None:
        fixed = fixed.replace(tzinfo=timezone.utc)

    class FrozenDateTime(RealDateTime):
        @classmethod
        def now(cls, tz=None):  # noqa: D102 - mock signature
            return fixed.replace(tzinfo=None) if tz is None else fixed.astimezone(tz)

    with patch.object(cost_tracker.datetime, "datetime", FrozenDateTime):
        yield


def period_at(tz_name: str, utc_iso: str, period_type: str = "daily"):
    instant = RealDateTime.fromisoformat(utc_iso.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    with patch.dict(os.environ, {"COST_TIMEZONE": tz_name}):
        return cost_tracker._current_period_window(period_type, instant)


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


class TimezoneAwarePeriodTests(unittest.TestCase):
    """Issue #18: all budget accounting follows local calendar windows."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_taipei_0759_and_0801_share_the_same_local_day_window(self) -> None:
        before = period_at("Asia/Taipei", "2026-01-15T23:59:00Z")
        after = period_at("Asia/Taipei", "2026-01-16T00:01:00Z")

        self.assertEqual(before.period_id, "2026-01-16")  # 07:59 Taipei
        self.assertEqual(after.period_id, "2026-01-16")  # 08:01 Taipei
        self.assertEqual(before.timezone, "Asia/Taipei")
        self.assertEqual(before.start_utc.isoformat(), "2026-01-15T16:00:00+00:00")
        self.assertEqual(before.end_utc.isoformat(), "2026-01-16T16:00:00+00:00")
        self.assertEqual(before, after)

    def test_reports_and_status_use_the_utc_window_across_utc_midnight(self) -> None:
        timestamps = (
            "2026-01-15T15:59:00Z",  # Before the Taipei day begins.
            "2026-01-15T23:59:00Z",  # 07:59 on the local day, previous UTC date.
            "2026-01-16T00:01:00Z",  # 08:01 on the same local day.
            "2026-01-16T16:00:00Z",  # Exact end boundary; belongs to next local day.
        )
        with patch.dict(os.environ, {"COST_TIMEZONE": "Asia/Taipei"}):
            for timestamp in timestamps:
                with freeze_utc(timestamp):
                    cost_tracker.record_usage(
                        self.root,
                        provider="groq",
                        operation="window_test",
                        status="succeeded",
                    )

            with freeze_utc("2026-01-16T00:02:00Z"):
                report = cost_tracker.get_cost_report(self.root)
                status = cost_tracker.get_budget_status(self.root)

        self.assertEqual(report["period"], "2026-01-16")
        self.assertEqual(report["accounting_timezone"], "Asia/Taipei")
        self.assertEqual(report["period_start_utc"], "2026-01-15T16:00:00+00:00")
        self.assertEqual(report["period_end_utc"], "2026-01-16T16:00:00+00:00")
        self.assertEqual(report["providers"][0]["calls"], 2)
        self.assertEqual(report["grand_total_estimated_usd"], 0.002)
        self.assertEqual(status["accounting_timezone"], "Asia/Taipei")
        self.assertEqual(status["spent_usd"], 0.002)
        self.assertEqual(status["blocked_operations_today"], 0)

    def test_budget_hard_stop_counts_usage_from_taipei_0759_at_0801(self) -> None:
        env = {
            "COST_TIMEZONE": "Asia/Taipei",
            "DAILY_BUDGET_USD": "0.001",
            "BUDGET_HARD_STOP": "true",
        }
        with patch.dict(os.environ, env):
            with freeze_utc("2026-01-15T23:59:00Z"):  # Jan 16, 07:59 Taipei.
                cost_tracker.record_usage(
                    self.root,
                    provider="groq",
                    operation="before_0800",
                    status="succeeded",
                )
            with freeze_utc("2026-01-16T00:01:00Z"):  # Jan 16, 08:01 Taipei.
                allowed, reservation_id, reason = cost_tracker.check_and_reserve_budget(
                    self.root, "after_0800", 1
                )

        self.assertFalse(allowed)
        self.assertIsNone(reservation_id)
        self.assertIn("Budget blocked", reason)

    def test_daily_local_midnight_rollover_keeps_old_reservation_period(self) -> None:
        env = {
            "COST_TIMEZONE": "Asia/Taipei",
            "DAILY_BUDGET_USD": "0.010",
            "BUDGET_HARD_STOP": "true",
        }
        with patch.dict(os.environ, env):
            with freeze_utc("2026-01-16T15:59:00Z"):  # Jan 16, 23:59 Taipei.
                allowed_old, old_id, _ = cost_tracker.check_and_reserve_budget(
                    self.root, "before_midnight", 6000
                )
            with freeze_utc("2026-01-16T16:01:00Z"):  # Jan 17, 00:01 Taipei.
                allowed_new, new_id, _ = cost_tracker.check_and_reserve_budget(
                    self.root, "after_midnight", 6000
                )
                blocked, _, reason = cost_tracker.check_and_reserve_budget(
                    self.root, "same_new_day_over_budget", 5000
                )

        self.assertTrue(allowed_old)
        self.assertTrue(allowed_new)
        self.assertFalse(blocked)
        self.assertIn("Budget blocked", reason)
        self.assertIsNotNone(old_id)
        self.assertIsNotNone(new_id)

        conn = cost_tracker.get_operations_db(self.root)
        try:
            reservations = conn.execute(
                "SELECT reservation_id, period_id, status FROM budget_reservations ORDER BY created_at"
            ).fetchall()
        finally:
            conn.close()
        by_id = {row["reservation_id"]: (row["period_id"], row["status"]) for row in reservations}
        self.assertEqual(by_id[old_id], ("2026-01-16", "reserved"))
        self.assertEqual(by_id[new_id], ("2026-01-17", "reserved"))

    def test_monthly_report_uses_local_month_boundaries(self) -> None:
        with patch.dict(os.environ, {"COST_TIMEZONE": "Asia/Taipei"}):
            with freeze_utc("2026-01-31T15:59:00Z"):  # Jan 31, 23:59 Taipei.
                cost_tracker.record_usage(self.root, "groq", "before_month", status="succeeded")
            with freeze_utc("2026-01-31T16:01:00Z"):  # Feb 1, 00:01 Taipei.
                cost_tracker.record_usage(self.root, "groq", "after_month", status="succeeded")
                report = cost_tracker.get_cost_report(self.root, period_type="monthly")

        self.assertEqual(report["period"], "2026-02")
        self.assertEqual(report["period_start_utc"], "2026-01-31T16:00:00+00:00")
        self.assertEqual(report["period_end_utc"], "2026-02-28T16:00:00+00:00")
        self.assertEqual(report["providers"][0]["calls"], 1)

    def test_dst_timezone_uses_23_and_25_hour_local_days(self) -> None:
        spring = period_at("America/New_York", "2026-03-08T12:00:00Z")
        fall = period_at("America/New_York", "2026-11-01T12:00:00Z")

        self.assertEqual(spring.period_id, "2026-03-08")
        self.assertEqual(spring.start_utc.isoformat(), "2026-03-08T05:00:00+00:00")
        self.assertEqual(spring.end_utc.isoformat(), "2026-03-09T04:00:00+00:00")
        self.assertEqual((spring.end_utc - spring.start_utc).total_seconds(), 23 * 3600)
        self.assertEqual(fall.period_id, "2026-11-01")
        self.assertEqual(fall.start_utc.isoformat(), "2026-11-01T04:00:00+00:00")
        self.assertEqual(fall.end_utc.isoformat(), "2026-11-02T05:00:00+00:00")
        self.assertEqual((fall.end_utc - fall.start_utc).total_seconds(), 25 * 3600)

    def test_invalid_timezone_fails_before_database_or_reservation_changes(self) -> None:
        with patch.dict(
            os.environ,
            {"COST_TIMEZONE": "Not/AZone", "DAILY_BUDGET_USD": "0.010", "BUDGET_HARD_STOP": "true"},
        ):
            with self.assertRaisesRegex(ValueError, "Invalid COST_TIMEZONE"):
                cost_tracker.check_and_reserve_budget(self.root, "invalid_tz", 1000)
            with self.assertRaisesRegex(ValueError, "Invalid COST_TIMEZONE"):
                cost_tracker.get_budget_status(self.root)
            with self.assertRaisesRegex(ValueError, "Invalid COST_TIMEZONE"):
                cost_tracker.get_cost_report(self.root)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_budget_block_dedup_uses_timezone_and_resets_on_local_day(self) -> None:
        env = {
            "COST_TIMEZONE": "Asia/Taipei",
            "DAILY_BUDGET_USD": "0.001",
            "BUDGET_HARD_STOP": "true",
        }
        with patch.dict(os.environ, env):
            for timestamp in (
                "2026-01-15T23:59:00Z",  # Jan 16, 07:59 Taipei.
                "2026-01-16T00:01:00Z",  # Jan 16, 08:01 Taipei; same dedup bucket.
                "2026-01-16T16:01:00Z",  # Jan 17, 00:01 Taipei; new dedup bucket.
            ):
                with freeze_utc(timestamp):
                    allowed, _, _ = cost_tracker.check_and_reserve_budget(
                        self.root, "blocked_without_provider_call", 2000
                    )
                    self.assertFalse(allowed)

        conn = cost_tracker.get_operations_db(self.root)
        try:
            keys = [
                row["dedup_key"]
                for row in conn.execute(
                    "SELECT dedup_key FROM notification_events WHERE event_type = 'budget.blocked' ORDER BY dedup_key"
                ).fetchall()
            ]
        finally:
            conn.close()
        self.assertEqual(
            keys,
            [
                "budget_blocked_Asia/Taipei_2026-01-16_default",
                "budget_blocked_Asia/Taipei_2026-01-17_default",
            ],
        )


if __name__ == "__main__":
    unittest.main()
