"""A refused proposal is zero-cost, including in configured-local cost reports."""
import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cost_tracker


class BlockedProposalReportTests(unittest.TestCase):
    def test_blocked_proposals_do_not_inflate_daily_or_monthly_spend(self):
        instant = datetime.datetime(2026, 9, 30, 23, 59, tzinfo=datetime.timezone.utc)

        class Frozen(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

        env = {"COST_TIMEZONE": "Asia/Taipei", "DAILY_BUDGET_USD": "0.0015", "BUDGET_HARD_STOP": "true"}
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, env), patch.object(cost_tracker.datetime, "datetime", Frozen):
            root = Path(directory)
            allowed, reservation, _ = cost_tracker.check_and_reserve_budget(root, "synthetic-transcription", 1000)
            self.assertTrue(allowed)
            cost_tracker.record_usage(root, "groq", "synthetic-transcription", reservation_id=reservation, request_units=60)
            allowed, reservation, _ = cost_tracker.check_and_reserve_budget(root, "rejected-proposal", 1000)
            self.assertFalse(allowed)
            self.assertIsNone(reservation)
            status = cost_tracker.get_budget_status(root)
            self.assertEqual(status["spent_usd"], 0.001)
            self.assertEqual(status["blocked_operations_today"], 1)
            for period_type, period in (("daily", "2026-10-01"), ("monthly", "2026-10")):
                with self.subTest(period_type=period_type):
                    report = cost_tracker.get_cost_report(root, period_type)
                    self.assertEqual(report["period"], period)
                    self.assertEqual(report["accounting_timezone"], "Asia/Taipei")
                    self.assertEqual(report["grand_total_estimated_usd"], 0.001)
                    self.assertEqual([p["provider"] for p in report["providers"]], ["groq"])
