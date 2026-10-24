"""Cross-PR contracts: one schema ledger and one grounded retrieval service."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import notifications
import operations
import pipeline
import web_services as services
from test_webapp import Dataset


class WebSchemaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_reading_base_schema_does_not_implicitly_install_dashboard(self):
        with closing(notifications.get_operations_db(self.root)) as db:
            self.assertFalse(operations.dashboard_schema_ready(db))
        before = hashlib.sha256((self.root / "operations.db").read_bytes()).hexdigest()
        with self.assertRaises(services.Unavailable):
            services._db(self.root)
        self.assertEqual(hashlib.sha256((self.root / "operations.db").read_bytes()).hexdigest(), before)
        with closing(notifications.get_operations_db(self.root)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM schema_migrations WHERE version=5").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='operation_tasks'").fetchone())

    def test_optional_schema_has_one_atomic_receipt_and_preserves_library(self):
        with closing(notifications.get_operations_db(self.root)) as db:
            identity = operations.get_or_create_library_id(db)
        with patch.object(pipeline, "request_json", side_effect=AssertionError("provider")), \
                patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            services.initialize(self.root)
            services.initialize(self.root)
        with closing(notifications.get_operations_db(self.root)) as db:
            self.assertEqual(operations.get_or_create_library_id(db), identity)
            self.assertTrue(operations.dashboard_schema_ready(db))
            self.assertEqual([r[0] for r in db.execute("SELECT version FROM schema_migrations ORDER BY version")], [1, 2, 3, 4, 5])
        self.assertEqual(operations.foundation_status(self.root)["status"], "ready")

    def test_failed_optional_migration_rolls_back_tables_and_receipt(self):
        with closing(notifications.get_operations_db(self.root)):
            pass
        broken = tuple(m for m in operations.MIGRATIONS if m[0] < 5) + (
            (5, "dashboard-broken", ("CREATE TABLE operation_tasks (task_id TEXT)", "INVALID SQL")),)
        with patch.object(operations, "MIGRATIONS", broken), self.assertRaises(sqlite3.Error):
            services.initialize(self.root)
        with closing(notifications.get_operations_db(self.root)) as db:
            self.assertFalse(db.execute("SELECT 1 FROM schema_migrations WHERE version=5").fetchone())
            self.assertFalse(db.execute("SELECT 1 FROM sqlite_master WHERE name='operation_tasks'").fetchone())
            self.assertFalse(db.in_transaction)
        services.initialize(self.root)
        with closing(services._db(self.root)) as db:
            self.assertTrue(operations.dashboard_schema_ready(db))

    def test_index_rebuild_retains_the_canonical_operational_library_identity(self):
        with closing(notifications.get_operations_db(self.root)) as db:
            identity = operations.get_or_create_library_id(db)
        with closing(pipeline.index_connection(self.root)) as db:
            self.assertEqual(db.execute("SELECT value FROM library_metadata WHERE key='library_id'").fetchone()[0], identity)
        (self.root / "library.db").unlink()
        with closing(pipeline.index_connection(self.root)) as db:
            self.assertEqual(db.execute("SELECT value FROM library_metadata WHERE key='library_id'").fetchone()[0], identity)

    def test_initial_operations_ledger_adopts_existing_index_identity(self):
        with closing(pipeline.index_connection(self.root)) as db:
            identity = db.execute("SELECT value FROM library_metadata WHERE key='library_id'").fetchone()[0]
            db.commit()
        with closing(notifications.get_operations_db(self.root)) as db:
            self.assertEqual(operations.get_or_create_library_id(db), identity)


class GroundedWebIntegrationTests(Dataset):
    def test_semantic_ask_without_answer_key_does_not_buy_retrieval(self):
        for mode, missing in ((mode, field) for mode in ("semantic", "hybrid") for field in ("key", "model")):
            with self.subTest(mode=mode, missing=missing):
                task = services.enqueue_task(self.root, "ask",
                    {"query": "Python", "mode": mode}, f"no-answer-{mode}-{missing}")
                with patch.dict(os.environ, {"MINIMAX_SUBSCRIPTION_KEY": "" if missing == "key" else "offline-key",
                        "MINIMAX_MODEL": "" if missing == "model" else "MiniMax-M3"}), \
                        patch.object(services, "_cloud_search", side_effect=AssertionError("billable retrieval")) as retrieve:
                    receipt = services.run_worker_once(self.root)
                self.assertEqual(receipt["task_id"], task["task_id"])
                self.assertEqual(receipt["status"], "blocked")
                self.assertEqual(receipt["result"]["state"], "disabled")
                retrieve.assert_not_called()

    def test_web_and_typed_retrieval_share_category_and_local_date_bounds(self):
        import datetime as dt
        from zoneinfo import ZoneInfo
        day = dt.datetime.fromisoformat(self.now).astimezone(ZoneInfo("Asia/Taipei")).date().isoformat()
        filters = {"category": "AI", "author": "Alice", "platform": "youtube",
                   "date_field": "first_completed_at", "date_from": day, "date_to": day}
        web = services.search(self.root, "Python", filters=filters)["items"]
        typed = pipeline.retrieve_evidence(self.root, "Python", 20,
            filters=pipeline.dashboard_search_filter(filters))["evidence"]
        catalog = services.videos(self.root, filters=filters)["items"]
        self.assertEqual({item["occurrence_id"] for item in web}, {item["occurrence_id"] for item in typed})
        self.assertEqual({item["job"] for item in web}, {item["job"] for item in catalog})
        self.assertEqual({item["job"] for item in web}, {"abc123def456"})

    def test_preselected_web_ask_retains_filters_and_verified_source(self):
        filters = {"category": "AI", "kind": "speech", "date_field": "first_completed_at"}
        hit = services.search(self.root, "Python", 1, filters)["items"][0]
        task = services.enqueue_task(self.root, "ask",
            {"query": "Python", "mode": "fts", "limit": 1, "filters": filters}, "grounded-web-1")
        response = json.dumps({"claims": [{"text": hit["text"], "evidence_ids": [hit["occurrence_id"]],
                    "quotes": [hit["text"]], "support_kind": "speaker_statement"}]})
        with patch.dict(os.environ, {"MINIMAX_SUBSCRIPTION_KEY": "offline-key", "MINIMAX_MODEL": "MiniMax-M3"}), \
                patch.object(pipeline, "question_keywords", side_effect=AssertionError("duplicate retrieval")), \
                patch.object(pipeline, "minimax_chat", return_value=response) as chat:
            receipt = services.run_worker_once(self.root)
        self.assertEqual(receipt["task_id"], task["task_id"])
        self.assertEqual(receipt["status"], "succeeded")
        result = receipt["result"]
        self.assertEqual(result["status"], "grounded")
        self.assertEqual(result["retrieval"]["date_field"], "first_completed_at")
        self.assertEqual(result["retrieval"]["filters"]["category"], "AI")
        self.assertEqual(result["citations"][0]["occurrence_id"], hit["occurrence_id"])
        chat.assert_called_once()

    def test_tracked_source_is_verified_internally_and_all_public_evidence_is_sanitized(self):
        job = self.root / "jobs" / "abc123def456"
        manifest = json.loads((job / "manifest.json").read_text())
        manifest["source"]["url"] += "&token=TRACKED_SECRET"
        (job / "manifest.json").write_text(json.dumps(manifest))
        timeline = json.loads((job / "timeline.json").read_text())
        timeline[0]["source"] = {"image": {"url": "https://cdn.example/image.jpg?sig=NESTED_SECRET"}}
        (job / "timeline.json").write_text(json.dumps(timeline))
        pipeline.build_index(self.root)
        filters = {"job": "abc123def456", "kind": "speech"}
        hit = services.search(self.root, "Python", 1, filters)["items"][0]
        self.assertEqual(hit["source"], "https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.assertNotIn("SECRET", json.dumps(hit))
        response = json.dumps({"claims": [{"text": hit["text"], "evidence_ids": [hit["occurrence_id"]],
                    "quotes": [hit["text"]], "support_kind": "speaker_statement"}]})
        task = services.enqueue_task(self.root, "ask",
            {"query": "Python", "mode": "fts", "limit": 1, "filters": filters}, "tracked-source")
        with patch.dict(os.environ, {"MINIMAX_SUBSCRIPTION_KEY": "offline-key", "MINIMAX_MODEL": "MiniMax-M3"}), \
                patch.object(pipeline, "minimax_chat", return_value=response) as chat:
            receipt = services.run_worker_once(self.root)
        self.assertEqual(receipt["task_id"], task["task_id"])
        self.assertEqual(receipt["result"]["status"], "grounded")
        self.assertNotIn("SECRET", json.dumps(receipt))
        self.assertNotIn("SECRET", json.dumps(chat.call_args.args[0]))
        for field in ("citations", "evidence"):
            self.assertEqual(receipt["result"][field][0]["source"], hit["source"])

    def test_changed_source_identity_cannot_be_restored_from_public_projection(self):
        job = self.root / "jobs" / "abc123def456"
        for original, changed in (("https://private.invalid/one?token=first", "https://private.invalid/two?token=second"),
                ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/watch?v=9bZkp7q19f0")):
            with self.subTest(original=original):
                manifest = json.loads((job / "manifest.json").read_text())
                manifest["source"]["url"] = original
                (job / "manifest.json").write_text(json.dumps(manifest))
                pipeline.build_index(self.root)
                hit = services.search(self.root, "Python", 1, {"job": job.name, "kind": "speech"})["items"][0]
                manifest["source"]["url"] = changed
                (job / "manifest.json").write_text(json.dumps(manifest))
                with patch.dict(os.environ, {"MINIMAX_SUBSCRIPTION_KEY": "offline-key", "MINIMAX_MODEL": "MiniMax-M3"}), \
                        patch.object(pipeline, "minimax_chat", side_effect=AssertionError("stale source")) as chat:
                    result = services._answer_preselected(self.root,
                        {"query": "Python", "limit": 1, "filters": {}}, [hit])
                self.assertEqual(result["status"], "insufficient_evidence")
                chat.assert_not_called()
