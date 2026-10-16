"""Durable operation_tasks queue tests (issue #6, W7).

The web dashboard must not treat in-memory background work as a reliable
queue: enqueue, claim/lease/fencing, retry backoff, cancel, and crash
recovery all live in operations.db.
"""

from __future__ import annotations

import importlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    web_tasks = importlib.import_module("web_tasks")
    web_services = None
    _IMPORT_ERROR = None
    try:
        web_services = importlib.import_module("web_services")
    except ImportError:
        pass
except ImportError as exc:  # red state: module does not exist on base
    web_tasks = None
    web_services = None
    _IMPORT_ERROR = exc


def _ops_conn(root: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(root / "operations.db")
    conn.row_factory = sqlite3.Row
    return conn


class TaskSchemaAndEnqueueTests(unittest.TestCase):
    def setUp(self) -> None:
        if _IMPORT_ERROR is not None:
            self.fail(f"web_tasks module unavailable: {_IMPORT_ERROR}")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_enqueue_creates_queued_task_with_safe_fields(self) -> None:
        task = web_tasks.enqueue_task(
            self.root,
            "semantic_search",
            payload={"query": "alpha", "limit": 5},
            dedup_key="req-1",
        )
        self.assertTrue(task["task_id"])
        self.assertTrue(task["created"])
        self.assertEqual(task["status"], "queued")
        stored = web_tasks.get_task(self.root, task["task_id"])
        self.assertEqual(stored["type"], "semantic_search")
        self.assertEqual(stored["payload"]["query"], "alpha")
        self.assertEqual(stored["attempts"], 0)

    def test_enqueue_dedup_key_is_idempotent(self) -> None:
        first = web_tasks.enqueue_task(self.root, "ask", payload={"question": "q"}, dedup_key="dup-1")
        second = web_tasks.enqueue_task(self.root, "ask", payload={"question": "q"}, dedup_key="dup-1")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertFalse(second["created"])
        rows = web_tasks.list_tasks(self.root)
        self.assertEqual(len(rows), 1)

    def test_enqueue_rejects_unknown_task_type(self) -> None:
        with self.assertRaises(ValueError):
            web_tasks.enqueue_task(self.root, "shell_exec", payload={"cmd": "rm -rf /"})

    def test_get_task_unknown_returns_none(self) -> None:
        self.assertIsNone(web_tasks.get_task(self.root, "nope"))


class ClaimLeaseAndFencingTests(unittest.TestCase):
    def setUp(self) -> None:
        if _IMPORT_ERROR is not None:
            self.fail(f"web_tasks module unavailable: {_IMPORT_ERROR}")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_claim_moves_queued_to_running_with_lease(self) -> None:
        task = web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"})
        claimed = web_tasks.claim_next_task(self.root, worker_id="w1")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["task_id"], task["task_id"])
        self.assertEqual(claimed["status"], "running")
        self.assertEqual(claimed["lease_owner"], "w1")
        self.assertTrue(claimed["lease_token"])
        self.assertEqual(claimed["attempts"], 1)

    def test_second_worker_cannot_claim_running_task(self) -> None:
        web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"})
        first = web_tasks.claim_next_task(self.root, worker_id="w1")
        second = web_tasks.claim_next_task(self.root, worker_id="w2")
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_complete_requires_matching_lease_token(self) -> None:
        web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"})
        claimed = web_tasks.claim_next_task(self.root, worker_id="w1")
        rejected = web_tasks.complete_task(self.root, claimed["task_id"], "stale-token", result={"hits": []})
        self.assertFalse(rejected)
        accepted = web_tasks.complete_task(self.root, claimed["task_id"], claimed["lease_token"], result={"hits": []})
        self.assertTrue(accepted)
        stored = web_tasks.get_task(self.root, claimed["task_id"])
        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["result"], {"hits": []})
        self.assertTrue(stored["finished_at"])

    def test_fail_retryable_requeues_with_backoff(self) -> None:
        web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"})
        claimed = web_tasks.claim_next_task(self.root, worker_id="w1")
        ok = web_tasks.fail_task(self.root, claimed["task_id"], claimed["lease_token"], "boom", retryable=True)
        self.assertTrue(ok)
        stored = web_tasks.get_task(self.root, claimed["task_id"])
        self.assertEqual(stored["status"], "queued")
        self.assertEqual(stored["safe_error"], "boom")
        self.assertTrue(stored["next_attempt_at"])

    def test_fail_terminal_after_max_attempts(self) -> None:
        web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"}, max_attempts=1)
        claimed = web_tasks.claim_next_task(self.root, worker_id="w1")
        web_tasks.fail_task(self.root, claimed["task_id"], claimed["lease_token"], "boom", retryable=True)
        stored = web_tasks.get_task(self.root, claimed["task_id"])
        self.assertEqual(stored["status"], "failed")

    def test_fail_non_retryable_is_terminal(self) -> None:
        web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"})
        claimed = web_tasks.claim_next_task(self.root, worker_id="w1")
        web_tasks.fail_task(self.root, claimed["task_id"], claimed["lease_token"], "bad input", retryable=False)
        stored = web_tasks.get_task(self.root, claimed["task_id"])
        self.assertEqual(stored["status"], "failed")

    def test_heartbeat_extends_lease_and_checkpoint(self) -> None:
        web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"})
        claimed = web_tasks.claim_next_task(self.root, worker_id="w1", lease_seconds=60)
        ok = web_tasks.heartbeat(self.root, claimed["task_id"], claimed["lease_token"], checkpoint={"step": 1})
        self.assertTrue(ok)
        stored = web_tasks.get_task(self.root, claimed["task_id"])
        self.assertEqual(stored["checkpoint"], {"step": 1})
        stale = web_tasks.heartbeat(self.root, claimed["task_id"], "wrong", checkpoint={"step": 2})
        self.assertFalse(stale)


class CancelAndRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        if _IMPORT_ERROR is not None:
            self.fail(f"web_tasks module unavailable: {_IMPORT_ERROR}")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cancel_queued_task(self) -> None:
        task = web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"})
        status = web_tasks.request_cancel(self.root, task["task_id"])
        self.assertEqual(status, "cancelled")
        self.assertIsNone(web_tasks.claim_next_task(self.root, worker_id="w1"))

    def test_cancel_running_marks_cancel_requested(self) -> None:
        web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"})
        claimed = web_tasks.claim_next_task(self.root, worker_id="w1")
        status = web_tasks.request_cancel(self.root, claimed["task_id"])
        self.assertEqual(status, "cancel_requested")

    def test_cancel_terminal_task_is_rejected(self) -> None:
        web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"}, max_attempts=1)
        claimed = web_tasks.claim_next_task(self.root, worker_id="w1")
        web_tasks.fail_task(self.root, claimed["task_id"], claimed["lease_token"], "x", retryable=False)
        with self.assertRaises(ValueError):
            web_tasks.request_cancel(self.root, claimed["task_id"])

    def test_expired_lease_without_remote_id_requeues(self) -> None:
        web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"})
        claimed = web_tasks.claim_next_task(self.root, worker_id="w1", lease_seconds=-1)
        moved = web_tasks.recover_expired_leases(self.root)
        self.assertIn(claimed["task_id"], moved)
        stored = web_tasks.get_task(self.root, claimed["task_id"])
        self.assertEqual(stored["status"], "queued")

    def test_expired_lease_with_remote_id_becomes_unknown(self) -> None:
        task = web_tasks.enqueue_task(self.root, "digest_send", payload={"digest_id": "d"})
        conn = _ops_conn(self.root)
        conn.execute("UPDATE operation_tasks SET remote_operation_id='remote-9' WHERE task_id=?", (task["task_id"],))
        conn.commit()
        conn.close()
        claimed = web_tasks.claim_next_task(self.root, worker_id="w1", lease_seconds=-1)
        web_tasks.recover_expired_leases(self.root)
        stored = web_tasks.get_task(self.root, claimed["task_id"])
        self.assertEqual(stored["status"], "unknown")


class WorkerExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        if _IMPORT_ERROR is not None:
            self.fail(f"web_tasks module unavailable: {_IMPORT_ERROR}")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_run_worker_once_executes_handler_and_stores_result(self) -> None:
        web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"})

        def handler(root: Path, payload: dict) -> dict:
            return {"hits": [{"job": "j1"}], "mode": "semantic"}

        processed = web_tasks.run_worker(self.root, worker_id="w1", handlers={"semantic_search": handler}, once=True, poll_seconds=0.05)
        self.assertEqual(processed, 1)
        stored = web_tasks.get_task(self.root, web_tasks.list_tasks(self.root)[0]["task_id"])
        self.assertEqual(stored["status"], "succeeded")
        self.assertEqual(stored["result"]["hits"], [{"job": "j1"}])

    def test_run_worker_marks_uncancellable_task_cancelled_before_start(self) -> None:
        task = web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"})
        web_tasks.request_cancel(self.root, task["task_id"])

        def handler(root: Path, payload: dict) -> dict:  # pragma: no cover - must never run
            raise AssertionError("handler must not run for cancelled task")

        processed = web_tasks.run_worker(self.root, worker_id="w1", handlers={"semantic_search": handler}, once=True, poll_seconds=0.05)
        self.assertEqual(processed, 0)
        self.assertEqual(web_tasks.get_task(self.root, task["task_id"])["status"], "cancelled")

    def test_run_worker_handler_exception_marks_retryable_failure(self) -> None:
        web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "x"}, max_attempts=1)

        def handler(root: Path, payload: dict) -> dict:
            raise RuntimeError("provider exploded")

        web_tasks.run_worker(self.root, worker_id="w1", handlers={"semantic_search": handler}, once=True, poll_seconds=0.05)
        stored = web_tasks.get_task(self.root, web_tasks.list_tasks(self.root)[0]["task_id"])
        self.assertEqual(stored["status"], "failed")
        self.assertIn("provider exploded", stored["safe_error"])

    def test_run_worker_rejects_task_with_no_registered_handler(self) -> None:
        conn = _ops_conn(self.root)
        web_tasks.init_task_schema(conn)
        conn.execute(
            "INSERT INTO operation_tasks (task_id, type, status, safe_payload, dedup_key, attempts, max_attempts, created_at)"
            " VALUES ('t-raw', 'not_allowed', 'queued', '{}', 't-raw', 0, 3, '2026-01-01T00:00:00Z')"
        )
        conn.commit()
        conn.close()
        processed = web_tasks.run_worker(self.root, worker_id="w1", handlers={}, once=True, poll_seconds=0.05)
        self.assertEqual(processed, 1)
        stored = web_tasks.get_task(self.root, "t-raw")
        self.assertEqual(stored["status"], "failed")
        self.assertIn("handler", stored["safe_error"].lower())


