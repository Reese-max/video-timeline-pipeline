"""Provider and MCP regressions: mocked HTTP, temporary HOME and shared ledger."""
import asyncio
import copy
import io
import json
import os
import tempfile
import unittest
import urllib.parse
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import patch

import pipeline
import cost_tracker
from notifications import get_operations_db

REAL_LOAD_DOTENV = pipeline.load_dotenv
with patch.object(pipeline, "load_dotenv"):
    import mcp_server as server


class BrightDataDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enter = self.enterContext
        self.enter(patch.dict(os.environ, {
            "BRIGHTDATA_API_TOKEN": "offline-token", "BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "50",
            "BRIGHTDATA_POST_DAILY_LIMIT": "2", "BRIGHTDATA_CREDIT_LEDGER": str(self.root / "ledger.sqlite3"),
            "BRIGHTDATA_ALLOWED_POST_URLS": "*", "HOME": str(self.root), "USERPROFILE": str(self.root),
        }, clear=True))
        self.enter(patch.object(pipeline, "load_dotenv"))
        self.enter(patch.object(server, "MCP_JOBS_ROOT", self.root / "mcp"))
        self.submit = self.enter(patch.object(pipeline, "request_json", return_value={"snapshot_id": "sd_test"}))
        self.read = self.enter(patch.object(pipeline, "get_json", side_effect=lambda url, headers:
            {"status": "ready"} if "/progress/" in url else []))

    def usage(self):
        import brightdata_credits
        return brightdata_credits.usage()

    def post(self, code="ABC"):
        return asyncio.run(server.collect_social_post(f"https://instagram.com/p/{code}", True))

    def facebook(self, **kwargs):
        return pipeline.fetch_facebook_author("example", "2026-01-01", "brightdata",
            max_items=5, allow_paid=True, root=self.root, **kwargs)

    def test_facebook_manual_paths_require_explicit_permission_despite_env(self):
        opml = self.root / "subscriptions.opml"
        opml.write_text('<opml><body><outline xmlUrl="https://facebook.com/example" '
                        'vidbeeKind="facebook-author" vidbeeFacebookProvider="brightdata" '
                        'vidbeeFacebookMaxItems="5" /></body></opml>', encoding="utf-8")
        with patch.dict(os.environ, {"RSS_PAID_DISCOVERY": "brightdata"}), \
                patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"), \
                patch.object(pipeline, "rss_writer_lock", side_effect=lambda root: nullcontext()), \
                patch.object(pipeline.brightdata_credits, "reserve", wraps=pipeline.brightdata_credits.reserve) as reserve:
            with self.subTest(path="cli"), patch.object(pipeline.sys, "argv", ["pipeline.py",
                    "--facebook-author", "example", "--facebook-provider", "brightdata",
                    "--facebook-max-items", "5", "--facebook-discover-only", "--root", str(self.root)]):
                with self.assertRaisesRegex(RuntimeError, "--allow-batch-cloud"):
                    pipeline.run_rss_command(pipeline.parse_args())
            with self.subTest(path="subscription"):
                with self.assertRaisesRegex(RuntimeError, "--allow-batch-cloud"):
                    pipeline.add_facebook_subscription(self.root, "example", provider="brightdata",
                                                       max_items=5, allow_paid=False)
            with self.subTest(path="opml"):
                self.assertEqual(pipeline.import_rss_opml(self.root / "imported", opml, allow_paid=False),
                                 {"added": 0, "existing": 0, "failed": 1})
            reserve.assert_not_called()
        self.submit.assert_not_called()
        self.read.assert_not_called()
        self.assertEqual(self.usage()["reserved"], 0)
        self.assertEqual(list(self.root.rglob(".submitted")), [])

    def test_facebook_refresh_env_permission_reaches_transport(self):
        state = {"subscriptions": [{"id": "fb", "kind": "facebook-author",
                 "url": "https://facebook.com/example", "facebook_page_id": "example",
                 "facebook_provider": "brightdata", "facebook_max_items": 5}], "items": []}
        with patch.dict(os.environ, {"RSS_PAID_DISCOVERY": "brightdata"}), \
                patch.object(pipeline, "load_rss_state", return_value=state), \
                patch.object(pipeline, "save_rss_state"), \
                patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"), \
                patch.object(pipeline, "write_facebook_discovery", return_value=self.root / "latest.json"):
            self.assertEqual(pipeline.refresh_rss(self.root)["failed"], 0)
        self.submit.assert_called_once()
        self.assertEqual(state["subscriptions"][0]["facebook_last_snapshot_id"], "sd_test")

    def test_usage_cli_loads_only_temporary_project_then_root_env(self):
        project = self.root / "project"
        project.mkdir()
        project_ledger = project / "credits.sqlite3"
        (project / ".env").write_text(
            f"BRIGHTDATA_CREDIT_LEDGER={project_ledger}\nBRIGHTDATA_MONTHLY_CREDIT_LIMIT=17\n"
            "BRIGHTDATA_POST_DAILY_LIMIT=4\n", encoding="utf-8")
        other_root = self.root / "output"
        other_root.mkdir()
        root_ledger = other_root / "credits.sqlite3"
        (other_root / ".env").write_text(
            f"BRIGHTDATA_CREDIT_LEDGER={root_ledger}\nBRIGHTDATA_MONTHLY_CREDIT_LIMIT=23\n"
            "BRIGHTDATA_POST_DAILY_LIMIT=6\n", encoding="utf-8")
        for root, ledger, limit, daily, paths in (
                (project, project_ledger, 17, 4, [project / ".env"]),
                (other_root, root_ledger, 23, 6, [project / ".env", other_root / ".env"])):
            with self.subTest(root=root), patch.object(pipeline, "__file__", str(project / "pipeline.py")), \
                    patch.object(pipeline, "load_dotenv", wraps=REAL_LOAD_DOTENV) as load, \
                    patch.object(pipeline.sys, "argv", ["pipeline.py", "--brightdata-usage", "--root", str(root)]), \
                    patch("sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(pipeline.main(), 0)
                result = json.loads(stdout.getvalue())
                self.assertEqual((result["limit"], result["daily_post_limit"]), (limit, daily))
                self.assertEqual(pipeline.brightdata_credits.ledger_path(), ledger)
                self.assertTrue(ledger.is_file())
                self.assertEqual([call.args[0].resolve() for call in load.call_args_list],
                                 [path.resolve() for path in paths])
        self.submit.assert_not_called()
        self.read.assert_not_called()

    def test_credit_rejection_does_not_enter_usd_ledger(self):
        cost_tracker.get_cost_report(self.root)
        with cost_tracker.cost_root_scope(self.root), \
                patch.dict(os.environ, {"BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "0"}):
            with self.assertRaises(pipeline.brightdata_credits.BrightDataQuotaError):
                pipeline.submit_brightdata_once(self.root / "trigger", "https://offline.invalid/trigger",
                                               {}, b'[{"url":"https://instagram.com/p/ABC"}]')
        with closing(get_operations_db(self.root)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM usage_costs").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM budget_reservations").fetchone()[0], 0)
        self.submit.assert_not_called()
        self.assertEqual(list(self.root.rglob(".submitted")), [])

    def test_usd_strict_rejection_releases_credit_and_allows_safe_retry(self):
        pipeline.brightdata_credits.reserve(3)
        folder = self.root / "trigger"
        body = b'[{"url":"https://instagram.com/p/ABC"}]'
        with cost_tracker.cost_root_scope(self.root), \
                patch.dict(os.environ, {"DAILY_BUDGET_USD": "1", "BUDGET_HARD_STOP": "true"}), \
                patch.object(pipeline.brightdata_credits, "reserve", wraps=pipeline.brightdata_credits.reserve) as reserve:
            with self.assertRaises(cost_tracker.BudgetBlockedError):
                pipeline.submit_brightdata_once(folder, "https://offline.invalid/trigger", {}, body, daily_post=True)
            reserve.assert_called_once()
        self.assertEqual(self.usage()["reserved"], 3)
        self.assertEqual(self.usage()["daily_posts_used"], 0)
        self.submit.assert_not_called()
        self.assertFalse((folder / ".submitted").exists())
        self.assertFalse((folder / "submission.json").exists())
        with cost_tracker.cost_root_scope(None):
            self.assertEqual(pipeline.submit_brightdata_once(
                folder, "https://offline.invalid/trigger", {}, body, daily_post=True), "sd_test")
        self.assertEqual(self.usage()["reserved"], 4)
        self.assertEqual(self.usage()["daily_posts_used"], 1)

    def test_transport_failure_after_marker_keeps_credit_reservation(self):
        folder = self.root / "trigger"
        self.submit.side_effect = cost_tracker.BudgetBlockedError("transport failed after marker")
        with cost_tracker.cost_root_scope(None):
            with self.assertRaises(RuntimeError):
                pipeline.submit_brightdata_once(folder, "https://offline.invalid/trigger", {}, b'[{}]')
            with self.assertRaises(RuntimeError):
                pipeline.submit_brightdata_once(folder, "https://offline.invalid/trigger", {}, b'[{}]')
        self.assertTrue((folder / ".submitted").exists())
        self.assertEqual(self.usage()["reserved"], 1)
        self.submit.assert_called_once()

    def test_quota_rejection_no_http_or_submitted_marker(self):
        with patch.dict(os.environ, {"BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "0"}):
            result = self.post()
        self.assertEqual(result["status"], "quota_exhausted")
        self.assertEqual(result["error_code"], "BRIGHTDATA_QUOTA")
        self.assertTrue(result["resets_at"].endswith("Z"))
        self.submit.assert_not_called()
        self.read.assert_not_called()
        self.assertEqual(list(self.root.rglob(".submitted")), [])

    def test_wildcard_and_exact_allowlist_and_authorization(self):
        self.read.side_effect = [{"status": "ready"}, [{"url": "https://instagram.com/p/ABC", "description": "text"}]]
        self.assertEqual(self.post()["status"], "ready")
        for url in ("https://facebook.com/Page", "https://facebook.com/Page/posts/123?comment_id=1",
                    "https://facebook.com/reel/123", "https://facebook.com/watch/?v=123"):
            self.assertEqual(asyncio.run(server.collect_social_post(url, True))["status"], "error")
        with patch.dict(os.environ, {"BRIGHTDATA_ALLOWED_POST_URLS": "https://instagram.com/p/ABC"}):
            self.assertEqual(self.post("OTHER")["status"], "error")
            self.assertEqual(self.post()["status"], "ready")
        self.assertEqual(asyncio.run(server.collect_social_post("https://instagram.com/p/ABC"))["status"], "error")
        self.submit.assert_called_once()

    def test_daily_limit_pending_resume_cache_and_settlement(self):
        self.read.side_effect = [{"status": "running"}, {"status": "ready"},
            [{"url": "https://instagram.com/p/ABC", "description": "text"}]]
        self.assertEqual(self.post()["status"], "pending")
        self.assertEqual(self.usage()["reserved"], 1)
        self.assertEqual(self.post()["status"], "ready")
        self.assertEqual(self.post()["status"], "ready")
        self.assertEqual(self.usage()["used"], 1)
        self.assertEqual(self.usage()["daily_posts_used"], 1)
        self.read.side_effect = lambda url, headers: {"status": "running"}
        self.assertEqual(self.post("SECOND")["status"], "pending")
        result = self.post("THIRD")
        self.assertEqual(result["status"], "quota_exhausted")
        self.assertEqual(result["error_code"], "BRIGHTDATA_QUOTA")
        self.assertEqual(self.usage()["daily_posts_used"], 2)
        self.assertEqual(self.submit.call_count, 2)

    def test_social_error_records_and_multiple_records_still_settle(self):
        self.read.side_effect = [{"status": "ready"}, [{"error": "bad_input"}, {"error": "bad_input"}]]
        self.assertEqual(self.post()["status"], "error")
        self.assertEqual(self.usage()["used"], 2)
        self.assertEqual(self.usage()["reserved"], 0)

    def test_unknown_trigger_and_failed_snapshot_keep_reservation(self):
        for response in ({}, {"snapshot_id": "sd_test"}):
            self.submit.return_value = response
            self.read.side_effect = lambda url, headers: {"status": "failed"}
            self.assertEqual(self.post(str(len(response)))["status"], "error")
        self.assertEqual(self.usage()["reserved"], 2)

    def test_facebook_body_query_filter_and_receipt_resume(self):
        records = [
            {"url": "https://facebook.com/reel/1", "post_id": "1", "content": "new", "date_posted": "2026-02-01T00:00:00Z"},
            {"url": "https://facebook.com/reel/2", "shortcode": "2", "date_posted": "2025-01-01T00:00:00Z"},
            {"url": "https://facebook.com/Page/posts/3", "post_id": "3", "content": "not video"},
            {"url": "https://evil.test/reel/4", "video_url": "https://a.fbcdn.net/v"},
            {"url": "https://facebook.com/reel/5"}, {"error": "bad_input"},
        ]
        self.read.side_effect = lambda url, headers: {"status": "ready"} if "/progress/" in url else records
        # Exercise real receipt/poll/accounting; only bypass sandbox-private ACL setup.
        with patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"):
            feed = self.facebook(exclude_ids=["9", "8", "9"])
            self.facebook(exclude_ids=["9", "8", "9"])
        self.submit.assert_called_once()
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.submit.call_args.args[0]).query)
        self.assertEqual(query, {"dataset_id": ["gd_lyclm3ey2q6rww027t"], "include_errors": ["true"], "format": ["json"]})
        self.assertEqual(json.loads(self.submit.call_args.kwargs["body"]), [{"url": "https://www.facebook.com/example", "num_of_posts": 5, "posts_to_not_include": ["8", "9"]}])
        self.assertEqual(len(feed["items"]), 2)
        self.assertEqual(feed["facebook"]["missing_date_count"], 1)
        self.assertFalse(feed["facebook"]["date_filter_complete"])
        self.assertEqual(self.usage()["used"], 6)
        self.assertEqual(self.usage()["reserved"], 0)

    def test_facebook_invalid_budget_never_dispatches(self):
        for value in (None, 0, -1, True, 1.5, "5"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "--facebook-max-items"):
                pipeline.fetch_facebook_author("example", provider="brightdata", max_items=value, allow_paid=True, root=self.root)
        self.submit.assert_not_called()
        self.read.assert_not_called()

    def test_instagram_reservation_and_actual_including_errors(self):
        records = [{"error": "bad_input"}, {"url": "https://instagram.com/reel/ABC", "date_posted": "2026-01-01"}]
        def read(url, headers):
            self.assertEqual(self.usage()["reserved"], 5)
            return {"status": "ready"} if "/progress/" in url else records
        self.read.side_effect = read
        with patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"):
            pipeline.fetch_instagram_author("example", max_items=5, allow_paid=True, root=self.root)
        self.assertEqual(self.usage()["used"], 2)
        self.assertEqual(self.usage()["reserved"], 0)
        self.assertEqual(self.usage()["daily_posts_used"], 0)

    def test_fallback_only_on_preflight_credit_rejection(self):
        fallback = {"facebook": {"provider": "apify"}, "items": []}
        with patch.dict(os.environ, {"BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "0", "RSS_FACEBOOK_FALLBACK_PROVIDER": "apify"}), \
                patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"), \
                patch.object(pipeline, "fetch_facebook_author_apify", return_value=fallback) as apify:
            result = self.facebook()
        self.assertEqual(result["facebook"]["provider"], "apify")
        self.assertEqual(result["facebook"]["fallback_from"], "brightdata")
        apify.assert_called_once()
        self.submit.assert_not_called()
        self.assertEqual(list(self.root.rglob(".submitted")), [])

    def test_no_fallback_after_submission_or_without_apify_permission(self):
        with patch.dict(os.environ, {"RSS_FACEBOOK_FALLBACK_PROVIDER": "apify"}), \
                patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"), \
                patch.object(pipeline, "fetch_facebook_author_apify") as apify:
            self.read.side_effect = RuntimeError("provider failed offline-token")
            with self.assertRaises(RuntimeError) as raised:
                self.facebook()
            self.assertNotIn("offline-token", str(raised.exception))
            apify.assert_not_called()
        with patch.dict(os.environ, {"BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "0", "RSS_FACEBOOK_FALLBACK_PROVIDER": "apify", "RSS_PAID_DISCOVERY": "brightdata"}), \
                patch.object(pipeline, "secure_rss_directory", return_value=self.root / "other"), \
                patch.object(pipeline, "fetch_facebook_author_apify") as apify:
            with self.assertRaises(RuntimeError):
                pipeline.fetch_facebook_author("example", provider="brightdata", max_items=5, root=self.root)
            apify.assert_not_called()

    def test_invalid_fallback_env_fails_before_http(self):
        with patch.dict(os.environ, {"RSS_FACEBOOK_FALLBACK_PROVIDER": "other"}):
            for provider in ("brightdata", "apify", "meta"):
                with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, "RSS_FACEBOOK_FALLBACK_PROVIDER"):
                    pipeline.fetch_facebook_author("example", provider=provider, max_items=5, allow_paid=True, root=self.root)
        self.submit.assert_not_called()
        self.read.assert_not_called()

    def test_invalid_credit_settings_block_pending_http_too(self):
        self.read.side_effect = lambda url, headers: {"status": "running"}
        self.assertEqual(self.post()["status"], "pending")
        self.read.reset_mock()
        with patch.dict(os.environ, {"BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "invalid"}):
            self.assertEqual(self.post()["status"], "error")
        self.read.assert_not_called()

    def test_non_object_trigger_response_retains_receipt_and_reservation(self):
        self.submit.return_value = ["unexpected"]
        self.assertEqual(self.post()["status"], "error")
        receipt = json.loads(next(self.root.rglob("submission.json")).read_text(encoding="utf-8"))
        self.assertEqual(receipt["response"], ["unexpected"])
        self.assertEqual(self.usage()["reserved"], 1)
        self.post()
        self.submit.assert_called_once()

    def test_refresh_does_not_promote_brightdata_permission_to_apify(self):
        state = {"subscriptions": [{"id": "fb", "kind": "facebook-author", "url": "https://facebook.com/example", "facebook_page_id": "example", "facebook_provider": "brightdata", "facebook_max_items": 5}], "items": []}
        with patch.dict(os.environ, {"RSS_PAID_DISCOVERY": "brightdata", "BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "0", "RSS_FACEBOOK_FALLBACK_PROVIDER": "apify"}), \
                patch.object(pipeline, "load_rss_state", return_value=state), \
                patch.object(pipeline, "save_rss_state"), \
                patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"), \
                patch.object(pipeline, "fetch_facebook_author_apify") as apify:
            self.assertEqual(pipeline.refresh_rss(self.root)["failed"], 1)
            apify.assert_not_called()
        self.submit.assert_not_called()

    def test_distinct_roots_and_all_providers_share_monthly_limit(self):
        self.read.side_effect = lambda url, headers: {"status": "running"}
        self.assertEqual(self.post()["status"], "pending")
        with patch.dict(os.environ, {"BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "5"}), \
                patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"):
            with self.assertRaises(pipeline.brightdata_credits.BrightDataQuotaError):
                self.facebook()
            with self.assertRaises(pipeline.brightdata_credits.BrightDataQuotaError):
                pipeline.fetch_instagram_author("example", max_items=5, allow_paid=True, root=self.root / "other")
        self.assertEqual(self.usage()["reserved"], 1)
        self.submit.assert_called_once()

    def test_facebook_refresh_schedule_interval_and_snapshot_advance(self):
        state = {"subscriptions": [{"id": "fb", "kind": "facebook-author", "url": "https://facebook.com/example", "facebook_page_id": "example", "facebook_provider": "brightdata", "facebook_max_items": 5}], "items": []}
        feed = {"items": [], "facebook": {"snapshot_id": "sd_new"}}
        with patch.dict(os.environ, {"RSS_PAID_DISCOVERY": "brightdata", "RSS_PAID_DISCOVERY_MIN_HOURS": "20"}), \
                patch.object(pipeline, "load_rss_state", side_effect=lambda root: copy.deepcopy(state)), \
                patch.object(pipeline, "save_rss_state", side_effect=lambda root, value: state.update(copy.deepcopy(value))), \
                patch.object(pipeline, "fetch_facebook_author", return_value=feed) as fetch, \
                patch.object(pipeline, "write_facebook_discovery", return_value=self.root / "latest.json"):
            self.assertEqual(pipeline.refresh_rss(self.root)["failed"], 0)
            self.assertTrue(fetch.call_args.kwargs["allow_paid"])
            self.assertEqual(fetch.call_args.kwargs["max_items"], 5)
            self.assertEqual(state["subscriptions"][0]["facebook_last_snapshot_id"], "sd_new")
            self.assertEqual(pipeline.refresh_rss(self.root)["deferred"], 1)
            fetch.assert_called_once()

    def test_cli_facebook_budget_and_usage_flags(self):
        with patch.object(pipeline.sys, "argv", ["pipeline.py", "--facebook-author", "example", "--facebook-provider", "brightdata", "--facebook-max-items", "5"]):
            args = pipeline.parse_args()
        self.assertEqual(args.facebook_max_items, 5)
        with patch.object(pipeline.sys, "argv", ["pipeline.py", "--brightdata-usage"]), patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(pipeline.main(), 0)
            result = json.loads(stdout.getvalue())
        self.assertEqual(set(result), {"month", "limit", "used", "reserved", "remaining", "daily_posts_used", "daily_post_limit"})

    def test_facebook_cli_add_update_and_opml_preserve_budget(self):
        def directory(root):
            target = root / "rss"
            target.mkdir(parents=True, exist_ok=True)
            return target
        def command(*argv):
            with patch.object(pipeline.sys, "argv", ["pipeline.py", *argv, "--root", str(self.root)]):
                return pipeline.run_rss_command(pipeline.parse_args())
        with patch.object(pipeline, "secure_rss_directory", side_effect=directory), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(command("--facebook-author", "example", "--facebook-provider", "brightdata",
                                     "--facebook-max-items", "5", "--allow-batch-cloud"), 0)
            saved = pipeline.load_rss_state(self.root)["subscriptions"][0]
            self.assertEqual(saved["facebook_last_snapshot_id"], "sd_test")
            self.assertEqual(saved["facebook_max_items"], 5)
            self.assertEqual(command("--rss-update", saved["id"], "--facebook-max-items", "7"), 0)
            with self.assertRaises(ValueError):
                command("--rss-update", saved["id"], "--facebook-max-items", "0")
            opml = self.root / "subscriptions.opml"
            pipeline.export_rss_opml(self.root, opml)
            self.assertIn('vidbeeFacebookMaxItems="7"', opml.read_text(encoding="utf-8"))
            self.assertEqual(pipeline.import_rss_opml(self.root / "imported", opml, allow_paid=True)["added"], 1)
            self.assertEqual(pipeline.load_rss_state(self.root / "imported")["subscriptions"][0]["facebook_max_items"], 7)

    def test_facebook_saved_snapshot_only_advances_after_successful_save(self):
        def directory(root):
            target = root / "rss"
            target.mkdir(parents=True, exist_ok=True)
            return target
        with patch.object(pipeline, "secure_rss_directory", side_effect=directory):
            with patch.object(pipeline, "save_rss_state", side_effect=OSError("disk full")), self.assertRaises(OSError):
                pipeline.add_facebook_subscription(self.root, "example", provider="brightdata", max_items=5, allow_paid=True)
            pipeline.add_facebook_subscription(self.root, "example", provider="brightdata", max_items=5, allow_paid=True)
            self.submit.assert_called_once()
            self.submit.return_value = {"snapshot_id": "sd_next"}
            with patch.object(pipeline, "save_rss_state", side_effect=OSError("disk full")), self.assertRaises(OSError):
                pipeline.refresh_rss(self.root, allow_paid=True)
            self.assertEqual(self.submit.call_count, 2)
            self.assertEqual(pipeline.load_rss_state(self.root)["subscriptions"][0]["facebook_last_snapshot_id"], "sd_test")
            self.assertEqual(pipeline.refresh_rss(self.root, allow_paid=True)["failed"], 0)
            self.assertEqual(self.submit.call_count, 2)
            self.assertEqual(pipeline.load_rss_state(self.root)["subscriptions"][0]["facebook_last_snapshot_id"], "sd_next")

    def test_facebook_empty_excludes_and_missing_post_id(self):
        record = {"url": "https://facebook.com/Page/posts/123", "shortcode": "short", "video_url": "https://a.fbcdn.net/v", "date_posted": "2026-01-02"}
        self.read.side_effect = lambda url, headers: {"status": "ready"} if "/progress/" in url else [record]
        with patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"):
            feed = self.facebook()
        self.assertEqual(feed["items"][0]["id"], "short")
        self.assertEqual(feed["items"][0]["title"], "Facebook Reel short")
        self.assertNotIn("posts_to_not_include", json.loads(self.submit.call_args.kwargs["body"])[0])
        del record["shortcode"]
        self.assertEqual(pipeline.brightdata_facebook_item(record)["id"], pipeline.job_key(record["url"]))

    def test_resume_and_cache_work_even_when_limits_now_zero(self):
        self.read.side_effect = [{"status": "running"}, {"status": "ready"}, [{"url": "https://instagram.com/p/ABC", "description": "text"}]]
        self.assertEqual(self.post()["status"], "pending")
        with patch.dict(os.environ, {"BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "0", "BRIGHTDATA_POST_DAILY_LIMIT": "0"}):
            self.assertEqual(self.post()["status"], "ready")
            self.assertEqual(self.post()["status"], "ready")
        self.assertEqual(self.usage()["daily_posts_used"], 1)
        self.submit.assert_called_once()

    def test_fallback_never_occurs_on_unknown_trigger_or_corrupt_receipt(self):
        self.submit.side_effect = TimeoutError("offline-token timed out")
        with patch.dict(os.environ, {"RSS_FACEBOOK_FALLBACK_PROVIDER": "apify"}), \
                patch.object(pipeline, "secure_rss_directory", return_value=self.root / "rss"), \
                patch.object(pipeline, "fetch_facebook_author_apify") as apify:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    self.facebook()
            apify.assert_not_called()
        self.submit.assert_called_once()
        self.assertEqual(self.usage()["reserved"], 5)


if __name__ == "__main__":
    unittest.main()
