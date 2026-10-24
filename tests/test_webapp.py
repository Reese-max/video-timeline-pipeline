"""Dashboard acceptance checks; every provider is fake and every dataset is temporary."""
from __future__ import annotations

import datetime as dt
from contextlib import closing
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import pipeline
import digests
import web_services as services
from email_providers import FakeProvider, ResendProvider
import notifications

WEB_INSTALLED = all(importlib.util.find_spec(name) for name in ("fastapi", "httpx", "jinja2"))


class Dataset(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = (digests.compute_digest_window()[1] - dt.timedelta(hours=1)).isoformat()
        for job, category in (("abc123def456", "AI"), ("def123abc456", "AI tools")):
            folder = self.root / "jobs" / job
            folder.mkdir(parents=True)
            (folder / "manifest.json").write_text(json.dumps({
                "status": "completed", "eligible_at": self.now,
                           "source": {"title": "<script>alert(1)</script> Python 影片", "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                           "author": "Alice", "published_at": "2026-09-01T00:00:00Z"},
                "video": {"duration_seconds": 120}, "categories": [category],
            }), encoding="utf-8")
            (folder / "summary.json").write_text(json.dumps({"short_summary": "Python summary", "brief": "Python summary"}), encoding="utf-8")
            (folder / "timeline.json").write_text(json.dumps([
                {"timestamp": i, "end": i + .8, "type": "speech", "text": f"Python evidence {i}"} for i in range(120)
            ]), encoding="utf-8")
            # F0 only registers verified local artifact bundles. Preserve real
            # digest/grounding checks rather than bypassing their new gates.
            transcript = {"segments": [{"start": i, "end": i + .8, "text": f"Python evidence {i}"}
                                       for i in range(120)]}
            (folder / "transcript.json").write_text(json.dumps(transcript), encoding="utf-8")
            manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
            manifest.update(schema_version=1, models={"transcribe": "offline-fixture"},
                artifacts={key: str((folder / f"{key}.json").relative_to(self.root))
                           for key in ("transcript", "timeline", "summary")})
            (folder / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with patch.object(notifications, "_utc_now_iso", return_value=self.now), \
                    patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "", "RSS_NOTIFY_WEBHOOK": ""}):
                self.assertTrue(notifications.record_pipeline_completed_event(self.root, folder, manifest))
        services.initialize(self.root)

    def snapshot(self):
        return {str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in self.root.rglob("*") if p.is_file()}


class ReadServicesTests(Dataset):
    def test_exact_filters_shared_with_cli_and_bounded_pages(self):
        page = services.videos(self.root, filters={"category": "AI", "author": "Alice"}, page_size=1)
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["job"], "abc123def456")
        cli = pipeline.search_index(self.root, "Python", 20, "AI")
        web = services.search(self.root, "Python", filters={"category": "AI"})
        self.assertEqual({x["job"] for x in cli}, {x["job"] for x in web["items"]})
        self.assertEqual({x["job"] for x in cli}, {"abc123def456"})
        for kwargs in ({"page_size": 101}, {"page": 0}, {"sort": "title; DROP TABLE videos"}):
            with self.assertRaises(ValueError):
                services.videos(self.root, **kwargs)

    def test_detail_uses_known_ids_and_paginates_evidence(self):
        result = services.video_detail(self.root, "abc123def456", page_size=10, page=2, kind="speech")
        self.assertEqual(len(result["evidence"]["items"]), 10)
        self.assertEqual(result["evidence"]["total"], 120)
        self.assertEqual(result["operations"]["tasks"]["items"], [])
        self.assertEqual(result["score"]["status"], "unscored")
        for job in ("../.env", "unknown", "abc123def456/../../.env"):
            with self.assertRaises((ValueError, LookupError)):
                services.video_detail(self.root, job)

    def test_artifact_symlink_cannot_escape_root(self):
        path = self.root / "jobs" / "abc123def456" / "summary.json"
        path.unlink()
        outside = Path(self.temp.name).with_suffix(".private")
        outside.write_text('"SECRET"', encoding="utf-8")
        self.addCleanup(outside.unlink, missing_ok=True)
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest("OS does not grant symlink creation")
        with self.assertRaises((ValueError, LookupError)):
            services.video_detail(self.root, "abc123def456")

    def test_stats_use_stable_completion_and_unknown_is_not_zero(self):
        original = services.stats(self.root)
        self.assertEqual(original["completed"], 2)
        self.assertIsNone(original["high_importance"])
        self.assertIn("window", original)
        pipeline.build_index(self.root)
        self.assertEqual(services.stats(self.root)["completed"], original["completed"])
        observed = services.videos(self.root)["items"][0]["first_completed_at"]
        self.assertEqual(dt.datetime.fromisoformat(observed.replace("Z", "+00:00")), dt.datetime.fromisoformat(self.now))

    def test_scoring_is_visible_and_date_filters_match_the_stats_timezone(self):
        import scoring
        scoring.score_video(self.root, "abc123def456", mock_score=scoring.ScoreResult(
            importance=9, relevance=8, novelty=None, novelty_status="insufficient_history",
            evidence_confidence=.9, priority_score=8.5, reasons={"importance": "evidence"}, claims=[]))
        stats = services.stats(self.root)
        self.assertEqual(stats["high_importance"], 1)
        self.assertEqual(stats["scoring_state"], "partial")
        self.assertEqual(services.videos(self.root)["items"][0]["score"]["importance"], 9)
        self.assertEqual(services.videos(self.root, filters=stats["video_filter"])["total"], stats["completed"])

    def test_missing_and_legacy_database_never_created_or_migrated(self):
        with tempfile.TemporaryDirectory() as empty:
            root = Path(empty)
            with self.assertRaises(services.Unavailable):
                services.videos(root)
            self.assertEqual(list(root.iterdir()), [])
            with closing(sqlite3.connect(root / "library.db")) as conn, conn:
                conn.execute("CREATE TABLE videos (job TEXT)")
            before = (root / "library.db").read_bytes()
            with self.assertRaises(services.Unavailable):
                services.search(root, "Python")
            self.assertEqual((root / "library.db").read_bytes(), before)

    def test_search_has_no_transport_or_false_summary_timestamp(self):
        before = self.snapshot()
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")), \
             patch.object(pipeline, "embed_texts", side_effect=AssertionError("embedding")), \
             patch.object(pipeline, "minimax_chat", side_effect=AssertionError("LLM")):
            hits = services.search(self.root, "Python")["items"]
        summaries = [h for h in hits if h["kind"] == "summary"]
        self.assertTrue(summaries)
        self.assertIsNone(summaries[0]["timestamp"])
        self.assertEqual(summaries[0]["time_label"], "影片層級")
        self.assertEqual(self.snapshot(), before)

    def test_reports_and_preview_are_read_only_and_local(self):
        before = self.snapshot()
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            costs = services.costs(self.root)
            operations = services.operations(self.root)
            draft = services.preview_digest(self.root)
        self.assertIn("unknown", costs)
        self.assertIn("scope", costs)
        self.assertEqual(operations["rss"]["source"], "rss/state.json")
        self.assertIn("payload_hash", draft)
        self.assertEqual(self.snapshot(), before)

    def test_unsafe_source_url_not_exposed(self):
        with closing(sqlite3.connect(self.root / "library.db")) as db, db:
            db.execute("UPDATE videos SET source='javascript:alert(1)'")
        self.assertTrue(all(item["source"] is None for item in services.videos(self.root)["items"]))

    def test_cli_does_not_require_web_packages(self):
        code = """import builtins,sys
real=builtins.__import__
def blocked(name,*a,**kw):
    if name.split('.')[0] in {'fastapi','uvicorn','jinja2','webapp'}: raise ImportError(name)
    return real(name,*a,**kw)
builtins.__import__=blocked
import pipeline
sys.argv=['pipeline.py','--help']
pipeline.main()
"""
        run = subprocess.run([sys.executable, "-X", "utf8", "-c", code], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("--serve", run.stdout)


class DurableTaskTests(Dataset):
    def enqueue(self, kind="ask", payload=None, key="request-1"):
        return services.enqueue_task(self.root, kind, payload or {"query": "Python", "limit": 5}, key)

    def test_duplicate_request_is_one_task_and_conflicting_key_is_rejected(self):
        first = self.enqueue()
        self.assertEqual(first["task_id"], self.enqueue()["task_id"])
        with self.assertRaises(ValueError):
            self.enqueue(payload={"query": "different", "limit": 5})
        with self.assertRaises(ValueError):
            self.enqueue(kind="shell", payload={"command": "whoami"})

    def test_claim_fencing_recovery_and_unknown_never_replay(self):
        task = self.enqueue()
        claim = services.claim_task(self.root, "worker-1")
        self.assertIsNone(services.claim_task(self.root, "worker-2"))
        self.assertFalse(services.finish_task(self.root, task["task_id"], "wrong-token", "succeeded", {}))
        services.checkpoint_task(self.root, task["task_id"], claim["lease_token"], "submitted")
        with closing(sqlite3.connect(self.root / "operations.db")) as db, db:
            db.execute("UPDATE operation_tasks SET lease_expires_at=0")
        self.assertIsNone(services.claim_task(self.root, "worker-2"))
        self.assertEqual(services.get_task(self.root, task["task_id"])["status"], "unknown")
        self.assertFalse(services.finish_task(self.root, task["task_id"], claim["lease_token"], "succeeded", {}))

    def test_new_request_can_retry_a_blocked_task_but_old_key_keeps_its_receipt(self):
        first = self.enqueue()
        claim = services.claim_task(self.root, "first")
        services.finish_task(self.root, first["task_id"], claim["lease_token"], "blocked", {"state": "disabled"})
        second = self.enqueue(key="request-2")
        self.assertNotEqual(first["task_id"], second["task_id"])
        self.assertEqual(self.enqueue()["task_id"], first["task_id"])

    def test_cancellation_and_expired_lease_stop_the_next_paid_step(self):
        for expire in (False, True):
            with self.subTest(expire=expire):
                task = self.enqueue(payload={"query": "Python", "mode": "semantic"}, key=f"cancel-{expire}")
                def first_step(*args, **kwargs):
                    if expire:
                        with closing(sqlite3.connect(self.root / "operations.db")) as db, db:
                            db.execute("UPDATE operation_tasks SET lease_expires_at=0 WHERE task_id=?", (task["task_id"],))
                        services.claim_task(self.root, "replacement")
                    else:
                        services.cancel_task(self.root, task["task_id"])
                    return [{"title": "evidence", "job": "abc123def456", "source": "", "timestamp": 1, "text": "Python", "kind": "speech"}]
                with patch.dict(os.environ, {"MINIMAX_SUBSCRIPTION_KEY": "offline-key", "MINIMAX_MODEL": "MiniMax-M3"}), \
                        patch.object(services, "_cloud_search", side_effect=first_step), patch.object(pipeline, "answer_question") as answer:
                    services.run_worker_once(self.root)
                answer.assert_not_called()
                self.assertEqual(services.get_task(self.root, task["task_id"])["status"], "unknown" if expire else "cancelled")

    def test_frozen_digest_content_change_and_unknown_do_not_resend(self):
        with patch.dict(os.environ, {"EMAIL_TO": "reader@example.test", "EMAIL_PROVIDER": "fake"}):
            draft = services.preview_digest(self.root, persist=True)
            confirmation = {key: draft[key] for key in ("digest_id", "revision", "payload_hash")}
            task = services.enqueue_task(self.root, "digest.send", confirmation, "unknown-send")
            provider = FakeProvider(mode="unknown")
            services.run_worker_once(self.root, provider=provider)
            self.assertEqual(services.get_task(self.root, task["task_id"])["status"], "unknown")
            again = services.enqueue_task(self.root, "digest.send", confirmation, "unknown-send-2")
            self.assertEqual(again["task_id"], task["task_id"])
            services.run_worker_once(self.root, provider=provider)
            self.assertEqual(len(provider.history), 1)
            (self.root / "jobs/abc123def456/summary.json").write_text('{"brief":"changed"}', encoding="utf-8")
            with self.assertRaises(ValueError):
                services.enqueue_task(self.root, "digest.send", confirmation, "stale-send")

    def test_rss_retry_preserves_attempt_budget_and_can_be_requested_again(self):
        state = {"schema_version": 1, "subscriptions": [], "items": [{"id": "rss-1", "status": "failed", "attempts": 1}], "scheduler_cursor": 0}
        (self.root / "rss").mkdir()
        path = self.root / "rss/state.json"
        path.write_text(json.dumps(state), encoding="utf-8")
        with patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"):
            first = services.enqueue_task(self.root, "rss.retry", {"job": "rss-1", "allow_cloud": True}, "rss-first")
            services.run_worker_once(self.root)
            saved = pipeline.load_rss_state(self.root)
            self.assertEqual(saved["items"][0]["attempts"], 1)
            saved["items"][0].update(status="failed", attempts=2)
            path.write_text(json.dumps(saved), encoding="utf-8")
            second = services.enqueue_task(self.root, "rss.retry", {"job": "rss-1", "allow_cloud": True}, "rss-second")
            self.assertNotEqual(first["task_id"], second["task_id"])

    def test_unsubmitted_crash_recovers_and_cancellation_is_durable(self):
        task = self.enqueue()
        services.claim_task(self.root, "crashed")
        with closing(sqlite3.connect(self.root / "operations.db")) as db, db:
            db.execute("UPDATE operation_tasks SET lease_expires_at=0")
        claim = services.claim_task(self.root, "new")
        self.assertEqual(claim["task_id"], task["task_id"])
        services.cancel_task(self.root, task["task_id"])
        self.assertEqual(services.get_task(self.root, task["task_id"])["status"], "cancel_requested")
        self.assertFalse(services.checkpoint_task(self.root, task["task_id"], claim["lease_token"], "submitted"))

    def test_rss_cooldown_is_rechecked_after_waiting_in_the_task_queue(self):
        (self.root / "rss").mkdir()
        path = self.root / "rss/state.json"
        state = {"schema_version": 1, "subscriptions": [], "items": [{"id": "rss-1", "status": "failed", "attempts": 1}]}
        path.write_text(json.dumps(state), encoding="utf-8")
        task = services.enqueue_task(self.root, "rss.retry", {"job": "rss-1", "allow_cloud": True}, "cooldown")
        state["items"][0]["next_retry_at"] = "2999-01-01T00:00:00+00:00"
        path.write_text(json.dumps(state), encoding="utf-8")
        before = path.read_bytes()
        with patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"):
            receipt = services.run_worker_once(self.root)
        self.assertEqual(receipt["task_id"], task["task_id"])
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(path.read_bytes(), before)

    def test_budget_denied_makes_zero_transport_calls(self):
        self.enqueue()
        with patch.dict(os.environ, {"DAILY_BUDGET_USD": "0.000001", "BUDGET_HARD_STOP": "true",
                                    "MINIMAX_SUBSCRIPTION_KEY": "fake-test-key", "MINIMAX_MODEL": "MiniMax-M3", "MINIMAX_BASE_URL": "https://api.minimax.io/v1"}), \
             patch("urllib.request.urlopen") as network:
            services.run_worker_once(self.root)
        network.assert_not_called()
        with closing(sqlite3.connect(self.root / "operations.db")) as db:
            status = db.execute("SELECT status FROM operation_tasks").fetchone()[0]
        self.assertEqual(status, "blocked")
        self.assertEqual(services.history(self.root, "operation_tasks")["items"][0]["safe_error"], "budget_blocked")

    def test_uncertain_post_is_one_transport_and_stays_unknown(self):
        failures = [TimeoutError("accepted then disconnected"),
                    urllib.error.HTTPError("https://provider.test", 503, "unknown", {}, io.BytesIO(b"{}")),
                    io.BytesIO(b"\xff"), io.BytesIO(b"not JSON")]
        for number, failure in enumerate(failures):
            with self.subTest(number=number):
                task = self.enqueue(payload={"query": f"Python evidence {number}"}, key=f"uncertain-{number}")
                with patch.dict(os.environ, {"MINIMAX_SUBSCRIPTION_KEY": "fake-test-key", "MINIMAX_MODEL": "MiniMax-M3",
                                            "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
                                            "DAILY_BUDGET_USD": "0", "BUDGET_HARD_STOP": "false"}), \
                     patch("urllib.request.urlopen", side_effect=[failure, io.BytesIO(b'{}')]) as transport, \
                     patch.object(pipeline.OperationBudget, "sleep"):
                    receipt = services.run_worker_once(self.root)
                self.assertEqual(transport.call_count, 1, receipt)
                self.assertEqual(receipt["task_id"], task["task_id"])
                self.assertEqual(receipt["status"], "unknown")
                self.assertEqual(self.enqueue(payload={"query": f"Python evidence {number}"}, key=f"replay-{number}")["task_id"], task["task_id"])

    def test_frozen_digest_retry_preserves_wire_content_and_provider(self):
        with patch.dict(os.environ, {"EMAIL_TO": "reader@example.test", "EMAIL_PROVIDER": "fake"}):
            draft = services.preview_digest(self.root, persist=True)
            confirmation = {key: draft[key] for key in ("digest_id", "revision", "payload_hash")}
            services.enqueue_task(self.root, "digest.send", confirmation, "retry-send")
            provider = FakeProvider(mode="retryable_not_accepted", retry_after=0)
            services.run_worker_once(self.root, provider=provider)
            provider.mode = "accepted"
            with patch.object(notifications, "get_configured_provider", return_value=provider) as select:
                self.assertEqual(notifications.run_notification_retry(self.root)["accepted"], 1)
            select.assert_called_once_with("fake")
            self.assertEqual(len(provider.history), 2)
            for field in ("recipient", "subject", "text_body", "html_body", "idempotency_key"):
                self.assertEqual(provider.history[0][field], provider.history[1][field], field)
            with closing(notifications.get_operations_db(self.root)) as db:
                event = notifications.record_event(db, "legacy", None, None, None, {}, "legacy-event")
                notifications.enqueue_delivery(db, event, "email", "reader@example.test", "ResendProvider", "legacy")
            with patch.object(notifications, "get_configured_provider", return_value=provider) as select:
                self.assertEqual(notifications.run_notification_retry(self.root)["accepted"], 1)
            select.assert_called_once_with()

    def test_digest_cancellation_stops_outbox_retries(self):
        with patch.dict(os.environ, {"EMAIL_TO": "reader@example.test", "EMAIL_PROVIDER": "fake"}):
            draft = services.preview_digest(self.root, persist=True)
            confirmation = {key: draft[key] for key in ("digest_id", "revision", "payload_hash")}
            task = services.enqueue_task(self.root, "digest.send", confirmation, "cancel-digest")
            provider = FakeProvider(mode="retryable_not_accepted", retry_after=0)
            send = provider.send

            def cancel_during_send(**kwargs):
                services.cancel_task(self.root, task["task_id"])
                return send(**kwargs)

            with patch.object(provider, "send", side_effect=cancel_during_send):
                receipt = services.run_worker_once(self.root, provider=provider)
            self.assertEqual(receipt["status"], "cancelled")
            provider.mode = "accepted"
            self.assertEqual(notifications.run_notification_retry(self.root, provider)["accepted"], 0)
            self.assertEqual(len(provider.history), 1)
            with closing(services._db(self.root)) as db:
                self.assertEqual(db.execute("SELECT status FROM notification_deliveries").fetchone()[0], "cancelled")

    def test_pending_web_digest_cannot_bypass_the_task_fence(self):
        with patch.dict(os.environ, {"EMAIL_TO": "reader@example.test", "EMAIL_PROVIDER": "fake"}):
            draft = services.preview_digest(self.root, persist=True)
            confirmation = {key: draft[key] for key in ("digest_id", "revision", "payload_hash")}
            task = services.enqueue_task(self.root, "digest.send", confirmation, "pending-digest")
            intruder, owner = FakeProvider(), FakeProvider()
            enqueue = notifications.enqueue_delivery

            def concurrent_retry(*args, **kwargs):
                delivery_id = enqueue(*args, **kwargs)
                services.cancel_task(self.root, task["task_id"])
                notifications.run_notification_retry(self.root, intruder)
                return delivery_id

            with patch.object(notifications, "enqueue_delivery", side_effect=concurrent_retry):
                receipt = services.run_worker_once(self.root, provider=owner)
            self.assertEqual(intruder.history, [])
            self.assertEqual(owner.history, [])
            self.assertEqual(receipt["status"], "cancelled")

    def test_cancel_after_outbox_update_cannot_be_overwritten_by_task_finish(self):
        with patch.dict(os.environ, {"EMAIL_TO": "reader@example.test", "EMAIL_PROVIDER": "fake"}):
            draft = services.preview_digest(self.root, persist=True)
            confirmation = {key: draft[key] for key in ("digest_id", "revision", "payload_hash")}
            task = services.enqueue_task(self.root, "digest.send", confirmation, "late-cancel")
            update = notifications.update_delivery_status
            intruder = FakeProvider()

            def cancel_after_update(*args, **kwargs):
                status = update(*args, **kwargs)
                notifications.run_notification_retry(self.root, intruder)
                services.cancel_task(self.root, task["task_id"])
                return status

            with patch.object(notifications, "update_delivery_status", side_effect=cancel_after_update):
                receipt = services.run_worker_once(self.root, provider=FakeProvider(mode="retryable_not_accepted", retry_after=0))
            self.assertEqual(intruder.history, [])
            self.assertEqual(receipt["status"], "cancelled")
            self.assertEqual(notifications.run_notification_retry(self.root, intruder)["claimed"], 0)

    def test_frozen_dispatch_rechecks_fence_after_validation(self):
        with patch.dict(os.environ, {"EMAIL_TO": "reader@example.test", "EMAIL_PROVIDER": "fake"}):
            for expire in (False, True):
                with self.subTest(expire=expire):
                    draft = services.preview_digest(self.root, persist=True, new_revision=True)
                    confirmation = {key: draft[key] for key in ("digest_id", "revision", "payload_hash")}
                    task = services.enqueue_task(self.root, "digest.send", confirmation, f"validate-fence-{expire}")
                    validate = notifications._frozen_delivery

                    def stop_after_validation(*args):
                        result = validate(*args)
                        if expire:
                            with closing(services._db(self.root, write=True)) as db, db:
                                db.execute("UPDATE operation_tasks SET lease_expires_at=0 WHERE task_id=?", (task["task_id"],))
                            services.claim_task(self.root, "replacement")
                        else:
                            services.cancel_task(self.root, task["task_id"])
                        return result

                    provider = FakeProvider()
                    with patch.object(notifications, "_frozen_delivery", side_effect=stop_after_validation):
                        receipt = services.run_worker_once(self.root, provider=provider)
                    self.assertEqual(provider.history, [])
                    self.assertEqual(receipt["status"], "unknown" if expire else "cancelled")

    def test_frozen_retry_rechecks_cancellation_before_transport(self):
        with patch.dict(os.environ, {"EMAIL_TO": "reader@example.test", "EMAIL_PROVIDER": "fake"}):
            for stage in ("validation", "provider"):
                with self.subTest(stage=stage):
                    draft = services.preview_digest(self.root, persist=True, new_revision=True)
                    confirmation = {key: draft[key] for key in ("digest_id", "revision", "payload_hash")}
                    task = services.enqueue_task(self.root, "digest.send", confirmation, f"retry-fence-{stage}")
                    services.run_worker_once(self.root, provider=FakeProvider(mode="retryable_not_accepted", retry_after=0))
                    provider = FakeProvider()
                    validate = notifications._frozen_delivery

                    def cancel_before_transport(*args):
                        result = validate(*args) if stage == "validation" else provider
                        services.cancel_task(self.root, task["task_id"])
                        return result

                    with patch.object(notifications, "_frozen_delivery" if stage == "validation" else "get_configured_provider",
                                      side_effect=cancel_before_transport):
                        receipt = notifications.run_notification_retry(self.root, provider if stage == "validation" else None)
                    self.assertEqual(provider.history, [])
                    self.assertEqual(receipt["accepted"], 0)
                    self.assertEqual(services.get_task(self.root, task["task_id"])["status"], "cancelled")

    def test_immediate_notification_does_not_steal_a_frozen_retry(self):
        with patch.dict(os.environ, {"EMAIL_TO": "reader@example.test", "EMAIL_PROVIDER": "fake"}):
            draft = services.preview_digest(self.root, persist=True)
            confirmation = {key: draft[key] for key in ("digest_id", "revision", "payload_hash")}
            services.enqueue_task(self.root, "digest.send", confirmation, "retry-isolation")
            receipt = services.run_worker_once(self.root, provider=FakeProvider(mode="retryable_not_accepted", retry_after=0))
            notifications.run_notify_test(self.root, FakeProvider())
            with closing(services._db(self.root)) as db:
                status = db.execute("SELECT status FROM notification_deliveries WHERE delivery_id=?",
                                    (receipt["result"]["delivery_id"],)).fetchone()[0]
            self.assertEqual(status, "retry_wait")

    def test_digest_uncertain_http_response_is_never_retried(self):
        with patch.dict(os.environ, {"EMAIL_TO": "reader@example.test", "EMAIL_PROVIDER": "resend",
                                    "DAILY_BUDGET_USD": "0", "BUDGET_HARD_STOP": "false"}):
            for index, code in enumerate((408, 500, 503), 1):
                with self.subTest(code=code):
                    draft = services.preview_digest(self.root, persist=True, new_revision=True)
                    confirmation = {key: draft[key] for key in ("digest_id", "revision", "payload_hash")}
                    services.enqueue_task(self.root, "digest.send", confirmation, f"digest-http-{code}")
                    error = urllib.error.HTTPError("https://api.resend.com/emails", code, "uncertain", {}, io.BytesIO(b"{}"))
                    with patch("urllib.request.urlopen", side_effect=error) as transport:
                        receipt = services.run_worker_once(self.root, provider=ResendProvider(api_key="fake-test-key"))
                        self.assertEqual(receipt["status"], "unknown")
                        with closing(services._db(self.root)) as db:
                            self.assertEqual([row[0] for row in db.execute("SELECT status FROM usage_costs WHERE provider='resend'")], ["unknown"] * index)
                            self.assertEqual([row[0] for row in db.execute("SELECT status FROM budget_reservations")], ["reserved"] * index)
                        with closing(services._db(self.root, write=True)) as db, db:
                            db.execute("UPDATE notification_deliveries SET next_attempt_at=NULL")
                        self.assertEqual(notifications.run_notification_retry(self.root, ResendProvider(api_key="fake-test-key"))["claimed"], 0)
                    self.assertEqual(transport.call_count, 1)

    def test_corrupt_frozen_retry_never_sends_replacement_content(self):
        with patch.dict(os.environ, {"EMAIL_TO": "reader@example.test", "EMAIL_PROVIDER": "fake"}):
            for corruption in ("json", "missing", "field", "type", "changed", "key"):
                with self.subTest(corruption=corruption):
                    draft = services.preview_digest(self.root, persist=True, new_revision=True)
                    confirmation = {key: draft[key] for key in ("digest_id", "revision", "payload_hash")}
                    services.enqueue_task(self.root, "digest.send", confirmation, f"corrupt-{corruption}")
                    receipt = services.run_worker_once(self.root, provider=FakeProvider(mode="retryable_not_accepted", retry_after=0))
                    delivery_id = receipt["result"]["delivery_id"]
                    with closing(services._db(self.root, write=True)) as db, db:
                        row = db.execute("SELECT e.event_id,e.payload_json FROM notification_events e JOIN notification_deliveries d USING(event_id) WHERE d.delivery_id=?", (delivery_id,)).fetchone()
                        payload = json.loads(row["payload_json"])
                        if corruption == "missing":
                            payload.pop("_web_frozen_email")
                        elif corruption == "field":
                            payload["_web_frozen_email"].pop("subject")
                        elif corruption == "type":
                            payload["_web_frozen_email"]["text_body"] = {"unsafe": "not text"}
                        elif corruption == "changed":
                            payload["_web_frozen_email"]["text_body"] = "unconfirmed replacement"
                        elif corruption == "key":
                            db.execute("UPDATE notification_deliveries SET provider_idempotency_key='changed-key' WHERE delivery_id=?", (delivery_id,))
                        db.execute("UPDATE notification_events SET payload_json=? WHERE event_id=?",
                                   ("{broken" if corruption == "json" else json.dumps(payload), row["event_id"]))
                    provider = FakeProvider()
                    result = notifications.run_notification_retry(self.root, provider)
                    self.assertEqual(provider.history, [])
                    self.assertEqual(result["accepted"], 0)
                    with closing(services._db(self.root)) as db:
                        self.assertEqual(db.execute("SELECT status FROM notification_deliveries WHERE delivery_id=?", (delivery_id,)).fetchone()[0], "dead")

    def test_no_evidence_ask_does_not_call_llm(self):
        task = self.enqueue(payload={"query": "no matching evidence", "limit": 5})
        with patch.object(pipeline, "minimax_chat", side_effect=AssertionError("LLM")):
            services.run_worker_once(self.root)
        result = services.get_task(self.root, task["task_id"])
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["state"], "insufficient_evidence")

    def test_frozen_digest_confirm_hash_recipient_and_single_send(self):
        with patch.dict(os.environ, {"EMAIL_TO": "reader@example.test", "EMAIL_PROVIDER": "fake"}):
            draft = services.preview_digest(self.root, persist=True)
            confirmation = {key: draft[key] for key in ("digest_id", "revision", "payload_hash")}
            with self.assertRaises(ValueError):
                services.enqueue_task(self.root, "digest.send", {**confirmation, "payload_hash": "changed"}, "bad")
            task = services.enqueue_task(self.root, "digest.send", confirmation, "send-1")
            provider = FakeProvider()
            services.run_worker_once(self.root, provider=provider)
            services.run_worker_once(self.root, provider=provider)
            self.assertEqual(len(provider.history), 1)
            result = services.get_task(self.root, task["task_id"])
            self.assertEqual(result["result"]["status"], "accepted")
            self.assertNotEqual(result["result"]["status"], "delivered")
            with patch.dict(os.environ, {"EMAIL_TO": "changed@example.test"}):
                with self.assertRaises(ValueError):
                    services.enqueue_task(self.root, "digest.send", confirmation, "send-2")


