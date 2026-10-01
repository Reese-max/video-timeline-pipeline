"""Regression contract: the cost guard must own every enabled paid transport (Issue #4).

C0/C8 acceptance: when the guard refuses a paid operation, the provider transport is
never dispatched (zero socket / SMTP / subprocess calls), and every dispatched
attempt is reserved before the send and settled (or kept outstanding as `unknown`)
afterwards.

The transports are exercised through their real public entry points so a guard
that exists but is not wired into the transport fails here.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import cost_tracker
import email_providers
import pipeline

BLOCKED_ENV = {"BUDGET_HARD_STOP": "true", "DAILY_BUDGET_USD": "0.000001"}
OPEN_ENV = {"BUDGET_HARD_STOP": "false", "DAILY_BUDGET_USD": "100"}


class FakeResponse:
    def __init__(self, payload: object, url: str = "https://example.test/") -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self._url = url

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._raw if size < 0 else self._raw[:size]

    def geturl(self) -> str:
        return self._url


def rows(root: Path, table: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(root / "operations.db")
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
    finally:
        conn.close()


class TransportBlockedTestCase(unittest.TestCase):
    """Every enabled transport must dispatch zero provider calls once budget is exhausted."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.env = patch.dict(os.environ, BLOCKED_ENV)
        self.env.start()
        self.scope = cost_tracker.cost_root_scope(self.root)
        self.scope.__enter__()
        self.urlopen = MagicMock(side_effect=AssertionError("transport dispatched while budget blocked"))
        self.patcher = patch.object(urllib.request, "urlopen", self.urlopen)
        self.patcher.start()
        self.smtp = MagicMock(side_effect=AssertionError("SMTP dispatched while budget blocked"))
        self.smtp_patch = patch.object(email_providers.smtplib, "SMTP", self.smtp)

    def tearDown(self) -> None:
        self.smtp_patch.stop()
        self.patcher.stop()
        self.scope.__exit__(None, None, None)
        self.env.stop()
        self.tmp_dir.cleanup()

    # --- Groq speech-to-text -------------------------------------------------
    def test_groq_transcription_blocked_makes_zero_transport_calls(self) -> None:
        audio = self.root / "chunk.wav"
        audio.write_bytes(b"RIFF" + b"\x00" * 32)
        with self.assertRaises(cost_tracker.BudgetBlockedError):
            pipeline.groq_transcribe(audio, "groq-key", "whisper-large-v3", "auto")
        self.urlopen.assert_not_called()

    # --- MiniMax chat (vision / summary / keywords / ask / digest / classify)
    def test_minimax_chat_blocked_makes_zero_transport_calls(self) -> None:
        os.environ["MINIMAX_BASE_URL"] = "https://api.minimax.io/v1"
        try:
            with self.assertRaises(cost_tracker.BudgetBlockedError):
                pipeline.minimax_chat([{"role": "user", "content": "hi"}], "mm-key", "MiniMax-M3")
        finally:
            os.environ.pop("MINIMAX_BASE_URL", None)
        self.urlopen.assert_not_called()

    # --- OpenRouter embeddings ---------------------------------------------
    def test_openrouter_embeddings_blocked_makes_zero_transport_calls(self) -> None:
        with self.assertRaises(cost_tracker.BudgetBlockedError):
            pipeline.embed_texts_openrouter(["alpha", "beta"], "or-key", "openai/text-embedding-3-small")
        self.urlopen.assert_not_called()

    # --- Meta Graph API ------------------------------------------------------
    def test_meta_graph_blocked_makes_zero_transport_calls(self) -> None:
        with self.assertRaises(cost_tracker.BudgetBlockedError):
            pipeline.facebook_graph_edge("123456", "videos", "meta-token", "v26.0", None)
        self.urlopen.assert_not_called()

    # --- Apify actor run start ----------------------------------------------
    def test_apify_actor_start_blocked_makes_zero_transport_calls(self) -> None:
        os.environ["APIFY_API_TOKEN"] = "apify-token"
        os.environ["APIFY_MAX_TOTAL_CHARGE_USD"] = "1.00"
        try:
            with self.assertRaises(cost_tracker.BudgetBlockedError):
                pipeline.fetch_facebook_author_apify("https://www.facebook.com/somepage", "365d")
        finally:
            os.environ.pop("APIFY_API_TOKEN", None)
            os.environ.pop("APIFY_MAX_TOTAL_CHARGE_USD", None)
        self.urlopen.assert_not_called()

    # --- Bright Data trigger -------------------------------------------------
    def test_brightdata_trigger_blocked_makes_zero_transport_calls(self) -> None:
        folder = self.root / "receipts"
        with self.assertRaises(cost_tracker.BudgetBlockedError):
            pipeline.submit_brightdata_once(
                folder,
                "https://api.brightdata.com/datasets/v3/trigger?dataset_id=x",
                {"Authorization": "Bearer bd-token", "Content-Type": "application/json"},
                json.dumps([{"url": "https://example.test/post"}]).encode("utf-8"),
            )
        self.urlopen.assert_not_called()
        # A refused reservation must not leave a submission marker that blocks
        # later reconciliation of work that was never sent.
        self.assertFalse((folder / ".submitted").exists())

    # --- Open Notebook source upload ----------------------------------------
    def test_open_notebook_sync_blocked_makes_zero_transport_calls(self) -> None:
        knowledge = self.root / "knowledge.md"
        knowledge.write_text("# notes\n", encoding="utf-8")
        with self.assertRaises(cost_tracker.BudgetBlockedError):
            pipeline.open_notebook_sync(
                knowledge,
                "http://127.0.0.1:8080",
                "nb-1",
                "password",
                "api/sources",
                1,
            )
        self.urlopen.assert_not_called()

    # --- RSS notify webhook --------------------------------------------------
    def test_rss_notify_webhook_blocked_makes_zero_transport_calls(self) -> None:
        with self.assertRaises(cost_tracker.BudgetBlockedError):
            pipeline.send_rss_notification(
                "https://hooks.example.test/rss",
                "failed",
                {"id": "item-1", "title": "t", "source_url": "https://example.test/v"},
            )
        self.urlopen.assert_not_called()

    # --- Email / webhook delivery providers ---------------------------------
    def test_resend_send_blocked_makes_zero_transport_calls(self) -> None:
        result = email_providers.ResendProvider(api_key="resend-key").send(
            "to@example.test", "subject", "text", None, "idem-1"
        )
        self.assertEqual(result.status, "permanent_failure")
        self.assertIn("Budget blocked", result.safe_error)
        self.urlopen.assert_not_called()

    def test_smtp_send_blocked_makes_zero_transport_calls(self) -> None:
        self.smtp_patch.start()
        result = email_providers.SmtpProvider(host="smtp.example.test", port=587).send(
            "to@example.test", "subject", "text", None, "idem-2"
        )
        self.assertEqual(result.status, "permanent_failure")
        self.assertIn("Budget blocked", result.safe_error)
        self.smtp.assert_not_called()

    def test_webhook_send_blocked_makes_zero_transport_calls(self) -> None:
        result = email_providers.WebhookProvider(webhook_url="https://hooks.example.test/x").send(
            "to@example.test", "subject", "text", None, "idem-3"
        )
        self.assertEqual(result.status, "permanent_failure")
        self.assertIn("Budget blocked", result.safe_error)
        self.urlopen.assert_not_called()


