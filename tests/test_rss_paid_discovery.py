"""RSS_PAID_DISCOVERY lets a scheduled refresh use only the named paid discovery providers."""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pipeline

SUBSCRIPTIONS = [
    {"id": "fb-apify", "kind": "facebook-author", "url": "https://www.facebook.com/somepage/",
     "facebook_page_id": "somepage", "facebook_provider": "apify", "facebook_since": "7d",
     "name": "fb", "enabled": True, "latest_only": False, "keywords": [], "tags": []},
    {"id": "ig-bd", "kind": "instagram-author", "url": "https://www.instagram.com/someone/",
     "instagram_username": "someone", "instagram_since": "7d", "instagram_max_items": 3,
     "name": "ig", "enabled": True, "latest_only": False, "keywords": [], "tags": []},
]


def seed(root: Path, last: dict[str, str] | None = None) -> None:
    """Write both subscriptions; last maps subscription id -> paid_discovery_at."""
    state = pipeline.load_rss_state(root)
    state["subscriptions"].extend(dict(item, **({"paid_discovery_at": (last or {})[item["id"]]}
                                                 if item["id"] in (last or {}) else {})) for item in SUBSCRIPTIONS)
    pipeline.save_rss_state(root, state)


def run_refresh(root: Path, env: dict[str, str], allow_paid: bool, out: dict | None = None,
                facebook_error: type[BaseException] = RuntimeError) -> dict[str, bool]:
    """Refresh and record the allow_paid each paid provider received (fetches stop right there)."""
    seen: dict[str, bool] = {}

    def facebook(author, since, provider, *, allow_paid=False):
        seen[provider] = allow_paid
        raise facebook_error("stop after the gate")

    def instagram(username, since, *, allow_paid=False, **kwargs):
        seen["brightdata"] = allow_paid
        raise RuntimeError("stop after the gate")

    with mock.patch.dict(pipeline.os.environ, env), \
         mock.patch.object(pipeline, "fetch_facebook_author", side_effect=facebook), \
         mock.patch.object(pipeline, "fetch_instagram_author", side_effect=instagram):
        totals = pipeline.refresh_rss(root, allow_paid=allow_paid)
    if out is not None:
        out["totals"] = totals
        out["subscriptions"] = saved(root)
    return seen


def saved(root: Path) -> dict[str, dict]:
    return {s["id"]: s for s in pipeline.load_rss_state(root)["subscriptions"]}


def refresh(env: dict[str, str], allow_paid: bool, last: dict[str, str] | None = None,
            out: dict | None = None) -> dict[str, bool]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        seed(root, last)
        return run_refresh(root, env, allow_paid, out)


def hours_ago(hours: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - hours * 3600))


class PaidDiscoveryAllowlistTests(unittest.TestCase):
    def test_without_the_setting_a_scheduled_refresh_pays_for_nothing(self) -> None:
        self.assertEqual(refresh({"RSS_PAID_DISCOVERY": ""}, False), {"apify": False, "brightdata": False})

    def test_named_provider_is_allowed_and_the_other_stays_blocked(self) -> None:
        self.assertEqual(refresh({"RSS_PAID_DISCOVERY": " Apify "}, False), {"apify": True, "brightdata": False})

    def test_allow_batch_cloud_still_allows_every_provider(self) -> None:
        self.assertEqual(refresh({"RSS_PAID_DISCOVERY": ""}, True), {"apify": True, "brightdata": True})

    def test_unknown_names_allow_nothing(self) -> None:
        self.assertEqual(refresh({"RSS_PAID_DISCOVERY": "all,*,yes"}, False), {"apify": False, "brightdata": False})

    def test_each_name_only_allows_itself(self) -> None:
        self.assertEqual(refresh({"RSS_PAID_DISCOVERY": "brightdata"}, False), {"apify": False, "brightdata": True})
        self.assertEqual(refresh({"RSS_PAID_DISCOVERY": "apify,brightdata"}, False), {"apify": True, "brightdata": True})

    def test_free_meta_provider_is_not_affected(self) -> None:
        self.assertFalse(pipeline.paid_discovery_allowed("meta", False))
        with mock.patch.dict(pipeline.os.environ, {"RSS_PAID_DISCOVERY": "apify"}):
            self.assertFalse(pipeline.paid_discovery_allowed("meta", False))
            self.assertTrue(pipeline.paid_discovery_allowed("apify", False))


