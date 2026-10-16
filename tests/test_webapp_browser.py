"""Local Chromium smoke coverage for the Issue #6 W11 dashboard workflows.

This is an optional Playwright test because the project's default test
environment does not install browser tooling. It uses a temporary data root,
blocks non-loopback browser requests, stubs the Ask keyword extraction, and
never runs the digest-send worker.

Run with a Python environment containing Playwright and a Chromium binary:
    python tests/test_webapp_browser.py
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit

# Direct execution sets sys.path[0] to tests/, so add the repository root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline
import web_services
import web_tasks
import webapp

try:
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # pragma: no cover - exercised by the default no-browser env
    expect = None
    sync_playwright = None


UTC = dt.timezone.utc
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser")


def _iso(value: dt.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _write_job(root: Path) -> None:
    now = _iso(dt.datetime.now(UTC))
    job_dir = root / "jobs" / "browser-smoke"
    job_dir.mkdir(parents=True)
    manifest = {
        "schema_version": 1,
        "status": "completed",
        "created_at": now,
        "eligible_at": now,
        "source": {
            "url": "https://example.invalid/video/browser-smoke",
            "title": "W11 Library smoke video",
            "author": "Local fixture",
            "published_at": now,
            "duration": 120,
        },
        "video": {"duration_seconds": 120},
    }
    (job_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (job_dir / "summary.json").write_text(
        json.dumps({"status": "ok", "brief": "Local browser smoke fixture", "terms": ["local"]}),
        encoding="utf-8",
    )
    (job_dir / "timeline.json").write_text(json.dumps({"chapters": []}), encoding="utf-8")

    conn = pipeline.index_connection(root)
    try:
        conn.execute(
            "INSERT INTO videos (job, title, source, status, duration, indexed_at, categories) "
            "VALUES (?, ?, ?, 'completed', 120, ?, '[]')",
            (
                "browser-smoke",
                "W11 Library smoke video",
                manifest["source"]["url"],
                now,
            ),
        )
        conn.execute(
            "INSERT INTO events (job, timestamp, kind, text) VALUES (?, ?, ?, ?)",
            ("browser-smoke", "00:01", "speech", "local browser fixture content"),
        )
        conn.commit()
    finally:
        conn.close()


@unittest.skipUnless(sync_playwright is not None, "optional Playwright dependency is not installed")
@unittest.skipUnless(CHROMIUM, "Chromium executable is not installed")
class DashboardW11BrowserSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tempdir = tempfile.TemporaryDirectory(prefix="dashboard-w11-")
        cls.root = Path(cls.tempdir.name)
        _write_job(cls.root)
        cls.server = webapp.build_server(cls.root, host="127.0.0.1", port=0)
        cls.server_thread = threading.Thread(
            target=cls.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        cls.server_thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            executable_path=CHROMIUM,
            headless=True,
            args=["--no-sandbox"],
        )
        cls.external_requests: list[str] = []
        cls.context = cls.browser.new_context(
            viewport={"width": 390, "height": 844},
            service_workers="block",
        )

        def local_requests_only(route) -> None:
            host = urlsplit(route.request.url).hostname
            if host not in {"127.0.0.1", "localhost"}:
                cls.external_requests.append(route.request.url)
                route.abort()
            else:
                route.continue_()

        cls.context.route("**/*", local_requests_only)

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "context", None):
            cls.context.close()
        if getattr(cls, "browser", None):
            cls.browser.close()
        if getattr(cls, "playwright", None):
            cls.playwright.stop()
        if getattr(cls, "server", None):
            cls.server.shutdown()
            cls.server_thread.join(timeout=5)
            cls.server.server_close()
        if getattr(cls, "tempdir", None):
            cls.tempdir.cleanup()

    def setUp(self) -> None:
        self.page = self.context.new_page()
        self.page_errors: list[str] = []
        self.page.on("pageerror", lambda error: self.page_errors.append(str(error)))

    def tearDown(self) -> None:
        self.page.close()

    def assert_browser_clean(self) -> None:
        self.assertEqual(self.page_errors, [])
        self.assertEqual(self.external_requests, [])

    def _submit_question(self, question: str) -> str:
        self.page.goto(f"{self.base_url}/search")
        self.page.locator("#ask-question").fill(question)
        self.page.locator('#ask-form button[type="submit"]').click()
        expect(self.page.locator("#ask-status")).to_have_attribute(
            "data-task-id", re.compile(r".+"), timeout=10_000
        )
        return self.page.locator("#ask-status").get_attribute("data-task-id") or ""

    def test_ask_no_evidence_displays_no_citation_answer(self) -> None:
        self.assertTrue(self._submit_question("ask about a never-indexed phrase"))

        # Keyword extraction and the final answer call are both provider paths.
        # Stub the first and make any final-call attempt fail this smoke test.
        with (
            patch.dict(
                os.environ,
                {
                    "MINIMAX_SUBSCRIPTION_KEY": "local-browser-test",
                    "MINIMAX_MODEL": "local-browser-test",
                    "OPENROUTER_API_KEY": "",
                    "EMBED_MODEL": "",
                },
            ),
            patch.object(pipeline, "question_keywords", return_value=["neverindexedw11token"]) as keywords,
            patch.object(pipeline, "minimax_chat", side_effect=AssertionError("provider call attempted")) as chat,
            patch("cost_tracker.record_usage"),
        ):
            processed = web_tasks.run_worker(
                self.root,
                worker_id="w11-browser-smoke",
                handlers={"ask": web_tasks.TASK_HANDLERS["ask"]},
                once=True,
            )
            self.assertEqual(processed, 1)
            keywords.assert_called_once()
            chat.assert_not_called()

        expect(self.page.locator("#ask-result")).to_have_attribute(
            "data-state", "no-evidence", timeout=10_000
        )
        result = self.page.locator("#ask-result").inner_text()
        self.assertIn("找不到可引用", result)
        self.assertIn("未提供無證據回答", result)
        self.assert_browser_clean()

    def test_ask_blocked_and_unknown_states_are_visible(self) -> None:
        blocked_task = web_tasks.enqueue_task(
            self.root, "ask", payload={"question": "blocked task fixture"}, dedup_key="w11-blocked"
        )
        with sqlite3.connect(self.root / "operations.db") as conn:
            conn.execute(
                "UPDATE operation_tasks SET status='blocked', safe_error='local smoke budget block' "
                "WHERE task_id=?",
                (blocked_task["task_id"],),
            )

        # Exercise the Ask form's backend-budget guard state without making a
        # paid request or inserting a task.
        with patch.object(
            web_services,
            "_budget_guard",
            side_effect=web_services.ServiceError(
                "budget_blocked", "local smoke budget block", http_status=403
            ),
        ):
            self.page.goto(f"{self.base_url}/search")
            self.page.locator("#ask-question").fill("blocked by local smoke budget")
            self.page.locator('#ask-form button[type="submit"]').click()
            expect(self.page.locator("#ask-status")).to_have_attribute(
                "data-state", "blocked", timeout=10_000
            )
            self.assertIn("local smoke budget block", self.page.locator("#ask-status").inner_text())

        # Drive unknown through the queue's real expired-lease recovery path.
        task_id = self._submit_question("reconcile an uncertain remote operation")
        claimed = web_tasks.claim_next_task(self.root, "w11-unknown-smoke", allowed_types={"ask"})
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed["task_id"], task_id)
        with sqlite3.connect(self.root / "operations.db") as conn:
            conn.execute(
                "UPDATE operation_tasks SET remote_operation_id='provider-accepted-locally', "
                "lease_expires_at='2000-01-01T00:00:00Z' WHERE task_id=?",
                (task_id,),
            )
        moved = web_tasks.recover_expired_leases(self.root, now=dt.datetime.now(UTC))
        self.assertIn(task_id, moved)
        expect(self.page.locator("#ask-result")).to_have_attribute(
            "data-state", "unknown", timeout=10_000
        )
        self.assertIn("不要直接重送", self.page.locator("#ask-result").inner_text())

        self.page.goto(f"{self.base_url}/tasks")
        self.assertGreater(self.page.locator(".task-status-blocked").count(), 0)
        self.assertGreater(self.page.locator(".task-status-unknown").count(), 0)
        self.assert_browser_clean()

    def test_digest_preview_requires_confirmation_and_only_queues(self) -> None:
        self.page.goto(f"{self.base_url}/digests")
        self.assertTrue(self.page.locator("#digest-send").is_disabled())
        self.page.locator('#digest-preview-form button[type="submit"]').click()
        expect(self.page.locator("#digest-status")).to_have_attribute(
            "data-state", "previewed", timeout=10_000
        )
        self.assertFalse(self.page.locator("#digest-preview").is_hidden())
        self.assertIn("候選 1", self.page.locator("#digest-counts").inner_text())
        self.assertTrue(self.page.locator("#digest-send").is_disabled())

        self.page.locator("#digest-confirm").check()
        self.assertFalse(self.page.locator("#digest-send").is_disabled())
        self.page.once("dialog", lambda dialog: dialog.accept())
        self.page.locator("#digest-send").click()
        expect(self.page.locator("#digest-status")).to_have_attribute(
            "data-state", "queued", timeout=10_000
        )
        status = self.page.locator("#digest-status").inner_text()
        self.assertIn("尚未表示已送達", status)
        task_id = status.split("背景工作 ", 1)[1].split("（", 1)[0]
        task = web_tasks.get_task(self.root, task_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["type"], "digest_send")
        self.assertEqual(task["status"], "queued")
        self.assert_browser_clean()

    def test_library_and_search_fit_narrow_viewport(self) -> None:
        response = self.page.goto(f"{self.base_url}/videos")
        self.assertIsNotNone(response)
        self.assertEqual(response.status, 200)
        self.page.get_by_role("link", name="W11 Library smoke video").wait_for()
        self.assertGreater(self.page.locator(".table-scroll").count(), 0)
        dimensions = self.page.evaluate(
            """() => ({
                viewport: document.documentElement.clientWidth,
                document: document.documentElement.scrollWidth,
                main: document.querySelector('main').clientWidth,
                mainContent: document.querySelector('main').scrollWidth
            })"""
        )
        self.assertLessEqual(dimensions["document"], dimensions["viewport"], dimensions)
        self.assertLessEqual(dimensions["mainContent"], dimensions["main"], dimensions)

        self.page.goto(f"{self.base_url}/search")
        box = self.page.locator("#ask-question").bounding_box()
        self.assertIsNotNone(box)
        self.assertLessEqual(box["x"] + box["width"], 390)
        self.assert_browser_clean()


if __name__ == "__main__":
    unittest.main(verbosity=2)