class GuardAccountingTests(unittest.TestCase):
    """A dispatched attempt reserves before the send and settles after it."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.env = patch.dict(os.environ, OPEN_ENV)
        self.env.start()
        self.scope = cost_tracker.cost_root_scope(self.root)
        self.scope.__enter__()
        os.environ["MINIMAX_BASE_URL"] = "https://api.minimax.io/v1"

    def tearDown(self) -> None:
        os.environ.pop("MINIMAX_BASE_URL", None)
        self.scope.__exit__(None, None, None)
        self.env.stop()
        self.tmp_dir.cleanup()

    @staticmethod
    def _ok_payload() -> dict:
        return {
            "choices": [{"finish_reason": "stop", "message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 120, "completion_tokens": 30},
        }

    def test_successful_call_is_recorded_and_reservation_settled(self) -> None:
        with patch.object(urllib.request, "urlopen", return_value=FakeResponse(self._ok_payload())):
            text = pipeline.minimax_chat([{"role": "user", "content": "hi"}], "mm-key", "MiniMax-M3")
        self.assertEqual(text, "hello")

        usage = rows(self.root, "usage_costs")
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["provider"], "minimax")
        self.assertEqual(usage[0]["status"], "succeeded")
        self.assertGreater(int(usage[0]["estimated_micro_usd"]), 0)

        reservations = rows(self.root, "budget_reservations")
        self.assertEqual(len(reservations), 1)
        self.assertEqual(reservations[0]["status"], "settled")

    def test_failed_call_records_failure_and_settles_reservation(self) -> None:
        error = urllib.error.HTTPError(
            "https://api.minimax.io/v1/chat/completions",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b'{"error":{"message":"invalid api key"}}'),
        )
        with patch.object(pipeline.time, "sleep", lambda *_: None), patch.object(
            urllib.request, "urlopen", side_effect=error
        ):
            with self.assertRaises(RuntimeError):
                pipeline.minimax_chat([{"role": "user", "content": "hi"}], "mm-key", "MiniMax-M3")

        usage = rows(self.root, "usage_costs")
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["status"], "failed")
        self.assertEqual(usage[0]["safe_error"], "auth")
        self.assertEqual(rows(self.root, "budget_reservations")[0]["status"], "settled")

    def test_timeout_keeps_reservation_outstanding_as_unknown(self) -> None:
        with patch.object(pipeline.time, "sleep", lambda *_: None), patch.object(
            urllib.request, "urlopen", side_effect=TimeoutError("read timed out")
        ):
            with self.assertRaises(RuntimeError):
                pipeline.minimax_chat([{"role": "user", "content": "hi"}], "mm-key", "MiniMax-M3")

        usage = rows(self.root, "usage_costs")
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["status"], "unknown")
        # An unproven attempt keeps its risk: the reservation must stay outstanding.
        self.assertEqual(rows(self.root, "budget_reservations")[0]["status"], "reserved")

    def test_remote_operation_id_is_kept_for_reconciliation(self) -> None:
        snapshot = "s_abc123"
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse({"snapshot_id": snapshot}, "https://api.brightdata.com/datasets/v3/trigger"),
        ):
            got = pipeline.submit_brightdata_once(
                self.root / "receipts",
                "https://api.brightdata.com/datasets/v3/trigger?dataset_id=x",
                {"Authorization": "Bearer bd-token", "Content-Type": "application/json"},
                json.dumps([{"url": "https://example.test/post"}]).encode("utf-8"),
            )
        self.assertEqual(got, snapshot)
        usage = rows(self.root, "usage_costs")
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["provider"], "brightdata")
        self.assertEqual(usage[0]["provider_request_id"], snapshot)


class UncertainSubmissionTests(unittest.TestCase):
    """A submitted Bright Data job that never answered stays unresolved, never "free"."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.env = patch.dict(os.environ, OPEN_ENV)
        self.env.start()
        self.scope = cost_tracker.cost_root_scope(self.root)
        self.scope.__enter__()

    def tearDown(self) -> None:
        self.scope.__exit__(None, None, None)
        self.env.stop()
        self.tmp_dir.cleanup()

    def test_timeout_after_submit_keeps_reservation_outstanding(self) -> None:
        folder = self.root / "receipts"
        with patch.object(pipeline.time, "sleep", lambda *_: None), patch.object(
            urllib.request, "urlopen", side_effect=TimeoutError("read timed out")
        ):
            with self.assertRaises(RuntimeError):
                pipeline.submit_brightdata_once(
                    folder,
                    "https://api.brightdata.com/datasets/v3/trigger?dataset_id=x",
                    {"Authorization": "Bearer bd-token", "Content-Type": "application/json"},
                    json.dumps([{"url": "https://example.test/post"}]).encode("utf-8"),
                )

        usage = rows(self.root, "usage_costs")
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["status"], "unknown")
        self.assertEqual(rows(self.root, "budget_reservations")[0]["status"], "reserved")
        # The receipt must survive so the snapshot can be reconciled instead of resubmitted.
        self.assertTrue((folder / "submission.json").exists())


