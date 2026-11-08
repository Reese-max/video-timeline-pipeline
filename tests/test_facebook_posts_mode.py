"""Offline FB posts/auto regressions; real receipts and a temporary credit ledger."""
import copy
import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.parse
from contextlib import closing, nullcontext
from pathlib import Path
from unittest.mock import patch

import cost_tracker
import pipeline


REELS = "gd_lyclm3ey2q6rww027t"
POSTS = "gd_lkaxegm826bjpoo9m5"
DEAD = {"error_code": "dead_page", "error": "Seems page have not reels"}


def record(post_id="video", **fields):
    return {"post_id": post_id, "url": f"https://www.facebook.com/NASA/posts/{post_id}",
            "post_type": "Reel", "date_posted": "2026-10-20T15:06:10.000Z", **fields}


class FacebookPostsModeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.enterContext(patch.dict(os.environ, {
            "HOME": str(self.root), "USERPROFILE": str(self.root),
            "BRIGHTDATA_CREDIT_LEDGER": str(self.root / "credits.sqlite3"),
            "BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "5000", "BRIGHTDATA_POST_DAILY_LIMIT": "20",
            "BRIGHTDATA_API_TOKEN": "offline-token",
        }, clear=True))
        self.enterContext(patch.object(pipeline, "load_dotenv"))
        self.enterContext(cost_tracker.cost_root_scope(self.root))
        # Bypass only the unavailable sandbox ACL operation, never the state/receipt writes.
        self.enterContext(patch.object(pipeline, "secure_rss_directory", side_effect=self.directory))
        self.enterContext(patch.object(pipeline, "rss_writer_lock", side_effect=lambda root: nullcontext()))
        self.enterContext(patch("urllib.request.urlopen", side_effect=AssertionError("real HTTP forbidden")))
        self.results = {REELS: [DEAD], POSTS: [record(), record("photo", post_type="Post")]}
        self.snapshots = {}
        self.submit = self.enterContext(patch.object(pipeline, "request_json", side_effect=self.trigger))
        self.read = self.enterContext(patch.object(pipeline, "get_json", side_effect=self.download))

    def directory(self, root):
        target = root / "rss"
        target.mkdir(parents=True, exist_ok=True)
        return target

    def trigger(self, url, headers, **kwargs):
        dataset = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["dataset_id"][0]
        snapshot = f"sd_{len(self.snapshots)}"
        self.snapshots[snapshot] = copy.deepcopy(self.results[dataset])
        return {"snapshot_id": snapshot}

    def download(self, url, headers):
        if "/progress/" in url:
            return {"status": "ready"}
        value = self.snapshots[url.split("/snapshot/")[1].split("?")[0]]
        if isinstance(value, Exception):
            raise value
        return value

    def fetch(self, **kwargs):
        return pipeline.fetch_facebook_author("NASA", "2026-01-01", "brightdata",
            allow_paid=True, max_items=5, root=self.root, **kwargs)

    def seed(self, **fields):
        state = pipeline.load_rss_state(self.root)
        state["subscriptions"] = [{"id": "fb", "kind": "facebook-author", "name": "NASA",
            "url": "https://www.facebook.com/NASA", "facebook_page_id": "NASA",
            "facebook_provider": "brightdata", "facebook_max_items": 5,
            "facebook_since": "2026-01-01", "facebook_last_snapshot_id": "sd_old", **fields}]
        pipeline.save_rss_state(self.root, state)
        return state

    def saved(self):
        return pipeline.load_rss_state(self.root)["subscriptions"][0]

    def datasets(self):
        return [urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[0]).query)["dataset_id"][0]
                for call in self.submit.call_args_list]

    def command(self, *args):
        with patch.object(pipeline.sys, "argv", ["pipeline.py", *args, "--root", str(self.root)]):
            return pipeline.run_rss_command(pipeline.parse_args())

    def test_posts_trigger_body_local_dates_and_receipt_reuse(self):
        self.results[POSTS] += [record("old", date_posted="2025-01-01"),
                                record("missing", date_posted=""), {"error_code": "bad_input"}]
        feed = self.fetch(brightdata_mode="posts", exclude_ids=["9", "8", "9"])
        self.fetch(brightdata_mode="posts", exclude_ids=["9", "8", "9"])
        self.submit.assert_called_once()
        call = self.submit.call_args
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(call.args[0]).query)
        self.assertEqual(query, {"dataset_id": [POSTS], "include_errors": ["true"], "format": ["json"]})
        self.assertEqual(json.loads(call.kwargs["body"]), [{"url": "https://www.facebook.com/NASA",
            "num_of_posts": 5, "posts_to_not_include": ["8", "9"]}])
        self.assertEqual({item["id"] for item in feed["items"]}, {"video", "missing"})
        meta = feed["facebook"]
        self.assertEqual((meta["mode"], meta["dataset_id"]), ("posts", POSTS))
        self.assertEqual((meta["raw_count"], meta["video_count"], meta["non_video_count"], meta["error_count"]),
                         (5, 3, 1, 1))
        self.assertEqual(meta["missing_date_count"], 1)
        self.assertEqual(pipeline.brightdata_credits.usage()["used"], 5)
        receipt = next(self.root.rglob("submission.json"))
        self.assertNotIn("offline-token", receipt.read_text(encoding="utf-8"))

    def test_posts_empty_excludes_and_mode_separate_receipts(self):
        self.results[REELS] = []
        self.fetch(brightdata_mode="reels")
        self.fetch(brightdata_mode="posts")
        self.assertEqual(self.datasets(), [REELS, POSTS])
        self.assertEqual(len(list(self.root.rglob("submission.json"))), 2)
        self.assertNotIn("posts_to_not_include", json.loads(self.submit.call_args.kwargs["body"])[0])

    def test_legacy_reels_receipt_is_reused_after_upgrade(self):
        self.results[REELS] = []
        self.fetch(brightdata_mode="reels")
        body = self.submit.call_args.kwargs["body"].decode("utf-8")
        receipt = next(self.root.rglob("submission.json"))
        legacy = receipt.parent.parent / pipeline.job_key("facebook:NASA:2026-01-01:", body)
        receipt.parent.rename(legacy)
        self.fetch(brightdata_mode="reels")
        self.submit.assert_called_once()

    def test_legacy_uncertain_reels_marker_blocks_trigger(self):
        body = json.dumps([{"url": "https://www.facebook.com/NASA", "num_of_posts": 5}], sort_keys=True)
        legacy = self.directory(self.root) / "brightdata" / pipeline.job_key("facebook:NASA:2026-01-01:", body)
        legacy.mkdir(parents=True)
        (legacy / ".submitted").write_text("", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "禁止再次計費"):
            self.fetch(brightdata_mode="reels")
        self.submit.assert_not_called()

    def test_posts_photo_only_success_advances_snapshot_and_excludes_next_time(self):
        self.seed(facebook_brightdata_mode="posts")
        self.results[POSTS] = [record("photo", post_type="Post")]
        totals = pipeline.refresh_rss(self.root, allow_paid=True)
        self.assertEqual((totals["failed"], totals["queued"]), (0, 0))
        self.assertEqual(self.saved()["facebook_seen_post_ids"], ["photo"])
        self.assertEqual(self.saved()["facebook_last_snapshot_id"], "sd_0")
        pipeline.refresh_rss(self.root, allow_paid=True)
        self.assertEqual(json.loads(self.submit.call_args.kwargs["body"])[0]["posts_to_not_include"], ["photo"])

    def test_auto_all_dead_page_errors_uses_every_code_and_independent_posts_receipt(self):
        self.results[REELS] = [DEAD, DEAD]
        self.results[POSTS] = []
        feed = self.fetch()
        self.assertEqual(feed["facebook"]["reels_error_codes"], ["dead_page", "dead_page"])
        self.assertEqual(self.datasets(), [REELS, POSTS])
        self.fetch()
        self.assertEqual(self.datasets(), [REELS, POSTS])
        self.assertEqual(pipeline.brightdata_credits.usage()["used"], 2)

    def test_explicit_posts_add_keeps_mode_without_trying_reels(self):
        subscription, _, _ = pipeline.add_facebook_subscription(self.root, "NASA",
            provider="brightdata", brightdata_mode="posts", max_items=5, allow_paid=True)
        self.assertEqual(subscription["facebook_brightdata_mode"], "posts")
        self.assertEqual(self.datasets(), [POSTS])

    def test_video_detection_urls_titles_ids_and_hosts(self):
        parse = pipeline.brightdata_facebook_post_item
        base = record(content="字" * 170)
        for fields in ({"post_type": "rEeL"}, {"post_type": "VIDEO"},
                       {"post_type": "Post", "attachments": [{"type": "vIdEo"}]},
                       {"post_type": "Post", "attachments": [{"video_url": "https://cdn.test/v"}]}):
            with self.subTest(fields=fields):
                item = parse({**base, **fields})
                self.assertEqual(item["url"], base["url"])
                self.assertEqual(item["title"], "字" * 160)
                self.assertEqual(item["published"], base["date_posted"])
        for fields in ({"post_type": "Post", "attachments": [{"type": "Photo"}]},
                       {"post_type": "Post", "attachments": [{"video_url": ""}]},
                       {"url": "https://facebook.com.evil.test/reel/1"},
                       {"url": "https://fbcdn.net/video.mp4"}):
            with self.subTest(rejected=fields):
                self.assertIsNone(parse({**base, **fields}))
        for url in ("https://www.facebook.com/reel/12/", "https://m.facebook.com/NASA/videos/12/",
                    "https://web.facebook.com/watch/?v=12&token=secret"):
            item = parse(record(attachments=[{"type": "Video", "url": url,
                                            "video_url": "https://cdn.test/expires"}]))
            self.assertEqual(item["url"], pipeline.public_url(url))
        for url in ("https://evil.test/reel/12", "https://cdn.test/v", "https://facebook.com/NASA/posts/12"):
            self.assertEqual(parse(record(attachments=[{"type": "Video", "url": url}]))["url"], base["url"])
        for url in ("https://facebook.com/reel/1/", "https://facebook.com/NASA/videos/1/"):
            self.assertEqual(parse(record(url=url, attachments=[{"type": "Video",
                "url": "https://facebook.com/reel/2/"}]))["url"], url)
        self.assertEqual(parse(record(attachments=[{"type": "Photo", "url": "https://facebook.com/reel/2/"}]))["url"], base["url"])
        item = parse(record(None, shortcode="short", content=""))
        self.assertEqual((item["id"], item["title"]), ("short", "Facebook video short"))
        item = parse(record(None, content=""))
        self.assertEqual(item["id"], pipeline.job_key(item["url"]))

    def test_video_attachment_can_supply_safe_url_when_record_url_is_unusable(self):
        for url in ("", "https://cdn.test/expired", "https://facebook.com.evil.test/posts/1"):
            with self.subTest(url=url):
                item = pipeline.brightdata_facebook_post_item(record(url=url,
                    attachments=[{"type": "Video", "url": "https://www.facebook.com/reel/123/"}]))
                self.assertEqual(item["url"], "https://www.facebook.com/reel/123/")

    def test_facebook_subdomain_watch_preserves_only_video_id(self):
        item = pipeline.brightdata_facebook_post_item(record(attachments=[{"type": "Video",
            "url": "https://mbasic.facebook.com/watch/?v=123&token=secret#fragment"}]))
        self.assertEqual(item["url"], "https://mbasic.facebook.com/watch/?v=123")

    def test_watch_attachment_requires_numeric_video_id_after_cleaning(self):
        for query in ("", "?ref=embed", "?v=abc", "?v=123abc", "?v=123&ref=embed"):
            with self.subTest(query=query):
                base = record(attachments=[{"type": "Video",
                    "url": "https://www.facebook.com/watch/" + query}])
                item = pipeline.brightdata_facebook_post_item(base)
                expected = "https://www.facebook.com/watch/?v=123" if query.startswith("?v=123&") else base["url"]
                self.assertEqual(item["url"], expected)

    def test_video_counts_exclude_invalid_urls_in_both_modes(self):
        for mode, dataset in (("posts", POSTS), ("reels", REELS)):
            with self.subTest(mode=mode):
                self.results[dataset] = [record("valid", url="https://facebook.com/reel/123/"),
                    record("invalid", url="https://cdn.test/reel/456/"),
                    record("photo", post_type="Post")]
                meta = self.fetch(brightdata_mode=mode)["facebook"]
                self.assertEqual(meta["video_count"], 1)
                self.assertEqual(meta["invalid_video_count"], 1)
                self.assertEqual(meta["non_video_count"], 1)
                self.assertIn("無效影片 1 筆", pipeline.facebook_discovery_detail(meta))

    def test_reels_video_type_with_unusable_url_counts_invalid(self):
        self.results[REELS] = [record(url="https://cdn.test/expired")]
        meta = self.fetch(brightdata_mode="reels")["facebook"]
        self.assertEqual(meta["invalid_video_count"], 1)
        self.assertEqual(meta["non_video_count"], 0)

    def test_whitespace_post_id_falls_back_to_trimmed_shortcode_in_both_parsers(self):
        for parse in (pipeline.brightdata_facebook_post_item, pipeline.brightdata_facebook_item):
            for post_id, shortcode, expected in (("  ", " short ", "short"), (" id ", "short", "id"),
                                                   ("  ", " \t ", None)):
                with self.subTest(parser=parse.__name__, post_id=post_id, shortcode=shortcode):
                    item = parse(record(post_id, shortcode=shortcode, url="https://facebook.com/reel/123/"))
                    self.assertEqual(item["id"], expected or pipeline.job_key(item["url"]))

    def test_auto_refresh_switches_settles_twice_then_only_posts(self):
        self.seed()
        self.assertEqual(pipeline.refresh_rss(self.root, allow_paid=True)["failed"], 0)
        saved = self.saved()
        self.assertEqual(saved["facebook_brightdata_mode"], "posts")
        self.assertEqual(set(saved["facebook_seen_post_ids"]), {"video", "photo"})
        self.assertEqual(self.datasets(), [REELS, POSTS])
        meta = json.loads((self.root / "rss" / saved["facebook_last_discovery"]).read_text(encoding="utf-8"))["facebook"]
        self.assertEqual((meta["switched_from"], meta["reels_error_codes"]), ("reels", ["dead_page"]))
        with closing(sqlite3.connect(self.root / "credits.sqlite3")) as connection:
            rows = connection.execute("SELECT credits, actual FROM reservations ORDER BY rowid").fetchall()
        self.assertEqual(rows, [(5, 1), (5, 2)])
        self.assertEqual(pipeline.brightdata_credits.usage()["reserved"], 0)
        self.assertEqual(pipeline.refresh_rss(self.root, allow_paid=True)["failed"], 0)
        self.assertEqual(self.datasets(), [REELS, POSTS, POSTS])
        self.assertEqual(json.loads(self.submit.call_args.kwargs["body"])[0]["posts_to_not_include"], ["photo", "video"])

    def test_dead_page_mode_saved_before_posts_attempt_even_when_it_fails(self):
        self.seed()
        original = self.trigger
        def trigger(url, headers, **kwargs):
            if POSTS in url:
                self.assertEqual(self.saved()["facebook_brightdata_mode"], "posts")
            return original(url, headers, **kwargs)
        self.submit.side_effect = trigger
        self.results[POSTS] = RuntimeError("posts failed offline-token")
        self.assertEqual(pipeline.refresh_rss(self.root, allow_paid=True)["failed"], 1)
        saved = self.saved()
        self.assertEqual(saved["facebook_brightdata_mode"], "posts")
        self.assertEqual(saved["facebook_last_snapshot_id"], "sd_old")
        self.assertNotIn("facebook_seen_post_ids", saved)
        self.assertNotIn("offline-token", saved["last_error"])
        self.assertEqual(pipeline.refresh_rss(self.root, allow_paid=True)["failed"], 1)
        self.assertEqual(self.datasets(), [REELS, POSTS])

    def test_auto_non_dead_and_partial_errors_do_not_switch(self):
        for index, rows in enumerate(([{"error_code": "bad_input"}], [DEAD, {"error_code": "bad_input"}],
                                      [DEAD, record(url="https://facebook.com/reel/123/")], [DEAD, None], [])):
            with self.subTest(rows=rows):
                self.results[REELS] = rows
                options = {"previous_snapshot_id": f"sd_case{index}"}
                if rows and all(isinstance(row, dict) and row.get("error_code") for row in rows):
                    with self.assertRaises(pipeline.BrightDataErrorRecords) as raised:
                        self.fetch(**options)
                    self.assertEqual(raised.exception.codes, [row["error_code"] for row in rows])
                    self.assertNotIsInstance(raised.exception, pipeline.brightdata_credits.BrightDataQuotaError)
                else:
                    self.assertEqual(self.fetch(**options)["facebook"]["mode"], "reels")
        self.assertEqual(self.datasets(), [REELS] * 5)

    def test_explicit_reels_never_switches(self):
        with self.assertRaises(pipeline.BrightDataErrorRecords):
            self.fetch(brightdata_mode="reels")
        self.assertEqual(self.datasets(), [REELS])

    def test_error_codes_all_rows_and_token_redaction(self):
        self.results[REELS] = [DEAD] * 3 + [{"error_code": "bad_input", "error": "offline-token"}]
        with self.assertRaises(pipeline.BrightDataErrorRecords) as raised:
            self.fetch()
        self.assertEqual(raised.exception.codes, ["dead_page"] * 3 + ["bad_input"])
        self.assertEqual(self.datasets(), [REELS])
        self.assertNotIn("offline-token", str(raised.exception))

    def test_switch_meta_and_detail_include_all_billed_reels_errors(self):
        self.results[REELS] = [DEAD, DEAD]
        meta = self.fetch()["facebook"]
        self.assertEqual(meta["reels_billed"], 2)
        self.assertIn("模式 reels → posts（自動切換；reels dead_page 已計費 2 筆）",
                      pipeline.facebook_discovery_detail(meta))

    def test_legacy_brightdata_detail_defaults_to_reels(self):
        meta = {"provider": "brightdata", "raw_count": 3, "error_count": 1}
        self.assertIn("模式 reels", pipeline.facebook_discovery_detail(meta))

    def test_posts_quota_fallback_requires_separate_apify_permission(self):
        self.seed()
        with patch.dict(os.environ, {"BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "5",
                "RSS_FACEBOOK_FALLBACK_PROVIDER": "apify", "RSS_PAID_DISCOVERY": "brightdata"}), \
                patch.object(pipeline, "fetch_facebook_author_apify") as apify:
            self.assertEqual(pipeline.refresh_rss(self.root)["failed"], 1)
            self.assertEqual(self.saved()["facebook_brightdata_mode"], "posts")
            apify.assert_not_called()
            apify.return_value = {"title": "NASA", "items": [], "facebook": {
                "provider": "apify", "page_id": "NASA", "author_url": "https://facebook.com/NASA"}}
            with patch.dict(os.environ, {"RSS_PAID_DISCOVERY": "brightdata,apify"}):
                self.assertEqual(pipeline.refresh_rss(self.root)["failed"], 0)
            apify.assert_called_once()
        self.assertEqual(self.datasets(), [REELS])

    def test_seen_ids_bounded_include_photos_and_ignore_errors_or_invalid_ids(self):
        self.seed(facebook_brightdata_mode="posts", facebook_seen_post_ids=[str(i) for i in range(500)])
        self.results[POSTS] = [record("photo", post_type="Post"), record("499", post_type="Post"),
            record(501), record("bad", error_code="bad_input"), record(None, post_type="Post"),
            record({"bad": "id"}, post_type="Post"), record(True, post_type="Post")]
        self.assertEqual(pipeline.refresh_rss(self.root, allow_paid=True)["failed"], 0)
        ids = self.saved()["facebook_seen_post_ids"]
        self.assertEqual(len(ids), 500)
        self.assertEqual(len(set(ids)), 500)
        self.assertTrue({"photo", "499", "501"}.issubset(ids))
        self.assertFalse({"bad", "None", "True", "0", "1"}.intersection(ids))
        pipeline.refresh_rss(self.root, allow_paid=True)
        self.assertEqual(set(json.loads(self.submit.call_args.kwargs["body"])[0]["posts_to_not_include"]), set(ids))

    def test_seen_ids_reject_non_lists_and_filter_mixed_elements_before_fetch_and_save(self):
        for index, (stored, expected) in enumerate((("photo", []), (42, []),
                ([" old ", 7, True, False, None, {}, [], " ", "7"], ["old", "7"]))):
            with self.subTest(stored=stored):
                state = self.seed(facebook_brightdata_mode="posts", facebook_seen_post_ids=stored,
                                  facebook_last_snapshot_id=f"sd_case{index}")
                state["items"] = []
                pipeline.save_rss_state(self.root, state)
                self.assertEqual(pipeline.refresh_rss(self.root, allow_paid=True)["failed"], 0)
                payload = json.loads(self.submit.call_args.kwargs["body"])[0]
                self.assertEqual(set(payload.get("posts_to_not_include", [])), set(expected))
                self.assertEqual(set(self.saved()["facebook_seen_post_ids"]), {*expected, "video", "photo"})

    def test_direct_fetch_seen_ids_also_rejects_string(self):
        self.fetch(brightdata_mode="posts", seen_post_ids="photo")
        self.assertNotIn("posts_to_not_include", json.loads(self.submit.call_args.kwargs["body"])[0])

    def test_failed_state_save_does_not_advance_seen_ids_or_resubmit_receipt(self):
        self.seed(facebook_brightdata_mode="posts", facebook_seen_post_ids=["old"])
        with patch.object(pipeline, "save_rss_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                pipeline.refresh_rss(self.root, allow_paid=True)
        self.assertEqual(self.saved()["facebook_seen_post_ids"], ["old"])
        pipeline.refresh_rss(self.root, allow_paid=True)
        self.submit.assert_called_once()
        self.assertEqual(set(self.saved()["facebook_seen_post_ids"]), {"old", "video", "photo"})

    def test_add_switch_success_persists_posts(self):
        subscription, _, _ = pipeline.add_facebook_subscription(self.root, "NASA", since="2026-01-01",
            provider="brightdata", max_items=5, allow_paid=True)
        self.assertEqual(subscription["facebook_brightdata_mode"], "posts")
        self.assertEqual(set(subscription["facebook_seen_post_ids"]), {"video", "photo"})
        meta = json.loads((self.root / "rss" / subscription["facebook_last_discovery"]).read_text(encoding="utf-8"))["facebook"]
        self.assertEqual(meta["switched_from"], "reels")

    def test_add_switch_failure_leaves_no_subscription_and_retry_reuses_both_receipts(self):
        self.results[POSTS] = RuntimeError("posts failed")
        with patch.object(pipeline, "save_rss_state", wraps=pipeline.save_rss_state) as save:
            with self.assertRaisesRegex(RuntimeError, "posts failed"):
                pipeline.add_facebook_subscription(self.root, "NASA", provider="brightdata", max_items=5, allow_paid=True)
            self.assertEqual(pipeline.load_rss_state(self.root)["subscriptions"], [])
            save.assert_not_called()
        self.assertEqual(self.datasets(), [REELS, POSTS])
        self.assertEqual(len(list(self.root.rglob("submission.json"))), 2)
        with closing(sqlite3.connect(self.root / "credits.sqlite3")) as connection:
            before = connection.execute("SELECT rowid, credits, actual FROM reservations ORDER BY rowid").fetchall()
        self.assertEqual([row[1:] for row in before], [(5, 1), (5, None)])
        self.snapshots["sd_1"] = [record(), record("photo", post_type="Post")]
        subscription, _, _ = pipeline.add_facebook_subscription(self.root, "NASA",
            provider="brightdata", max_items=5, allow_paid=True)
        self.assertEqual(subscription["facebook_brightdata_mode"], "posts")
        self.assertTrue(subscription["facebook_last_discovery"])
        self.assertEqual(self.datasets(), [REELS, POSTS])
        with closing(sqlite3.connect(self.root / "credits.sqlite3")) as connection:
            after = connection.execute("SELECT rowid, credits, actual FROM reservations ORDER BY rowid").fetchall()
        self.assertEqual(after, [(before[0][0], 5, 1), (before[1][0], 5, 2)])
        self.assertEqual(pipeline.brightdata_credits.usage()["reserved"], 0)

    def test_cli_add_switch_failure_leaves_no_subscription(self):
        self.results[POSTS] = RuntimeError("posts failed")
        with self.assertRaisesRegex(RuntimeError, "posts failed"):
            self.command("--facebook-author", "NASA", "--facebook-provider", "brightdata",
                         "--facebook-max-items", "5", "--allow-batch-cloud")
        self.assertEqual(pipeline.load_rss_state(self.root)["subscriptions"], [])

    def opml(self, mode="auto"):
        source = self.root / "subscriptions.opml"
        source.write_text('<opml version="2.0"><body><outline xmlUrl="https://www.facebook.com/NASA" '
            'vidbeeKind="facebook-author" vidbeeFacebookProvider="brightdata" vidbeeFacebookMaxItems="5" '
            f'vidbeeFacebookBrightdataMode="{mode}" vidbeeEnabled="false" /></body></opml>', encoding="utf-8")
        return source

    def test_disabled_opml_switch_failure_leaves_no_subscription_then_retry_stays_disabled(self):
        self.results[POSTS] = RuntimeError("posts failed")
        source = self.opml()
        self.assertEqual(pipeline.import_rss_opml(self.root, source, allow_paid=True),
                         {"added": 0, "existing": 0, "failed": 1})
        self.assertEqual(pipeline.load_rss_state(self.root)["subscriptions"], [])
        with patch.dict(os.environ, {"RSS_PAID_DISCOVERY": "brightdata"}):
            with self.assertRaisesRegex(RuntimeError, "尚未加入 RSS 訂閱"):
                pipeline.refresh_rss(self.root)
        self.assertEqual(self.datasets(), [REELS, POSTS])
        self.snapshots["sd_1"] = [record()]
        self.assertEqual(pipeline.import_rss_opml(self.root, source, allow_paid=True),
                         {"added": 1, "existing": 0, "failed": 0})
        self.assertEqual(self.saved()["facebook_brightdata_mode"], "posts")
        self.assertFalse(self.saved()["enabled"])
        with patch.dict(os.environ, {"RSS_PAID_DISCOVERY": "brightdata"}):
            pipeline.refresh_rss(self.root)
        self.assertEqual(self.datasets(), [REELS, POSTS])

    def test_disabled_opml_successful_switch_stays_disabled(self):
        self.assertEqual(pipeline.import_rss_opml(self.root, self.opml(), allow_paid=True)["added"], 1)
        self.assertEqual(self.saved()["facebook_brightdata_mode"], "posts")
        self.assertFalse(self.saved()["enabled"])

    def test_manual_posts_and_auto_still_require_allow_batch_cloud(self):
        with patch.dict(os.environ, {"RSS_PAID_DISCOVERY": "brightdata"}):
            for mode in ("auto", "posts"):
                with self.subTest(mode=mode), self.assertRaisesRegex(RuntimeError, "--allow-batch-cloud"):
                    self.command("--facebook-author", "NASA", "--facebook-provider", "brightdata",
                        "--facebook-max-items", "5", "--facebook-brightdata-mode", mode, "--facebook-discover-only")
        self.submit.assert_not_called()

    def test_discover_only_prints_mode_switch_and_counts_without_subscription(self):
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(self.command("--facebook-author", "NASA", "--facebook-provider", "brightdata",
                "--facebook-max-items", "5", "--facebook-discover-only", "--allow-batch-cloud"), 0)
        output = stdout.getvalue()
        for expected in ("模式 reels → posts（自動切換；reels dead_page 已計費 1 筆）",
                         "原始 2 筆", "影片 1 筆", "非影片 1 筆", "錯誤 0 筆"):
            self.assertIn(expected, output)
        self.assertEqual(pipeline.load_rss_state(self.root)["subscriptions"], [])

    def test_reels_and_add_cli_also_print_mode_and_all_counts(self):
        self.results[REELS] = [record(url="https://facebook.com/reel/123/")]
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(self.command("--facebook-author", "NASA", "--facebook-provider", "brightdata",
                "--facebook-max-items", "5", "--facebook-brightdata-mode", "reels",
                "--facebook-discover-only", "--allow-batch-cloud"), 0)
        for expected in ("reels", "原始 1 筆", "影片 1 筆", "非影片 0 筆", "錯誤 0 筆"):
            self.assertIn(expected, stdout.getvalue())
        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(self.command("--facebook-author", "NASA", "--facebook-provider", "brightdata",
                "--facebook-max-items", "5", "--facebook-brightdata-mode", "posts", "--allow-batch-cloud"), 0)
        for expected in ("posts", "原始 2 筆", "影片 1 筆", "非影片 1 筆", "錯誤 0 筆"):
            self.assertIn(expected, stdout.getvalue())

    def test_switch_save_failure_stops_before_posts_trigger(self):
        self.seed()
        with patch.object(pipeline, "save_rss_state", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                pipeline.refresh_rss(self.root, allow_paid=True)
        self.assertEqual(self.datasets(), [REELS])
        self.assertNotIn("facebook_brightdata_mode", self.saved())
        pipeline.refresh_rss(self.root, allow_paid=True)
        self.assertEqual(self.datasets(), [REELS, POSTS])

    def test_seen_ids_do_not_change_reels_exclusions(self):
        self.results[REELS] = []
        self.fetch(brightdata_mode="reels", exclude_ids=["video"], seen_post_ids=["photo"])
        self.assertEqual(json.loads(self.submit.call_args.kwargs["body"])[0]["posts_to_not_include"], ["video"])

    def test_posts_sent_quota_like_error_is_never_apify_fallback(self):
        self.results[POSTS] = [{"error_code": "quota_exhausted", "error": "offline-token"}]
        with patch.dict(os.environ, {"RSS_FACEBOOK_FALLBACK_PROVIDER": "apify"}), \
                patch.object(pipeline, "fetch_facebook_author_apify") as apify:
            for _ in range(2):
                with self.assertRaises(pipeline.BrightDataErrorRecords):
                    self.fetch(brightdata_mode="posts")
            apify.assert_not_called()
        self.assertEqual(self.datasets(), [POSTS])

    def test_same_run_auto_posts_quota_can_use_authorized_apify(self):
        self.seed()
        with patch.dict(os.environ, {"BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "5", "RSS_FACEBOOK_FALLBACK_PROVIDER": "apify"}), \
                patch.object(pipeline, "fetch_facebook_author_apify", return_value={"title": "NASA", "items": [],
                    "facebook": {"provider": "apify", "page_id": "NASA", "author_url": "https://facebook.com/NASA"}}) as apify:
            self.assertEqual(pipeline.refresh_rss(self.root, allow_paid=True)["failed"], 0)
            apify.assert_called_once()
        self.assertEqual(self.saved()["facebook_brightdata_mode"], "posts")
        self.assertEqual(self.datasets(), [REELS])
        meta = json.loads((self.root / "rss" / self.saved()["facebook_last_discovery"]).read_text(encoding="utf-8"))["facebook"]
        self.assertEqual((meta["fallback_from"], meta["switched_from"]), ("brightdata", "reels"))

    def test_cli_update_and_opml_preserve_mode(self):
        self.seed()
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(self.command("--rss-update", "fb", "--facebook-brightdata-mode", "posts"), 0)
        self.assertEqual(self.saved()["facebook_brightdata_mode"], "posts")
        opml = self.root / "subscriptions.opml"
        pipeline.export_rss_opml(self.root, opml)
        self.assertIn('vidbeeFacebookBrightdataMode="posts"', opml.read_text(encoding="utf-8"))
        imported = self.root / "imported"
        self.assertEqual(pipeline.import_rss_opml(imported, opml, allow_paid=True)["added"], 1)
        self.assertEqual(pipeline.load_rss_state(imported)["subscriptions"][0]["facebook_brightdata_mode"], "posts")
        self.assertEqual(self.datasets(), [POSTS])

    def test_cli_mode_rejects_non_brightdata_and_non_facebook_updates(self):
        for fields in ({"facebook_provider": "apify"}, {"facebook_provider": "meta"},
                       {"kind": "rss"}, {"kind": "instagram-author"}):
            self.seed(**fields)
            before = pipeline.load_rss_state(self.root)
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.command("--rss-update", "fb", "--facebook-brightdata-mode", "posts")
            self.assertEqual(pipeline.load_rss_state(self.root), before)
        with self.assertRaises(ValueError):
            self.command("--facebook-author", "NASA", "--facebook-provider", "meta", "--facebook-brightdata-mode", "auto")
        self.submit.assert_not_called()

    def test_opml_invalid_brightdata_mode_counts_failed_without_subscription(self):
        self.assertEqual(pipeline.import_rss_opml(self.root, self.opml("invalid"), allow_paid=True),
                         {"added": 0, "existing": 0, "failed": 1})
        self.assertEqual(pipeline.load_rss_state(self.root)["subscriptions"], [])
        self.submit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