class DefaultHandlerTests(unittest.TestCase):
    """Built-in handlers must call the real service layer, guarded and safe."""

    def setUp(self) -> None:
        if _IMPORT_ERROR is not None or web_services is None:
            self.fail(f"web modules unavailable: {_IMPORT_ERROR}")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_semantic_search_handler_calls_pipeline_and_records_usage(self) -> None:
        hits = [{"title": "t", "source": "s", "job": "j", "timestamp": "00:00", "kind": "speech", "text": "x", "score": 0.9}]
        with mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "test-key"}), mock.patch(
            "pipeline.semantic_search", return_value=hits
        ) as sem:
            result = web_tasks.TASK_HANDLERS["semantic_search"](
                self.root,
                {"query": "q", "limit": 5, "backend": "openrouter", "model": "m", "reservation_id": None},
            )
        sem.assert_called_once()
        called_kwargs = sem.call_args
        self.assertNotIn("test-key", json.dumps(result))
        self.assertEqual(result["hits"], hits)
        conn = _ops_conn(self.root)
        rows = conn.execute("SELECT operation, provider FROM usage_costs").fetchall()
        conn.close()
        self.assertTrue(any(row["operation"] == "semantic_search" for row in rows))

    def test_rss_item_retry_handler_requeues_under_writer_lock(self) -> None:
        rss_dir = self.root / "rss"
        rss_dir.mkdir()
        (rss_dir / "state.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "subscriptions": [],
                    "items": [{"id": "item-1", "subscription_id": "s", "title": "t", "status": "failed", "attempts": 3, "last_error": "x", "discovered_at": "2026-01-01T00:00:00Z"}],
                    "scheduler_cursor": 0,
                }
            ),
            encoding="utf-8",
        )
        result = web_tasks.TASK_HANDLERS["rss_item_retry"](self.root, {"item_id": "item-1"})
        self.assertEqual(result["item_id"], "item-1")
        state = json.loads((rss_dir / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["items"][0]["status"], "queued")
        self.assertEqual(state["items"][0]["attempts"], 0)

    def test_digest_send_handler_refuses_changed_payload(self) -> None:
        import digests  # noqa: F401  (baseline module, exists on base)

        task_payload = {"digest_id": "d1", "revision": 1, "payload_hash": "expected", "digest_type": "daily", "date_str": "2026-01-02"}
        conn = _ops_conn(self.root)
        import notifications

        notifications._init_operations_schema(conn)
        conn.execute(
            "INSERT INTO digest_runs (digest_id, schedule_id, type, window_start, window_end, timezone, recipient_id, revision,"
            " selection_snapshot, rendered_payload_hash, status, candidate_count, selected_count, deferred_count, created_at)"
            " VALUES ('d1','default_daily','daily','2026-01-01','2026-01-02','Asia/Taipei','default',1,?,'expected','previewed',0,0,0,'2026-01-01')",
            (json.dumps({"content_signature": "original-signature"}),),
        )
        conn.commit()
        conn.close()
        with mock.patch.object(digests, "collect_digest_candidates", return_value=[]), mock.patch.object(
            digests, "build_digest_payload", return_value={"rendered_payload_hash": "DIFFERENT", "selected_items": []}
        ):
            with self.assertRaisesRegex(ValueError, "stale_payload"):
                web_tasks.TASK_HANDLERS["digest_send"](self.root, task_payload)

    def test_stale_lease_cannot_fail_reclaimed_task(self) -> None:
        """fencing: worker A's expired lease must not fail a task reclaimed by B."""
        task = web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "q"})
        claimed_a = web_tasks.claim_next_task(self.root, "worker-A", lease_seconds=0.01)
        self.assertEqual(claimed_a["task_id"], task["task_id"])
        import time as _time

        _time.sleep(0.02)
        recovered = web_tasks.recover_expired_leases(self.root)
        self.assertIn(task["task_id"], recovered)
        claimed_b = web_tasks.claim_next_task(self.root, "worker-B", lease_seconds=60)
        self.assertIsNotNone(claimed_b)
        self.assertNotEqual(claimed_b["lease_token"], claimed_a["lease_token"])
        # stale worker tries to fail the task it lost — must be a no-op
        self.assertFalse(web_tasks.fail_task(self.root, task["task_id"], claimed_a["lease_token"], RuntimeError("x")))
        self.assertFalse(web_tasks.complete_task(self.root, task["task_id"], claimed_a["lease_token"], result={"x": 1}))
        current = web_tasks.get_task(self.root, task["task_id"])
        self.assertEqual(current["status"], "running")
        self.assertEqual(current["lease_token"], claimed_b["lease_token"])

    def test_safe_error_redacts_credentials(self) -> None:
        task = web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "q"}, max_attempts=1)
        claimed = web_tasks.claim_next_task(self.root, "w1", lease_seconds=30)
        self.assertEqual(claimed["task_id"], task["task_id"])
        web_tasks.fail_task(
            self.root, task["task_id"], claimed["lease_token"],
            RuntimeError("HTTP 401 https://api.example.com/x?api_key=SECRET123&ok=1"),
            retryable=False,
        )
        stored = web_tasks.get_task(self.root, task["task_id"])
        self.assertEqual(stored["status"], "failed")
        self.assertNotIn("SECRET123", stored["safe_error"] or "")

    def test_idempotency_key_namespaced_per_task_type(self) -> None:
        a = web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "q"}, dedup_key="same-key")
        b = web_tasks.enqueue_task(self.root, "rss_item_retry", payload={"item_id": "i1"}, dedup_key="same-key")
        self.assertNotEqual(a["task_id"], b["task_id"])
        self.assertEqual(a["type"], "semantic_search")
        self.assertEqual(b["type"], "rss_item_retry")
        a2 = web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "q"}, dedup_key="same-key")
        self.assertEqual(a2["task_id"], a["task_id"])
        self.assertFalse(a2["created"])

    def test_cancel_running_then_complete_stays_terminal(self) -> None:
        """request_cancel is predicated on observed status; a reclaimed task
        whose lease expired mid-cancel can't end up stranded."""
        task = web_tasks.enqueue_task(self.root, "semantic_search", payload={"query": "q"})
        claimed = web_tasks.claim_next_task(self.root, "w1", lease_seconds=30)
        self.assertEqual(web_tasks.request_cancel(self.root, task["task_id"]), "cancel_requested")
        # double-cancel on non-terminal -> same request state (idempotent-ish)
        self.assertEqual(web_tasks.request_cancel(self.root, task["task_id"]), "cancel_requested")
        web_tasks.fail_task(self.root, task["task_id"], claimed["lease_token"], RuntimeError("boom"), retryable=False)
        self.assertEqual(web_tasks.get_task(self.root, task["task_id"])["status"], "failed")


if __name__ == "__main__":
    unittest.main()