class UnidentifiedSubmissionTests(unittest.TestCase):
    """An accepted submission without a usable snapshot id stays unknown, never settled."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.env = patch.dict(os.environ, OPEN_ENV)
        self.env.start()
        self.scope = cost_tracker.cost_root_scope(self.root)
        self.scope.__enter__()

    def tearDown(self) -> None:
        self.scope.__exit__(None, None, None)
        self.env.stop()
        self.tmp_dir.cleanup()

    def test_missing_snapshot_id_keeps_reservation_outstanding(self) -> None:
        folder = self.root / "receipts"
        with patch.object(
            urllib.request,
            "urlopen",
            return_value=FakeResponse({"status": "started"}, "https://api.brightdata.com/datasets/v3/trigger"),
        ):
            with self.assertRaises(RuntimeError) as caught:
                pipeline.submit_brightdata_once(
                    folder,
                    "https://api.brightdata.com/datasets/v3/trigger?dataset_id=x",
                    {"Authorization": "Bearer bd-token", "Content-Type": "application/json"},
                    json.dumps([{"url": "https://example.test/post"}]).encode("utf-8"),
                )

        self.assertIn("snapshot_id", str(caught.exception))
        usage = rows(self.root, "usage_costs")
        self.assertEqual(len(usage), 1)
        self.assertEqual(usage[0]["status"], "unknown")
        self.assertEqual(rows(self.root, "budget_reservations")[0]["status"], "reserved")
        self.assertTrue((folder / "submission.json").exists())


class NotificationEntryPointTests(unittest.TestCase):
    """Email delivery through a notifications entry point inherits the same guard."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_notify_test_email_blocked_makes_zero_transport_calls(self) -> None:
        import notifications

        urlopen = MagicMock(side_effect=AssertionError("Resend dispatched while budget blocked"))
        with patch.dict(os.environ, BLOCKED_ENV), patch.object(urllib.request, "urlopen", urlopen), (
            cost_tracker.cost_root_scope(self.root)
        ), patch.object(notifications, "get_configured_provider", return_value=email_providers.ResendProvider(api_key="resend-key")):
            result = notifications.run_notify_test(self.root)

        self.assertEqual(result["status"], "permanent_failure")
        self.assertIn("Budget blocked", str(result["safe_error"]))
        urlopen.assert_not_called()

    def test_notify_test_email_allowed_records_attempt(self) -> None:
        import notifications

        payload = FakeResponse({"id": "msg-1"})
        with patch.dict(os.environ, OPEN_ENV), patch.object(
            urllib.request, "urlopen", return_value=payload
        ), cost_tracker.cost_root_scope(self.root), patch.object(
            notifications, "get_configured_provider", return_value=email_providers.ResendProvider(api_key="resend-key")
        ):
            result = notifications.run_notify_test(self.root)

        self.assertEqual(result["status"], "accepted")
        usage = rows(self.root, "usage_costs")
        self.assertEqual([row["provider"] for row in usage], ["resend"])
        self.assertEqual(usage[0]["status"], "succeeded")
        self.assertEqual(rows(self.root, "budget_reservations")[0]["status"], "settled")