class PaidDiscoveryIntervalTests(unittest.TestCase):
    ENV = {"RSS_PAID_DISCOVERY": "apify", "RSS_PAID_DISCOVERY_MIN_HOURS": "20"}

    def test_a_recent_scheduled_discovery_waits_without_counting_as_a_failure(self) -> None:
        out: dict = {}
        six_hours_ago = hours_ago(6)
        seen = refresh(self.ENV, False, last={"fb-apify": six_hours_ago}, out=out)
        self.assertNotIn("apify", seen, "Apify must not be called again within 20 hours")
        self.assertEqual(out["totals"]["deferred"], 1)
        self.assertEqual(out["totals"]["failed"], 1, "only the blocked Bright Data subscription fails")
        self.assertEqual(out["subscriptions"]["fb-apify"]["paid_discovery_at"], six_hours_ago)
        self.assertNotIn("last_checked", out["subscriptions"]["fb-apify"])

    def test_back_to_back_scheduled_refreshes_call_apify_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root)
            self.assertEqual(run_refresh(root, self.ENV, False).get("apify"), True)
            self.assertNotIn("apify", run_refresh(root, self.ENV, False), "the saved rss_now() stamp must be honoured")

    def test_a_run_killed_after_dispatch_still_starts_the_clock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root)
            with self.assertRaises(KeyboardInterrupt):
                run_refresh(root, self.ENV, False, facebook_error=KeyboardInterrupt)
            self.assertIn("paid_discovery_at", saved(root)["fb-apify"], "reserved before the billable call")
            self.assertNotIn("apify", run_refresh(root, self.ENV, False))

    def test_the_stamp_is_on_disk_when_the_paid_call_is_made(self) -> None:
        on_disk: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root, {"fb-apify": hours_ago(30)})

            def facebook(author, since, provider, *, allow_paid=False):
                on_disk.append(saved(root)["fb-apify"]["paid_discovery_at"])
                raise RuntimeError("stop after the gate")

            with mock.patch.dict(pipeline.os.environ, self.ENV), \
                 mock.patch.object(pipeline, "fetch_facebook_author", side_effect=facebook), \
                 mock.patch.object(pipeline, "fetch_instagram_author", side_effect=RuntimeError("blocked")):
                pipeline.refresh_rss(root)
        self.assertEqual(len(on_disk), 1)
        self.assertGreater(on_disk[0], hours_ago(1), "stamped at dispatch time, not left at the old value")

    def test_an_explicit_run_restarts_the_clock_for_the_schedule(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root)
            self.assertEqual(run_refresh(root, self.ENV, True), {"apify": True, "brightdata": True})
            out: dict = {}
            self.assertEqual(run_refresh(root, self.ENV, False, out), {}, "both paid subscriptions wait after the manual run")
            self.assertEqual(out["totals"]["deferred"], 2)

    def test_without_an_interval_nothing_extra_is_saved_before_the_call(self) -> None:
        out: dict = {}
        refresh({"RSS_PAID_DISCOVERY": "apify"}, False, out=out)
        self.assertNotIn("paid_discovery_at", out["subscriptions"]["fb-apify"])

    def test_a_stamp_that_cannot_be_saved_blocks_the_paid_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root)
            calls: list[str] = []
            with mock.patch.dict(pipeline.os.environ, self.ENV), \
                 mock.patch.object(pipeline, "save_rss_state", side_effect=OSError("disk full")), \
                 mock.patch.object(pipeline, "fetch_facebook_author", side_effect=lambda *a, **k: calls.append("apify")), \
                 mock.patch.object(pipeline, "fetch_instagram_author", side_effect=RuntimeError("blocked")):
                with self.assertRaises(OSError):
                    pipeline.refresh_rss(root)
            self.assertEqual(calls, [], "no billable call without a saved stamp")
            self.assertNotIn("paid_discovery_at", saved(root)["fb-apify"])

    def test_interval_boundaries_and_future_stamps(self) -> None:
        self.assertNotIn("apify", refresh(self.ENV, False, last={"fb-apify": hours_ago(19.9)}))
        self.assertEqual(refresh(self.ENV, False, last={"fb-apify": hours_ago(20.1)}).get("apify"), True)
        self.assertEqual(refresh(self.ENV, False, last={"fb-apify": hours_ago(-48)}).get("apify"), True,
                         "a stamp from the future must not block discovery forever")

    def test_after_the_interval_the_next_run_discovers_and_restarts_the_clock(self) -> None:
        out: dict = {}
        seen = refresh(self.ENV, False, last={"fb-apify": hours_ago(21)}, out=out)
        self.assertEqual(seen["apify"], True)
        self.assertEqual(out["totals"]["deferred"], 0)
        self.assertGreater(out["subscriptions"]["fb-apify"]["paid_discovery_at"], hours_ago(1),
                           "a failed paid attempt may still be billed, so it starts the clock too")

    def test_an_explicit_allow_batch_cloud_run_is_not_throttled(self) -> None:
        seen = refresh(self.ENV, True, last={"fb-apify": hours_ago(1), "ig-bd": hours_ago(1)})
        self.assertEqual(seen, {"apify": True, "brightdata": True})

    def test_a_blocked_provider_records_no_paid_attempt(self) -> None:
        out: dict = {}
        refresh(self.ENV, False, out=out)
        self.assertIn("paid_discovery_at", out["subscriptions"]["fb-apify"])
        self.assertNotIn("paid_discovery_at", out["subscriptions"]["ig-bd"])

    def test_a_bad_interval_is_a_configuration_error(self) -> None:
        for value in ("daily", "-1", "nan"):
            for explicit in (False, True):  # bypassing the interval does not bypass validating it
                with self.assertRaises(ValueError):
                    refresh({"RSS_PAID_DISCOVERY": "apify", "RSS_PAID_DISCOVERY_MIN_HOURS": value}, explicit)

    def test_a_failed_stamp_save_is_rolled_back_in_memory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            seed(root)
            real_save, saves = pipeline.save_rss_state, []

            def flaky_save(target_root, state):
                saves.append(1)
                if len(saves) == 1:
                    raise OSError("disk hiccup")  # the pre-dispatch stamp; the final save then succeeds
                return real_save(target_root, state)

            calls: list[str] = []
            with mock.patch.dict(pipeline.os.environ, self.ENV), \
                 mock.patch.object(pipeline, "save_rss_state", side_effect=flaky_save), \
                 mock.patch.object(pipeline, "fetch_facebook_author", side_effect=lambda *a, **k: calls.append("apify")), \
                 mock.patch.object(pipeline, "fetch_instagram_author", side_effect=RuntimeError("blocked")):
                pipeline.refresh_rss(root)
            self.assertEqual(calls, [])
            self.assertNotIn("paid_discovery_at", saved(root)["fb-apify"], "no call was made, so no cooldown")


