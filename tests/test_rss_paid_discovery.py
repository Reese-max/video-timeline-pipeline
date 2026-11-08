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
        self.assertEqual(refresh({"RSS_PAID_DISCOVERY": ""}, False), {})

    def test_named_provider_is_allowed_and_the_other_stays_blocked(self) -> None:
        self.assertEqual(refresh({"RSS_PAID_DISCOVERY": " Apify "}, False), {"apify": True})

    def test_allow_batch_cloud_still_allows_every_provider(self) -> None:
        self.assertEqual(refresh({"RSS_PAID_DISCOVERY": ""}, True), {"apify": True, "brightdata": True})

    def test_unknown_names_allow_nothing(self) -> None:
        self.assertEqual(refresh({"RSS_PAID_DISCOVERY": "all,*,yes"}, False), {})

    def test_each_name_only_allows_itself(self) -> None:
        self.assertEqual(refresh({"RSS_PAID_DISCOVERY": "brightdata"}, False), {"brightdata": True})
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
        self.assertEqual(out["totals"]["failed"], 0, "a provider excluded by policy is not a failed feed")
        self.assertEqual(out["totals"]["skipped"], 1)
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
            self.assertEqual(out["totals"]["deferred"], 1)
            self.assertEqual(out["totals"]["skipped"], 1)

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


if __name__ == "__main__":
    unittest.main()
