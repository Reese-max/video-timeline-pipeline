"""Offline regressions for source identity and the IRISX cascade boundaries."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

HOOKS = Path(__file__).resolve().parents[1] / "irisx-hooks"
sys.path.insert(0, str(HOOKS))


def load(name):
    spec = importlib.util.spec_from_file_location(name, HOOKS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


cascade = load("video_takeaway_cascade")
brightdata = load("brightdata_ig_fallback")
apify = load("apify_ig_fallback")
og = load("ig_og_fallback")


class IrisxIdentityTests(unittest.TestCase):
    URL = "https://www.instagram.com/reel/AbCdE123/"
    OTHER = "https://www.instagram.com/reel/AbCdE1234/"

    def setUp(self):
        for target in ("socket.getaddrinfo", "socket.socket.connect", "socket.socket.connect_ex"):
            blocker = mock.patch(target, side_effect=AssertionError("network is forbidden in this fixture"))
            blocker.start()
            self.addCleanup(blocker.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.jobs = root / "jobs"
        self.jobs.mkdir()
        self.log = root / "logs"
        self.log.mkdir()
        bright = root / "bright.py"
        apify_script = root / "apify.py"
        og_script = root / "og.py"
        for path in (bright, apify_script, og_script):
            path.touch()
        patcher = mock.patch.multiple(cascade, JOBS=self.jobs, LOG=self.log,
                                     BRIGHTDATA=bright, APIFY=apify_script, OG=og_script)
        patcher.start()
        self.addCleanup(patcher.stop)

    def job(self, name, source_url, item_file, *, marker=None):
        job = self.jobs / name
        job.mkdir()
        (job / "notes.md").write_text("Synthetic content for " + source_url, encoding="utf-8")
        (job / "source.info.json").write_text(json.dumps({"webpage_url": source_url}), encoding="utf-8")
        (job / item_file).write_text(json.dumps({"shortCode": source_url.split('/')[-2], "url": source_url, "caption": "synthetic"}), encoding="utf-8")
        if marker is not None:
            (job / "request_url.txt").write_text(marker, encoding="utf-8")
        return job

    @staticmethod
    def response(job):
        return subprocess.CompletedProcess([], 0, stdout=json.dumps({"ok": True, "job": str(job)}), stderr="")

    def test_wrong_subprocess_job_never_becomes_a_match_by_writing_request_url(self):
        for provider, method, item_file in (("bright", cascade.level2_brightdata, "brightdata.item.json"),
                                             ("apify", cascade.level2_apify, "apify.item.json")):
            with self.subTest(provider=provider):
                job = self.job(f"{'brightdata' if provider == 'bright' else provider}-AbCdE123", self.OTHER, item_file)
                with mock.patch.object(cascade.subprocess, "run", return_value=self.response(job)):
                    self.assertIsNone(method(self.URL))
                self.assertFalse((job / "request_url.txt").exists(), "a rejected job must not be modified")

    def test_request_marker_cannot_override_a_mismatched_provider_item(self):
        for provider, method, item_file in (("bright", cascade.level2_brightdata, "brightdata.item.json"),
                                             ("apify", cascade.level2_apify, "apify.item.json")):
            with self.subTest(provider=provider):
                job = self.job(f"{'brightdata' if provider == 'bright' else provider}-AbCdE123", self.OTHER, item_file, marker=self.URL + "\n")
                (job / "source.json").write_text(json.dumps({"url": self.URL}), encoding="utf-8")
                with mock.patch.object(cascade.subprocess, "run", return_value=self.response(job)):
                    self.assertIsNone(method(self.URL))

    def test_matching_provider_item_remains_usable(self):
        for provider, method, item_file in (("bright", cascade.level2_brightdata, "brightdata.item.json"),
                                             ("apify", cascade.level2_apify, "apify.item.json")):
            with self.subTest(provider=provider):
                job = self.job(f"{'brightdata' if provider == 'bright' else provider}-AbCdE123", self.URL, item_file)
                with mock.patch.object(cascade.subprocess, "run", return_value=self.response(job)):
                    self.assertEqual(method(self.URL), job)

    def test_job_matching_rejects_id_prefixes(self):
        job = self.job("prefix", self.OTHER, "brightdata.item.json")
        self.assertFalse(cascade.job_matches(job, self.URL))
        self.assertTrue(cascade.job_matches(job, self.OTHER))

    def test_l1_matches_source_metadata_not_notes_or_request_marker(self):
        job = self.jobs / "normal-pipeline-job"
        job.mkdir()
        (job / "notes.md").write_text("This unrelated video mentions AbCdE123", encoding="utf-8")
        (job / "knowledge.md").write_text(self.URL, encoding="utf-8")
        (job / "request_url.txt").write_text(self.URL, encoding="utf-8")
        (job / "source.info.json").write_text(json.dumps({"extractor": "Instagram", "webpage_url": self.OTHER}), encoding="utf-8")
        self.assertFalse(cascade.job_matches(job, self.URL))
        self.assertIsNone(cascade.find_job(self.URL))
        (job / "source.info.json").unlink()
        self.assertFalse(cascade.job_matches(job, self.URL), "unverified content cannot establish source identity")
        (job / "manifest.json").write_text(json.dumps({"status": "completed", "source": {"url": self.URL}}), encoding="utf-8")
        (job / "source.json").write_text(json.dumps({"source": {"url": self.URL}}), encoding="utf-8")
        (job / "source.info.json").write_text(json.dumps({"extractor": "Instagram", "webpage_url": self.URL, "id": "AbCdE123"}), encoding="utf-8")
        self.assertTrue(cascade.job_matches(job, self.URL))
        self.assertEqual(cascade.find_job(self.URL), job)
        (job / "source.info.json").write_text(json.dumps({"extractor": "Instagram", "webpage_url": self.OTHER}), encoding="utf-8")
        self.assertFalse(cascade.job_matches(job, self.URL), "conflicting authoritative sources must fail closed")

    def test_video_identity_uses_the_actual_host_and_only_identity_query_fields(self):
        self.assertFalse(cascade.video_keys("https://evil.invalid/?next=" + self.URL))
        self.assertFalse(cascade.video_keys("https://instagram.com/?next=/reel/AbCdE123/"))
        self.assertEqual(cascade.video_keys(self.URL + "?other=/reel/Other999/"), {"AbCdE123"})
        self.assertEqual(cascade.video_keys("https://www.youtube.com/watch?v=Video12345&next=/reel/Other999/"), {"Video12345"})

    def test_native_pipeline_info_id_is_accepted_only_with_the_matching_extractor(self):
        job = self.jobs / "youtube-job"
        job.mkdir()
        (job / "notes.md").write_text("synthetic", encoding="utf-8")
        info = job / "source.info.json"
        url = "https://youtu.be/Video12345"
        info.write_text(json.dumps({"id": "Video12345", "extractor_key": "Youtube"}), encoding="utf-8")
        self.assertEqual(cascade.find_job(url), job)
        info.write_text(json.dumps({"id": "Video12345", "extractor_key": "Youtube", "webpage_url": url}), encoding="utf-8")
        self.assertEqual(cascade.find_job(url), job)
        info.write_text(json.dumps({"id": "Other99999", "extractor_key": "Youtube", "webpage_url": url}), encoding="utf-8")
        self.assertIsNone(cascade.find_job(url), "canonical URL cannot hide a contradictory YouTube ID")
        info.write_text(json.dumps({"id": "Video12345", "extractor_key": "Instagram", "webpage_url": url}), encoding="utf-8")
        self.assertIsNone(cascade.find_job(url), "a conflicting extractor cannot establish this source")
        info.write_text(json.dumps({"id": "Video12345", "extractor_key": "Instagram"}), encoding="utf-8")
        self.assertIsNone(cascade.find_job(url))
        info.write_text("{", encoding="utf-8")
        self.assertIsNone(cascade.find_job(url))

    def test_instagram_numeric_media_id_can_differ_from_shortcode(self):
        job = self.jobs / "instagram-job"
        job.mkdir()
        (job / "notes.md").write_text("synthetic", encoding="utf-8")
        info = job / "source.info.json"
        info.write_text(json.dumps({"extractor_key": "Instagram", "id": "123456789", "webpage_url": self.URL}), encoding="utf-8")
        self.assertEqual(cascade.find_job(self.URL), job)
        info.write_text(json.dumps({"extractor_key": "Instagram", "id": "123456789", "shortcode": "Other999", "webpage_url": self.URL}), encoding="utf-8")
        self.assertIsNone(cascade.find_job(self.URL))

    def test_comparable_extractor_ids_match_their_platform_urls(self):
        cases = (
            ("https://x.com/a/status/12345678", "Twitter", "12345678", "12345679"),
            ("https://threads.net/@a/post/PostABC123", "Threads", "PostABC123", "OtherABC9"),
            ("https://www.tiktok.com/@a/video/12345678", "TikTok", "12345678", "12345679"),
            ("https://www.douyin.com/video/12345678", "Douyin", "12345678", "12345679"),
            ("https://vimeo.com/12345678", "Vimeo", "12345678", "12345679"),
            ("https://www.facebook.com/a/videos/12345678/", "Facebook", "12345678", "12345679"),
            ("https://www.bilibili.com/video/BV12345678", "Bilibili", "BV12345678", "av12345678"),
        )
        for index, (url, extractor, wanted, other) in enumerate(cases):
            with self.subTest(extractor=extractor):
                job = self.jobs / f"platform-{index}"
                job.mkdir()
                info = job / "source.info.json"
                info.write_text(json.dumps({"extractor_key": extractor, "id": wanted, "webpage_url": url}), encoding="utf-8")
                self.assertTrue(cascade.job_matches(job, url))
                info.write_text(json.dumps({"extractor_key": extractor, "id": other, "webpage_url": url}), encoding="utf-8")
                self.assertFalse(cascade.job_matches(job, url))

    def test_cascade_never_starts_l1_for_a_spoofed_social_url(self):
        fake_link = self.jobs.parent / "fake-link"
        fake_link.touch()
        url = "https://evil.invalid/?next=" + self.URL
        with mock.patch.object(cascade, "LINK", fake_link), mock.patch.object(cascade.subprocess, "run") as run:
            self.assertFalse(cascade.cascade(url)["ok"])
            run.assert_not_called()

    def test_provider_selection_requires_exact_returned_identity(self):
        wrong = {"shortCode": "AbCdE1234", "url": self.OTHER, "caption": "other"}
        correct = {"shortCode": "AbCdE123", "url": self.URL, "caption": "correct"}
        self.assertIsNone(apify.pick_hit([wrong], "AbCdE123"))
        self.assertEqual(apify.pick_hit([wrong, correct], "AbCdE123"), correct)
        self.assertIsNone(brightdata.pick_hit([wrong], "AbCdE123", "instagram"))
        self.assertEqual(brightdata.pick_hit([wrong, correct], "AbCdE123", "instagram"), correct)
        conflicting = {"shortCode": "Wrong999", "url": self.URL, "caption": "wrong"}
        self.assertIsNone(apify.pick_hit([conflicting], "AbCdE123"))
        self.assertIsNone(brightdata.pick_hit([conflicting], "AbCdE123", "instagram"))

    def test_verified_provider_jobs_run_the_real_writers_with_mocked_providers(self):
        hit = {"shortCode": "AbCdE123", "url": self.URL, "caption": "synthetic", "transcript": "Synthetic transcript"}
        for name in ("brightdata", "apify"):
            output = io.StringIO()
            with self.subTest(provider=name), contextlib.redirect_stdout(output):
                if name == "brightdata":
                    with mock.patch.object(brightdata, "JOBS", self.jobs), \
                         mock.patch.object(brightdata, "token", return_value="test-only"), \
                         mock.patch.object(brightdata, "fetch_instagram", return_value=(hit, "mock", "", [hit])):
                        self.assertEqual(brightdata.main(self.URL), 0)
                    method = cascade.level2_brightdata
                else:
                    with mock.patch.object(apify, "JOBS", self.jobs), \
                         mock.patch.object(apify, "token", return_value="test-only"), \
                         mock.patch.object(apify, "api", return_value={"data": {"id": "synthetic-run"}}), \
                         mock.patch.object(apify, "wait_run", return_value={"status": "SUCCEEDED"}), \
                         mock.patch.object(apify, "dataset_items", return_value=[hit]):
                        self.assertEqual(apify.main(self.URL), 0)
                    method = cascade.level2_apify
                meta = json.loads(output.getvalue().splitlines()[-1])
                self.assertTrue(meta["ok"])
                job = Path(meta["job"])
                self.assertTrue((job / "notes.md").is_file())
                with mock.patch.object(cascade.subprocess, "run", return_value=self.response(job)):
                    self.assertEqual(method(self.URL), job)

    def test_mocked_x_and_verified_threads_create_usable_brightdata_jobs(self):
        cases = [
            ("x", "https://x.com/person/status/123456789", "123456789"),
            ("threads", "https://www.threads.com/@person/post/PostABC123", "PostABC123"),
        ]
        for platform, url, identifier in cases:
            with self.subTest(platform=platform), mock.patch.object(brightdata, "JOBS", self.jobs), \
                 mock.patch.object(brightdata, "token", return_value="test-only"), contextlib.redirect_stdout(io.StringIO()) as output:
                if platform == "x":
                    item = {"tweet_id": identifier, "url": url, "text": "synthetic post"}
                    with mock.patch.object(brightdata, "fetch_x", return_value=(item, "mock", "", [item])):
                        self.assertEqual(brightdata.main(url), 0)
                else:
                    result = {"structuredContent": {"url": url}, "content": [{"type": "text", "text": "Synthetic post body. " * 10}]}
                    with mock.patch.object(brightdata, "mcp_tool_call", return_value=result):
                        self.assertEqual(brightdata.main(url), 0)
                meta = json.loads(output.getvalue().splitlines()[-1])
                self.assertTrue(meta["ok"])
                job = Path(meta["job"])
                with mock.patch.object(cascade.subprocess, "run", return_value=self.response(job)):
                    self.assertEqual(cascade.level2_brightdata(url), job)

    def test_threads_requires_provider_source_metadata_not_self_filled_fields(self):
        url = "https://www.threads.net/@person/post/PostABC123"
        other = "https://www.threads.net/@person/post/OtherABC999"
        text = "Synthetic article content for testing source metadata. " * 4
        content = [{"type": "text", "text": text}]
        for result in ({"content": content}, {"content": content, "structuredContent": {"url": other}}):
            with self.subTest(result=result), mock.patch.object(brightdata, "mcp_tool_call", return_value=result):
                hit, _via, note, _items = brightdata.fetch_threads("test-only", url, "PostABC123")
                self.assertIsNone(hit)
                self.assertIn("source", note)
        contradiction = {"content": [{"type": "text", "text": "Source URL: " + other + "\n" + text}],
                         "structuredContent": {"url": url}}
        with mock.patch.object(brightdata, "mcp_tool_call", return_value=contradiction):
            hit, _via, note, _items = brightdata.fetch_threads("test-only", url, "PostABC123")
            self.assertIsNone(hit, "an explicit conflicting source claim must not be ignored")
            self.assertIn("source", note)
        with mock.patch.object(brightdata, "mcp_tool_call", return_value={"content": content, "structuredContent": {"url": url}}):
            hit, _via, _note, _items = brightdata.fetch_threads("test-only", url, "PostABC123")
        self.assertIsNotNone(hit)
        self.assertEqual(hit["_verified_source_url"], url)

        job = self.jobs / "brightdata-PostABC123"
        job.mkdir()
        (job / "notes.md").write_text("synthetic", encoding="utf-8")
        (job / "brightdata.item.json").write_text(json.dumps({"id": "PostABC123", "url": url, "_source_markdown": "content of " + other}), encoding="utf-8")
        self.assertFalse(cascade.provider_job_matches(job, url, "brightdata"))

    def test_brightdata_errors_never_return_even_a_prefix_of_the_synthetic_token(self):
        token = "S3cR+Synthetic/Token123456"
        self.assertNotIn(token[:4], brightdata.redact(token, "error " + token))
        encoded = urllib.parse.quote(token, safe="")
        self.assertNotIn(token[:4], brightdata.redact(token, "error " + encoded))

    def test_apify_errors_never_return_the_synthetic_token(self):
        token = "S3cR+Synthetic/Token123456"
        output = io.StringIO()
        with mock.patch.object(apify, "token", return_value=token), \
             mock.patch.object(apify, "api", side_effect=RuntimeError("failed " + token + " " + urllib.parse.quote(token, safe=""))), \
             contextlib.redirect_stdout(output):
            apify.main(self.URL)
        self.assertNotIn(token, output.getvalue())
        self.assertNotIn(token[:4], output.getvalue())

    def test_non_social_url_never_reaches_paid_or_og_subprocesses(self):
        bad_urls = ["https://evil.invalid/?next=https://instagram.com/reel/AbCdE123/",
                    "https://instagram.com.evil.invalid/reel/AbCdE123/",
                    "https://instagram.com@evil.invalid/reel/AbCdE123/",
                    "ftp://instagram.com/reel/AbCdE123/"]
        for url in bad_urls:
            with self.subTest(url=url), mock.patch.object(cascade.subprocess, "run") as run:
                self.assertIsNone(cascade.level2_brightdata(url))
                self.assertIsNone(cascade.level2_apify(url))
                self.assertEqual(cascade.level3_og(url), "")
                run.assert_not_called()
                self.assertEqual(brightdata.detect_platform(url), "")
        self.assertEqual(brightdata.detect_platform(self.URL), "instagram")
        self.assertEqual(brightdata.detect_platform("https://x.com/a/status/123456"), "x")
        self.assertEqual(brightdata.detect_platform("https://www.threads.net/@a/post/AbCdE123"), "threads")

    def test_direct_og_entry_does_not_fetch_an_untrusted_host(self):
        with mock.patch.object(og, "fetch") as fetch, contextlib.redirect_stdout(io.StringIO()):
            result = og.main("https://evil.invalid/?next=https://instagram.com/reel/AbCdE123/")
        self.assertFalse(result["ok"])
        fetch.assert_not_called()

    def test_direct_og_entry_keeps_valid_instagram_preview(self):
        html = b'<meta property="og:description" content="Synthetic preview text">' + b' ' * 1100
        with mock.patch.object(og, "fetch", return_value=html) as fetch, contextlib.redirect_stdout(io.StringIO()):
            result = og.main(self.URL)
        self.assertTrue(result["ok"])
        self.assertEqual(result["caption"], "Synthetic preview text")
        self.assertTrue(fetch.call_args.args[0].startswith("https://www.instagram.com/"))

    def test_provider_direct_entry_rejects_bad_hosts_before_reading_credentials(self):
        url = "https://evil.invalid/?next=https://instagram.com/reel/AbCdE123/"
        for module in (brightdata, apify):
            with self.subTest(module=module.__name__), mock.patch.object(module, "token", return_value="") as token, \
                 contextlib.redirect_stdout(io.StringIO()):
                module.main(url)
                token.assert_not_called()

    def test_og_redirects_are_confined_to_the_original_social_platform(self):
        handler_type = getattr(og, "SocialRedirectHandler", lambda _platform: urllib.request.HTTPRedirectHandler())
        handler = handler_type("instagram")
        request = urllib.request.Request(self.URL)
        for destination in ("http://127.0.0.1/private", "https://evil.invalid/", "https://facebook.com/other"):
            with self.subTest(destination=destination):
                self.assertIsNone(handler.redirect_request(request, None, 302, "Found", {}, destination))
        self.assertIsNotNone(handler.redirect_request(request, None, 302, "Found", {}, "https://m.instagram.com/reel/AbCdE123/"))
        self.assertIsNone(handler.redirect_request(request, None, 302, "Found", {}, "http://instagram.com/reel/AbCdE123/"))

    def test_og_fetch_installs_the_guarded_opener(self):
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b"synthetic"
        opener = mock.Mock()
        opener.open.return_value = response
        with mock.patch.object(og.urllib.request, "build_opener", return_value=opener) as build:
            self.assertEqual(og.fetch(self.URL), b"synthetic")
        handler = build.call_args.args[0]
        self.assertIsInstance(handler, og.SocialRedirectHandler)
        self.assertEqual(handler.platform, "instagram")
        opener.open.assert_called_once()

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX shell hook")
    def test_hook_session_id_cannot_escape_the_synthetic_session_directory(self):
        root = self.jobs.parent
        fake = root / "fake_cascade.py"
        fake.write_text('import json; print(json.dumps({"ok": False, "draft": "", "level": 5}))\n', encoding="utf-8")
        source = (HOOKS / "video-url-prefetch.sh").read_text(encoding="utf-8")
        source = source.replace('/home/box/irisx-failover-restore/20260906/runtime/video-hook', str(root / 'runtime'))
        source = source.replace('/home/box/irisx-failover-restore/20260906/profile-irisx/hooks/video_takeaway_cascade.py', str(fake))
        script = root / "hook.sh"
        script.write_text(source, encoding="utf-8")
        payload = json.dumps({"session_id": "../../escaped", "user_message": self.URL})
        result = subprocess.run(["bash", str(script)], input=payload, text=True, capture_output=True,
                                cwd=root, env={"PATH": "/usr/bin:/bin", "HOME": str(root)}, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((root / "escaped.json").exists(), "session file must remain under the fixture runtime")
        self.assertFalse((root / "escaped.pending").exists())
        self.assertEqual(len(list((root / "runtime" / "sessions").glob("*.json"))), 1)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX shell hook")
    def test_hook_preserves_valid_session_and_replaces_previous_url_draft(self):
        root = self.jobs.parent
        fake = root / "fake_cascade.py"
        fake.write_text('import json, sys; print(json.dumps({"ok": True, "url": sys.argv[1], "draft": "Synthetic: " + sys.argv[1], "level": 4, "note": "og-preview"}))\n', encoding="utf-8")
        source = (HOOKS / "video-url-prefetch.sh").read_text(encoding="utf-8")
        source = source.replace('/home/box/irisx-failover-restore/20260906/runtime/video-hook', str(root / 'runtime'))
        source = source.replace('/home/box/irisx-failover-restore/20260906/profile-irisx/hooks/video_takeaway_cascade.py', str(fake))
        script = root / "hook.sh"
        script.write_text(source, encoding="utf-8")

        def invoke(message):
            payload = json.dumps({"session_id": "session.1", "user_message": message})
            result = subprocess.run(["bash", str(script)], input=payload, text=True, capture_output=True,
                                    cwd=root, env={"PATH": "/usr/bin:/bin", "HOME": str(root)}, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)["context"]

        self.assertIn("Synthetic: " + self.URL, invoke(self.URL))
        self.assertTrue((root / "runtime" / "sessions" / "session.1.json").is_file())
        self.assertIn("Synthetic: " + self.URL, invoke("請再講一次可帶走內容"))
        updated = invoke(self.OTHER)
        self.assertIn("Synthetic: " + self.OTHER, updated)
        self.assertNotIn("Synthetic: " + self.URL + "\n", updated)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX shell hook")
    def test_hook_followup_renders_only_successful_results(self):
        base = self.jobs.parent
        original = (HOOKS / "video-url-prefetch.sh").read_text(encoding="utf-8")
        for mode in ("success", "nonzero", "exception", "bad_encoding", "failure_text", "stale_failure", "stale_failure_retry_fails", "wrong_source", "outside_root"):
            with self.subTest(mode=mode):
                root = base / mode
                sessions = root / "runtime" / "sessions"
                sessions.mkdir(parents=True)
                jobs = root / "jobs"
                jobs.mkdir()
                job = (root / "outside" if mode == "outside_root" else jobs) / "job"
                job.mkdir(parents=True)
                (job / "notes.md").write_text("synthetic", encoding="utf-8")
                (job / "source.info.json").write_text(json.dumps({"webpage_url": self.OTHER if mode == "wrong_source" else self.URL}), encoding="utf-8")
                cascade_script = root / "real_cascade.py"
                cascade_code = (HOOKS / "video_takeaway_cascade.py").read_text(encoding="utf-8")
                cascade_code = cascade_code.replace('JOBS = Path("/workspace/video-timeline-pipeline/jobs")', f'JOBS = Path({str(jobs)!r})')
                cascade_script.write_text(cascade_code, encoding="utf-8")
                (root / "irisx_identity.py").write_text((HOOKS / "irisx_identity.py").read_text(encoding="utf-8"), encoding="utf-8")
                bundle = {"url": self.URL, "verified_url": self.URL, "session_schema": 2, "outcome": "success",
                          "job": str(job), "draft": "(render failed: old)" if mode.startswith("stale_") else ""}
                bundle_path = sessions / "followup.json"
                bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
                renderer = root / "fake_renderer.py"
                body = {
                    "success": "print('Synthetic rendered draft')\n",
                    "nonzero": "import sys; print('stale draft'); sys.exit(1)\n",
                    "exception": "print('unreachable')\n",
                    "bad_encoding": "import sys; sys.stdout.buffer.write(b'\\xff')\n",
                    "failure_text": "print('(render failed: synthetic)')\n",
                    "stale_failure": "print('Synthetic rendered draft')\n",
                    "stale_failure_retry_fails": "import sys; sys.exit(1)\n",
                    "wrong_source": "print('Synthetic wrong source')\n",
                    "outside_root": "print('Synthetic outside root')\n",
                }[mode]
                renderer.write_text(body, encoding="utf-8")
                source = original.replace('/home/box/irisx-failover-restore/20260906/runtime/video-hook', str(root / 'runtime'))
                source = source.replace('/home/box/irisx-failover-restore/20260906/profile-irisx/hooks/render_takeaway.py', str(renderer))
                source = source.replace('/home/box/irisx-failover-restore/20260906/profile-irisx/hooks/video_takeaway_cascade.py', str(cascade_script))
                if mode == "exception":
                    needle = 'sys.executable, str(RENDER)'
                    self.assertIn(needle, source)
                    source = source.replace(needle, repr(str(root / 'missing-python')) + ', str(RENDER)')
                script = root / "hook.sh"
                script.write_text(source, encoding="utf-8")
                payload = json.dumps({"session_id": "followup", "user_message": "請再講一次可帶走內容"})
                result = subprocess.run(["bash", str(script)], input=payload, text=True, capture_output=True,
                                        cwd=root, env={"PATH": "/usr/bin:/bin", "HOME": str(root)}, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                persisted = json.loads(bundle_path.read_text(encoding="utf-8"))
                if mode in {"success", "stale_failure"}:
                    self.assertIn("Synthetic rendered draft", json.loads(result.stdout)["context"])
                    self.assertEqual(persisted["draft"], "Synthetic rendered draft")
                    self.assertTrue((sessions / "followup.pending").is_file())
                else:
                    self.assertEqual(result.stdout, "", "renderer failure must not become answer context")
                    self.assertEqual(persisted, bundle, "failed rendering must leave the session retryable")
                    self.assertFalse((sessions / "followup.pending").exists())

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX shell hook")
    def test_legacy_session_is_not_reused_and_refresh_restores_followup(self):
        root = self.jobs.parent
        sessions = root / "runtime" / "sessions"
        sessions.mkdir(parents=True)
        bundle = sessions / "legacy.json"
        original = json.dumps({"url": self.URL, "job": str(self.jobs / "wrong"), "draft": "Synthetic old unrelated draft"}).encode()
        bundle.write_bytes(original)
        fake = root / "fake_cascade.py"
        fake.write_text('import json, sys; print(json.dumps({"ok": True, "url": sys.argv[1], "draft": "Synthetic refreshed", "level": 4, "note": "og-preview"}))\n', encoding="utf-8")
        source = (HOOKS / "video-url-prefetch.sh").read_text(encoding="utf-8")
        source = source.replace('/home/box/irisx-failover-restore/20260906/runtime/video-hook', str(root / 'runtime'))
        source = source.replace('/home/box/irisx-failover-restore/20260906/profile-irisx/hooks/video_takeaway_cascade.py', str(fake))
        script = root / "hook.sh"
        script.write_text(source, encoding="utf-8")

        def invoke(message):
            result = subprocess.run(["bash", str(script)], input=json.dumps({"session_id": "legacy", "user_message": message}),
                                    text=True, capture_output=True, cwd=root, env={"PATH": "/usr/bin:/bin", "HOME": str(root)}, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        self.assertEqual(invoke("請再講一次可帶走內容"), "")
        self.assertEqual(bundle.read_bytes(), original, "unknown legacy session must not be modified")
        self.assertFalse((sessions / "legacy.pending").exists())
        self.assertIn("Synthetic refreshed", json.loads(invoke(self.URL))["context"])
        self.assertIn("Synthetic refreshed", json.loads(invoke("請再講一次可帶走內容"))["context"])

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX shell hook")
    def test_honest_cascade_failure_remains_a_failure_on_followup(self):
        root = self.jobs.parent
        fake = root / "fake_cascade.py"
        fake.write_text('import json, sys; print(json.dumps({"ok": False, "url": sys.argv[1], "draft": "Synthetic no verified content", "level": 5, "note": "all_levels_failed"}))\n', encoding="utf-8")
        source = (HOOKS / "video-url-prefetch.sh").read_text(encoding="utf-8")
        source = source.replace('/home/box/irisx-failover-restore/20260906/runtime/video-hook', str(root / 'runtime'))
        source = source.replace('/home/box/irisx-failover-restore/20260906/profile-irisx/hooks/video_takeaway_cascade.py', str(fake))
        script = root / "hook.sh"
        script.write_text(source, encoding="utf-8")
        for message in (self.URL, "請再講一次可帶走內容"):
            result = subprocess.run(["bash", str(script)], input=json.dumps({"session_id": "failure", "user_message": message}),
                                    text=True, capture_output=True, cwd=root, env={"PATH": "/usr/bin:/bin", "HOME": str(root)}, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            context = json.loads(result.stdout)["context"]
            self.assertIn("Synthetic no verified content", context)
            self.assertIn("級聯失敗回報", context)
            self.assertNotIn("可帶走草稿", context)

    @unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "POSIX shell hook")
    def test_new_provider_result_is_checked_before_becoming_a_trusted_session(self):
        base = self.jobs.parent
        for matching in (True, False):
            with self.subTest(matching=matching):
                root = base / ("matching" if matching else "mismatch")
                jobs = root / "jobs"
                job = jobs / "brightdata-AbCdE123"
                job.mkdir(parents=True)
                (job / "notes.md").write_text("synthetic", encoding="utf-8")
                source_url = self.URL if matching else self.OTHER
                (job / "brightdata.item.json").write_text(json.dumps({"shortCode": source_url.split('/')[-2], "url": source_url}), encoding="utf-8")
                (root / "irisx_identity.py").write_text((HOOKS / "irisx_identity.py").read_text(encoding="utf-8"), encoding="utf-8")
                cascade_code = (HOOKS / "video_takeaway_cascade.py").read_text(encoding="utf-8")
                cascade_code = cascade_code.replace('JOBS = Path("/workspace/video-timeline-pipeline/jobs")', f'JOBS = Path({str(jobs)!r})')
                cascade_code = cascade_code.split('if __name__ == "__main__":', 1)[0]
                cascade_code += '\nif __name__ == "__main__":\n    print(json.dumps({"ok": True, "url": sys.argv[1], "job": ' + repr(str(job)) + ', "draft": "Synthetic provider draft", "level": 2}))\n'
                cascade_script = root / "fake_cascade.py"
                cascade_script.write_text(cascade_code, encoding="utf-8")
                source = (HOOKS / "video-url-prefetch.sh").read_text(encoding="utf-8")
                source = source.replace('/home/box/irisx-failover-restore/20260906/runtime/video-hook', str(root / 'runtime'))
                source = source.replace('/home/box/irisx-failover-restore/20260906/profile-irisx/hooks/video_takeaway_cascade.py', str(cascade_script))
                script = root / "hook.sh"
                script.write_text(source, encoding="utf-8")
                result = subprocess.run(["bash", str(script)], input=json.dumps({"session_id": "new", "user_message": self.URL}),
                                        text=True, capture_output=True, cwd=root, env={"PATH": "/usr/bin:/bin", "HOME": str(root)}, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                context = json.loads(result.stdout)["context"]
                saved = json.loads((root / "runtime" / "sessions" / "new.json").read_text(encoding="utf-8"))
                if matching:
                    self.assertIn("Synthetic provider draft", context)
                    self.assertEqual(saved["outcome"], "success")
                else:
                    self.assertNotIn("Synthetic provider draft", context)
                    self.assertEqual(saved["outcome"], "failure")
                    self.assertIn("級聯失敗回報", context)

    def test_render_failure_is_not_returned_as_a_successful_draft(self):
        job = self.job("render", self.URL, "brightdata.item.json")
        with mock.patch.object(cascade.subprocess, "run", side_effect=subprocess.TimeoutExpired("render", 30)):
            self.assertFalse(cascade.render(job))
        with mock.patch.object(cascade.subprocess, "run", side_effect=UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
            self.assertFalse(cascade.render(job))
        with mock.patch.object(cascade.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="stale draft", stderr="failed")):
            self.assertFalse(cascade.render(job))
        for level in ("brightdata", "apify"):
            with self.subTest(level=level), \
                 mock.patch.object(cascade, "level1_pipeline", return_value=None), \
                 mock.patch.object(cascade, "level2_brightdata", return_value=job if level == "brightdata" else None), \
                 mock.patch.object(cascade, "level2_apify", return_value=job if level == "apify" else None), \
                 mock.patch.object(cascade, "level3_og", return_value=""), \
                 mock.patch.object(cascade, "render", return_value="(render failed: synthetic)"):
                self.assertFalse(cascade.cascade(self.URL)["ok"])


if __name__ == "__main__":
    unittest.main()