class PaidDiscoveryAddStampTests(unittest.TestCase):
    """新增訂閱的付費盤點也必須寫入 paid_discovery_at，間隔內的排程刷新才不會再付費一次（issue #128）。"""
    ENV = {"RSS_PAID_DISCOVERY": "apify", "RSS_PAID_DISCOVERY_MIN_HOURS": "20"}
    FB_ID = pipeline.job_key("facebook-author", "somepage")
    IG_ID = pipeline.job_key("instagram-author", "someone")

    @staticmethod
    def facebook_feed(author: str, provider: str) -> dict:
        return {"title": f"Facebook {author}", "items": [], "raw_items": [],
                "facebook": {"provider": provider, "page_id": author,
                             "author_url": f"https://www.facebook.com/{author}",
                             "since": "365d", "snapshot_id": "", "fetched_at": ""},
                "not_modified": False}

    @staticmethod
    def instagram_feed(username: str) -> dict:
        return {"title": f"Instagram @{username}", "items": [], "raw_items": [],
                "instagram": {"provider": "brightdata", "username": username,
                              "author_url": f"https://www.instagram.com/{username}/",
                              "since": "365d", "snapshot_id": "sd_add", "fetched_at": ""},
                "not_modified": False}

    def test_a_paid_add_stamps_before_the_call_and_the_schedule_defers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paid_calls: list[str] = []
            stamp_on_disk: list[bool] = []

            def facebook(author, since, provider, *, allow_paid=False, **kwargs):
                paid_calls.append("facebook")
                stamp_on_disk.append(bool(
                    saved(root).get(self.FB_ID, {}).get("paid_discovery_at")))
                return self.facebook_feed(author, provider)

            def instagram(username, since, *, allow_paid=False, **kwargs):
                paid_calls.append("instagram")
                return self.instagram_feed(username)

            with mock.patch.dict(pipeline.os.environ, self.ENV), \
                 mock.patch.object(pipeline, "fetch_facebook_author", side_effect=facebook), \
                 mock.patch.object(pipeline, "fetch_instagram_author", side_effect=instagram):
                pipeline.add_facebook_subscription(root, "somepage", since="365d",
                                                   provider="apify", allow_paid=True)
                pipeline.add_instagram_subscription(root, "someone", since="365d",
                                                    max_items=5, allow_paid=True)
                self.assertEqual(stamp_on_disk, [True],
                                 "the stamp must be on disk before the billable call")
                totals = pipeline.refresh_rss(root, allow_paid=False)
            self.assertEqual(paid_calls, ["facebook", "instagram"],
                             "the scheduled refresh must not pay again within the interval")
            self.assertEqual(totals["deferred"], 2)
            self.assertEqual(totals["failed"], 0)
            subscriptions = saved(root)
            self.assertIn("paid_discovery_at", subscriptions[self.FB_ID])
            self.assertIn("paid_discovery_at", subscriptions[self.IG_ID])

    def test_without_an_interval_a_paid_add_writes_no_stamp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(pipeline.os.environ, {"RSS_PAID_DISCOVERY": "apify"}), \
                 mock.patch.object(pipeline, "fetch_facebook_author",
                                   side_effect=lambda *a, **k: self.facebook_feed("somepage", "apify")), \
                 mock.patch.object(pipeline, "fetch_instagram_author",
                                   side_effect=lambda *a, **k: self.instagram_feed("someone")):
                pipeline.add_facebook_subscription(root, "somepage", since="365d",
                                                   provider="apify", allow_paid=True)
                pipeline.add_instagram_subscription(root, "someone", since="365d",
                                                    max_items=5, allow_paid=True)
            for subscription in pipeline.load_rss_state(root)["subscriptions"]:
                self.assertNotIn("paid_discovery_at", subscription)

    def test_an_add_that_never_pays_is_never_stamped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(pipeline.os.environ, self.ENV):
                with self.assertRaisesRegex(RuntimeError, "--allow-batch-cloud"):
                    pipeline.add_facebook_subscription(root, "somepage", since="365d",
                                                       provider="apify", allow_paid=False)
                with self.assertRaisesRegex(RuntimeError, "--allow-batch-cloud"):
                    pipeline.add_instagram_subscription(root, "someone", since="365d",
                                                        max_items=5, allow_paid=False)
            self.assertEqual(pipeline.load_rss_state(root)["subscriptions"], [],
                             "a refused add must not leave a subscription behind")

    def test_invalid_add_options_leave_state_unchanged(self) -> None:
        cases = [("facebook", {"provider": "apify", "since": "invalid"}),
                 ("facebook", {"provider": "apify", "brightdata_mode": "posts"}),
                 ("facebook", {"provider": "brightdata", "brightdata_mode": "invalid", "max_items": 5})]
        for platform in ("facebook", "instagram"):
            options = {"provider": "brightdata"} if platform == "facebook" else {}
            cases.extend((platform, dict(options, max_items=budget))
                         for budget in (None, 0, -1, True, "5", 1.5))
            cases.append((platform, dict(options, max_items=5, since="invalid")))
        for platform, options in cases:
            with self.subTest(platform=platform, options=options), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                pipeline.secure_rss_directory(root)
                before = pipeline.load_rss_state(root)
                with mock.patch.dict(pipeline.os.environ, {
                    **self.ENV, "APIFY_API_TOKEN": "offline-token", "APIFY_MAX_TOTAL_CHARGE_USD": "1",
                    "BRIGHTDATA_API_TOKEN": "offline-token",
                }, clear=True), mock.patch.object(pipeline, "request_json", side_effect=AssertionError("no HTTP")) as submit, \
                     mock.patch.object(pipeline, "get_json", side_effect=AssertionError("no HTTP")) as read:
                    add = getattr(pipeline, f"add_{platform}_subscription")
                    with self.assertRaises(ValueError):
                        add(root, "example", allow_paid=True, **options)
                submit.assert_not_called()
                read.assert_not_called()
                self.assertEqual(pipeline.load_rss_state(root), before,
                                 "invalid options must not persist an unusable subscription or cooldown")

    def test_failed_disabled_opml_add_stays_disabled_before_and_after_the_call(self) -> None:
        for platform, provider in (("facebook", "apify"), ("facebook", "brightdata"), ("instagram", "brightdata")):
            with self.subTest(platform=platform, provider=provider), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / "disabled.opml"
                source.write_text(
                    f'<opml version="2.0"><body><outline xmlUrl="https://www.{platform}.com/example/" '
                    f'vidbeeKind="{platform}-author" vidbeeEnabled="false" '
                    f'vidbeeFacebookProvider="{provider}" vidbeeFacebookMaxItems="5" '
                    'vidbeeInstagramMaxItems="5" /></body></opml>', encoding="utf-8")
                enabled_at_call = []

                def failed_fetch(*args, **kwargs):
                    enabled_at_call.append(pipeline.load_rss_state(root)["subscriptions"][0]["enabled"])
                    raise RuntimeError("provider outcome unknown")

                with mock.patch.dict(pipeline.os.environ, self.ENV), \
                     mock.patch.object(pipeline, f"fetch_{platform}_author", side_effect=failed_fetch) as fetch:
                    self.assertEqual(pipeline.import_rss_opml(root, source, allow_paid=True),
                                     {"added": 0, "existing": 0, "failed": 1})
                    state = pipeline.load_rss_state(root)
                    self.assertEqual(enabled_at_call, [False], "persist the OPML permission before dispatch")
                    self.assertFalse(state["subscriptions"][0]["enabled"])
                    state["subscriptions"][0]["paid_discovery_at"] = hours_ago(21)
                    pipeline.save_rss_state(root, state)
                    fetch.reset_mock()
                    self.assertEqual(pipeline.refresh_rss(root)["feeds"], 0)
                    fetch.assert_not_called()

    def test_failed_paid_add_retains_an_exportable_subscription(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with mock.patch.dict(pipeline.os.environ, self.ENV), \
                 mock.patch.object(pipeline, "fetch_instagram_author", side_effect=RuntimeError("provider timeout")):
                with self.assertRaisesRegex(RuntimeError, "provider timeout"):
                    pipeline.add_instagram_subscription(root, "someone", max_items=5, allow_paid=True)
            subscription = pipeline.load_rss_state(root)["subscriptions"][0]
            self.assertTrue(subscription["enabled"])
            self.assertTrue(subscription["paid_discovery_at"])
            output = root / "subscriptions.opml"
            self.assertEqual(pipeline.export_rss_opml(root, output), 1)
            outline = pipeline.ET.fromstring(output.read_text(encoding="utf-8")).find(".//outline")
            self.assertEqual(outline.attrib["xmlUrl"], pipeline.instagram_author_url("someone"),
                             "OPML imports silently skip retained subscriptions with an empty xmlUrl")


if __name__ == "__main__":
    unittest.main()
