import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


class FrameSelectionTests(unittest.TestCase):
    def test_larger_budget_reuses_smaller_selection(self) -> None:
        eight = pipeline.limit_items(list(range(100)), 8)
        twenty = pipeline.limit_items(list(range(100)), 20)
        self.assertLessEqual(set(eight), set(twenty))
        self.assertEqual(twenty, sorted(twenty))


class OutputTests(unittest.TestCase):
    def test_vidbee_sidecars_do_not_overwrite_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "video.mp4"
            srt = root / "source.srt"
            notes = root / "source.md"
            video.write_bytes(b"")
            srt.write_text("caption", encoding="utf-8")
            notes.write_text("notes", encoding="utf-8")
            result = pipeline.write_vidbee_sidecar(video, srt, notes)
            self.assertEqual(result["status"], "written")
            self.assertEqual(result["notes"]["status"], "written")
            (root / "video.notes.md").write_text("keep", encoding="utf-8")
            result = pipeline.write_vidbee_sidecar(video, srt, notes)
            self.assertEqual(result["notes"]["status"], "conflict")


class ReleaseTests(unittest.TestCase):
    def test_version_is_semantic(self) -> None:
        self.assertRegex(pipeline.__version__, r"^\d+\.\d+\.\d+$")


class FacebookAuthorTests(unittest.TestCase):
    def test_graph_discovery_paginates_reels_and_deduplicates(self) -> None:
        responses = iter(
            [
                {
                    "data": [
                        {
                            "id": "101",
                            "title": "影片",
                            "permalink_url": "https://www.facebook.com/page/videos/101/",
                            "created_time": "2026-08-01T00:00:00+0000",
                        }
                    ],
                    "paging": {"cursors": {"after": "page-2"}},
                },
                {
                    "data": [
                        {
                            "id": "102",
                            "description": "第二支影片",
                            "created_time": "2026-07-01T00:00:00+0000",
                        }
                    ]
                },
                {
                    "data": [
                        {"id": "101", "created_time": "2026-08-01T00:00:00+0000"},
                        {"id": "103", "description": "Reel", "created_time": "2026-06-01T00:00:00+0000"},
                    ]
                },
            ]
        )
        calls: list[tuple[str, dict[str, str]]] = []

        def fake_get(url: str, headers: dict[str, str]) -> dict:
            calls.append((url, headers))
            return next(responses)

        with mock.patch.object(pipeline, "get_json", side_effect=fake_get), mock.patch.dict(
            pipeline.os.environ,
            {"FACEBOOK_PAGE_ACCESS_TOKEN": "test-token", "FACEBOOK_GRAPH_API_VERSION": "v26.0"},
        ):
            feed = pipeline.fetch_facebook_author("https://www.facebook.com/example/videos", "2025-01-01")

        self.assertEqual([item["id"] for item in feed["items"]], ["101", "102", "103"])
        self.assertIn("/reel/101", feed["items"][0]["url"])
        self.assertEqual(feed["facebook"]["pages"], 3)
        self.assertEqual(pipeline.facebook_page_identifier("https://facebook.com/profile.php?id=123"), "123")
        self.assertEqual(pipeline.parse_facebook_since("7d", now=604800), 0)
        self.assertEqual(
            pipeline.public_url("https://www.facebook.com/watch/?v=103&tracking=secret"),
            "https://www.facebook.com/watch/?v=103",
        )
        self.assertTrue(all("access_token" not in url for url, _ in calls))
        self.assertTrue(all(headers["Authorization"] == "Bearer test-token" for _, headers in calls))
        with mock.patch.dict(pipeline.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "FACEBOOK_PAGE_ACCESS_TOKEN"):
                pipeline.fetch_facebook_author("example")

    def test_apify_discovery_requires_opt_in_and_keeps_only_videos(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--allow-batch-cloud"):
            pipeline.fetch_facebook_author("example", provider="apify")

        calls: list[tuple[str, dict[str, str]]] = []

        def fake_get(url: str, headers: dict[str, str]):
            calls.append((url, headers))
            if "/actor-runs/" in url:
                return {
                    "data": {
                        "id": "run-1",
                        "status": "SUCCEEDED",
                        "defaultDatasetId": "dataset-1",
                    }
                }
            return [
                {
                    "postId": "video-1",
                    "url": "https://www.facebook.com/example/videos/123/?tracking=secret",
                    "time": "2026-08-01T00:00:00.000Z",
                    "text": "公開影片",
                    "isVideo": True,
                },
                {
                    "postId": "photo-1",
                    "url": "https://www.facebook.com/example/posts/456/",
                    "time": "2026-08-02T00:00:00.000Z",
                    "text": "相片",
                    "isVideo": False,
                },
            ]

        environment = {
            "APIFY_API_TOKEN": "test-apify-token",
            "APIFY_FACEBOOK_ACTOR": "apify~facebook-posts-scraper",
            "APIFY_FACEBOOK_RESULTS_LIMIT": "1000000",
            "APIFY_MAX_TOTAL_CHARGE_USD": "1.00",
            "APIFY_RUN_TIMEOUT_SECONDS": "60",
        }
        with mock.patch.object(
            pipeline,
            "post_json",
            return_value={"data": {"id": "run-1", "status": "RUNNING"}},
        ) as post, mock.patch.object(pipeline, "get_json", side_effect=fake_get), mock.patch.dict(
            pipeline.os.environ, environment, clear=True
        ):
            feed = pipeline.fetch_facebook_author(
                "https://www.facebook.com/example/", "365d", "apify", allow_paid=True
            )

        self.assertEqual([item["id"] for item in feed["items"]], ["video-1"])
        self.assertEqual(feed["items"][0]["url"], "https://www.facebook.com/example/videos/123/")
        self.assertEqual(feed["facebook"]["provider"], "apify")
        run_url, run_headers, run_body = post.call_args.args[:3]
        self.assertNotIn("test-apify-token", run_url)
        self.assertEqual(run_headers["Authorization"], "Bearer test-apify-token")
        self.assertIn("maxTotalChargeUsd=1.0", run_url)
        actor_input = pipeline.json.loads(run_body)
        self.assertFalse(actor_input["captionText"])
        self.assertEqual(actor_input["resultsLimit"], 1000000)
        self.assertIn("onlyPostsNewerThan", actor_input)
        self.assertTrue(all("test-apify-token" not in url for url, _ in calls))


class InstagramAuthorTests(unittest.TestCase):
    def test_brightdata_discovery_is_opt_in_and_filters_known_dates(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "--allow-batch-cloud"):
            pipeline.fetch_instagram_author("example")

        calls: list[tuple[str, dict[str, str]]] = []

        def fake_get(url: str, headers: dict[str, str]):
            calls.append((url, headers))
            if "/progress/" in url:
                return {"snapshot_id": "s_test", "status": "ready"}
            return [
                {
                    "post_id": "video-1",
                    "shortcode": "CURRENT",
                    "url": "https://www.instagram.com/reel/CURRENT/?tracking=secret",
                    "description": "近期 Reel",
                    "date_posted": "2026-08-01T00:00:00.000Z",
                },
                {
                    "post_id": "old-video",
                    "shortcode": "OLD",
                    "url": "https://www.instagram.com/reel/OLD/",
                    "date_posted": "2024-01-01T00:00:00.000Z",
                },
                {
                    "post_id": "missing-date",
                    "shortcode": "NODATE",
                    "url": "https://www.instagram.com/reel/NODATE/",
                    "description": "缺日期仍保留待核對",
                },
            ]

        environment = {
            "BRIGHTDATA_API_TOKEN": "test-brightdata-token",
            "BRIGHTDATA_RUN_TIMEOUT_SECONDS": "60",
        }
        notice = io.StringIO()

        def fake_post(*args, **kwargs):
            self.assertIn("num_of_posts=20", notice.getvalue())
            self.assertIn("計費", notice.getvalue())
            self.assertNotIn("test-brightdata-token", notice.getvalue())
            return {"snapshot_id": "s_test"}

        with mock.patch.object(
            pipeline, "post_json", side_effect=fake_post
        ) as post, mock.patch.object(
            pipeline, "get_json", side_effect=fake_get
        ), mock.patch.dict(pipeline.os.environ, environment, clear=True), mock.patch.object(
            pipeline.sys, "stderr", notice
        ):
            feed = pipeline.fetch_instagram_author(
                "https://www.instagram.com/example/",
                "2025-01-01",
                max_items=20,
                exclude_ids=["already-seen"],
                allow_paid=True,
            )

        self.assertEqual([item["id"] for item in feed["items"]], ["video-1", "missing-date"])
        self.assertEqual(feed["items"][0]["url"], "https://www.instagram.com/reel/CURRENT/")
        self.assertFalse(feed["instagram"]["date_filter_complete"])
        self.assertEqual(feed["instagram"]["missing_date_count"], 1)
        trigger_url, headers, body = post.call_args.args[:3]
        self.assertNotIn("test-brightdata-token", trigger_url)
        self.assertIn("dataset_id=gd_lyclm20il4r5helnj", trigger_url)
        self.assertIn("type=discover_new", trigger_url)
        self.assertEqual(headers["Authorization"], "Bearer test-brightdata-token")
        request_input = pipeline.json.loads(body)["input"][0]
        self.assertEqual(request_input["url"], "https://www.instagram.com/example/")
        self.assertEqual(request_input["num_of_posts"], 20)
        self.assertEqual(request_input["posts_to_not_include"], ["already-seen"])
        self.assertTrue(all("test-brightdata-token" not in url for url, _ in calls))
        self.assertTrue(all(headers["Authorization"] == "Bearer test-brightdata-token" for _, headers in calls))
        with self.assertRaisesRegex(ValueError, "不可使用單篇"):
            pipeline.instagram_username("https://www.instagram.com/reel/CURRENT/")

    def test_brightdata_discovery_fails_closed_without_finite_budget(self) -> None:
        """--allow-batch-cloud 只是 opt-in，不是預算；無上限請求在送出前拒絕（#8）"""
        calls: list[str] = []
        environment = {
            "BRIGHTDATA_API_TOKEN": "test-brightdata-token",
            "BRIGHTDATA_RUN_TIMEOUT_SECONDS": "60",
        }
        with mock.patch.object(
            pipeline, "post_json", side_effect=lambda url, *a, **kw: calls.append(url)
        ), mock.patch.object(
            pipeline, "get_json", side_effect=lambda url, *a, **kw: calls.append(url)
        ), mock.patch.dict(pipeline.os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, "instagram-max-items"):
                pipeline.fetch_instagram_author(
                    "https://www.instagram.com/example/",
                    "365d",
                    max_items=None,
                    allow_paid=True,
                )
            for bad in (0, -5):
                with self.assertRaisesRegex(ValueError, "instagram-max-items"):
                    pipeline.fetch_instagram_author(
                        "https://www.instagram.com/example/",
                        "365d",
                        max_items=bad,
                        allow_paid=True,
                    )
        self.assertEqual(calls, [], "預算閘失敗前不得有任何 Bright Data 請求")

    def test_brightdata_discovery_rejects_non_integer_budgets(self) -> None:
        environment = {"BRIGHTDATA_API_TOKEN": "test-brightdata-token"}
        with mock.patch.dict(pipeline.os.environ, environment, clear=True), mock.patch.object(
            pipeline, "post_json", return_value={"snapshot_id": "s_test"}
        ) as post, mock.patch.object(
            pipeline, "get_json", side_effect=lambda url, headers: {"status": "ready"} if "/progress/" in url else []
        ) as get:
            for bad in (True, False, 1.5, 2.0, float("nan"), float("inf"), float("-inf"), "3", [], {}):
                with self.assertRaisesRegex(ValueError, "instagram-max-items", msg=repr(bad)):
                    pipeline.fetch_instagram_author("example", max_items=bad, allow_paid=True)
            post.assert_not_called()
            get.assert_not_called()

    def test_brightdata_trigger_is_not_automatically_retried(self) -> None:
        with mock.patch.dict(
            pipeline.os.environ, {"BRIGHTDATA_API_TOKEN": "test-brightdata-token"}, clear=True
        ), mock.patch.object(
            pipeline.urllib.request, "urlopen", side_effect=TimeoutError("lost acknowledgement")
        ) as transport, mock.patch.object(pipeline.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "API 網路錯誤"):
                pipeline.fetch_instagram_author("example", max_items=3, allow_paid=True)
            self.assertEqual(transport.call_count, 1, "A billable trigger must never be automatically resubmitted")

    def test_instagram_budget_help_describes_required_bound(self) -> None:
        output = io.StringIO()
        with mock.patch.object(pipeline.sys, "argv", ["pipeline.py", "--help"]), mock.patch.object(
            pipeline.sys, "stdout", output
        ):
            with self.assertRaises(SystemExit) as exited:
                pipeline.parse_args()
        self.assertEqual(exited.exception.code, 0)
        budget_help = output.getvalue().rsplit("--instagram-max-items", 1)[1].split("--instagram-discover-only", 1)[0]
        self.assertIn("必填", budget_help)
        self.assertIn("正整數", budget_help)
        self.assertNotIn("預設不設上限", budget_help)


if __name__ == "__main__":
    unittest.main()
