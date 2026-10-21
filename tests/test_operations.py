"""F0 contracts: one ledger, content identity, atomic completion, safe readers."""
import copy
import datetime
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import cost_tracker
import digests
import notifications
import pipeline
import scoring


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "", "RSS_NOTIFY_WEBHOOK": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def job(self, name="job_a"):
        job = self.root / "jobs" / name
        job.mkdir(parents=True, exist_ok=True)
        artifacts = {
            "transcript": {"segments": [{"start": 0, "end": 1, "text": "hello"}], "cache": {"hits": 0}},
            "timeline": [{"timestamp": 0, "type": "speech", "text": "hello"}],
            "summary": {"brief": "hello"},
        }
        for key, value in artifacts.items():
            pipeline.write_json(job / f"{key}.json", value)
        manifest = {"status": "completed", "schema_version": 1, "source": {"title": "Demo"},
                    "created_at": "2020-01-01T00:00:00Z", "models": {"summary": "model-v1"},
                    "artifacts": {key: str((job / f"{key}.json").relative_to(self.root)) for key in artifacts}}
        pipeline.write_json(job / "manifest.json", manifest)
        return job, manifest

    def rows(self, table):
        with closing(notifications.get_operations_db(self.root)) as conn:
            return [dict(row) for row in conn.execute(f"SELECT * FROM {table}")]

    def test_shared_migrations_preserve_legacy_feature_data(self):
        import operations as ops
        cost_tracker.record_usage(self.root, "groq", "fixture", cache_hit=True)
        with closing(notifications.get_operations_db(self.root)) as conn:
            conn.executescript("DROP TABLE processed_revisions; DROP TABLE library_meta; DROP TABLE schema_migrations;")
        with closing(notifications.get_operations_db(self.root)) as conn:
            self.assertEqual([row[0] for row in conn.execute("SELECT version FROM schema_migrations ORDER BY version")],
                             list(range(1, ops.FOUNDATION_SCHEMA_VERSION + 1)))
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM usage_costs").fetchone()[0], 1)
            self.assertTrue(conn.execute("SELECT name FROM sqlite_master WHERE name='video_scores'").fetchone())

    def test_failed_migration_rolls_back_schema_and_registry(self):
        import operations as ops
        with closing(notifications.get_operations_db(self.root)) as conn:
            with self.assertRaises(sqlite3.Error):
                ops.apply_migrations(conn, [(99, "broken", ("CREATE TABLE half_applied (id INTEGER)", "INVALID SQL"))])
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='half_applied'").fetchone())
            self.assertIsNone(conn.execute("SELECT version FROM schema_migrations WHERE version=99").fetchone())
            self.assertFalse(conn.in_transaction)

    def test_concurrent_initializers_and_move_preserve_one_library_id(self):
        import operations as ops

        def identity(_):
            with closing(notifications.get_operations_db(self.root)) as conn:
                return ops.get_or_create_library_id(conn)

        with ThreadPoolExecutor(max_workers=4) as pool:
            identities = list(pool.map(identity, range(8)))
        self.assertEqual(len(set(identities)), 1)
        uuid.UUID(identities[0])
        moved = self.root / "moved"
        moved.mkdir()
        (self.root / "operations.db").replace(moved / "operations.db")
        with closing(notifications.get_operations_db(moved)) as conn:
            self.assertEqual(ops.get_or_create_library_id(conn), identities[0])
        with closing(notifications.get_operations_db(self.root)) as conn:
            self.assertNotEqual(ops.get_or_create_library_id(conn), identities[0])

    def test_reports_do_not_create_or_migrate_databases(self):
        import operations as ops
        readers = (cost_tracker.get_cost_report, cost_tracker.get_budget_status, cost_tracker.get_provider_health,
                   cost_tracker.get_failed_list, scoring.generate_score_report,
                   lambda root: scoring.explain_score(root, "job_a"), ops.foundation_status)
        for reader in readers:
            with self.subTest(reader=reader):
                self.assertEqual(reader(self.root)["status"], "unavailable")
                self.assertFalse((self.root / "operations.db").exists())
        with closing(sqlite3.connect(self.root / "operations.db")) as conn, conn:
            conn.execute("CREATE TABLE legacy (id INTEGER)")
        original = (self.root / "operations.db").read_bytes()
        for reader in readers:
            with self.subTest(reader=reader):
                self.assertEqual(reader(self.root)["status"], "migration_required")
                self.assertEqual((self.root / "operations.db").read_bytes(), original)

    def test_ready_read_connection_rejects_writes_and_future_schema(self):
        import operations as ops
        notifications.get_operations_db(self.root).close()
        with closing(ops.open_readonly(self.root)) as conn:
            with self.assertRaises(sqlite3.OperationalError):
                conn.execute("DELETE FROM library_meta")
        with closing(notifications.get_operations_db(self.root)) as conn, conn:
            conn.execute("INSERT INTO schema_migrations VALUES (999, 'future', '2026-10-01')")
        original = (self.root / "operations.db").read_bytes()
        self.assertEqual(ops.foundation_status(self.root)["status"], "migration_required")
        self.assertEqual(cost_tracker.get_cost_report(self.root)["status"], "migration_required")
        with self.assertRaisesRegex(RuntimeError, "schema"):
            notifications.get_operations_db(self.root)
        self.assertEqual((self.root / "operations.db").read_bytes(), original)

    def test_completion_is_content_based_and_preserves_first_eligibility(self):
        job, manifest = self.job()
        first = notifications.record_pipeline_completed_event(self.root, job, manifest)
        self.assertIsNotNone(first)
        initial = self.rows("processed_revisions")[0]
        self.assertEqual(initial["backfill"], 0)
        changed_manifest = copy.deepcopy(manifest)
        changed_manifest.update(created_at="2030-01-01", categories=["changed"], indexed_at="later", job_dir="C:/moved")
        transcript = json.loads((job / "transcript.json").read_text())
        transcript["cache"] = {"hits": 99, "misses": 0}
        pipeline.write_json(job / "transcript.json", transcript)
        self.assertEqual(notifications.record_pipeline_completed_event(self.root, job, changed_manifest), first)
        self.assertEqual(len(self.rows("processed_revisions")), 1)
        self.assertEqual(self.rows("processed_revisions")[0]["eligible_at"], initial["eligible_at"])
        self.assertEqual(len(self.rows("notification_events")), 1)
        pipeline.write_json(job / "summary.json", {"brief": "changed content"})
        second = notifications.record_pipeline_completed_event(self.root, job, changed_manifest)
        self.assertNotEqual(second, first)
        self.assertEqual(len(self.rows("processed_revisions")), 2)
        self.assertEqual(len(self.rows("notification_events")), 2)
        changed_manifest["models"]["summary"] = "model-v2"
        self.assertNotEqual(notifications.record_pipeline_completed_event(self.root, job, changed_manifest), second)

    def test_completion_rolls_back_with_outbox_failure(self):
        job, manifest = self.job()
        with closing(notifications.get_operations_db(self.root)) as conn, conn:
            conn.execute("CREATE TRIGGER reject_event BEFORE INSERT ON notification_events BEGIN SELECT RAISE(ABORT, 'fixture'); END")
        with patch("notifications.get_configured_provider") as provider:
            self.assertIsNone(notifications.record_pipeline_completed_event(self.root, job, manifest))
            provider.assert_not_called()
        self.assertEqual(self.rows("processed_revisions"), [])
        self.assertEqual(self.rows("notification_events"), [])

    def test_concurrent_completion_creates_one_revision_and_event(self):
        job, manifest = self.job()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: notifications.record_pipeline_completed_event(self.root, job, copy.deepcopy(manifest)), range(8)))
        self.assertNotIn(None, results)
        self.assertEqual(len(set(results)), 1)
        self.assertEqual(len(self.rows("processed_revisions")), 1)
        self.assertEqual(len(self.rows("notification_events")), 1)

    def test_concurrent_enqueue_reuses_one_delivery(self):
        with closing(notifications.get_operations_db(self.root)) as conn:
            event = notifications.record_event(conn, "fixture", None, None, None, {}, "enqueue-fixture")
        barrier, real_uuid = threading.Barrier(4), uuid.uuid4

        def synchronized_uuid():
            barrier.wait(timeout=10)
            return real_uuid()

        def enqueue(_):
            with closing(notifications.get_operations_db(self.root)) as conn:
                return notifications.enqueue_delivery(conn, event, "email", "fixture@example.test", "FakeProvider", "hash")

        # Both callers pass the old SELECT before either INSERT; this is an
        # actual race rather than a sequential "two workers" approximation.
        with patch("notifications.uuid.uuid4", side_effect=synchronized_uuid), ThreadPoolExecutor(max_workers=4) as pool:
            deliveries = list(pool.map(enqueue, range(4)))
        self.assertEqual(len(set(deliveries)), 1)
        self.assertEqual(len(self.rows("notification_deliveries")), 1)

    def test_media_location_and_provider_file_id_do_not_change_revision(self):
        import operations as ops
        job, manifest = self.job()
        manifest["artifacts"]["visual"] = str((job / "visual.json").relative_to(self.root))
        visual = {"timestamp": 0, "screen_type": "影片", "visual_summary": "screen",
                  "visible_text": [], "important_details": [], "confidence": 0.9,
                  "analysis_method": "minimax_video", "source_video": "C:/old/source.mp4", "minimax_file_id": "old"}
        pipeline.write_json(job / "visual.json", [visual])
        first, _ = ops.completion_snapshot(self.root, job, manifest)
        visual.update(source_video="D:/moved/source.mp4", minimax_file_id="new")
        pipeline.write_json(job / "visual.json", [visual])
        self.assertEqual(ops.completion_snapshot(self.root, job, manifest)[0], first)
        visual["visual_summary"] = "changed evidence"
        pipeline.write_json(job / "visual.json", [visual])
        self.assertNotEqual(ops.completion_snapshot(self.root, job, manifest)[0], first)

    def test_legacy_warm_pipeline_rerun_does_not_notify_history(self):
        import email_providers
        original_root = self.root
        real_record = notifications.record_pipeline_completed_event
        for fail_first, changed in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(fail_first=fail_first, changed=changed):
                self.root = original_root / f"{fail_first}-{changed}"
                self.root.mkdir()
                url = "https://example.test/legacy"
                job, manifest = self.job(pipeline.job_key(url))
                transcript = json.loads((job / "transcript.json").read_text())
                pipeline.write_json(job / "timeline.json", pipeline.merge_timeline(transcript, []))
                manifest["models"] = {"transcript": None, "visual": None, "summary": "MiniMax-M3"}
                pipeline.write_json(job / "manifest.json", manifest)
                provider = email_providers.FakeProvider(mode="accepted")
                failed = False

                def transient_failure(*args, **kwargs):
                    nonlocal failed
                    if fail_first and not failed and kwargs.get("backfill"):
                        failed = True
                        return None
                    return real_record(*args, **kwargs)

                env = {"GROQ_API_KEY": "offline-fixture", "MINIMAX_SUBSCRIPTION_KEY": "offline-fixture",
                       "DISCORD_WEBHOOK_URL": "https://example.test/webhook", "MINIMAX_BASE_URL": "https://api.minimax.io/v1"}
                with patch("sys.argv", ["pipeline.py", "--root", str(self.root), "--url", url, "--no-video"]):
                    args = pipeline.parse_args()
                with patch.dict(os.environ, env), patch("pipeline.load_dotenv"), \
                     patch("pipeline.minimax_model", return_value="MiniMax-M3"), \
                     patch("pipeline.create_summary", return_value={"brief": "new evidence" if changed else "hello"}), \
                     patch("notifications.record_pipeline_completed_event", side_effect=transient_failure), \
                     patch("notifications.get_configured_provider", return_value=provider), cost_tracker.cost_root_scope(None):
                    pipeline.run_pipeline(args)
                self.assertEqual(len(provider.history), int(changed))
                rows = self.rows("processed_revisions")
                self.assertEqual(len(rows), 2 if changed and not fail_first else 1)
                self.assertEqual(sum(row["eligible_at"] is not None for row in rows), int(changed))

    def test_incomplete_local_missing_and_escaping_artifacts_cannot_register_or_send(self):
        job, manifest = self.job()
        cases = [{**manifest, "status": status} for status in ("running", "failed", "dry-run", "local-only")]
        cases += [{**manifest, "artifacts": {}}, {**manifest, "artifacts": {**manifest["artifacts"], "summary": "../outside.json"}}]
        for candidate in cases:
            with self.subTest(candidate=candidate), patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://example.test/webhook"}), \
                 patch("notifications.get_configured_provider") as provider:
                self.assertIsNone(notifications.record_pipeline_completed_event(self.root, job, candidate))
                provider.assert_not_called()
        self.assertFalse((self.root / "operations.db").exists())

    def test_parseable_but_invalid_evidence_cannot_register_or_send(self):
        job, manifest = self.job()
        cases = [("summary", {"error": "provider failed"}), ("summary", {"short_summary": " "}),
                 ("transcript", {"segments": [None, "invalid row"]}),
                 ("transcript", {"segments": [{"start": True, "end": 1, "text": "bad"}]}),
                 ("transcript", {"segments": [{"start": 1, "end": 0, "text": "bad"}]})]
        for name, value in cases:
            with self.subTest(name=name, value=value):
                original = (job / f"{name}.json").read_bytes()
                pipeline.write_json(job / f"{name}.json", value)
                with patch("notifications.get_configured_provider") as provider:
                    self.assertIsNone(notifications.record_pipeline_completed_event(self.root, job, manifest))
                    provider.assert_not_called()
                (job / f"{name}.json").write_bytes(original)
        self.assertFalse((self.root / "operations.db").exists())
        (job / "summary.json").write_text("{bad json", encoding="utf-8")
        self.assertIsNone(notifications.record_pipeline_completed_event(self.root, job, manifest))
        self.assertFalse((self.root / "operations.db").exists())

    def test_invalid_timeline_cannot_register_or_send(self):
        job, manifest = self.job()
        for timeline in ([{}], [{"timestamp": True, "type": "speech", "text": "bad"}],
                         [{"timestamp": -1, "type": "speech", "text": "bad"}],
                         [{"timestamp": 10**400, "type": "speech", "text": "bad"}],
                         [{"timestamp": 0, "type": "speech"}],
                         [{"timestamp": 0, "type": "visual", "data": {}}]):
            with self.subTest(timeline=timeline), \
                 patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://example.test/webhook"}), \
                 patch("notifications.get_configured_provider") as provider:
                pipeline.write_json(job / "timeline.json", timeline)
                self.assertIsNone(notifications.record_pipeline_completed_event(self.root, job, manifest))
                provider.assert_not_called()
                self.assertFalse((self.root / "operations.db").exists())

    def test_invalid_timestamp_does_not_abort_digest_or_reconciliation(self):
        import operations as ops
        job, manifest = self.job()
        notifications.record_pipeline_completed_event(self.root, job, manifest)
        self.job("job_b")
        pipeline.write_json(job / "transcript.json",
                            {"segments": [{"start": 0, "end": 10**400, "text": "bad"}]})
        now = datetime.datetime.now(datetime.timezone.utc)
        with self.subTest(reader="digest"):
            self.assertEqual(digests.collect_digest_candidates(self.root, now - datetime.timedelta(days=1),
                                                               now + datetime.timedelta(days=1)), [])
        with self.subTest(reader="reconcile"):
            self.assertEqual(ops.reconcile_processed_revisions(self.root),
                             {"scanned": 2, "registered": 1, "skipped": 1})

    def test_overflowing_visual_confidence_is_invalid(self):
        visual = {"screen_type": "影片", "visual_summary": "screen", "visible_text": [],
                  "important_details": [], "confidence": 10**400}
        self.assertFalse(pipeline.usable_visual_observation(visual))

    def test_digest_does_not_render_summary_outside_verified_snapshot(self):
        import operations as ops
        job, manifest = self.job()
        notifications.record_pipeline_completed_event(self.root, job, manifest)
        now = datetime.datetime.now(datetime.timezone.utc)
        start, end = now - datetime.timedelta(days=1), now + datetime.timedelta(days=1)
        original = (job / "summary.json").read_bytes()
        declared = job / "verified-summary.json"
        declared.write_bytes(original)
        manifest["artifacts"]["summary"] = str(declared.relative_to(self.root))
        pipeline.write_json(job / "manifest.json", manifest)
        pipeline.write_json(job / "summary.json", {"brief": "unregistered content"})
        with self.subTest(case="different declared artifact"):
            self.assertEqual(digests.collect_digest_candidates(self.root, start, end), [])

        manifest["artifacts"]["summary"] = str((job / "summary.json").relative_to(self.root))
        pipeline.write_json(job / "manifest.json", manifest)

        def restore_before_validation(*args, **kwargs):
            (job / "summary.json").write_bytes(original)
            return ops.completion_snapshot(*args, **kwargs)

        with self.subTest(case="concurrent rewrite"), \
             patch("digests.completion_snapshot", side_effect=restore_before_validation):
            self.assertEqual(digests.collect_digest_candidates(self.root, start, end), [])

    def test_digest_renders_pipeline_short_summary(self):
        job, manifest = self.job()
        pipeline.write_json(job / "summary.json", {"short_summary": "pipeline summary"})
        notifications.record_pipeline_completed_event(self.root, job, manifest)
        now = datetime.datetime.now(datetime.timezone.utc)
        candidates = digests.collect_digest_candidates(self.root, now - datetime.timedelta(days=1),
                                                       now + datetime.timedelta(days=1))
        self.assertEqual([item["summary"] for item in candidates], ["pipeline summary"])

    def test_immediate_notification_senders_claim_only_their_own_delivery(self):
        import email_providers
        original_root = self.root
        for sender in (notifications.run_digest_send, notifications.run_notify_test):
            with self.subTest(sender=sender.__name__):
                self.root = original_root / sender.__name__
                job, manifest = self.job()
                with patch("notifications._utc_now_iso", return_value="2026-09-05T10:00:00+00:00"):
                    notifications.record_pipeline_completed_event(self.root, job, manifest)
                with closing(notifications.get_operations_db(self.root)) as conn:
                    event = notifications.record_event(conn, "fixture", None, None, None, {}, sender.__name__)
                    unrelated = notifications.enqueue_delivery(
                        conn, event, "webhook", "https://example.test/unrelated", "WebhookProvider", "hash")
                provider = email_providers.FakeProvider(mode="accepted")
                real_send = provider.send

                def assert_recipient(**kwargs):
                    self.assertEqual(kwargs["recipient"], "intended@example.test")
                    return real_send(**kwargs)

                with patch.dict(os.environ, {"EMAIL_TO": "intended@example.test", "NOTIFY_EMAIL_ENABLED": "true"}), \
                     patch.object(provider, "send", side_effect=assert_recipient):
                    options = {"date_str": "2026-09-05"} if sender is notifications.run_digest_send else {}
                    result = sender(self.root, provider=provider, **options)
                self.assertEqual(result["status"], "accepted")
                self.assertEqual([item["recipient"] for item in provider.history], ["intended@example.test"])
                deliveries = {row["delivery_id"]: row for row in self.rows("notification_deliveries")}
                self.assertEqual(deliveries[unrelated]["status"], "pending")
                self.assertEqual(deliveries[result["delivery_id"]]["status"], "accepted")

    def test_digest_send_records_item_disposition(self):
        import email_providers
        job, manifest = self.job()
        with patch("notifications._utc_now_iso", return_value="2026-09-05T10:00:00+00:00"):
            notifications.record_pipeline_completed_event(self.root, job, manifest)
        with patch.dict(os.environ, {"EMAIL_TO": "intended@example.test", "NOTIFY_EMAIL_ENABLED": "true"}):
            result = notifications.run_digest_send(self.root, date_str="2026-09-05",
                                                   provider=email_providers.FakeProvider(mode="accepted"))
        self.assertEqual(result["status"], "accepted")
        self.assertEqual([item["disposition"] for item in self.rows("digest_items")], ["delivered"])

    def test_reconciliation_backfills_unknown_time_and_outbox_without_sending(self):
        import operations as ops
        job, manifest = self.job()
        with patch("notifications.get_configured_provider") as provider:
            self.assertEqual(ops.reconcile_processed_revisions(self.root)["registered"], 1)
            self.assertEqual(ops.reconcile_processed_revisions(self.root)["registered"], 0)
            provider.assert_not_called()
        row = self.rows("processed_revisions")[0]
        self.assertEqual(row["backfill"], 1)
        self.assertIsNone(row["first_completed_at"])
        self.assertIsNone(row["eligible_at"])
        self.assertEqual(len(self.rows("notification_events")), 1)
        # An unchanged warm rerun must not turn historical content into new work.
        notifications.record_pipeline_completed_event(self.root, job, manifest)
        self.assertIsNone(self.rows("processed_revisions")[0]["eligible_at"])
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertEqual(digests.collect_digest_candidates(self.root, now - datetime.timedelta(days=3650),
                                                          now + datetime.timedelta(days=3650)), [])

    def test_digest_uses_exact_content_revision_and_fails_closed(self):
        job, manifest = self.job()
        notifications.record_pipeline_completed_event(self.root, job, manifest)
        now = datetime.datetime.now(datetime.timezone.utc)
        start, end = now - datetime.timedelta(days=1), now + datetime.timedelta(days=1)
        self.assertEqual(len(digests.collect_digest_candidates(self.root, start, end)), 1)
        # A rewrite can erase manifest hints; the registry remains authoritative.
        pipeline.write_json(job / "manifest.json", manifest)
        self.assertEqual(len(digests.collect_digest_candidates(self.root, start, end)), 1)
        pipeline.write_json(job / "summary.json", {"brief": "unregistered edit"})
        self.assertEqual(digests.collect_digest_candidates(self.root, start, end), [])
        (self.root / "operations.db").write_bytes(b"corrupt sqlite fixture")
        self.assertEqual(cost_tracker.get_cost_report(self.root)["status"], "unavailable")
        self.assertEqual(digests.collect_digest_candidates(self.root, start, end), [])

    def test_unregistered_history_has_no_filesystem_time_fallback(self):
        self.job()
        now = datetime.datetime.now(datetime.timezone.utc)
        self.assertEqual(digests.collect_digest_candidates(self.root, now - datetime.timedelta(days=3650),
                                                          now + datetime.timedelta(days=3650)), [])

    def test_search_rebuild_preserves_operations_and_vector_cache(self):
        job, manifest = self.job()
        notifications.record_pipeline_completed_event(self.root, job, manifest)
        original = (self.root / "operations.db").read_bytes()
        with closing(pipeline.index_connection(self.root)) as conn, conn:
            conn.execute("ALTER TABLE videos ADD COLUMN fixture_extension TEXT")
            conn.execute("INSERT INTO vectors VALUES ('hash', 'model', 1, X'00')")
            pipeline.index_job(conn, job)
        pipeline.build_index(self.root)
        self.assertEqual((self.root / "operations.db").read_bytes(), original)
        with closing(pipeline.index_connection(self.root)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM vectors").fetchone()[0], 1)

    def test_ops_reconcile_cli_is_local_and_does_not_run_pipeline(self):
        self.job()
        with patch("sys.argv", ["pipeline.py", "--root", str(self.root), "--ops-reconcile"]), \
             patch("pipeline.run_pipeline") as run, patch("notifications.get_configured_provider") as provider:
            self.assertEqual(pipeline.main(), 0)
            run.assert_not_called()
            provider.assert_not_called()
        self.assertEqual(len(self.rows("processed_revisions")), 1)
