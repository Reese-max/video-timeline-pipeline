"""Web dashboard HTTP contract tests (issue #6, W0-W10).

Covers the read-only skeleton & security baseline (W0/W8), stats labels
(W1), library list/detail (W2), search cost boundary (W3), operations
views & guarded retry (W4), cost/budget reads (W5), digest preview/send
handshake (W6), durable tasks API (W7), error contract (W9) and UI states
(W10) — using only local fixtures; GET endpoints must never trigger
external calls.
"""

from __future__ import annotations

import datetime
import importlib
import http.client
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.parse
from pathlib import Path

try:
    webapp = importlib.import_module("webapp")
    web_services = importlib.import_module("web_services")
    web_tasks = importlib.import_module("web_tasks")
    _IMPORT_ERROR = None
except ImportError as exc:  # red state: modules do not exist on base
    webapp = None
    web_services = None
    web_tasks = None
    _IMPORT_ERROR = exc

import pipeline  # noqa: E402  (baseline module)


UTC = datetime.timezone.utc


def _iso(dt: datetime.datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_job(root: Path, job: str, *, title: str, status: str = "completed",
               categories: list[str] | None = None, eligible_at: str | None = None,
               url: str = "https://www.youtube.com/watch?v=abc", author: str = "author-1") -> None:
    job_dir = root / "jobs" / job
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "status": status,
        "created_at": "2026-01-01T00:00:00Z",
        "source": {"url": url, "title": title, "author": author, "published_at": "2025-12-31T00:00:00Z"},
        "video": {"duration_seconds": 120.0},
    }
    if eligible_at:
        manifest["eligible_at"] = eligible_at
    if categories is not None:
        manifest["categories"] = categories
    (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (job_dir / "summary.json").write_text(
        json.dumps({"status": "ok", "brief": f"summary of {title}", "terms": ["x"]}), encoding="utf-8"
    )
    (job_dir / "timeline.json").write_text(
        json.dumps({"entries": [{"start": 0.0, "end": 5.0, "label": "intro", "detail": "d"}] * 3}),
        encoding="utf-8",
    )


def _build_index(root: Path) -> None:
    conn = pipeline.index_connection(root)
    try:
        conn.execute(
            "INSERT INTO videos (job, title, source, status, duration, indexed_at, categories)"
            " VALUES ('job1','Alpha title','https://www.youtube.com/watch?v=abc','completed',120.0,'2026-01-02T00:00:00Z','[\"AI\"]')"
        )
        conn.execute(
            "INSERT INTO videos (job, title, source, status, duration, indexed_at, categories)"
            " VALUES ('job2','Beta <script>alert(1)</script>','https://vimeo.com/x','completed',60.0,'2026-01-03T00:00:00Z','[\"AI Safety\",\"Cooking\"]')"
        )
        conn.execute(
            "INSERT INTO events (job, timestamp, kind, text) VALUES ('job1','00:01','speech','alpha transcript words here')"
        )
        conn.execute(
            "INSERT INTO events (job, timestamp, kind, text) VALUES ('job2','00:02','summary','beta summary text')"
        )
        conn.commit()
    finally:
        conn.close()


def _write_rss_state(root: Path, items: list[dict]) -> None:
    rss = root / "rss"
    rss.mkdir(exist_ok=True)
    (rss / "state.json").write_text(
        json.dumps({"schema_version": 1, "subscriptions": [{"id": "sub1", "kind": "rss", "url": "https://feed", "name": "Feed", "enabled": True}], "items": items, "scheduler_cursor": 0}),
        encoding="utf-8",
    )


class ServerFixture(unittest.TestCase):
    """Spin up a real loopback server per test class."""

    root: Path
    base_url: str
    server: object
    csrf: str

    @classmethod
    def make_root(cls) -> Path:
        tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(tmp.cleanup)
        return Path(tmp.name)

    @classmethod
    def build_server(cls, root: Path):
        return webapp.build_server(root, host="127.0.0.1", port=0)

    @classmethod
    def setUpClass(cls) -> None:
        if _IMPORT_ERROR is not None:
            raise unittest.SkipTest  # replaced by explicit failure in setUp
        cls.root = cls.make_root()
        cls.server = cls.build_server(cls.root)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.csrf = cls.server.csrf_token

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "server", None):
            cls.server.shutdown()
            cls.thread.join(timeout=5)
            cls.server.server_close()

    def setUp(self) -> None:
        if _IMPORT_ERROR is not None:
            self.fail(f"web modules unavailable: {_IMPORT_ERROR}")

    # --- helpers ---------------------------------------------------------
    def request(self, method: str, path: str, *, body: dict | None = None,
                headers: dict | None = None, host: str | None = None) -> tuple[int, dict, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=10)
        payload = json.dumps(body).encode() if body is not None else None
        hdrs = {"Host": host or f"127.0.0.1:{self.server.server_address[1]}"}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        if headers:
            hdrs.update(headers)
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = None
        return resp.status, dict(resp.getheaders()), {"json": parsed, "raw": raw}

    def post_json(self, path: str, body: dict, **kw):
        headers = kw.pop("headers", {}) or {}
        headers.setdefault("X-CSRF-Token", self.csrf)
        headers.setdefault("Origin", self.base_url)
        return self.request("POST", path, body=body, headers=headers, **kw)


