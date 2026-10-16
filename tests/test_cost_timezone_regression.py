"""Issue #18: provider-free accounting checks using a real SQLite ledger."""

import datetime
import json
import os
import socket
import sqlite3
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

import cost_tracker
import notifications


def at(utc_iso):
    fixed = datetime.datetime.fromisoformat(utc_iso).replace(tzinfo=datetime.timezone.utc)

    class FrozenDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed.astimezone(tz) if tz else fixed

    return patch.object(cost_tracker.datetime, "datetime", FrozenDateTime)


class CostTimezoneRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        notifications._init_operations_schema(self.conn)
        cost_tracker._init_cost_schema(self.conn)
        for guard in (
            patch.object(cost_tracker, "get_operations_db", return_value=self.conn),
            patch.dict(os.environ, {
                "COST_TIMEZONE": "Asia/Taipei",
                "DAILY_BUDGET_USD": "0.001",
                "BUDGET_HARD_STOP": "true",
            }),
            patch.object(socket, "socket", side_effect=AssertionError("network forbidden")),
            patch.object(urllib.request, "urlopen", side_effect=AssertionError("HTTP forbidden")),
        ):
            guard.start()
            self.addCleanup(guard.stop)

    def usage_at(self, instant):
        with at(instant):
            cost_tracker.record_usage(self.root, "groq", "test")

    def test_settled_cost_at_local_month_start_enforces_budget(self):
        with at("2026-01-31T16:30:00"):
            cost_tracker.record_usage(self.root, "groq", "test")
            self.assertEqual(cost_tracker.get_budget_status(self.root)["spent_usd"], 0.001)
            for period_type, expected in (("daily", "2026-02-01"), ("monthly", "2026-02")):
                report = cost_tracker.get_cost_report(self.root, period_type)
                self.assertEqual(report["period"], expected)
                self.assertEqual(report["grand_total_estimated_usd"], 0.001)
            self.assertFalse(cost_tracker.check_and_reserve_budget(self.root, "test", 1000)[0])

    def test_taipei_0759_and_0801_share_spend_and_event_dedup(self):
        self.usage_at("2026-01-15T23:58:00")
        for instant in ("2026-01-15T23:59:00", "2026-01-16T00:01:00"):
            with at(instant):
                self.assertEqual(cost_tracker._current_period_id(), "2026-01-16")
                self.assertFalse(cost_tracker.check_and_reserve_budget(self.root, "test", 1000)[0])
                status = cost_tracker.get_budget_status(self.root)
                self.assertEqual(status["spent_usd"], 0.001)
        events = self.conn.execute("SELECT payload_json FROM notification_events").fetchall()
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0][0])["period_id"], "2026-01-16")
        self.assertEqual(status["blocked_operations_today"], 2)

    def test_local_midnight_releases_only_the_new_period(self):
        with at("2026-01-15T15:59:00"):
            allowed, original, _ = cost_tracker.check_and_reserve_budget(self.root, "old", 1000)
            self.assertTrue(allowed)
        with at("2026-01-15T16:01:00"):
            allowed, current, _ = cost_tracker.check_and_reserve_budget(self.root, "new", 1000)
            self.assertTrue(allowed)
            cost_tracker.record_usage(self.root, "groq", "old", reservation_id=original)
            rows = {r["reservation_id"]: r for r in self.conn.execute("SELECT * FROM budget_reservations")}
            self.assertEqual((rows[original]["period_id"], rows[original]["status"]), ("2026-01-15", "settled"))
            self.assertEqual((rows[current]["period_id"], rows[current]["status"]), ("2026-01-16", "reserved"))
            self.assertFalse(cost_tracker.check_and_reserve_budget(self.root, "retry", 1000)[0])

    def test_legacy_utc_labelled_reservations_still_count_in_local_day(self):
        with at("2026-01-15T17:00:00"):
            allowed, reservation, _ = cost_tracker.check_and_reserve_budget(self.root, "old", 1000)
            self.assertTrue(allowed)
            self.conn.execute("UPDATE budget_reservations SET period_id = '2026-01-15' WHERE reservation_id = ?",
                              (reservation,))
        for instant in ("2026-01-15T23:59:00", "2026-01-16T00:01:00"):
            with at(instant):
                self.assertFalse(cost_tracker.check_and_reserve_budget(self.root, "retry", 1000)[0])

    def test_calendar_windows_respect_both_edges_and_dst(self):
        windows = (
            ("daily", "Asia/Taipei", "2026-01-15T16:00:00", "2026-01-16T16:00:00", "2026-01-16"),
            ("daily", "America/New_York", "2026-03-08T05:00:00", "2026-03-09T04:00:00", "2026-03-08"),
            ("daily", "America/New_York", "2026-11-01T04:00:00", "2026-11-02T05:00:00", "2026-11-01"),
            ("monthly", "Asia/Taipei", "2026-01-31T16:00:00", "2026-02-28T16:00:00", "2026-02"),
            ("monthly", "America/New_York", "2026-03-01T05:00:00", "2026-04-01T04:00:00", "2026-03"),
        )
        for period_type, zone, start, end, expected in windows:
            with self.subTest(zone=zone, day=expected), patch.dict(os.environ, {"COST_TIMEZONE": zone}):
                self.conn.execute("DELETE FROM usage_costs")
                lower = datetime.datetime.fromisoformat(start)
                upper = datetime.datetime.fromisoformat(end)
                for index, instant in enumerate((lower - datetime.timedelta(microseconds=1), lower,
                                                  upper - datetime.timedelta(microseconds=1), upper)):
                    with patch.object(cost_tracker, "estimate_operation_cost_micro_usd", return_value=100 * 2**index):
                        self.usage_at(instant.isoformat())
                with at(start):
                    report = cost_tracker.get_cost_report(self.root, period_type)
                    self.assertEqual(report["period"], expected)
                    self.assertEqual(report["grand_total_estimated_usd"], 0.0006)
                    if period_type == "daily":
                        self.assertEqual(cost_tracker.get_budget_status(self.root)["spent_usd"], 0.0006)

    def test_invalid_timezone_fails_before_database_access(self):
        for zone in ("Not/AZone", "", "../UTC"):
            for entry in (cost_tracker.check_and_reserve_budget,
                          cost_tracker.get_budget_status, cost_tracker.get_cost_report):
                with self.subTest(zone=zone, entry=entry.__name__):
                    with patch.dict(os.environ, {"COST_TIMEZONE": zone}):
                        with patch.object(cost_tracker, "get_operations_db") as database:
                            with self.assertRaisesRegex(ValueError, "COST_TIMEZONE"):
                                if entry is cost_tracker.check_and_reserve_budget:
                                    entry(self.root, "test", 1000)
                                else:
                                    entry(self.root)
                            database.assert_not_called()
                    self.assertFalse((self.root / "operations.db").exists())

    def test_timezone_is_visible_in_reports_and_budget_events(self):
        with at("2026-01-15T16:30:00"):
            for output in (cost_tracker.get_budget_status(self.root),
                           cost_tracker.get_cost_report(self.root)):
                self.assertEqual(output["timezone"], "Asia/Taipei")
            self.assertFalse(cost_tracker.check_and_reserve_budget(self.root, "test", 2000)[0])
        event = self.conn.execute("SELECT payload_json FROM notification_events").fetchone()
        self.assertEqual(json.loads(event[0])["timezone"], "Asia/Taipei")

    def test_blocked_proposals_do_not_inflate_cost_reports(self):
        with patch.dict(os.environ, {"COST_TIMEZONE": "UTC"}), at("2026-01-15T12:00:00"):
            self.assertFalse(cost_tracker.check_and_reserve_budget(self.root, "test", 2000)[0])
            self.assertEqual(cost_tracker.get_cost_report(self.root)["grand_total_estimated_usd"], 0)
            self.assertEqual(cost_tracker.get_budget_status(self.root)["blocked_operations_today"], 1)


if __name__ == "__main__":
    unittest.main()
