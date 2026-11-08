"""Offline boundary regressions for issues 126–131; no remote API is called."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

import cost_tracker
import drive_storage
import pipeline

with mock.patch.object(pipeline, "load_dotenv"):
    import mcp_server as server


class RssBoundaryRegressions(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(mock.patch.dict(os.environ, {
            "RSS_PAID_DISCOVERY": "apify,brightdata", "RSS_PAID_DISCOVERY_MIN_HOURS": "20",
            "APIFY_API_TOKEN": "offline-token", "APIFY_MAX_TOTAL_CHARGE_USD": "1",
            "APIFY_FACEBOOK_RESULTS_LIMIT": "10", "BRIGHTDATA_API_TOKEN": "offline-token",
            "BRIGHTDATA_CREDIT_LEDGER": str(self.root / "credits.sqlite3"),
            "BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "50", "BRIGHTDATA_POST_DAILY_LIMIT": "10",
            "BUDGET_HARD_STOP": "false", "RSS_FACEBOOK_FALLBACK_PROVIDER": "",
        }))
        self.enterContext(cost_tracker.cost_root_scope(self.root))

    def test_first_drive_upload_reads_complete_folder_metadata(self):
        remote, uploads = {}, []
        ids = iter(["folder_1", "file_1"])
        job = self.root / "jobs" / "job0123456789"
        job.mkdir(parents=True)
        (job / "knowledge.md").write_text("# knowledge\n", encoding="utf-8")

        def api(method, url, token, data=None, **kwargs):
            parsed = urlparse(url)
            if parsed.path.endswith("/generateIds"):
                return {"ids": [next(ids)]}
            fields = parse_qs(parsed.query).get("fields", [""])[0].split(",")
            if method == "POST":
                remote[data["id"]] = {**data, "trashed": False}
                obj = remote[data["id"]]
            else:
                obj = remote.get(parsed.path.rsplit("/", 1)[-1])
                if obj is None:
                    raise drive_storage.DriveAPIError(404)
            return {k: v for k, v in obj.items() if k in fields}

        def upload(token, path, name, parent_id, mime_type, *, file_id):
            uploads.append(file_id)
            remote[file_id] = {"id": file_id, "name": name, "mimeType": mime_type,
                "size": str(path.stat().st_size), "sha256Checksum": hashlib.sha256(path.read_bytes()).hexdigest(),
                "parents": [parent_id], "trashed": False,
                "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"}
            return remote[file_id]

        with mock.patch.dict(os.environ, {"GOOGLE_DRIVE_ACCESS_TOKEN": "offline-token",
                "GOOGLE_DRIVE_FOLDER_ID": "parent_1", "RSS_DRIVE_UPLOAD": "true"}), \
                mock.patch.object(drive_storage, "_api_json", side_effect=api), \
                mock.patch.object(drive_storage, "_upload_file", side_effect=upload):
            result = pipeline.upload_rss_result(self.root, job, {"source": {"title": "first"}})
        self.assertEqual(result["status"], "uploaded", result)
        self.assertEqual(uploads, ["file_1"])

    def test_added_paid_subscription_does_not_pay_again_on_immediate_refresh(self):
        for platform in ("facebook", "instagram"):
            with self.subTest(platform=platform):
                root = self.root / platform
                calls = []

                def post(url, headers, **kwargs):
                    calls.append(url)
                    if "apify" in url:
                        return {"data": {"id": "run1", "status": "SUCCEEDED", "defaultDatasetId": "dataset1"}}
                    return {"snapshot_id": "sd_test"}

                with mock.patch.object(pipeline, "request_json", side_effect=post), \
                        mock.patch.object(pipeline, "get_json", side_effect=lambda url, headers:
                            {"status": "ready"} if "/progress/" in url else []):
                    if platform == "facebook":
                        pipeline.add_facebook_subscription(root, "example", provider="apify", allow_paid=True)
                    else:
                        pipeline.add_instagram_subscription(root, "example", max_items=5, allow_paid=True)
                    totals = pipeline.refresh_rss(root)
                self.assertEqual(len(calls), 1)
                self.assertEqual(totals["deferred"], 1)
                self.assertTrue(pipeline.load_rss_state(root)["subscriptions"][0]["paid_discovery_at"])

    def test_excluded_provider_is_skipped_without_changing_error_or_failing_cli(self):
        state = pipeline.load_rss_state(self.root)
        state["subscriptions"] = [
            {"id": "fb", "kind": "facebook-author", "url": "https://facebook.com/example",
             "facebook_page_id": "example", "facebook_provider": "apify", "enabled": True},
            {"id": "ig", "kind": "instagram-author", "url": "https://instagram.com/example",
             "instagram_username": "example", "instagram_max_items": 5, "enabled": True, "last_error": "keep"},
        ]
        pipeline.save_rss_state(self.root, state)
        feed = {"title": "fb", "items": [], "facebook": {"provider": "apify", "page_id": "example"}}
        argv = ["pipeline.py", "--rss-refresh", "--root", str(self.root)]
        with mock.patch.dict(os.environ, {"RSS_PAID_DISCOVERY": "apify"}), \
                mock.patch.object(pipeline, "fetch_facebook_author", return_value=feed), \
                mock.patch.object(pipeline, "fetch_instagram_author", side_effect=RuntimeError("blocked")) as blocked, \
                mock.patch.object(pipeline.sys, "argv", argv), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(pipeline.run_rss_command(pipeline.parse_args()), 0)
        blocked.assert_not_called()
        ig = pipeline.load_rss_state(self.root)["subscriptions"][1]
        self.assertEqual(ig["last_error"], "keep")
        self.assertNotIn("last_checked", ig)

    def test_canceled_author_stops_without_sleep_or_a_second_submit(self):
        for platform in ("facebook", "instagram"):
            with self.subTest(platform=platform), \
                    mock.patch.object(pipeline, "request_json", return_value={"snapshot_id": "sd_test"}) as submit, \
                    mock.patch.object(pipeline, "get_json", return_value={"status": "CANCELED"}), \
                    mock.patch.object(pipeline.time, "sleep", side_effect=AssertionError("must not sleep")):
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError, "取消"):
                        if platform == "facebook":
                            pipeline.fetch_facebook_author("example", provider="brightdata", brightdata_mode="reels",
                                max_items=5, allow_paid=True, root=self.root / platform)
                        else:
                            pipeline.fetch_instagram_author("example", max_items=5, allow_paid=True,
                                root=self.root / platform)
                self.assertEqual(submit.call_count, 1)

    def test_canceled_social_post_never_returns_pending_or_resubmits(self):
        with mock.patch.object(pipeline, "request_json", return_value={"snapshot_id": "sd_post"}) as submit, \
                mock.patch.object(pipeline, "get_json", return_value={"status": "CANCELED"}):
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "取消"):
                    pipeline.fetch_social_post("https://instagram.com/p/ABC/", self.root, allow_paid=True)
        self.assertEqual(submit.call_count, 1)

    def test_interrupted_initial_paid_call_is_durable_and_cools_down(self):
        for platform in ("facebook", "instagram"):
            with self.subTest(platform=platform):
                root = self.root / platform
                fetcher = "fetch_facebook_author" if platform == "facebook" else "fetch_instagram_author"
                def interrupted(*args, **kwargs):
                    item, = pipeline.load_rss_state(root)["subscriptions"]
                    self.assertTrue(item["paid_discovery_at"], "reserved before the paid dispatch")
                    raise KeyboardInterrupt
                with mock.patch.object(pipeline, fetcher, side_effect=interrupted):
                    with self.assertRaises(KeyboardInterrupt):
                        if platform == "facebook":
                            pipeline.add_facebook_subscription(root, "example", provider="apify", allow_paid=True)
                        else:
                            pipeline.add_instagram_subscription(root, "example", max_items=5, allow_paid=True)
                with mock.patch.object(pipeline, fetcher) as fetch:
                    self.assertEqual(pipeline.refresh_rss(root)["deferred"], 1)
                    fetch.assert_not_called()

    def test_initial_discovery_without_interval_keeps_legacy_state_shape(self):
        feed = {"title": "fb", "items": [], "facebook": {"provider": "apify", "page_id": "example",
                "author_url": "https://facebook.com/example"}}
        with mock.patch.dict(os.environ, {"RSS_PAID_DISCOVERY_MIN_HOURS": ""}), \
                mock.patch.object(pipeline, "fetch_facebook_author", return_value=feed):
            subscription, _, _ = pipeline.add_facebook_subscription(self.root, "example", provider="apify", allow_paid=True)
        self.assertNotIn("paid_discovery_at", subscription)

    def test_allowed_provider_failure_still_fails_refresh_cli(self):
        state = pipeline.load_rss_state(self.root)
        state["subscriptions"] = [{"id": "fb", "kind": "facebook-author", "url": "https://facebook.com/example",
                                  "facebook_provider": "apify", "facebook_page_id": "example"}]
        pipeline.save_rss_state(self.root, state)
        with mock.patch.object(pipeline, "fetch_facebook_author", side_effect=RuntimeError("provider unavailable")), \
                mock.patch.object(pipeline.sys, "argv", ["pipeline.py", "--rss-refresh", "--root", str(self.root)]), \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(pipeline.run_rss_command(pipeline.parse_args()), 1)
        self.assertEqual(pipeline.load_rss_state(self.root)["subscriptions"][0]["last_error"], "provider unavailable")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
class VideoTailRegressions(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def make_video(self, *, long_audio=False):
        video = self.root / "source.mp4"
        args = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                "testsrc2=size=64x64:rate=30:duration=4"]
        if long_audio:
            args += ["-f", "lavfi", "-i", "sine=frequency=440:duration=5.2", "-c:a", "aac"]
        subprocess.run([*args, "-c:v", "libx264", "-g", "15", "-pix_fmt", "yuv420p", str(video)], check=True)
        return video

    def test_longer_audio_split_completes_and_rerun_does_not_encode_again(self):
        video = self.make_video(long_audio=True)
        duration = pipeline.video_duration(pipeline.probe_video(video))
        self.assertGreater(duration, 5)
        for mode in ("copy", "transcode"):
            with self.subTest(mode=mode), mock.patch.dict(os.environ,
                    {"MCP_SEGMENT_MODE": mode, "MCP_SEGMENT_MAX_LONG_SIDE": "320"}):
                target = self.root / mode
                parts = server.split_video(video, target, 2, duration)
                self.assertAlmostEqual(parts[-1][2], 4, delta=.25)
                with mock.patch.object(pipeline, "run_external", wraps=pipeline.run_external) as run:
                    again = server.split_video(video, target, 2, duration)
                self.assertEqual(parts, again)
                self.assertFalse(any(Path(call.args[0][0]).name == "ffmpeg" for call in run.call_args_list))

    def test_stale_audio_only_tail_is_removed_before_resume(self):
        video = self.make_video(long_audio=True)
        duration = pipeline.video_duration(pipeline.probe_video(video))
        with mock.patch.dict(os.environ, {"MCP_SEGMENT_MODE": "transcode"}):
            target = self.root / "segments"
            parts = server.split_video(video, target, 2, duration)
            audio = target / f"segment_{len(parts):03d}.mp4"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i",
                "sine=frequency=440:duration=0.4", "-c:a", "aac", str(audio)], check=True)
            with (target / "segments.csv").open("a", encoding="utf-8") as handle:
                handle.write(f"{audio.name},4,4\n")
            with mock.patch.object(pipeline, "run_external", wraps=pipeline.run_external) as run:
                again = server.split_video(video, target, 2, duration)
            self.assertEqual(parts, again)
            self.assertFalse(audio.exists())
            self.assertFalse(any(Path(call.args[0][0]).name == "ffmpeg" for call in run.call_args_list))

    def test_corrupt_tail_interval_does_not_delete_real_visual_media(self):
        video = self.make_video()
        target = self.root / "segments"
        target.mkdir()
        segment = target / "segment_000.mp4"
        shutil.copyfile(video, segment)
        listing = target / "segments.csv"
        for start, end in ((0, 0), (4, 3)):
            with self.subTest(start=start, end=end):
                listing.write_text(f"{segment.name},{start},{end}\n", encoding="utf-8")
                server._remove_nonvisual_tail(target, listing)
                self.assertTrue(segment.exists())
                self.assertEqual(listing.read_text(encoding="utf-8"), f"{segment.name},{start},{end}\n")
                with self.assertRaisesRegex(RuntimeError, "時間無效"):
                    server._validated_segments(target, 4)

    def test_chunk_tail_remains_inside_original_duration_after_merge_and_read(self):
        video = self.make_video()
        duration = pipeline.video_duration(pipeline.probe_video(video))
        chunks = pipeline.split_video_mp4_into_chunks(video, self.root / "chunks", 2, duration)
        self.assertTrue(all(chunk.end <= duration for chunk in chunks))

        def chat(messages, *args, **kwargs):
            hint = float(re.search(r"影片長度約 ([0-9.]+) 秒", messages[0]["content"][0]["text"]).group(1))
            return json.dumps({"events": [{"timestamp": stamp, "screen_type": "影片片段", "visible_text": [],
                "visual_summary": "tail", "important_details": [], "confidence": .9} for stamp in (0, hint)],
                "overall_summary": "summary"})

        with mock.patch.object(pipeline, "MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET", 1000), \
                mock.patch.object(pipeline, "VIDEO_MP4_MIN_CHUNK_SECONDS", 1), \
                mock.patch.dict(os.environ, {"ANALYZE_VIDEO_FPS": "1", "ANALYZE_VIDEO_DETAIL": "high",
                    "MINIMAX_VIDEO_CHUNKING": "on", "MINIMAX_VIDEO_CHUNK_SECONDS": ""}), \
                mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
                mock.patch.object(pipeline, "minimax_chat", side_effect=chat):
            rows = pipeline.analyze_video_mp4(video, "offline-key", "MiniMax-M3", duration=duration,
                cache_dir=self.root / "cache", work_dir=self.root / "work")
        last = max(rows, key=lambda row: row["timestamp"])
        self.assertLessEqual(last["timestamp"], duration)
        timeline = [{"type": "visual", "timestamp": last["timestamp"], "data": last}]
        self.assertIsNone(pipeline.saved_visual_timestamp_issue(timeline[0], duration))
        refreshed = server.refreshed_analysis({"timeline": timeline, "duration_seconds": duration})
        self.assertIsInstance(refreshed["timeline"][0]["timestamp"], (int, float))
