import io
import json
import tempfile
import urllib.error
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


class ThreadsTests(unittest.TestCase):
    def _page(self, media: dict) -> str:
        payload = {"data": {"media": media}}
        return f'<script type="application/json" data-sjs>{json.dumps(payload)}</script>'

    def test_parse_single_video_chooses_largest_variant(self) -> None:
        html = self._page(
            {
                "code": "POST123",
                "caption": {"text": "公開影片"},
                "user": {"username": "creator"},
                "video_versions": [
                    {"url": "https://cdn.example/low.mp4", "width": 360, "height": 640},
                    {"url": "https://cdn.example/high.mp4", "width": 1080, "height": 1920},
                ],
            }
        )

        result = pipeline.parse_threads_post(html, "https://www.threads.com/@creator/post/POST123")

        self.assertEqual(result["title"], "公開影片")
        self.assertEqual(result["uploader"], "creator")
        self.assertEqual([item["url"] for item in result["entries"]], ["https://cdn.example/high.mp4"])

    def test_parse_carousel_collects_video_items_only(self) -> None:
        html = self._page(
            {
                "code": "CAROUSEL",
                "caption": {"text": "混合 carousel"},
                "user": {"username": "creator"},
                "carousel_media": [
                    {"media_type": 1, "image_versions2": {"candidates": [{"url": "https://cdn.example/a.jpg"}]}},
                    {"media_type": 2, "video_versions": [{"url": "https://cdn.example/a.mp4"}]},
                    {"media_type": 2, "video_versions": [{"url": "https://cdn.example/b.mp4"}]},
                ],
            }
        )

        result = pipeline.parse_threads_post(html, "https://threads.net/t/CAROUSEL")

        self.assertEqual([item["url"] for item in result["entries"]], [
            "https://cdn.example/a.mp4",
            "https://cdn.example/b.mp4",
        ])

    def test_fetch_retries_http_429(self) -> None:
        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, _limit):
                return b"<html></html>"

        url = "https://www.threads.com/@creator/post/POST123"
        error = urllib.error.HTTPError(url, 429, "rate limited", {"Retry-After": "0"}, io.BytesIO())
        with mock.patch.object(
            pipeline.urllib.request, "urlopen", side_effect=[error, Response()]
        ) as urlopen, mock.patch.object(pipeline.time, "sleep") as sleep:
            result = pipeline.fetch_threads_html(url)

        self.assertIn("html", result)
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(0.0)

    def test_download_routes_threads_url(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            job_dir = Path(temporary)
            expected = job_dir / "source.mp4"
            with mock.patch.object(pipeline, "download_threads_video", return_value=expected) as download:
                result = pipeline.download_video("https://threads.com/t/POST123", job_dir)

        self.assertEqual(result, expected)
        download.assert_called_once()

    def test_silent_video_skips_audio_extraction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            video = root / "source.mp4"
            video.write_bytes(b"fixture")

            chunks = pipeline.extract_audio(video, root, 900, has_audio=False)

            self.assertEqual(chunks, [])
            self.assertEqual(json.loads((root / "audio" / "chunks.json").read_text()), [])

    def test_empty_audio_does_not_call_transcription_provider(self) -> None:
        transcript, raw = pipeline.transcribe_all([], "key", "model", "auto", 1, Path("unused-cache"))

        self.assertEqual(transcript["provider"], "none")
        self.assertEqual(raw["reason"], "source has no audio")


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

        def fake_get(url: str, headers: dict[str, str], **_kwargs) -> dict:
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
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            pipeline, "request_json", return_value={"snapshot_id": "s_test"}
        ) as post, mock.patch.object(
            pipeline, "get_json", side_effect=fake_get
        ), mock.patch.dict(pipeline.os.environ, environment):
            feed = pipeline.fetch_instagram_author(
                "https://www.instagram.com/example/",
                "2025-01-01",
                max_items=20,
                exclude_ids=["already-seen"],
                allow_paid=True,
                root=Path(root),
            )

        self.assertEqual([item["id"] for item in feed["items"]], ["video-1", "missing-date"])
        self.assertEqual(feed["items"][0]["url"], "https://www.instagram.com/reel/CURRENT/")
        self.assertFalse(feed["instagram"]["date_filter_complete"])
        self.assertEqual(feed["instagram"]["missing_date_count"], 1)
        trigger_url, headers = post.call_args.args
        body = post.call_args.kwargs["body"]
        self.assertEqual(post.call_args.kwargs["attempts"], 1)
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

    def test_brightdata_discovery_fails_without_finite_budget(self) -> None:
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(pipeline.os.environ, {"BRIGHTDATA_API_TOKEN": "test-token"}, clear=True), \
             mock.patch.object(pipeline, "request_json") as submit:
            with self.assertRaisesRegex(ValueError, "finite 預算"):
                pipeline.fetch_instagram_author("testauthor", allow_paid=True, root=Path(root))
            submit.assert_not_called()

    def test_brightdata_discovery_uses_env_var_when_max_items_not_supplied(self) -> None:
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(pipeline.os.environ, {
                 **{key: pipeline.os.environ[key] for key in ("USERNAME", "PATH", "SYSTEMROOT") if key in pipeline.os.environ},
                 "BRIGHTDATA_API_TOKEN": "test-token",
                 "BRIGHTDATA_INSTAGRAM_MAX_ITEMS": "15",
                 "BRIGHTDATA_RUN_TIMEOUT_SECONDS": "60",
             }, clear=True), \
             mock.patch.object(pipeline, "request_json", return_value={"snapshot_id": "s_test"}), \
             mock.patch.object(pipeline, "get_json", side_effect=[{"status": "ready"}, []]):
            feed = pipeline.fetch_instagram_author("testauthor", allow_paid=True, root=Path(root))
            self.assertEqual(feed["instagram"]["max_items"], 15)

    def test_brightdata_discovery_prints_bound_before_triggering(self) -> None:
        with tempfile.TemporaryDirectory() as root, \
             mock.patch.dict(pipeline.os.environ, {
                 **{key: pipeline.os.environ[key] for key in ("USERNAME", "PATH", "SYSTEMROOT") if key in pipeline.os.environ},
                 "BRIGHTDATA_API_TOKEN": "test-token",
                 "BRIGHTDATA_INSTAGRAM_MAX_ITEMS": "10",
                 "BRIGHTDATA_RUN_TIMEOUT_SECONDS": "60",
             }, clear=True), \
             mock.patch.object(pipeline, "request_json", return_value={"snapshot_id": "s_test"}) as submit, \
             mock.patch.object(pipeline, "get_json", side_effect=[{"status": "ready"}, []]):
            with mock.patch("pipeline.print") as mock_print:
                pipeline.fetch_instagram_author("testauthor", allow_paid=True, root=Path(root))
            mock_print.assert_called_once()
            self.assertIn("10", mock_print.call_args.args[0])
            submit.assert_called_once()

    def test_run_rss_command_requires_instagram_budget(self) -> None:
        with mock.patch.dict(pipeline.os.environ, {"BRIGHTDATA_API_TOKEN": "test-token"}, clear=True), \
             mock.patch.object(pipeline.sys, "argv", [
                 "pipeline.py", "--instagram-author", "testauthor", "--allow-batch-cloud"
             ]):
            args = pipeline.parse_args()
            with self.assertRaisesRegex(ValueError, "finite 預算"):
                pipeline.run_rss_command(args)


if __name__ == "__main__":
    unittest.main()
