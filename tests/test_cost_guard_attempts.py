"""Offline regressions at real provider entrypoints, with temporary ledgers."""
from __future__ import annotations

import concurrent.futures
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd()))
import cost_tracker
import pipeline


class Response:
    def __init__(self, payload):
        self.stream = io.BytesIO(json.dumps(payload).encode())

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, size=-1):
        return self.stream.read(size)


def rows(root, table):
    with closing(sqlite3.connect(root / "operations.db")) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute("SELECT * FROM " + table + " ORDER BY rowid")]


class CostAttemptTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {
            "BUDGET_HARD_STOP": "false", "DAILY_BUDGET_USD": "100",
            "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
            "APIFY_API_TOKEN": "offline-placeholder", "APIFY_MAX_TOTAL_CHARGE_USD": "0.75",
        }, clear=True)
        self.environment.start()
        self.scope = cost_tracker.cost_root_scope(self.root)
        self.scope.__enter__()

    def tearDown(self):
        self.scope.__exit__(None, None, None)
        self.environment.stop()
        self.directory.cleanup()

    def test_concurrent_apify_runs_cannot_approve_same_budget_balance(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="1")
        cost_tracker.check_and_reserve_budget(self.root, "schema-prime", 0)
        barrier = threading.Barrier(2)
        local = threading.local()
        original_connect = cost_tracker.get_operations_db
        original_now = cost_tracker._utc_now_iso

        def connect(root):
            connection = original_connect(root)
            local.connection = connection
            return connection

        def scheduled_now():
            # Force concurrent readers to reach the insert together if they
            # have no protecting transaction. A held writer transaction lets
            # the first reservation commit while the second worker waits.
            if not getattr(local, "timestamp_seen", False):
                local.timestamp_seen = True
                if not local.connection.in_transaction:
                    barrier.wait(timeout=5)
            return original_now()

        def transport(request, **_):
            if request.get_method() == "POST":
                return Response({"data": {"id": "offline-run", "status": "SUCCEEDED",
                                          "defaultDatasetId": "offline-dataset"}})
            return Response([])

        def run(_):
            try:
                pipeline.fetch_facebook_author_apify("https://www.facebook.com/offline-page", "365d")
                return "allowed"
            except cost_tracker.BudgetBlockedError:
                return "blocked"

        with patch.object(cost_tracker, "get_operations_db", connect), \
                patch.object(cost_tracker, "_utc_now_iso", scheduled_now), \
                patch.object(urllib.request, "urlopen", side_effect=transport) as sender:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(run, range(2)))
        self.assertCountEqual(outcomes, ["allowed", "blocked"])
        self.assertEqual(sum(call.args[0].get_method() == "POST" for call in sender.call_args_list), 1)
        spend = sum(row["estimated_micro_usd"] for row in rows(self.root, "usage_costs")
                    if row["status"] != "blocked_budget")
        self.assertEqual(spend, 750_000)

    def test_timeout_then_success_records_two_actual_attempts_without_logical_double_charge(self):
        responses = [TimeoutError("offline uncertain timeout"), Response({
            "choices": [{"finish_reason": "stop", "message": {"content": "offline answer"}}]
        })]
        with patch.object(urllib.request, "urlopen", side_effect=responses) as sender, \
                patch.object(pipeline.OperationBudget, "sleep", lambda *_: None):
            self.assertEqual(pipeline.minimax_chat([{"role": "user", "content": "test"}],
                                                 "offline-placeholder", "MiniMax-M3"), "offline answer")
        usage = rows(self.root, "usage_costs")
        reservations = rows(self.root, "budget_reservations")
        self.assertEqual(sender.call_count, 2)
        self.assertEqual([row["status"] for row in usage], ["unknown", "succeeded"])
        self.assertEqual([row["attempt_no"] for row in usage], [1, 2])
        self.assertEqual(len({row["logical_operation_id"] for row in usage}), 1)
        self.assertIsNotNone(usage[0]["logical_operation_id"])
        self.assertEqual([row["status"] for row in reservations], ["reserved", "settled"])
        self.assertEqual([row["reservation_id"] for row in usage],
                         [row["reservation_id"] for row in reservations])

    def test_four_uncertain_retries_keep_four_risk_reservations(self):
        with patch.object(urllib.request, "urlopen", side_effect=TimeoutError("offline timeout")) as sender, \
                patch.object(pipeline.OperationBudget, "sleep", lambda *_: None):
            with self.assertRaises(pipeline.ApiRequestError):
                pipeline.minimax_chat([{"role": "user", "content": "test"}], "offline", "MiniMax-M3")
        self.assertEqual(sender.call_count, 4)
        self.assertEqual([row["status"] for row in rows(self.root, "usage_costs")], ["unknown"] * 4)
        self.assertEqual([row["status"] for row in rows(self.root, "budget_reservations")], ["reserved"] * 4)

    def test_brightdata_503_keeps_uncertain_submission_and_never_retriggers(self):
        error = urllib.error.HTTPError("https://api.brightdata.com/datasets/v3/trigger", 503,
                                       "Unavailable", {}, io.BytesIO(b'{"error":"offline"}'))
        folder = self.root / "brightdata-receipt"
        with patch.object(urllib.request, "urlopen", side_effect=error) as sender:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    pipeline.submit_brightdata_once(folder, error.url, {}, b'[{"url":"https://example.test/p"}]')
        self.assertEqual(sender.call_count, 1)
        self.assertEqual([row["status"] for row in rows(self.root, "usage_costs")], ["unknown"])
        self.assertEqual([row["status"] for row in rows(self.root, "budget_reservations")], ["reserved"])
        self.assertTrue((folder / "submission.json").exists())

    def test_apify_unknown_risk_is_counted_once_when_reserving_next_run(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="1")
        error = urllib.error.HTTPError("https://api.apify.com/v2/actors/offline/runs", 503,
                                       "Unavailable", {}, io.BytesIO(b'{"error":"offline"}'))
        with patch.object(urllib.request, "urlopen", side_effect=error):
            with self.assertRaises(RuntimeError):
                pipeline.fetch_facebook_author_apify("https://www.facebook.com/offline-page", "365d")
        os.environ["APIFY_MAX_TOTAL_CHARGE_USD"] = "0.20"
        responses = [Response({"data": {"id": "run-2", "status": "SUCCEEDED", "defaultDatasetId": "ds-2"}}),
                     Response([])]
        with patch.object(urllib.request, "urlopen", side_effect=responses) as sender:
            self.assertEqual(pipeline.fetch_facebook_author_apify(
                "https://www.facebook.com/offline-page", "365d")["items"], [])
        self.assertEqual(sender.call_count, 2)
        self.assertEqual([row["status"] for row in rows(self.root, "usage_costs")], ["unknown", "succeeded"])
        self.assertEqual([row["status"] for row in rows(self.root, "budget_reservations")], ["reserved", "settled"])

    def test_strict_notebook_replacement_is_refused_before_delete(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="1")
        knowledge = self.root / "knowledge.md"
        knowledge.write_text("offline knowledge", encoding="utf-8")
        with patch.object(urllib.request, "urlopen", return_value=Response({})) as sender:
            with self.assertRaises(cost_tracker.BudgetBlockedError):
                pipeline.open_notebook_sync(knowledge, "https://notebook.example.test", "nb", "offline",
                                            "api/sources", 0, replace_source_id="existing-source")
        sender.assert_not_called()

    def test_soft_notebook_delete_and_upload_are_separate_recorded_attempts(self):
        knowledge = self.root / "knowledge.md"
        knowledge.write_text("offline knowledge", encoding="utf-8")
        with patch.object(urllib.request, "urlopen", side_effect=[Response({}), Response({"id": "new-source"})]) as sender:
            result = pipeline.open_notebook_sync(knowledge, "https://notebook.example.test", "nb", "offline",
                                                  "api/sources", 0, replace_source_id="old-source")
        self.assertTrue(result["replaced"])
        self.assertEqual([call.args[0].get_method() for call in sender.call_args_list], ["DELETE", "POST"])
        usage = rows(self.root, "usage_costs")
        self.assertEqual([row["attempt_no"] for row in usage], [1, 2])
        self.assertEqual([row["status"] for row in usage], ["succeeded", "succeeded"])
        self.assertEqual(usage[-1]["provider_request_id"], "new-source")

    def test_cached_summary_records_zero_local_reuse_without_another_request(self):
        content = json.dumps({"short_summary": "offline summary"})
        response = Response({"choices": [{"finish_reason": "stop", "message": {"content": content}}]})
        cache = self.root / "summary-cache"
        cache.mkdir()
        with patch.object(urllib.request, "urlopen", return_value=response) as sender:
            first = pipeline.cached_minimax_summary(cache, "partial", [], "offline", "MiniMax-M3", partial=True)
            second = pipeline.cached_minimax_summary(cache, "partial", [], "offline", "MiniMax-M3", partial=True)
        self.assertEqual(first, second)
        self.assertEqual(sender.call_count, 1)
        usage = rows(self.root, "usage_costs")
        self.assertEqual([row["status"] for row in usage], ["succeeded", "cache_hit"])
        self.assertEqual(usage[1]["estimated_micro_usd"], 0)
        self.assertEqual(usage[1]["cost_status"], "zero_local")
        self.assertEqual(len(rows(self.root, "budget_reservations")), 1)
        report = cost_tracker.get_cost_report(self.root)
        self.assertEqual(report["providers"][0]["cache_hits"], 1)

    def test_paid_transport_holds_no_sqlite_writer_lock(self):
        def transport(_request, **_):
            with closing(sqlite3.connect(self.root / "operations.db", timeout=0)) as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            return Response({"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]})
        with patch.object(urllib.request, "urlopen", side_effect=transport):
            self.assertEqual(pipeline.minimax_chat([{"role": "user", "content": "test"}],
                                                 "offline", "MiniMax-M3"), "ok")

    def test_strict_retry_ledger_failure_cannot_dispatch_second_request(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="1")
        reserve = cost_tracker.check_and_reserve_budget
        calls = 0

        def reserve_then_fail(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise sqlite3.OperationalError("offline readonly ledger")
            return reserve(*args, **kwargs)

        with patch.object(cost_tracker, "check_and_reserve_budget", reserve_then_fail), \
                patch.object(urllib.request, "urlopen", side_effect=TimeoutError("offline timeout")) as sender, \
                patch.object(pipeline.OperationBudget, "sleep", lambda *_: None):
            with self.assertRaises(cost_tracker.BudgetBlockedError):
                with cost_tracker.provider_cost_guard("apify", "actor.run.start",
                        estimated_micro_usd=200_000, provider_enforced_upper_bound_micro_usd=200_000):
                    pipeline.request_json("https://api.apify.com/v2/actors/offline/runs", {},
                                          method="POST", body=b"{}", timeout=1, attempts=2)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual([row["status"] for row in rows(self.root, "usage_costs")], ["unknown"])
        self.assertEqual([row["status"] for row in rows(self.root, "budget_reservations")], ["reserved"])

    def test_legacy_schema_migration_preserves_cost_history(self):
        cost_tracker.record_usage(self.root, provider="minimax", operation="legacy",
                                  estimated_micro_usd=123, status="succeeded")
        before = rows(self.root, "usage_costs")
        with closing(sqlite3.connect(self.root / "operations.db")) as connection, connection:
            connection.execute("ALTER TABLE usage_costs DROP COLUMN reservation_id")
        report = cost_tracker.get_cost_report(self.root)
        after = rows(self.root, "usage_costs")
        self.assertEqual(before, after)
        self.assertEqual(report["providers"][0]["calls"], 1)
        self.assertEqual(after[0]["estimated_micro_usd"], 123)

    def test_provider_quota_and_permission_are_classified_after_body_redaction(self):
        for code, body, category in [
            (429, {"error": {"code": "insufficient_quota", "detail": "offline-placeholder"}}, "quota_exhausted"),
            (403, {"error": {"code": "insufficient_scope", "detail": "offline-placeholder"}}, "permission"),
        ]:
            with self.subTest(category=category), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                error = urllib.error.HTTPError("https://api.minimax.io/v1/chat/completions", code,
                    "Provider rejected", {"Retry-After": "0"}, io.BytesIO(json.dumps(body).encode()))
                with cost_tracker.cost_root_scope(root), \
                        patch.object(urllib.request, "urlopen", side_effect=error) as sender, \
                        patch.object(pipeline.OperationBudget, "sleep", lambda *_: None):
                    with self.assertRaises(pipeline.ApiRequestError) as failure:
                        pipeline.minimax_chat([{"role": "user", "content": "test"}],
                                             "offline-placeholder", "MiniMax-M3")
                self.assertEqual(sender.call_count, 1)
                self.assertNotIn("offline-placeholder", failure.exception.response_body)
                usage = rows(root, "usage_costs")
                self.assertEqual([row["safe_error"] for row in usage], [category])
                self.assertEqual([row["status"] for row in usage], ["failed"])

    def test_transient_rate_limit_preserves_retry_after_and_separate_attempts(self):
        error = urllib.error.HTTPError("https://api.minimax.io/v1/chat/completions", 429,
            "Rate limited", {"Retry-After": "7"}, io.BytesIO(b'{"error":{"code":"rate_limit"}}'))
        result = Response({"choices": [{"finish_reason": "stop", "message": {"content": "ok"}}]})
        with patch.object(urllib.request, "urlopen", side_effect=[error, result]) as sender, \
                patch.object(pipeline.OperationBudget, "sleep") as sleep:
            self.assertEqual(pipeline.minimax_chat([{"role": "user", "content": "test"}],
                                                 "offline-placeholder", "MiniMax-M3"), "ok")
        self.assertEqual(sender.call_count, 2)
        sleep.assert_called_once_with(7.0)
        self.assertEqual([row["safe_error"] for row in rows(self.root, "usage_costs")], ["rate_limit", None])

    def test_budget_status_exposes_outstanding_and_available_risk(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="1")
        allowed, reservation, _ = cost_tracker.check_and_reserve_budget(self.root, "offline", 750_000)
        self.assertTrue(allowed)
        status = cost_tracker.get_budget_status(self.root)
        self.assertEqual(status["outstanding_micro_usd"], 750_000)
        self.assertEqual(status["projected_micro_usd"], 750_000)
        self.assertEqual(status["available_micro_usd"], 250_000)
        self.assertEqual(status["remaining_usd"], 0.25)
        self.assertFalse(cost_tracker.check_and_reserve_budget(self.root, "next", 250_001)[0])
        cost_tracker.record_usage(self.root, provider="apify", operation="offline", status="unknown",
                                  estimated_micro_usd=750_000, reservation_id=reservation, settle_reservation=False)
        unknown = cost_tracker.get_budget_status(self.root)
        self.assertEqual(unknown["spent_micro_usd"], 0)
        self.assertEqual(unknown["outstanding_micro_usd"], 750_000)
        self.assertEqual(unknown["projected_micro_usd"], 750_000)
        self.assertEqual(unknown["remaining_usd"], 0.25)

    def test_positive_submicro_budget_is_not_treated_as_unlimited(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="0.0000001")
        with patch.object(urllib.request, "urlopen", side_effect=[
            Response({"data": {"id": "offline", "status": "SUCCEEDED", "defaultDatasetId": "offline"}}),
            Response([]),
        ]) as sender:
            with self.assertRaises(cost_tracker.BudgetBlockedError):
                pipeline.fetch_facebook_author_apify("https://www.facebook.com/offline-page", "365d")
        sender.assert_not_called()
        self.assertEqual(cost_tracker.get_budget_status(self.root)["available_micro_usd"], 0)

    def test_strict_flag_normalization_matches_guard_budget_and_status(self):
        for flag, strict in [(" true ", True), (" TRUE ", True), (" 1 ", True), (" Yes ", True),
                             (" false ", False), (" FALSE ", False), (" 0 ", False), (" No ", False)]:
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, {"BUDGET_HARD_STOP": flag, "DAILY_BUDGET_USD": "0.000001"}), \
                    cost_tracker.cost_root_scope(Path(directory)):
                root = Path(directory)
                self.assertEqual(cost_tracker.guard_is_strict(), strict)
                responses = [Response({"data": {"id": "offline", "status": "SUCCEEDED",
                                                 "defaultDatasetId": "offline"}}), Response([])]
                with patch.object(urllib.request, "urlopen", side_effect=responses) as sender:
                    if strict:
                        with self.assertRaises(cost_tracker.BudgetBlockedError):
                            pipeline.fetch_facebook_author_apify("https://www.facebook.com/offline-page", "365d")
                        sender.assert_not_called()
                    else:
                        self.assertEqual(pipeline.fetch_facebook_author_apify(
                            "https://www.facebook.com/offline-page", "365d")["items"], [])
                        self.assertEqual(sender.call_count, 2)
                self.assertEqual(cost_tracker.get_budget_status(root)["hard_stop_enabled"], strict)


if __name__ == "__main__":
    unittest.main()
