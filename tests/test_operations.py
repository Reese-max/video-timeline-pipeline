"""F0 operations foundation tests (Issue #7 roadmap).

Covers:
- F0.1: a single versioned migration registry for operations.db, and a
  read-only access contract that never runs DDL — an uninitialized store
  reports ``unavailable``/``migration_required`` instead of being created.
- F0.2: stable per-collection ``library_id``, content-derived
  ``analysis_revision`` (index times, run times and absolute paths are never
  part of the hash), the ``processed_revisions`` completion registry,
  revision + outbox event committed in one transaction, and backfill rows
  that are not eligible for notification by default.
"""

from __future__ import annotations

import datetime
import importlib
import json
import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import digests
import notifications


def _ops():
    return importlib.import_module("operations")


def _write_job(root: Path, job: str, manifest: dict, summary: dict | None = None) -> Path:
    job_dir = root / "jobs" / job
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if summary is not None:
        (job_dir / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return job_dir


class FoundationSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.conn = notifications.get_operations_db(self.root)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_foundation_tables_and_migration_registry(self) -> None:
        _ops()
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertIn("schema_migrations", tables)
        self.assertIn("library_meta", tables)
        self.assertIn("processed_revisions", tables)

        versions = {
            row[0] for row in self.conn.execute("SELECT version FROM schema_migrations")
        }
        self.assertIn(1, versions)

        columns = {
            row[1]
            for row in self.conn.execute("PRAGMA table_info(processed_revisions)")
        }
        for col in (
            "library_id",
            "job",
            "analysis_revision",
            "artifact_hashes",
            "first_completed_at",
            "eligible_at",
            "backfill",
            "status",
        ):
            self.assertIn(col, columns)

        # Re-running initialization must succeed without error.
        notifications._init_operations_schema(self.conn)

    def test_failed_migration_leaves_no_partial_schema(self) -> None:
        ops = _ops()
        with self.assertRaises(sqlite3.Error):
            ops.apply_migrations(
                self.conn,
                (
                    (
                        999,
                        "broken_migration",
                        (
                            "CREATE TABLE partial_t (id TEXT)",
                            "INSERT INTO nonexistent_table VALUES (1)",
                        ),
                    ),
                ),
            )
        tables = {
            row[0]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        self.assertNotIn("partial_t", tables)
        applied = {
            row[0] for row in self.conn.execute("SELECT version FROM schema_migrations")
        }
        self.assertNotIn(999, applied)


class LibraryIdentityTests(unittest.TestCase):
    def test_library_id_stable_across_connections(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn1 = notifications.get_operations_db(root)
            id1 = ops.get_or_create_library_id(conn1)
            conn1.close()

            conn2 = notifications.get_operations_db(root)
            id2 = ops.get_or_create_library_id(conn2)
            conn2.close()

            self.assertEqual(id1, id2)
            self.assertEqual(str(uuid.UUID(id1)), id1)

            with tempfile.TemporaryDirectory() as other:
                conn3 = notifications.get_operations_db(Path(other))
                id3 = ops.get_or_create_library_id(conn3)
                conn3.close()
            self.assertNotEqual(id1, id3)


class RevisionIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.job_dir = self.root / "jobs" / "job_a"
        self.job_dir.mkdir(parents=True)
        (self.job_dir / "summary.json").write_text(
            json.dumps({"brief": "summary A"}), encoding="utf-8"
        )
        (self.job_dir / "transcript.json").write_text(
            json.dumps({"segments": [{"t": 0, "text": "hello"}]}), encoding="utf-8"
        )
        self.manifest = {
            "schema_version": 1,
            "status": "completed",
            "created_at": "2026-01-01T00:00:00Z",
            "job_dir": "jobs/job_a",
            "artifacts": {
                "summary": "jobs/job_a/summary.json",
                "transcript": "jobs/job_a/transcript.json",
            },
        }

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_revision_ignores_volatile_manifest_fields(self) -> None:
        ops = _ops()
        hashes = ops.hash_job_artifacts(self.root, self.manifest)
        rev1 = ops.compute_analysis_revision(self.manifest, hashes)

        mutated = dict(self.manifest)
        mutated["created_at"] = "2026-09-09T09:09:09Z"
        mutated["indexed_at"] = "2026-09-10T00:00:00Z"
        mutated["job_dir"] = "/absolutely/different/root/jobs/job_a"
        mutated["processed_at"] = "2027-01-01T00:00:00Z"
        rev2 = ops.compute_analysis_revision(mutated, hashes)
        self.assertEqual(rev1, rev2)

    def test_revision_changes_with_artifact_bytes(self) -> None:
        ops = _ops()
        rev1 = ops.compute_analysis_revision(
            self.manifest, ops.hash_job_artifacts(self.root, self.manifest)
        )
        (self.job_dir / "summary.json").write_text(
            json.dumps({"brief": "summary A revised"}), encoding="utf-8"
        )
        rev2 = ops.compute_analysis_revision(
            self.manifest, ops.hash_job_artifacts(self.root, self.manifest)
        )
        self.assertNotEqual(rev1, rev2)

    def test_revision_stable_across_root_relocation(self) -> None:
        ops = _ops()
        rev1 = ops.compute_analysis_revision(
            self.manifest, ops.hash_job_artifacts(self.root, self.manifest)
        )
        with tempfile.TemporaryDirectory() as tmp2:
            root2 = Path(tmp2)
            job2 = root2 / "jobs" / "job_a"
            job2.mkdir(parents=True)
            for name in ("summary.json", "transcript.json"):
                (job2 / name).write_bytes((self.job_dir / name).read_bytes())
            rev2 = ops.compute_analysis_revision(
                self.manifest, ops.hash_job_artifacts(root2, self.manifest)
            )
        self.assertEqual(rev1, rev2)

    def test_out_of_root_artifact_paths_are_never_hashed(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp2:
            foreign = Path(tmp2) / "foreign.json"
            foreign.write_text("foreign bytes", encoding="utf-8")
            manifest = dict(self.manifest)
            manifest["artifacts"] = {"summary": str(foreign)}
            # An absolute path recorded before a library move is provenance,
            # not content of this root — it must not be hashed as foreign bytes.
            self.assertEqual(ops.hash_job_artifacts(self.root, manifest), {})


class ProcessedRevisionRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.conn = notifications.get_operations_db(self.root)

    def tearDown(self) -> None:
        self.conn.close()
        self.tmp_dir.cleanup()

    def test_register_is_idempotent_per_revision(self) -> None:
        ops = _ops()
        with self.conn:
            row1, created1 = ops.register_processed_revision(
                self.conn,
                library_id="lib-1",
                job="job_a",
                analysis_revision="rev1",
                artifact_hashes={"summary": "abc"},
                first_completed_at="2026-09-01T00:00:00Z",
                eligible_at="2026-09-01T00:00:00Z",
                backfill=False,
                status="completed",
            )
        self.assertTrue(created1)
        with self.conn:
            row2, created2 = ops.register_processed_revision(
                self.conn,
                library_id="lib-1",
                job="job_a",
                analysis_revision="rev1",
                artifact_hashes={"summary": "abc"},
                first_completed_at="2026-09-02T00:00:00Z",
                eligible_at="2026-09-02T00:00:00Z",
                backfill=False,
                status="completed",
            )
        self.assertFalse(created2)
        self.assertEqual(row1["revision_id"], row2["revision_id"])
        # Original completion times are preserved on re-registration.
        self.assertEqual(row2["first_completed_at"], "2026-09-01T00:00:00Z")

        count = self.conn.execute(
            "SELECT COUNT(*) FROM processed_revisions"
        ).fetchone()[0]
        self.assertEqual(count, 1)


class CompletionHookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def _manifest(self, status: str = "completed") -> dict:
        return {
            "schema_version": 1,
            "status": status,
            "created_at": "2026-09-30T00:00:00Z",
            "source": {"title": "Demo", "url": "https://example.com/v"},
            "artifacts": {"summary": "jobs/job_a/summary.json"},
        }

    def test_completion_registers_revision_and_event(self) -> None:
        ops = _ops()
        job_dir = _write_job(
            self.root, "job_a", self._manifest(), {"brief": "hello"}
        )
        event_id = notifications.record_pipeline_completed_event(
            self.root, job_dir, self._manifest()
        )
        self.assertIsNotNone(event_id)

        conn = notifications.get_operations_db(self.root)
        try:
            revisions = conn.execute("SELECT * FROM processed_revisions").fetchall()
            self.assertEqual(len(revisions), 1)
            rev = revisions[0]
            self.assertEqual(rev["job"], "job_a")
            self.assertEqual(rev["status"], "completed")
            self.assertEqual(rev["backfill"], 0)
            self.assertIsNotNone(rev["first_completed_at"])
            self.assertIsNotNone(rev["eligible_at"])
            self.assertTrue(rev["library_id"])

            events = conn.execute("SELECT * FROM notification_events").fetchall()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["event_type"], "video.completed")
            self.assertEqual(events[0]["analysis_revision"], rev["analysis_revision"])
            self.assertEqual(events[0]["library_id"], rev["library_id"])
        finally:
            conn.close()

        # Derived identity is written back into manifest.json for readers.
        on_disk = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(on_disk["analysis_revision"], rev["analysis_revision"])
        self.assertEqual(on_disk["library_id"], rev["library_id"])
        self.assertEqual(on_disk["eligible_at"], rev["eligible_at"])

    def test_revision_and_event_commit_in_one_transaction(self) -> None:
        _ops()
        job_dir = _write_job(
            self.root, "job_a", self._manifest(), {"brief": "hello"}
        )
        with mock.patch(
            "notifications._insert_event", side_effect=sqlite3.Error("boom")
        ):
            result = notifications.record_pipeline_completed_event(
                self.root, job_dir, self._manifest()
            )
        self.assertIsNone(result)

        conn = notifications.get_operations_db(self.root)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM processed_revisions").fetchone()[0],
                0,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_rerun_same_content_is_not_a_new_notifiable_video(self) -> None:
        _ops()
        manifest = self._manifest()
        job_dir = _write_job(self.root, "job_a", manifest, {"brief": "hello"})
        first = notifications.record_pipeline_completed_event(self.root, job_dir, manifest)
        second = notifications.record_pipeline_completed_event(
            self.root, job_dir, self._manifest()
        )
        self.assertEqual(first, second)

        conn = notifications.get_operations_db(self.root)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM processed_revisions").fetchone()[0],
                1,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM notification_events").fetchone()[0],
                1,
            )
        finally:
            conn.close()

        # eligible_at written back equals the FIRST completion, not the rerun.
        on_disk = json.loads((job_dir / "manifest.json").read_text(encoding="utf-8"))
        conn = notifications.get_operations_db(self.root)
        try:
            row = conn.execute("SELECT eligible_at FROM processed_revisions").fetchone()
        finally:
            conn.close()
        self.assertEqual(on_disk["eligible_at"], row["eligible_at"])

    def test_changed_content_registers_new_revision(self) -> None:
        _ops()
        manifest = self._manifest()
        job_dir = _write_job(self.root, "job_a", manifest, {"brief": "v1"})
        notifications.record_pipeline_completed_event(self.root, job_dir, manifest)
        (job_dir / "summary.json").write_text(
            json.dumps({"brief": "v2 changed"}), encoding="utf-8"
        )
        notifications.record_pipeline_completed_event(
            self.root, job_dir, self._manifest()
        )
        conn = notifications.get_operations_db(self.root)
        try:
            rows = conn.execute(
                "SELECT DISTINCT analysis_revision FROM processed_revisions"
            ).fetchall()
        finally:
            conn.close()
        self.assertEqual(len(rows), 2)

    def test_non_completed_manifest_is_not_registered(self) -> None:
        _ops()
        job_dir = _write_job(
            self.root, "job_a", self._manifest(status="local-only"), {"brief": "x"}
        )
        notifications.record_pipeline_completed_event(
            self.root, job_dir, self._manifest(status="local-only")
        )
        conn = notifications.get_operations_db(self.root)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM processed_revisions").fetchone()[0],
                0,
            )
        finally:
            conn.close()

    def test_stale_eligible_at_not_trusted_for_new_revision(self) -> None:
        """A manifest eligible_at from different content must not backdate a
        new revision's first completion — that would drop it from digests."""
        _ops()
        manifest = self._manifest()
        manifest["eligible_at"] = "2020-01-01T00:00:00Z"
        # Either no recorded revision, or a recorded revision that does not
        # match the freshly computed content hash → distrust the timestamp.
        manifest["analysis_revision"] = "stale-or-other"
        job_dir = _write_job(self.root, "job_a", manifest, {"brief": "hi"})
        notifications.record_pipeline_completed_event(self.root, job_dir, manifest)

        conn = notifications.get_operations_db(self.root)
        try:
            row = conn.execute("SELECT * FROM processed_revisions").fetchone()
        finally:
            conn.close()
        self.assertNotEqual(row["first_completed_at"], "2020-01-01T00:00:00Z")
        self.assertNotEqual(row["eligible_at"], "2020-01-01T00:00:00Z")


class ReadOnlyContractTests(unittest.TestCase):
    def test_status_unavailable_when_db_missing(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            status = ops.foundation_status(root)
            self.assertEqual(status["status"], "unavailable")
            # The read path must not create the database implicitly.
            self.assertFalse((root / "operations.db").exists())

    def test_status_ready_after_init(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn = notifications.get_operations_db(root)
            ops.get_or_create_library_id(conn)
            conn.close()
            status = ops.foundation_status(root)
            self.assertEqual(status["status"], "ready")
            self.assertTrue(status["library_id"])
            self.assertGreaterEqual(status["schema_version"], 1)

    def test_eligibility_index_without_db_returns_empty(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(ops.load_eligibility_index(root), {})
            self.assertFalse((root / "operations.db").exists())

    def test_corrupt_store_reports_unavailable_and_fails_closed(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "operations.db").write_bytes(b"not a sqlite file")
            status = ops.foundation_status(root)
            self.assertEqual(status["status"], "unavailable")
            # Registry present but unreadable → None so consumers fail closed.
            self.assertIsNone(ops.load_eligibility_index(root))
            _write_job(
                root,
                "job_x",
                {
                    "status": "completed",
                    "created_at": datetime.datetime.now(
                        datetime.timezone.utc
                    ).isoformat(),
                    "source": {"title": "X"},
                },
                {"brief": "x"},
            )
            start = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)
            end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)
            self.assertEqual(
                digests.collect_digest_candidates(root, start, end), []
            )

    def test_second_connection_schema_init_is_clean(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            conn1 = notifications.get_operations_db(root)
            conn1.close()
            # A second opener must not trip on already-applied migrations.
            conn2 = notifications.get_operations_db(root)
            try:
                ops.ensure_foundation_schema(conn2)
                versions = {
                    r[0]
                    for r in conn2.execute("SELECT version FROM schema_migrations")
                }
                self.assertEqual(versions, {1})
            finally:
                conn2.close()


class ReconcileBackfillTests(unittest.TestCase):
    def test_backfill_marks_unknown_first_completion(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_job(
                root,
                "legacy_job",
                {
                    "status": "completed",
                    "created_at": "2026-01-01T00:00:00Z",
                    "source": {"title": "Old Video"},
                    "artifacts": {},
                },
                {"brief": "old"},
            )
            result = ops.reconcile_processed_revisions(root)
            self.assertEqual(result["registered"], 1)

            conn = notifications.get_operations_db(root)
            try:
                row = conn.execute(
                    "SELECT * FROM processed_revisions WHERE job='legacy_job'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row["backfill"], 1)
            self.assertIsNone(row["first_completed_at"])
            self.assertIsNone(row["eligible_at"])

    def test_backfill_preserves_recorded_eligible_at(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # The completion hook writes analysis_revision and eligible_at as
            # a pair — both present means the timestamp belongs to this
            # content and is safe to trust.
            _write_job(
                root,
                "job_b",
                {
                    "status": "completed",
                    "analysis_revision": "rev-B",
                    "eligible_at": "2026-08-15T12:00:00Z",
                    "source": {"title": "B"},
                    "artifacts": {},
                },
                {"brief": "b"},
            )
            ops.reconcile_processed_revisions(root)
            conn = notifications.get_operations_db(root)
            try:
                row = conn.execute(
                    "SELECT * FROM processed_revisions WHERE job='job_b'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row["backfill"], 1)
            self.assertEqual(row["eligible_at"], "2026-08-15T12:00:00Z")
            self.assertEqual(row["first_completed_at"], "2026-08-15T12:00:00Z")

    def test_backfill_distrusts_eligible_at_without_revision(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # A bare eligible_at (no analysis_revision pair) may belong to
            # different content — never fabricate a first completion from it.
            _write_job(
                root,
                "job_c",
                {
                    "status": "completed",
                    "eligible_at": "2026-08-15T12:00:00Z",
                    "source": {"title": "C"},
                    "artifacts": {},
                },
                {"brief": "c"},
            )
            ops.reconcile_processed_revisions(root)
            conn = notifications.get_operations_db(root)
            try:
                row = conn.execute(
                    "SELECT * FROM processed_revisions WHERE job='job_c'"
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(row["backfill"], 1)
            self.assertIsNone(row["eligible_at"])
            self.assertIsNone(row["first_completed_at"])

    def test_reconcile_skips_non_completed(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_job(root, "failed_job", {"status": "failed"}, {"brief": "x"})
            result = ops.reconcile_processed_revisions(root)
            self.assertEqual(result["registered"], 0)


class DigestEligibilityTests(unittest.TestCase):
    def _window_covering(self, iso: str) -> tuple:
        dt = datetime.datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return (
            dt - datetime.timedelta(hours=1),
            dt + datetime.timedelta(hours=1),
        )

    def test_backfilled_job_is_not_digested(self) -> None:
        ops = _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            _write_job(
                root,
                "legacy_job",
                {
                    "status": "completed",
                    "created_at": now,
                    "source": {"title": "Legacy"},
                    "artifacts": {},
                },
                {"brief": "legacy"},
            )
            ops.reconcile_processed_revisions(root)
            start, end = self._window_covering(now)
            jobs = [
                c["job"]
                for c in digests.collect_digest_candidates(root, start, end)
            ]
            self.assertNotIn("legacy_job", jobs)

    def test_registered_job_uses_registry_eligible_at(self) -> None:
        _ops()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = {
                "status": "completed",
                "created_at": "2020-01-01T00:00:00Z",
                "source": {"title": "New"},
                "artifacts": {"summary": "jobs/newjob/summary.json"},
            }
            job_dir = _write_job(root, "newjob", manifest, {"brief": "new"})
            notifications.record_pipeline_completed_event(root, job_dir, manifest)
            # The manifest's eligible_at was just written by the hook; even if a
            # manifest rewrite loses it, the registry remains authoritative.
            payload = json.loads((job_dir / "manifest.json").read_text())
            payload.pop("eligible_at", None)
            (job_dir / "manifest.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            start, end = self._window_covering(now)
            jobs = [
                c["job"]
                for c in digests.collect_digest_candidates(root, start, end)
            ]
            self.assertIn("newjob", jobs)

    def test_unregistered_job_falls_back_to_manifest_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = datetime.datetime.now(datetime.timezone.utc).isoformat()
            _write_job(
                root,
                "plain_job",
                {
                    "status": "completed",
                    "created_at": now,
                    "source": {"title": "Plain"},
                },
                {"brief": "plain"},
            )
            start, end = self._window_covering(now)
            jobs = [
                c["job"]
                for c in digests.collect_digest_candidates(root, start, end)
            ]
            self.assertIn("plain_job", jobs)
