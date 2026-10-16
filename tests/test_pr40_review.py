"""Offline regression checks for PR 40 review findings."""
import concurrent.futures
import contextlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import cost_tracker
import email_providers
import notifications
import pipeline
from test_cost_guard_transports import FakeResponse, rows


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        stack.enter_context(patch.dict(os.environ, {
            **{key: os.environ[key] for key in ("USERNAME", "PATH", "SYSTEMROOT") if key in os.environ},
            "BUDGET_HARD_STOP": "false", "DAILY_BUDGET_USD": "100",
            "APIFY_API_TOKEN": "offline-token", "APIFY_MAX_TOTAL_CHARGE_USD": "1.00",
            "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
        }, clear=True))
        stack.enter_context(cost_tracker.cost_root_scope(self.root))
        stack.enter_context(patch.object(pipeline.time, "sleep", return_value=None))

    def apify(self):
        return pipeline.fetch_facebook_author_apify("example", "365d")

    def test_apify_results_can_be_persisted_as_json(self):
        with patch.object(pipeline.urllib.request, "urlopen", side_effect=[
            FakeResponse({"data": {"id": "run-1", "status": "SUCCEEDED", "defaultDatasetId": "data-1"}}),
            FakeResponse([]),
        ]):
            feed = self.apify()
        saved = pipeline.write_facebook_discovery(self.root, feed)
        self.assertEqual(json.loads(saved.read_text(encoding="utf-8"))["facebook"]["max_total_charge_usd"], 1.0)

    def test_apify_uncertain_creation_is_never_automatically_replayed(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="1")
        with patch.object(pipeline.urllib.request, "urlopen", side_effect=TimeoutError("read timed out")) as transport:
            with self.assertRaises(RuntimeError):
                self.apify()
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(rows(self.root, "usage_costs")[0]["status"], "unknown")
        self.assertEqual(rows(self.root, "budget_reservations")[0]["status"], "reserved")

    def test_each_retry_has_its_own_attempt_and_reservation(self):
        audio = self.root / "audio.wav"
        audio.write_bytes(b"RIFF" + b"\0" * 32)
        calls = [
            (lambda: pipeline.groq_transcribe(audio, "key", "whisper-large-v3", "auto"), {"text": "hello"}),
            (lambda: pipeline.minimax_chat([{"role": "user", "content": "hi"}], "key", "MiniMax-M3"),
             {"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}]}),
            (lambda: pipeline.embed_texts_openrouter(["hello"], "key", "model"),
             {"data": [{"index": 0, "embedding": [0.1, 0.2]}]}),
            (lambda: pipeline.facebook_graph_payload("https://graph.facebook.com/test", "key"), {"data": []}),
        ]
        for index, (call, payload) in enumerate(calls):
            with self.subTest(provider=index):
                root = self.root / str(index)
                root.mkdir()
                with cost_tracker.cost_root_scope(root), patch.object(
                    pipeline.urllib.request, "urlopen", side_effect=[TimeoutError("read timed out"), FakeResponse(payload)]
                ) as transport:
                    call()
                self.assertEqual(transport.call_count, 2)
                self.assertEqual([r["status"] for r in rows(root, "usage_costs")], ["unknown", "succeeded"])
                self.assertEqual([r["status"] for r in rows(root, "budget_reservations")], ["reserved", "settled"])

    def test_notebook_budget_rejection_preserves_existing_source(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="1")
        notes = self.root / "notes.md"
        notes.write_text("notes", encoding="utf-8")
        with patch.object(pipeline.urllib.request, "urlopen", return_value=FakeResponse({})) as transport:
            with self.assertRaises(cost_tracker.BudgetBlockedError):
                pipeline.open_notebook_sync(notes, "https://notebook.test", "nb", "key", "api/sources", 0,
                                            replace_source_id="existing-source")
        transport.assert_not_called()

    def test_notification_cli_loads_project_and_root_budget_settings(self):
        for env_location in ("project", "root"):
            with self.subTest(env_location=env_location), patch.dict(os.environ):
                project, root = self.root / (env_location + "-project"), self.root / (env_location + "-data")
                project.mkdir()
                root.mkdir()
                (project if env_location == "project" else root).joinpath(".env").write_text(
                    "BUDGET_HARD_STOP=true\nDAILY_BUDGET_USD=1\n", encoding="utf-8")
                with patch.object(pipeline, "__file__", str(project / "pipeline.py")), patch.object(
                    pipeline.sys, "argv", ["pipeline.py", "--root", str(root), "--notify-test"]
                ), patch.object(notifications, "get_configured_provider", return_value=email_providers.ResendProvider("key")), patch.object(
                    pipeline.urllib.request, "urlopen", return_value=FakeResponse({"id": "message-1"})
                ) as transport:
                    self.assertEqual(pipeline.main(), 1)
                transport.assert_not_called()

    def test_concurrent_reservations_cannot_spend_the_same_balance(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="1")
        with contextlib.closing(cost_tracker.get_operations_db(self.root)) as conn:
            cost_tracker._init_cost_schema(conn)
        reads = threading.Barrier(2)

        class Cursor(sqlite3.Cursor):
            def execute(self, sql, parameters=()):
                self.balance_read = "SUM(max_micro_usd)" in sql
                return super().execute(sql, parameters)

            def fetchone(self):
                result = super().fetchone()
                if self.balance_read:
                    try:
                        reads.wait(timeout=0.5)
                    except threading.BrokenBarrierError:
                        pass
                return result

        class Connection(sqlite3.Connection):
            def cursor(self):
                return super().cursor(factory=Cursor)

        def connect(_root):
            conn = sqlite3.connect(self.root / "operations.db", factory=Connection, timeout=5)
            conn.row_factory = sqlite3.Row
            return conn

        with patch.object(cost_tracker, "get_operations_db", side_effect=connect), concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda _: cost_tracker.check_and_reserve_budget(self.root, "apify/start", 600_000), range(2)))
        self.assertEqual(sum(allowed for allowed, _, _ in results), 1)
        self.assertEqual(sum(r["max_micro_usd"] for r in rows(self.root, "budget_reservations")), 600_000)

    def test_strict_configuration_cannot_disable_the_actual_budget_check(self):
        for flag, limit in [(" true ", "0.5"), ("true", "0.0000001")]:
            with self.subTest(flag=flag, limit=limit), patch.dict(os.environ, {"BUDGET_HARD_STOP": flag, "DAILY_BUDGET_USD": limit}):
                with self.assertRaises(cost_tracker.BudgetBlockedError):
                    with cost_tracker.provider_cost_guard("apify", "start", provider_enforced_upper_bound_micro_usd=1_000_000):
                        self.fail("over-budget transport was authorized")

    def test_unknown_attempt_uses_its_reserved_budget_only_once(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="2")
        with self.assertRaises(TimeoutError):
            with cost_tracker.provider_cost_guard("apify", "start", provider_enforced_upper_bound_micro_usd=1_000_000):
                raise TimeoutError("read timed out")
        with cost_tracker.provider_cost_guard("apify", "start", provider_enforced_upper_bound_micro_usd=1_000_000):
            pass
        self.assertEqual([r["status"] for r in rows(self.root, "budget_reservations")], ["reserved", "settled"])

    def test_existing_ledger_migration_preserves_prior_usage(self):
        cost_id = cost_tracker.record_usage(self.root, "minimax", "chat", estimated_micro_usd=123)
        with contextlib.closing(sqlite3.connect(self.root / "operations.db")) as conn:
            conn.execute("ALTER TABLE usage_costs DROP COLUMN reservation_id")
        cost_tracker.record_usage(self.root, "minimax", "chat", estimated_micro_usd=456)
        usage = rows(self.root, "usage_costs")
        self.assertEqual([r["estimated_micro_usd"] for r in usage], [123, 456])
        self.assertEqual(usage[0]["cost_id"], cost_id)
        self.assertIsNone(usage[0]["reservation_id"])

    def test_unusable_response_cannot_settle_unknown_work_from_diagnostic_words(self):
        with patch.object(pipeline.urllib.request, "urlopen", return_value=FakeResponse(["quota_warning"])):
            with self.assertRaises(RuntimeError):
                pipeline.submit_brightdata_once(self.root / "receipt", "https://brightdata.test/trigger", {}, b"[]")
        self.assertEqual(rows(self.root, "usage_costs")[0]["status"], "unknown")
        self.assertEqual(rows(self.root, "budget_reservations")[0]["status"], "reserved")

    def test_local_marker_failure_records_no_cloud_charge(self):
        real_open = Path.open

        def open_file(path, *args, **kwargs):
            if path.name == ".submitted":
                raise PermissionError("offline marker failure")
            return real_open(path, *args, **kwargs)

        with patch.object(Path, "open", open_file), patch.object(pipeline.urllib.request, "urlopen") as transport:
            with self.assertRaises(PermissionError):
                pipeline.submit_brightdata_once(self.root / "receipt", "https://brightdata.test/trigger", {}, b"[]")
        transport.assert_not_called()
        usage = rows(self.root, "usage_costs")[0]
        self.assertEqual((usage["status"], usage["estimated_micro_usd"]), ("failed", 0))
        self.assertEqual(rows(self.root, "budget_reservations")[0]["status"], "settled")

    def test_minimax_records_returned_token_usage(self):
        payload = {"choices": [{"finish_reason": "stop", "message": {"content": "hello"}}],
                   "usage": {"prompt_tokens": 1000, "completion_tokens": 1000}}
        with patch.object(pipeline.urllib.request, "urlopen", return_value=FakeResponse(payload)):
            pipeline.minimax_chat([{"role": "user", "content": "hi"}], "key", "MiniMax-M3")
        usage = rows(self.root, "usage_costs")[0]
        self.assertEqual((usage["request_units"], usage["response_units"]), (1000, 1000))
        self.assertEqual(usage["estimated_micro_usd"], 1500)

    def test_known_usage_increases_unresolved_risk_before_next_strict_run(self):
        payload = {"usage": {"prompt_tokens": 1000, "completion_tokens": 1000}}
        with patch.object(pipeline.urllib.request, "urlopen", return_value=FakeResponse(payload)), patch.object(
            pipeline, "read_response", return_value=json.dumps(payload).encode()
        ), patch.object(pipeline.OperationBudget, "check", side_effect=[None, None, pipeline.DeadlineExceeded("API deadline")]):
            with self.assertRaises(pipeline.DeadlineExceeded):
                pipeline.minimax_chat([{"role": "user", "content": "hi"}], "key", "MiniMax-M3")
        self.assertEqual(rows(self.root, "usage_costs")[0]["estimated_micro_usd"], 1500)
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="0.001")
        with patch.object(cost_tracker, "_current_period_id", return_value="2099-01-01"):
            self.assertFalse(cost_tracker.check_and_reserve_budget(self.root, "apify/start", 500)[0])
        self.assertEqual(rows(self.root, "budget_reservations")[0]["max_micro_usd"], 1500)

    def test_unresolved_reservations_remain_risk_after_midnight(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="1")
        with patch.object(cost_tracker, "_current_period_id", side_effect=["2026-10-16", "2026-10-17"]):
            self.assertTrue(cost_tracker.check_and_reserve_budget(self.root, "start", 600_000)[0])
            self.assertFalse(cost_tracker.check_and_reserve_budget(self.root, "start", 600_000)[0])

    def test_strict_ledger_failure_dispatches_no_apify_transport(self):
        os.environ.update(BUDGET_HARD_STOP="true", DAILY_BUDGET_USD="1")
        with patch.object(cost_tracker, "get_operations_db", side_effect=sqlite3.OperationalError("offline disk full")), patch.object(
            pipeline.urllib.request, "urlopen"
        ) as transport:
            with self.assertRaises(cost_tracker.BudgetBlockedError):
                self.apify()
        transport.assert_not_called()

    def test_builtin_self_test_remains_offline_with_guarded_transport(self):
        with cost_tracker.cost_root_scope(None), patch.object(
            pipeline.urllib.request, "urlopen", side_effect=AssertionError("unexpected real transport")
        ) as transport:
            pipeline.self_test()
        transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
