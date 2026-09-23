import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline
import mcp_server as server


class SocialPostTests(unittest.TestCase):
    def test_url_scope_and_canonicalization(self):
        self.assertEqual(pipeline.social_post_target("https://instagram.com/author/reels/ABC/?tracking=x")[:3],
                         ("instagram", "ABC", "https://www.instagram.com/reel/ABC/"))
        self.assertEqual(pipeline.social_post_target("https://threads.net/@author/post/ABC?x=1")[2],
                         "https://www.threads.com/@author/post/ABC")
        for url in ["http://instagram.com/p/ABC", "https://instagram.com/author", "https://threads.com/@author",
                    "https://instagram.com.evil/p/ABC", "https://user:pass@instagram.com/p/ABC",
                    "https://instagram.com:123/p/ABC", "https://127.0.0.1/p/ABC",
                    "https://threads.com/@author/post/ABC/other", "https://threads.com/@author/post/%2f"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                pipeline.social_post_target(url)

    def test_facebook_post_urls_and_rejected_scopes(self):
        aliases = ["https://facebook.com/Page/posts/123?mibextid=tracking",
                   "https://m.facebook.com/story.php?story_fbid=123&id=456",
                   "https://web.facebook.com/permalink.php?id=456&story_fbid=123"]
        for url in aliases:
            with self.subTest(url=url):
                target = pipeline.social_post_target(url)
                self.assertEqual(target[:2], ("facebook", "123"))
                self.assertEqual(target[3], "gd_lyclm1571iy3mv57zw")
                self.assertNotIn("mibextid", target[2])
        self.assertEqual(pipeline.social_post_target("https://facebook.com/Page/posts/pfbidABC")[1], "pfbidABC")
        for kind in ["p", "v"]:
            self.assertEqual(pipeline.social_post_target(f"https://facebook.com/share/{kind}/ABC/?x=1")[2],
                             f"https://www.facebook.com/share/{kind}/ABC/")
        for url in ["https://facebook.com/Page", "https://facebook.com/groups/123/posts/456",
                    "https://facebook.com.evil/Page/posts/123", "https://user:pass@facebook.com/Page/posts/123",
                    "https://facebook.com/l.php?u=https://example.com", "https://facebook.com/Page/posts/../123",
                    "https://facebook.com/story.php?story_fbid=123", "https://facebook.com/story.php?story_fbid=123&id=x",
                    "https://facebook.com/story.php?story_fbid=123&story_fbid=999&id=456",
                    "https://facebook.com/permalink.php?story_fbid=123&id=456&id=789",
                    "https://facebook.com/watch/?v=123", "https://facebook.com/share/r/ABC",
                    "https://facebook.com/Page/posts/123?comment_id=456"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                pipeline.social_post_target(url)

    def test_facebook_text_attachments_and_identity(self):
        url = "https://www.facebook.com/share/v/ABC/"
        photo, video = "https://a.fbcdn.net/photo.jpg?sig=keep", "https://b.fbcdn.net/video.mp4?sig=keep"
        record = {"url": url, "content": "貼文原文", "user_username_raw": "發布者",
                  "date_posted": "2026-09-24T01:00:00Z", "post_id": "123", "likes": 10,
                  "num_comments": 0, "num_shares": 2,
                  "attachments": [{"type": "photo", "attachment_url": photo},
                                  {"type": "video", "attachment_url": photo, "video_url": video},
                                  {"type": "video", "video_url": "https://127.0.0.1/private"},
                                  {"type": "photo", "attachment_url": "https://a.fbcdn.net:bad/photo"},
                                  {"type": "photo", "attachment_url": "https://a.fbcdn.net.evil/photo"}]}
        result = pipeline.normalize_social_post(record, url)
        self.assertEqual(result["text"], "貼文原文")
        self.assertEqual(result["author"], "發布者")
        self.assertEqual(result["published_at"], record["date_posted"])
        self.assertEqual(result["image_urls"], [photo])
        self.assertEqual(result["video_urls"], [video])
        self.assertFalse(result["media_analyzed"])
        self.assertEqual(result["metrics"], {"likes": 10, "comments": 0, "shares": 2})
        self.assertEqual(result["post_id"], "123")
        result = pipeline.normalize_social_post({"url": "https://facebook.com/Page/posts/123", "content": "純文字"},
                    "https://m.facebook.com/story.php?story_fbid=123&id=456")
        self.assertEqual(result["text"], "純文字")
        for bad in [{**record, "url": "https://facebook.com/share/v/OTHER/"},
                    {**record, "url": "https://facebook.com/Page/posts/123"},
                    {**record, "is_private": True}, {"url": url, "likes": 100}]:
            with self.subTest(bad=bad), self.assertRaises((ValueError, RuntimeError)):
                pipeline.normalize_social_post(bad, url)
        with self.assertRaisesRegex(RuntimeError, "post_id"):
            pipeline.normalize_social_post({"url": "https://facebook.com/Page/posts/123", "content": "wrong", "post_id": "999"},
                                           "https://facebook.com/Page/posts/123")

    def test_public_text_and_carousel_are_not_claimed_as_media_analysis(self):
        url = "https://instagram.com/p/ABC/"
        image = "https://a.cdninstagram.com/photo.jpg?signature=needed"
        video = "https://a.fbcdn.net/video.mp4?signature=needed"
        result = pipeline.normalize_social_post({"url": url, "description": "實際文字",
            "photos": [image, "https://127.0.0.1/private"], "videos": [video],
            "post_content": [{"type": "Photo", "url": image}, {"type": "Video", "url": video}]}, url)
        self.assertEqual(result["text"], "實際文字")
        self.assertEqual(result["image_urls"], [image])
        self.assertEqual(result["video_urls"], [video])
        self.assertFalse(result["media_analyzed"])
        url = "https://threads.net/@author/post/ABC"
        result = pipeline.normalize_social_post({"url": url, "post_content": "文字貼文", "profile_name": "author"}, url)
        self.assertEqual(result["text"], "文字貼文")
        for bad in [{"url": url}, {"url": url, "error": "login required"},
                    {"url": url, "is_private": True}, {"url": "https://threads.com/t/OTHER", "post_content": "wrong"}]:
            with self.subTest(bad=bad), self.assertRaises((ValueError, RuntimeError)):
                pipeline.normalize_social_post(bad, url)

    def test_pending_resume_cache_and_fixed_dataset(self):
        for url, dataset in [("https://instagram.com/p/ABC", "gd_lk5ns7kz21pck8jpis"),
                             ("https://instagram.com/reel/ABC", "gd_lyclm20il4r5helnj"),
                             ("https://threads.net/@author/post/ABC", "gd_md75myxy14rihbjksa"),
                             ("https://facebook.com/Page/posts/123", "gd_lyclm1571iy3mv57zw"),
                             ("https://facebook.com/share/v/ABC/", "gd_lyclm1571iy3mv57zw")]:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as root, \
                    patch.dict(pipeline.os.environ, {"BRIGHTDATA_API_TOKEN": "test-secret"}), \
                    patch.object(pipeline, "request_json", return_value={"snapshot_id": "sd_test"}) as submit, \
                    patch.object(pipeline, "get_json", side_effect=[{"status": "running"}, {"status": "ready"},
                        [{"url": url, "description": "IG text", "post_content": "Threads text", "content": "FB text"}]]) as read:
                result = pipeline.fetch_social_post(url, Path(root), allow_paid=True)
                self.assertEqual(result["status"], "pending")
                result = pipeline.fetch_social_post(url, Path(root), allow_paid=True)
                self.assertEqual(result["status"], "ready")
                self.assertEqual(pipeline.fetch_social_post(url, Path(root), allow_paid=True), result)
                if result["platform"] == "facebook" and result["post_id"] == "123":
                    self.assertEqual(pipeline.fetch_social_post(
                        "https://m.facebook.com/story.php?story_fbid=123&id=456", Path(root), allow_paid=True), result)
                submit.assert_called_once()
                self.assertEqual(read.call_count, 3)
                self.assertIn("dataset_id=" + dataset, submit.call_args.args[0])
                self.assertNotIn("test-secret", submit.call_args.args[0])
                self.assertEqual(submit.call_args.kwargs["attempts"], 1)
                self.assertEqual(len(pipeline.json.loads(submit.call_args.kwargs["body"])), 1)
                self.assertNotIn("test-secret", str(result))

    def test_instagram_discovery_snapshot_formats(self):
        for snapshot in ["s_test", "sd_test", "sd_../escape"]:
            with self.subTest(snapshot=snapshot), \
                    patch.dict(pipeline.os.environ, {"BRIGHTDATA_API_TOKEN": "test-secret"}), \
                    patch.object(pipeline, "post_json", return_value={"snapshot_id": snapshot}), \
                    patch.object(pipeline, "get_json", side_effect=[{"status": "ready"}, []]) as read:
                if "/" in snapshot:
                    with self.assertRaisesRegex(RuntimeError, "snapshot_id"):
                        pipeline.fetch_instagram_author("testauthor", allow_paid=True)
                    read.assert_not_called()
                else:
                    result = pipeline.fetch_instagram_author("testauthor", allow_paid=True)
                    self.assertEqual(result["instagram"]["snapshot_id"], snapshot)

    def test_uncertain_submission_never_retries_or_leaks_token(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(pipeline.os.environ, {"BRIGHTDATA_API_TOKEN": "test-secret"}), \
                patch.object(pipeline, "request_json", side_effect=RuntimeError("timeout test-secret")) as submit:
            for _ in range(2):
                with self.assertRaises(RuntimeError) as raised:
                    pipeline.fetch_social_post("https://instagram.com/p/ABC", Path(root), allow_paid=True)
                self.assertNotIn("test-secret", str(raised.exception))
            submit.assert_called_once()

    def test_unauthorized_request_never_reaches_transport(self):
        with tempfile.TemporaryDirectory() as root, patch.object(pipeline, "request_json") as submit:
            with self.assertRaisesRegex(RuntimeError, "授權"):
                pipeline.fetch_social_post("https://instagram.com/p/ABC", Path(root))
            self.assertEqual(list(Path(root).iterdir()), [])
            submit.assert_not_called()
        with patch.dict(server.os.environ, {"BRIGHTDATA_ALLOWED_POST_URLS": ""}), \
                patch.object(pipeline, "fetch_social_post") as fetch:
            result = asyncio.run(server.collect_social_post("https://instagram.com/p/ABC", True))
            self.assertEqual(result["status"], "error")
            fetch.assert_not_called()

    def test_failed_snapshot_does_not_retrigger(self):
        with tempfile.TemporaryDirectory() as root, patch.dict(pipeline.os.environ, {"BRIGHTDATA_API_TOKEN": "test-secret"}), \
                patch.object(pipeline, "request_json", return_value={"snapshot_id": "s_test"}) as submit, \
                patch.object(pipeline, "get_json", return_value={"status": "failed"}):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "保留 snapshot"):
                    pipeline.fetch_social_post("https://instagram.com/p/ABC", Path(root), allow_paid=True)
            submit.assert_called_once()

    def test_mcp_gate_schema_and_busy(self):
        with patch.dict(server.os.environ, {"BRIGHTDATA_ALLOWED_POST_URLS": "https://instagram.com/p/ABC"}), \
                patch.object(pipeline, "fetch_social_post", return_value={"status": "ready", "text": "source"}) as fetch:
            result = asyncio.run(server.mcp._tool_manager.call_tool("collect_social_post",
                {"url": "https://instagram.com/p/ABC?tracking=x", "allow_paid": True}, convert_result=True))
            self.assertEqual(result[1]["text"], "source")
            self.assertTrue(fetch.call_args.kwargs["allow_paid"])
            with server.JOB_LOCK:
                result = asyncio.run(server.collect_social_post("https://instagram.com/p/ABC", True))
            self.assertEqual(result["status"], "busy")
            fetch.assert_called_once()
        tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "collect_social_post")
        self.assertIsNotNone(tool.outputSchema)
        self.assertFalse(tool.annotations.readOnlyHint)
        self.assertFalse(tool.inputSchema["properties"]["allow_paid"]["default"])

    def test_facebook_mcp_authorization_and_data_result(self):
        with tempfile.TemporaryDirectory() as root, patch.object(server, "MCP_JOBS_ROOT", Path(root)), \
                patch.dict(server.os.environ, {"BRIGHTDATA_ALLOWED_POST_URLS": "https://facebook.com/Page/posts/123",
                                              "BRIGHTDATA_API_TOKEN": "test-secret"}), \
                patch.object(pipeline, "request_json", return_value={"snapshot_id": "s_test"}) as submit, \
                patch.object(pipeline, "get_json", side_effect=[{"status": "ready"},
                    [{"url": "https://facebook.com/Page/posts/123", "content": "FB source"}]]):
            for args in [{"url": "https://facebook.com/Page/posts/123"},
                         {"url": "https://facebook.com/Page/posts/999", "allow_paid": True}]:
                result = asyncio.run(server.collect_social_post(**args))
                self.assertEqual(result["status"], "error")
            submit.assert_not_called()
            result = asyncio.run(server.mcp._tool_manager.call_tool("collect_social_post",
                {"url": "https://m.facebook.com/story.php?story_fbid=123&id=456", "allow_paid": True}, convert_result=True))
            self.assertEqual(result[1]["text"], "FB source")
            self.assertEqual(result[1]["platform"], "facebook")
            submit.assert_called_once()
        tool = next(t for t in asyncio.run(server.mcp.list_tools()) if t.name == "collect_social_post")
        self.assertIn("Facebook", tool.description)


if __name__ == "__main__":
    unittest.main()
