"""Offline credit ledger contract; never use the operator's home or ledger."""
import concurrent.futures
import datetime as dt
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import brightdata_credits as credits
from cost_tracker import BudgetBlockedError


class CreditLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {
            "BRIGHTDATA_CREDIT_LEDGER": str(self.root / "credits.sqlite3"),
            "BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "10", "BRIGHTDATA_POST_DAILY_LIMIT": "2",
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_reserve_settle_actual_and_idempotency(self):
        reservation = credits.reserve(5)
        self.assertEqual(credits.usage()["reserved"], 5)
        credits.settle(reservation, 3)
        credits.settle(reservation, 3)
        self.assertEqual(credits.usage()["used"], 3)
        self.assertEqual(credits.usage()["reserved"], 0)
        self.assertEqual(credits.usage()["remaining"], 7)

    def test_release_only_removes_unsettled_reservation(self):
        pending = credits.reserve(5, daily_post=True, reservation_id="pending")
        settled = credits.reserve(2, reservation_id="settled")
        credits.settle(settled, 1)
        credits.release(pending)
        credits.release(settled)
        credits.release("missing")
        state = credits.usage()
        self.assertEqual((state["reserved"], state["used"], state["daily_posts_used"]), (0, 1, 0))
        with self.assertRaisesRegex(RuntimeError, "禁止重送"):
            credits.reserve(1, reservation_id=settled)
        credits.reserve(5, reservation_id=pending)

    def test_unknown_remains_charged_and_actual_can_exceed_reservation(self):
        credits.reserve(4)
        reservation = credits.reserve(2)
        credits.settle(reservation, 7)
        self.assertEqual(credits.usage()["used"], 7)
        self.assertEqual(credits.usage()["reserved"], 4)
        self.assertEqual(credits.usage()["remaining"], 0)
        with self.assertRaises(BudgetBlockedError):
            credits.reserve(1)

    def test_utc_month_rollover_and_old_settlement(self):
        january = dt.datetime(2026, 1, 31, 23, 59, tzinfo=dt.timezone.utc)
        february = dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
        reservation = credits.reserve(10, now=january)
        self.assertEqual(credits.usage(now=february)["remaining"], 10)
        credits.settle(reservation, 8)
        self.assertEqual(credits.usage(now=january)["used"], 8)
        self.assertEqual(credits.usage(now=february)["used"], 0)

    def test_zero_and_invalid_settings_fail_closed(self):
        for name in ("BRIGHTDATA_MONTHLY_CREDIT_LIMIT", "BRIGHTDATA_POST_DAILY_LIMIT"):
            for value in ("-1", "bad", "1.5", "", "+1", "true"):
                with self.subTest(name=name, value=value), patch.dict(os.environ, {name: value}):
                    with self.assertRaises(ValueError):
                        credits.reserve(1, daily_post=True)
        with patch.dict(os.environ, {"BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "0"}):
            with self.assertRaises(credits.BrightDataQuotaError) as raised:
                credits.reserve(1)
            self.assertIn("UTC", str(raised.exception))
            self.assertTrue(raised.exception.resets_at.endswith("Z"))

    def test_daily_limit_is_atomic_and_resets_at_utc_midnight(self):
        today = dt.datetime(2026, 3, 1, 23, 59, tzinfo=dt.timezone.utc)
        for _ in range(2):
            credits.reserve(1, daily_post=True, now=today)
        with self.assertRaises(credits.BrightDataQuotaError) as raised:
            credits.reserve(1, daily_post=True, now=today)
        self.assertEqual(raised.exception.resets_at, "2026-03-02T00:00:00Z")
        self.assertEqual(credits.usage(now=today)["daily_posts_used"], 2)
        self.assertEqual(credits.usage(now=today + dt.timedelta(days=1))["daily_posts_used"], 0)

    def test_two_connections_cannot_overreserve(self):
        credits.usage()  # Create the schema before racing the two independent connections.
        start = threading.Barrier(2)
        original_usage = credits._usage

        def slow_usage(*args):
            state = original_usage(*args)
            time.sleep(0.3)  # Without BEGIN IMMEDIATE, both readers see zero before either writes.
            return state

        def reserve():
            start.wait(timeout=5)
            try:
                credits.reserve(6)
                return True
            except credits.BrightDataQuotaError:
                return False
        with patch.object(credits, "_usage", side_effect=slow_usage), concurrent.futures.ThreadPoolExecutor(2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: reserve(), range(2))), [False, True])
        self.assertEqual(credits.usage()["reserved"], 6)

    def test_default_path_and_defaults_use_temporary_home(self):
        with patch.dict(os.environ, {"HOME": str(self.root), "USERPROFILE": str(self.root)}, clear=True):
            self.assertEqual(credits.ledger_path(), self.root / ".local/state/video-timeline-pipeline/brightdata-credits.sqlite3")
            result = credits.usage()
            self.assertEqual(result["limit"], 4500)
            self.assertEqual(result["daily_post_limit"], 20)

    def test_zero_daily_limit_only_blocks_single_posts(self):
        with patch.dict(os.environ, {"BRIGHTDATA_POST_DAILY_LIMIT": "0"}):
            with self.assertRaises(credits.BrightDataQuotaError):
                credits.reserve(1, daily_post=True)
            credits.reserve(2)
        self.assertEqual(credits.usage()["reserved"], 2)

    def test_default_directory_requests_private_mode_even_if_it_exists(self):
        with patch.dict(os.environ, {"HOME": str(self.root), "USERPROFILE": str(self.root)}, clear=True), \
                patch.object(os, "chmod") as chmod:
            credits.usage()
        self.assertEqual(chmod.call_args.args[:2], (self.root / ".local/state/video-timeline-pipeline", 0o700))

    def test_calendar_uses_utc_even_for_offset_datetime(self):
        taipei = dt.datetime(2026, 2, 1, 7, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        credits.reserve(1, daily_post=True, now=taipei)
        result = credits.usage(now=taipei)
        self.assertEqual(result["month"], "2026-01")
        with self.assertRaises(credits.BrightDataQuotaError) as raised:
            credits.reserve(10, now=taipei)
        self.assertEqual(raised.exception.resets_at, "2026-02-01T00:00:00Z")

    def test_same_trigger_cannot_rebook_an_orphan_reservation(self):
        credits.reserve(5, reservation_id="same-trigger")
        with self.assertRaisesRegex(RuntimeError, "禁止重送"):
            credits.reserve(5, reservation_id="same-trigger")
        self.assertEqual(credits.usage()["reserved"], 5)
        with patch.dict(os.environ, {"BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "0"}):
            with self.assertRaises(RuntimeError):
                credits.reserve(5, reservation_id="same-trigger")


if __name__ == "__main__":
    unittest.main()