class GuardModeTests(unittest.TestCase):
    """Strict mode refuses what it cannot bound; the default stays transparent."""

    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_strict_mode_refuses_operations_without_a_verified_bound(self) -> None:
        with patch.dict(os.environ, BLOCKED_ENV), cost_tracker.cost_root_scope(self.root):
            with self.assertRaises(cost_tracker.BudgetBlockedError):
                with cost_tracker.provider_cost_guard("resend", "email.send"):
                    pass

    def test_strict_mode_refuses_transports_without_a_cost_root(self) -> None:
        with patch.dict(os.environ, BLOCKED_ENV), cost_tracker.cost_root_scope(None):
            with self.assertRaises(cost_tracker.BudgetBlockedError):
                with cost_tracker.provider_cost_guard("minimax", "summarize"):
                    pass

    def test_default_mode_is_transparent_and_persists_nothing(self) -> None:
        calls: list[str] = []
        with patch.dict(os.environ, {"BUDGET_HARD_STOP": "false", "DAILY_BUDGET_USD": "0"}), (
            patch.dict(os.environ, {}, clear=False)
        ), cost_tracker.cost_root_scope(None):
            with cost_tracker.provider_cost_guard("minimax", "summarize") as outcome:
                calls.append("dispatched")
                self.assertFalse(outcome.enforced)
                self.assertIsNone(outcome.reservation_id)
        self.assertEqual(calls, ["dispatched"])

    def test_unknown_cost_is_reported_as_unknown_not_zero(self) -> None:
        cost_id = cost_tracker.record_usage(
            self.root,
            provider="resend",
            operation="email.send",
            status="succeeded",
        )
        self.assertTrue(cost_id)
        row = rows(self.root, "usage_costs")[0]
        self.assertEqual(row["cost_status"], "unknown")


if __name__ == "__main__":
    unittest.main()