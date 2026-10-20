"""RSS_PAID_DISCOVERY lets a scheduled refresh use only the named paid discovery providers."""
from __future__ import annotations

import tempfile
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


def refresh(env: dict[str, str], allow_paid: bool) -> dict[str, bool]:
    """Run refresh_rss and record the allow_paid each paid provider received (fetches stop right there)."""
    seen: dict[str, bool] = {}

    def facebook(author, since, provider, *, allow_paid=False):
        seen[provider] = allow_paid
        raise RuntimeError("stop after the gate")

    def instagram(username, since, *, allow_paid=False, **kwargs):
        seen["brightdata"] = allow_paid
        raise RuntimeError("stop after the gate")

    with tempfile.TemporaryDirectory() as directory, \
         mock.patch.dict(pipeline.os.environ, env), \
         mock.patch.object(pipeline, "fetch_facebook_author", side_effect=facebook), \
         mock.patch.object(pipeline, "fetch_instagram_author", side_effect=instagram):
        root = Path(directory)
        state = pipeline.load_rss_state(root)
        state["subscriptions"].extend(dict(item) for item in SUBSCRIPTIONS)
        pipeline.save_rss_state(root, state)
        pipeline.refresh_rss(root, allow_paid=allow_paid)
    return seen


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


if __name__ == "__main__":
    unittest.main()