class SecurityBaselineTests(ServerFixture):
    def test_health_endpoint(self) -> None:
        status, headers, body = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(body["json"]["ok"])
        self.assertTrue(body["json"]["schema_version"])
        self.assertEqual(body["json"]["scope"], "local-dashboard")

    def test_security_headers_present(self) -> None:
        _, headers, _ = self.request("GET", "/api/health")
        lowered = {k.lower(): v for k, v in headers.items()}
        self.assertEqual(lowered.get("x-content-type-options"), "nosniff")
        self.assertIn("default-src 'self'", lowered.get("content-security-policy", ""))
        self.assertEqual(lowered.get("referrer-policy"), "no-referrer")

    def test_host_allowlist_rejects_foreign_host(self) -> None:
        status, _, body = self.request("GET", "/api/health", host="evil.example.com")
        self.assertEqual(status, 400)
        self.assertEqual(body["json"]["error"]["code"], "invalid_host")

    def test_404_is_masked_json(self) -> None:
        status, _, body = self.request("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(body["json"]["error"]["code"], "not_found")
        self.assertNotIn("Traceback", body["raw"].decode())

    def test_error_shape_has_contract_fields(self) -> None:
        status, _, body = self.request("GET", "/api/nope")
        err = body["json"]["error"]
        for key in ("code", "safe_message", "retryable", "request_id"):
            self.assertIn(key, err)

    def test_post_without_csrf_rejected(self) -> None:
        status, _, body = self.request(
            "POST", "/api/search", body={"query": "x"},
            headers={"Origin": self.base_url, "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["json"]["error"]["code"], "csrf_failed")

    def test_post_foreign_origin_rejected(self) -> None:
        status, _, body = self.request(
            "POST", "/api/search", body={"query": "x"},
            headers={"Origin": "https://evil.example.com", "X-CSRF-Token": self.csrf, "Content-Type": "application/json"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["json"]["error"]["code"], "origin_not_allowed")

    def test_post_body_size_capped(self) -> None:
        status, _, _ = self.post_json("/api/search", {"query": "x" * (200 * 1024)})
        self.assertEqual(status, 413)

    def test_method_not_allowed(self) -> None:
        status, _, _ = self.request("DELETE", "/api/health")
        self.assertIn(status, (404, 405))

    def test_get_endpoints_do_not_require_csrf(self) -> None:
        status, _, _ = self.request("GET", "/api/videos")
        self.assertIn(status, (200, 503))


class RemoteBindGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        if _IMPORT_ERROR is not None:
            self.fail(f"web modules unavailable: {_IMPORT_ERROR}")
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_non_loopback_refused_without_allow_remote(self) -> None:
        with self.assertRaises(Exception):
            webapp.build_server(self.root, host="0.0.0.0", port=0)

    def test_non_loopback_refused_without_token(self) -> None:
        with self.assertRaises(Exception):
            webapp.build_server(self.root, host="0.0.0.0", port=0, allow_remote=True)

    def test_non_loopback_refused_without_trusted_proxy(self) -> None:
        with self.assertRaises(Exception):
            webapp.build_server(self.root, host="0.0.0.0", port=0, allow_remote=True, auth_token="tok")

    def test_remote_requires_bearer_on_reads(self) -> None:
        server = webapp.build_server(
            self.root, host="127.0.0.1", port=0,
            auth_token="tok", remote_mode=True,
            extra_allowed_hosts=["dashboard.internal"],
        )
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", "/api/health", headers={"Host": "dashboard.internal"})
            resp = conn.getresponse()
            resp.read()
            self.assertEqual(resp.status, 401)
            conn.close()
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", "/api/health", headers={"Host": "dashboard.internal", "Authorization": "Bearer tok"})
            resp = conn.getresponse()
            body = resp.read()
            conn.close()
            self.assertEqual(resp.status, 200)
            self.assertTrue(json.loads(body)["ok"])
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


class FirstCompletionMetricTests(unittest.TestCase):
    def test_new_video_windows_ignore_created_at_completed_at_and_manifest_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            jobs = root / "jobs"
            jobs.mkdir()
            today = _iso(datetime.datetime.now(UTC))
            for job, manifest in (
                ("created-only", {"status": "completed", "created_at": today}),
                ("retry-time", {"status": "completed", "completed_at": today}),
            ):
                job_dir = jobs / job
                job_dir.mkdir()
                manifest_path = job_dir / "manifest.json"
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                # Rewriting/indexing a manifest today must not make an old or
                # unregistered job look newly completed.
                manifest_path.touch()

            stats = web_services.dashboard_stats(root)
            self.assertEqual(stats["new_videos"]["today"], 0)
            self.assertEqual(stats["new_videos"]["this_week"], 0)

            eligible_dir = jobs / "stable-eligible"
            eligible_dir.mkdir()
            (eligible_dir / "manifest.json").write_text(
                json.dumps({"status": "completed", "eligible_at": today}), encoding="utf-8"
            )
            stats = web_services.dashboard_stats(root)
            self.assertEqual(stats["new_videos"]["today"], 1)
            self.assertEqual(stats["new_videos"]["this_week"], 1)


class StatsAndLibraryTests(ServerFixture):
    @classmethod
    def make_root(cls) -> Path:
        root = super().make_root()
        today = _iso(datetime.datetime.now(UTC))
        _write_job(root, "job1", title="Alpha title", categories=["AI"], eligible_at=today)
        _write_job(root, "job2", title="Beta <script>alert(1)</script>", categories=["AI Safety", "Cooking"],
                   eligible_at="2020-01-01T00:00:00Z", url="https://vimeo.com/x")
        _build_index(root)
        _write_rss_state(root, [
            {"id": "it1", "subscription_id": "sub1", "title": "ok", "status": "completed", "discovered_at": today},
            {"id": "it2", "subscription_id": "sub1", "title": "bad", "status": "failed", "attempts": 1, "last_error": "token=abc123 SECRET", "discovered_at": today},
            {"id": "it3", "subscription_id": "sub1", "title": "wait", "status": "queued", "discovered_at": today},
        ])
        return root

    def test_stats_labels_window_timezone_scope(self) -> None:
        status, _, body = self.request("GET", "/api/stats")
        self.assertEqual(status, 200)
        stats = body["json"]
        for key in ("as_of", "timezone", "scope", "window"):
            self.assertIn(key, stats)
        self.assertEqual(stats["scope"], "local")

    def test_stats_counts_and_cost_unavailable_not_zero(self) -> None:
        _, _, body = self.request("GET", "/api/stats")
        stats = body["json"]
        self.assertEqual(stats["new_videos"]["today"], 1)
        self.assertGreaterEqual(stats["new_videos"]["this_week"], 1)
        self.assertEqual(stats["rss"]["failed"], 1)
        self.assertEqual(stats["rss"]["queued"], 1)
        cost = stats["cost"]
        self.assertFalse(cost["available"])
        self.assertNotIn("spent_usd", cost)

    def test_stats_scoring_unavailable_label(self) -> None:
        _, _, body = self.request("GET", "/api/stats")
        self.assertEqual(body["json"]["scoring"]["available"], False)

    def test_videos_list_paginated_and_sorted(self) -> None:
        status, _, body = self.request("GET", "/api/videos")
        self.assertEqual(status, 200)
        data = body["json"]
        self.assertEqual(data["page"], 1)
        self.assertEqual(data["page_size"], 50)
        self.assertEqual(data["total"], 2)
        jobs = [v["job"] for v in data["items"]]
        self.assertIn("job1", jobs)
        row = next(v for v in data["items"] if v["job"] == "job1")
        self.assertEqual(row["platform"], "YouTube")
        self.assertEqual(row["author"], "author-1")
        self.assertTrue(row["first_completed_at"])

    def test_videos_page_size_over_max_rejected(self) -> None:
        status, _, body = self.request("GET", "/api/videos?page_size=101")
        self.assertEqual(status, 400)
        self.assertEqual(body["json"]["error"]["code"], "invalid_param")

    def test_videos_sort_allowlist_enforced(self) -> None:
        status, _, _ = self.request("GET", "/api/videos?sort=" + urllib.parse.quote("title;DROP TABLE videos"))
        self.assertEqual(status, 400)
        status, _, body = self.request("GET", "/api/videos?sort=title&order=asc")
        self.assertEqual(status, 200)

    def test_category_filter_is_exact_match(self) -> None:
        _, _, body = self.request("GET", "/api/videos?category=AI")
        jobs = [v["job"] for v in body["json"]["items"]]
        self.assertEqual(jobs, ["job1"])

    def test_status_filter(self) -> None:
        _, _, body = self.request("GET", "/api/videos?status=completed")
        self.assertEqual(body["json"]["total"], 2)
        _, _, body = self.request("GET", "/api/videos?status=failed")
        self.assertEqual(body["json"]["total"], 0)

    def test_date_field_first_completed_filter(self) -> None:
        # job1 completed today; job2 completed in 2020 — indexed_at dates differ
        status, _, body = self.request(
            "GET", "/api/videos?date_field=first_completed_at&date_from=2026-06-01T00:00:00Z"
        )
        self.assertEqual(status, 200)
        self.assertEqual([v["job"] for v in body["json"]["items"]], ["job1"])
        status, _, _ = self.request("GET", "/api/videos?date_field=bogus&date_from=2026-01-01")
        self.assertEqual(status, 400)

    def test_platform_filter(self) -> None:
        _, _, body = self.request("GET", "/api/videos?platform=youtube")
        self.assertEqual([v["job"] for v in body["json"]["items"]], ["job1"])

    def test_video_detail_and_traversal_blocked(self) -> None:
        status, _, body = self.request("GET", "/api/videos/job1")
        self.assertEqual(status, 200)
        detail = body["json"]
        self.assertEqual(detail["job"], "job1")
        self.assertEqual(detail["summary"]["brief"], "summary of Alpha title")
        self.assertEqual(len(detail["timeline"]["entries"]), 3)
        status, _, _ = self.request("GET", "/api/videos/..%2F..%2F.pipeline")
        self.assertIn(status, (400, 404))
        status, _, _ = self.request("GET", "/api/videos/no-such-job")
        self.assertEqual(status, 404)

    def test_html_escapes_titles(self) -> None:
        status, _, body = self.request("GET", "/videos")
        self.assertEqual(status, 200)
        raw = body["raw"].decode()
        self.assertIn("&lt;script&gt;", raw)
        self.assertNotIn("<script>alert", raw)

    def test_home_page_renders(self) -> None:
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        raw = body["raw"].decode()
        self.assertIn("<html", raw)
        self.assertIn("csrf-token", raw)

    def test_index_missing_returns_503_not_crash(self) -> None:
        # point server at a root without library.db
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        server = webapp.build_server(Path(tmp.name), host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            conn.request("GET", "/api/videos", headers={"Host": "127.0.0.1"})
            resp = conn.getresponse()
            payload = json.loads(resp.read())
            conn.close()
            self.assertEqual(resp.status, 503)
            self.assertIn(payload["error"]["code"], ("index_unavailable", "migration_required"))
            self.assertFalse(payload["error"]["retryable"])
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


class SearchBoundaryTests(ServerFixture):
    @classmethod
    def make_root(cls) -> Path:
        root = super().make_root()
        _write_job(root, "job1", title="Alpha title", categories=["AI"])
        _build_index(root)
        return root

    def test_get_search_uses_local_fts_only(self) -> None:
        status, _, body = self.request("GET", "/api/search?q=alpha")
        self.assertEqual(status, 200)
        data = body["json"]
        self.assertEqual(data["mode"], "local")
        self.assertEqual(data["external_calls"], 0)
        self.assertTrue(any(hit["job"] == "job1" for hit in data["results"]))

    def test_get_search_empty_query_rejected(self) -> None:
        status, _, _ = self.request("GET", "/api/search?q=")
        self.assertEqual(status, 400)

    def test_get_search_query_length_capped(self) -> None:
        status, _, _ = self.request("GET", "/api/search?q=" + "x" * 201)
        self.assertEqual(status, 400)

    def test_post_search_enqueues_semantic_task(self) -> None:
        status, _, body = self.post_json("/api/search", {"query": "deep dive", "mode": "semantic", "limit": 5})
        self.assertEqual(status, 202)
        data = body["json"]
        self.assertTrue(data["task_id"])
        self.assertEqual(data["status"], "queued")
        task = web_tasks.get_task(self.root, data["task_id"])
        self.assertEqual(task["type"], "semantic_search")

    def test_post_search_rejects_bad_mode(self) -> None:
        status, _, _ = self.post_json("/api/search", {"query": "x", "mode": "exec"})
        self.assertEqual(status, 400)

    def test_post_ask_enqueues_task(self) -> None:
        status, _, body = self.post_json("/api/ask", {"question": "what is alpha?", "limit": 5})
        self.assertEqual(status, 202)
        task = web_tasks.get_task(self.root, body["json"]["task_id"])
        self.assertEqual(task["type"], "ask")

    def test_idempotency_key_dedups_enqueue(self) -> None:
        headers = {"Idempotency-Key": "req-42"}
        s1, _, b1 = self.post_json("/api/search", {"query": "same", "mode": "semantic"}, headers=headers)
        s2, _, b2 = self.post_json("/api/search", {"query": "same", "mode": "semantic"}, headers=headers)
        self.assertEqual(s1, 202)
        self.assertEqual(s2, 202)
        self.assertEqual(b1["json"]["task_id"], b2["json"]["task_id"])
        self.assertTrue(b2["json"]["deduplicated"])

    def test_get_task_poll_does_not_start_work(self) -> None:
        _, _, body = self.post_json("/api/search", {"query": "lazy", "mode": "semantic"})
        task_id = body["json"]["task_id"]
        status, _, fetched = self.request("GET", f"/api/tasks/{task_id}")
        self.assertEqual(status, 200)
        self.assertEqual(fetched["json"]["status"], "queued")

    def test_cancel_task(self) -> None:
        _, _, body = self.post_json("/api/search", {"query": "cancel me", "mode": "semantic"})
        task_id = body["json"]["task_id"]
        status, _, resp = self.post_json(f"/api/tasks/{task_id}/cancel", {})
        self.assertEqual(status, 200)
        self.assertEqual(resp["json"]["status"], "cancelled")


class OperationsAndRetryTests(ServerFixture):
    @classmethod
    def make_root(cls) -> Path:
        root = super().make_root()
        _write_rss_state(root, [
            {"id": "it-failed", "subscription_id": "sub1", "title": "f", "status": "failed", "attempts": 1,
             "last_error": "call failed api_key=SECRETKEY123", "discovered_at": "2026-01-01T00:00:00Z"},
            {"id": "it-dead", "subscription_id": "sub1", "title": "d", "status": "dead-letter", "attempts": 3,
             "discovered_at": "2026-01-01T00:00:00Z"},
            {"id": "it-proc", "subscription_id": "sub1", "title": "p", "status": "processing",
             "processing_started_at": _iso(datetime.datetime.now(UTC)), "discovered_at": "2026-01-01T00:00:00Z"},
            {"id": "it-done", "subscription_id": "sub1", "title": "c", "status": "completed",
             "discovered_at": "2026-01-01T00:00:00Z"},
        ])
        return root

    def test_operations_lists_rss_state_with_source(self) -> None:
        status, _, body = self.request("GET", "/api/operations")
        self.assertEqual(status, 200)
        data = body["json"]
        self.assertEqual(data["source"], "rss/state.json")
        self.assertTrue(data["updated_at"])
        statuses = {item["id"]: item["status"] for item in data["items"]}
        self.assertEqual(statuses["it-failed"], "failed")

    def test_failed_view_sanitizes_secrets(self) -> None:
        status, _, body = self.request("GET", "/api/operations/failed")
        self.assertEqual(status, 200)
        raw = json.dumps(body["json"])
        self.assertNotIn("SECRETKEY123", raw)
        self.assertNotIn("api_key=", raw)

    def test_retry_failed_item_enqueues_task(self) -> None:
        status, _, body = self.post_json("/api/jobs/it-failed/retry", {})
        self.assertEqual(status, 202)
        task = web_tasks.get_task(self.root, body["json"]["task_id"])
        self.assertEqual(task["type"], "rss_item_retry")
        self.assertEqual(task["payload"]["item_id"], "it-failed")

    def test_retry_completed_item_conflict(self) -> None:
        status, _, body = self.post_json("/api/jobs/it-done/retry", {})
        self.assertEqual(status, 409)
        self.assertEqual(body["json"]["error"]["code"], "not_retryable")

    def test_retry_processing_item_is_unknown_conflict(self) -> None:
        status, _, body = self.post_json("/api/jobs/it-proc/retry", {})
        self.assertEqual(status, 409)
        self.assertIn(body["json"]["error"]["code"], ("unknown_state", "not_retryable"))

    def test_retry_missing_item_404(self) -> None:
        status, _, _ = self.post_json("/api/jobs/nope/retry", {})
        self.assertEqual(status, 404)

    def test_providers_health_endpoint(self) -> None:
        status, _, body = self.request("GET", "/api/operations/providers")
        self.assertEqual(status, 200)
        self.assertIn("providers", body["json"])


class CostsDigestAndNotificationTests(ServerFixture):
    @classmethod
    def make_root(cls) -> Path:
        root = super().make_root()
        today = _iso(datetime.datetime.now(UTC))
        _write_job(root, "job1", title="Alpha", categories=["AI"], eligible_at=today)
        import cost_tracker

        cost_tracker.record_usage(root, provider="groq", operation="transcribe", job="job1",
                                  request_units=10, response_units=0, latency_ms=5.0)
        return root

    def test_costs_report_local_readonly(self) -> None:
        status, _, body = self.request("GET", "/api/costs")
        self.assertEqual(status, 200)
        data = body["json"]
        self.assertTrue(data["available"])
        self.assertGreaterEqual(data["report"]["grand_total_estimated_usd"], 0.0)

    def test_budget_status(self) -> None:
        status, _, body = self.request("GET", "/api/budget-status")
        self.assertEqual(status, 200)
        self.assertIn("spent_usd", body["json"]["status"])

    def test_jobs_failed_view(self) -> None:
        status, _, body = self.request("GET", "/api/jobs/failed")
        self.assertEqual(status, 200)
        self.assertIn("items", body["json"])

    def test_digest_preview_then_send_handshake(self) -> None:
        status, _, body = self.post_json("/api/digest/preview", {"digest_type": "daily"})
        self.assertEqual(status, 200)
        preview = body["json"]
        for key in ("digest_id", "revision", "payload_hash", "window", "counts"):
            self.assertIn(key, preview)
        # wrong hash rejected
        status, _, body = self.post_json("/api/digest/send", {
            "digest_id": preview["digest_id"], "revision": preview["revision"], "payload_hash": "bogus"})
        self.assertEqual(status, 409)
        self.assertEqual(body["json"]["error"]["code"], "stale_preview")
        # correct hash enqueues
        status, _, body = self.post_json("/api/digest/send", {
            "digest_id": preview["digest_id"], "revision": preview["revision"], "payload_hash": preview["payload_hash"]})
        self.assertEqual(status, 202)
        task = web_tasks.get_task(self.root, body["json"]["task_id"])
        self.assertEqual(task["type"], "digest_send")

    def test_digest_send_unknown_digest_404(self) -> None:
        status, _, _ = self.post_json("/api/digest/send", {"digest_id": "nope", "revision": 1, "payload_hash": "x"})
        self.assertEqual(status, 404)

    def test_digest_preview_repeat_is_idempotent_and_change_bumps_revision(self) -> None:
        _, _, b1 = self.post_json("/api/digest/preview", {"digest_type": "daily"})
        _, _, b2 = self.post_json("/api/digest/preview", {"digest_type": "daily"})
        self.assertEqual(b1["json"]["digest_id"], b2["json"]["digest_id"])
        self.assertEqual(b2["json"]["revision"], 1)
        # new candidate inside the same window changes the payload -> new revision
        today = _iso(datetime.datetime.now(UTC))
        _write_job(self.root, "job-new", title="New", categories=["AI"], eligible_at=today)
        _, _, b3 = self.post_json("/api/digest/preview", {"digest_type": "daily"})
        self.assertEqual(b3["json"]["revision"], 2)
        self.assertNotEqual(b3["json"]["payload_hash"], b1["json"]["payload_hash"])

    def test_digests_and_notifications_lists(self) -> None:
        status, _, body = self.request("GET", "/api/digests")
        self.assertEqual(status, 200)
        self.assertIn("items", body["json"])
        status, _, body = self.request("GET", "/api/notifications")
        self.assertEqual(status, 200)
        self.assertIn("items", body["json"])


if __name__ == "__main__":
    unittest.main()