@unittest.skipUnless(WEB_INSTALLED, "install requirements-web.txt for HTTP integration checks")
class HTTPTests(Dataset):
    def setUp(self):
        super().setUp()
        from fastapi.testclient import TestClient
        from webapp import create_app
        self.token = "test-dashboard-token-with-at-least-32-chars"
        self.app = create_app(self.root, port=8080, token=self.token, enable_writes=True)
        self.client = TestClient(self.app, base_url="http://127.0.0.1:8080", raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.auth = {"Authorization": f"Bearer {self.token}", "Origin": "http://127.0.0.1:8080", "Idempotency-Key": "http-request-1"}

    def test_all_gets_local_safe_and_no_database_mutation(self):
        before = self.snapshot()
        with patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            for path in ("/api/health", "/api/stats", "/api/videos", "/api/videos/abc123def456",
                         "/api/search?q=Python", "/api/costs", "/api/budget-status", "/api/jobs/failed",
                         "/api/digests", "/api/notifications", "/api/operations/providers"):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200, (path, response.text))
                self.assertEqual(response.json()["schema_version"], 1)
                self.assertNotIn(str(self.root), response.text)
        self.assertEqual(before, self.snapshot())

    def test_host_origin_and_cross_site_requests_are_rejected(self):
        for headers in ({"Host": "evil.test"}, {"Origin": "https://evil.test"}, {"Sec-Fetch-Site": "cross-site"}):
            response = self.client.get("/api/videos", headers=headers)
            self.assertEqual(response.status_code, 403)
            self.assertIn("request_id", response.json()["error"])
        response = self.client.post("/api/ask", json={"query": "Python"}, headers={"Origin": "https://evil.test", **{"Authorization": self.auth["Authorization"]}})
        self.assertEqual(response.status_code, 403)

    def test_write_auth_idempotency_readonly_and_no_get_send(self):
        self.assertEqual(self.client.post("/api/ask", json={"query": "Python"}).status_code, 401)
        self.assertEqual(self.client.get("/api/digest/send").status_code, 405)
        first = self.client.post("/api/ask", json={"query": "Python"}, headers=self.auth)
        self.assertEqual(first.status_code, 202, first.text)
        second = self.client.post("/api/ask", json={"query": "Python"}, headers=self.auth)
        self.assertEqual(first.json()["data"]["task_id"], second.json()["data"]["task_id"])
        polled = self.client.get("/api/tasks/" + first.json()["data"]["task_id"])
        self.assertEqual(polled.json()["data"]["status"], "queued")
        from webapp import create_app
        from fastapi.testclient import TestClient
        with TestClient(create_app(self.root, token=self.token), base_url="http://127.0.0.1:8080") as readonly:
            self.assertEqual(readonly.post("/api/ask", json={"query": "Python"}, headers=self.auth).status_code, 403)

    def test_validation_errors_mask_inputs_and_internal_errors(self):
        for path in ("/api/videos?page_size=10000", "/api/search?q=", "/api/videos?sort=DROP", "/api/videos?date_from=bad"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 422, (path, response.text))
        with patch.object(services, "videos", side_effect=RuntimeError(f"SECRET {self.root}/.env")):
            response = self.client.get("/api/videos")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("SECRET", response.text)
        self.assertNotIn(str(self.root), response.text)
        self.assertIn("retryable", response.json()["error"])
        with patch.object(services, "videos", side_effect=services.Unavailable()):
            response = self.client.get("/api/videos")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "migration_required")

    def test_request_size_and_sql_inputs_are_bounded(self):
        self.assertEqual(self.client.post("/api/ask", json={"query": "x" * 20000}, headers=self.auth).status_code, 413)
        for payload in ({"query": "Python", "filters": "unsafe"}, {"query": "Python", "limit": -1}, {"query": "Python", "limit": True}):
            self.assertEqual(self.client.post("/api/ask", json=payload, headers=self.auth).status_code, 422)
        self.assertEqual(self.client.get("/api/videos?author=%27%20OR%201%3D1--").json()["data"]["total"], 0)

    def test_xss_autoescaped_and_phone_layout_has_viewport(self):
        page = self.client.get("/videos")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("<script>alert(1)</script>", page.text)
        self.assertIn("&lt;script&gt;", page.text)
        self.assertIn('name="viewport"', page.text)
        self.assertNotIn(self.token, page.text)
        self.assertIn("影片資料庫", page.text)

    def test_remote_bind_requires_all_security_gates(self):
        from webapp import create_app
        for options in ({}, {"allow_remote": True}, {"allow_remote": True, "token": self.token},
                        {"allow_remote": True, "token": self.token, "public_origin": "https://dashboard.example.test"}):
            with self.assertRaises(ValueError):
                create_app(self.root, host="0.0.0.0", **options)


if __name__ == "__main__":
    unittest.main()
