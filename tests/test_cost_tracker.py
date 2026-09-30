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

import datetime
import json
import os
import tempfile
import unittest
from contextlib import closing
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


class _FrozenDatetimeModule:
    """Stand-in for the ``datetime`` module with ``datetime.now()`` frozen.

    ``cost_tracker`` does ``import datetime``; patching the module attribute pins
    ``_utc_now_iso()`` and the period-id derivation to the same instant without
    any provider or network call.
    """

    def __init__(self, frozen_utc: datetime.datetime) -> None:
        if frozen_utc.tzinfo is None:
            raise ValueError("frozen_utc must be timezone-aware")

        class FrozenDatetime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                if tz is None:
                    return frozen_utc.replace(tzinfo=None)
                return frozen_utc.astimezone(tz)

        self.datetime = FrozenDatetime
        self.timezone = datetime.timezone
        self.timedelta = datetime.timedelta
        self.date = datetime.date
        self.time = datetime.time
        self.tzinfo = datetime.tzinfo


class BudgetTimezoneAccountingTests(unittest.TestCase):
    """Issue #18: COST_TIMEZONE must drive daily/monthly budget period boundaries."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _freeze(self, *utc_args: int):
        """Patch cost_tracker.datetime so now() returns the given UTC instant."""
        frozen = datetime.datetime(*utc_args, tzinfo=datetime.timezone.utc)
        return patch.object(cost_tracker, "datetime", _FrozenDatetimeModule(frozen))

    def _reservation_period(self, reservation_id: str) -> str:
        with closing(cost_tracker.get_operations_db(self.root)) as conn:
            row = conn.execute(
                "SELECT period_id FROM budget_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return row["period_id"]

    def _reservation_status(self, reservation_id: str) -> str:
        with closing(cost_tracker.get_operations_db(self.root)) as conn:
            row = conn.execute(
                "SELECT status FROM budget_reservations WHERE reservation_id = ?",
                (reservation_id,),
            ).fetchone()
        self.assertIsNotNone(row)
        return row["status"]

    def test_taipei_day_spans_utc_date_boundary(self) -> None:
        env = {"COST_TIMEZONE": "Asia/Taipei", "DAILY_BUDGET_USD": "0.010", "BUDGET_HARD_STOP": "true"}
        with patch.dict(os.environ, env):
            # 2026-09-30 23:59 UTC == 2026-10-01 07:59 in Asia/Taipei
            with self._freeze(2026, 9, 30, 23, 59):
                allowed, reservation_id, _ = cost_tracker.check_and_reserve_budget(
                    self.root, operation_name="transcribe", estimated_micro_usd=6000
                )
                self.assertTrue(allowed)
                self.assertEqual(self._reservation_period(reservation_id), "2026-10-01")
                # Settled spend: apify estimates 50,000 uUSD per scrape run.
                cost_tracker.record_usage(
                    self.root,
                    provider="apify",
                    operation="scrape",
                    reservation_id=reservation_id,
                    status="succeeded",
                )

            # 2026-10-01 00:01 UTC == 2026-10-01 08:01 Taipei: the same configured
            # local day, so the earlier settled spend must still count toward it.
            with self._freeze(2026, 10, 1, 0, 1):
                allowed2, reservation_id2, reason2 = cost_tracker.check_and_reserve_budget(
                    self.root, operation_name="transcribe", estimated_micro_usd=5000
                )
                self.assertFalse(allowed2)
                self.assertIsNone(reservation_id2)
                self.assertIn("Budget blocked", reason2)

                status = cost_tracker.get_budget_status(self.root)
                self.assertEqual(status["period"], "2026-10-01")

    def test_local_midnight_rollover_boundary(self) -> None:
        with patch.dict(os.environ, {"COST_TIMEZONE": "Asia/Taipei"}):
            # 2026-10-01 15:59:59 UTC == 23:59:59 Taipei on Oct 1
            with self._freeze(2026, 10, 1, 15, 59, 59):
                self.assertEqual(cost_tracker._current_period_id("daily"), "2026-10-01")
                self.assertEqual(cost_tracker._current_period_id("monthly"), "2026-10")
            # 2026-10-01 16:00:01 UTC == 00:00:01 Taipei on Oct 2
            with self._freeze(2026, 10, 1, 16, 0, 1):
                self.assertEqual(cost_tracker._current_period_id("daily"), "2026-10-02")
                self.assertEqual(cost_tracker._current_period_id("monthly"), "2026-10")
            # 2026-12-31 16:30 UTC == 2027-01-01 00:30 Taipei — Dec→Jan rollover
            with self._freeze(2026, 12, 31, 16, 30):
                self.assertEqual(cost_tracker._current_period_id("daily"), "2027-01-01")
                self.assertEqual(cost_tracker._current_period_id("monthly"), "2027-01")

    def test_dst_zone_uses_iana_rules_not_fixed_offset(self) -> None:
        with patch.dict(os.environ, {"COST_TIMEZONE": "America/New_York"}):
            # 2026-07-01 04:30 UTC -> EDT (UTC-4) -> Jul 1 00:30 local.
            # A fixed UTC-5 offset would produce Jun 30 23:30 instead.
            with self._freeze(2026, 7, 1, 4, 30):
                self.assertEqual(cost_tracker._current_period_id("daily"), "2026-07-01")
            # 2026-01-15 04:30 UTC -> EST (UTC-5) -> Jan 14 23:30 local.
            with self._freeze(2026, 1, 15, 4, 30):
                self.assertEqual(cost_tracker._current_period_id("daily"), "2026-01-14")

    def test_invalid_timezone_fails_explicitly(self) -> None:
        for bad_tz in ("Not/AZone", ""):
            with self.subTest(tz=bad_tz), patch.dict(os.environ, {"COST_TIMEZONE": bad_tz}):
                with self.assertRaises(ValueError):
                    cost_tracker.check_and_reserve_budget(
                        self.root, operation_name="op", estimated_micro_usd=1
                    )
                with self.assertRaises(ValueError):
                    cost_tracker.get_budget_status(self.root)
                with self.assertRaises(ValueError):
                    cost_tracker.get_cost_report(self.root)

    def test_reservations_stay_bound_to_original_period(self) -> None:
        env = {"COST_TIMEZONE": "Asia/Taipei", "DAILY_BUDGET_USD": "1.00", "BUDGET_HARD_STOP": "true"}
        with patch.dict(os.environ, env):
            # 23:30 Taipei on Oct 1 (15:30 UTC)
            with self._freeze(2026, 10, 1, 15, 30):
                allowed1, res1, _ = cost_tracker.check_and_reserve_budget(
                    self.root, operation_name="op", estimated_micro_usd=5000
                )
                self.assertTrue(allowed1)
            # 00:30 Taipei on Oct 2 (Oct 1, 16:30 UTC) — after local-midnight rollover
            with self._freeze(2026, 10, 1, 16, 30):
                allowed2, res2, _ = cost_tracker.check_and_reserve_budget(
                    self.root, operation_name="op", estimated_micro_usd=5000
                )
                self.assertTrue(allowed2)
                status = cost_tracker.get_budget_status(self.root)
                self.assertEqual(status["period"], "2026-10-02")

            self.assertEqual(self._reservation_period(res1), "2026-10-01")
            self.assertEqual(self._reservation_period(res2), "2026-10-02")

            # Settle the prior-day reservation after local midnight. Settlement
            # must not rewrite the period that was reserved before rollover.
            with self._freeze(2026, 10, 1, 16, 45):
                cost_tracker.record_usage(
                    self.root,
                    provider="groq",
                    operation="transcribe",
                    reservation_id=res1,
                    status="succeeded",
                )
            self.assertEqual(self._reservation_period(res1), "2026-10-01")
            self.assertEqual(self._reservation_status(res1), "settled")

    def test_blocked_event_and_outputs_share_local_period(self) -> None:
        env = {"COST_TIMEZONE": "Asia/Taipei", "DAILY_BUDGET_USD": "0.001", "BUDGET_HARD_STOP": "true"}
        with patch.dict(os.environ, env):
            # 2026-09-30 16:30 UTC == 2026-10-01 00:30 Taipei
            with self._freeze(2026, 9, 30, 16, 30):
                allowed, _, reason = cost_tracker.check_and_reserve_budget(
                    self.root, operation_name="op", estimated_micro_usd=5000
                )
                self.assertFalse(allowed)
                self.assertIn("Budget blocked", reason)
                # A second block in the same configured period dedups to one event.
                allowed_again, _, _ = cost_tracker.check_and_reserve_budget(
                    self.root, operation_name="op", estimated_micro_usd=5000
                )
                self.assertFalse(allowed_again)
                status = cost_tracker.get_budget_status(self.root)
                report = cost_tracker.get_cost_report(self.root)

            self.assertEqual(status["period"], "2026-10-01")
            self.assertEqual(status["accounting_timezone"], "Asia/Taipei")
            self.assertEqual(report["period"], "2026-10-01")
            self.assertEqual(report["accounting_timezone"], "Asia/Taipei")

            with closing(cost_tracker.get_operations_db(self.root)) as conn:
                rows = conn.execute(
                    "SELECT dedup_key, payload_json FROM notification_events "
                    "WHERE event_type = 'budget.blocked'"
                ).fetchall()
            self.assertEqual(len(rows), 1)
            self.assertIn("2026-10-01", rows[0]["dedup_key"])
            payload = json.loads(rows[0]["payload_json"])
            self.assertEqual(payload["period_id"], "2026-10-01")
            self.assertEqual(payload["accounting_timezone"], "Asia/Taipei")

    def test_monthly_report_uses_local_month_start(self) -> None:
        with patch.dict(os.environ, {"COST_TIMEZONE": "Asia/Taipei"}):
            # Sep 30 23:59 UTC == Oct 1 07:59 Taipei — Taipei October has started.
            with self._freeze(2026, 9, 30, 23, 59):
                cost_tracker.record_usage(
                    self.root, provider="groq", operation="t", request_units=60
                )
            # Oct 1 00:30 UTC == 08:30 Taipei; the spend belongs to the same
            # Taipei day/month even though its stored UTC date is Sep 30.
            with self._freeze(2026, 10, 1, 0, 30):
                daily = cost_tracker.get_cost_report(self.root, period_type="daily")
                monthly = cost_tracker.get_cost_report(self.root, period_type="monthly")

            self.assertEqual(daily["period"], "2026-10-01")
            self.assertEqual(daily["grand_total_estimated_usd"], 0.001)
            self.assertEqual(monthly["period"], "2026-10")
            self.assertEqual(monthly["grand_total_estimated_usd"], 0.001)


if __name__ == "__main__":
    unittest.main()
