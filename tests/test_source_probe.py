import asyncio
import io
import json
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import mcp_server as server
import pipeline
from job_queue import JobQueue


@pytest.mark.parametrize("message,host,code,retryable", [
    ("ERROR: [facebook] 1441463020884039: Cannot parse data", "www.facebook.com", "LOGIN_REQUIRED_OR_PRIVATE", False),
    ("Empty or private data", "facebook.com", "LOGIN_REQUIRED_OR_PRIVATE", False),
    ("Sign in to confirm you're not a bot", "youtube.com", "BOT_CHECK", False),
    ("Sign in to confirm you’re not a bot", "youtube.com", "BOT_CHECK", False),
    ("Unable to connect to proxy", "youtube.com", "PROXY_UNREACHABLE", True),
    ("Tunnel connection failed: 403 Forbidden", "youtube.com", "PROXY_UNREACHABLE", True),
    ("unable to download video data: HTTP Error 403: Forbidden", "youtube.com", "FORBIDDEN_403", False),
    ("Unsupported URL: https://example.test", "example.test", "UNSUPPORTED_URL", False),
    ("Video unavailable", "youtube.com", "VIDEO_UNAVAILABLE", False),
    ("Sign in to confirm your age", "youtube.com", "AGE_RESTRICTED", False),
    ("This video is not available in your country", "youtube.com", "GEO_BLOCKED", False),
    ("HTTP Error 429: Too Many Requests", "youtube.com", "RATE_LIMITED", True),
    ("File is larger than max-filesize", "vimeo.com", "FILE_TOO_LARGE", False),
    ("影片超過 MCP_MAX_SOURCE_MB=512。", "vimeo.com", "FILE_TOO_LARGE", False),
    ("The read operation timed out", "youtube.com", "TIMEOUT", True),
    ("Cannot parse data", "notfacebook.com", "UNKNOWN", False),
    ("something completely unexpected", "example.test", "UNKNOWN", False),
])
def test_classification(message, host, code, retryable):
    result = pipeline.classify_download_error(message, host)
    assert result["error_code"] == code
    assert result["retryable"] is retryable
    assert result["raw_error"] == message
    assert result["user_message"] and result["next_steps"]
    assert not any("cookie" in step.lower() for step in result["next_steps"])


def test_raw_error_is_truncated_and_facebook_wording_is_uncertain():
    assert pipeline.classify_download_error("x" * 700)["raw_error"] == "x" * 500
    result = pipeline.classify_download_error("Cannot parse data", "https://www.facebook.com/reel/123")
    assert "可能不是公開" in result["user_message"]
    assert "頁面格式" in result["user_message"]


@pytest.fixture
def probe_environment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "MCP_ROOT", tmp_path)
    monkeypatch.setattr(server, "MCP_JOBS_ROOT", tmp_path / "mcp_jobs")
    monkeypatch.setenv("YTDLP_YOUTUBE_PROXY", "http://proxy.example:18443")
    monkeypatch.setattr(server, "check_public_host", lambda *args: None)
    monkeypatch.setattr(server, "validate_share_redirects", lambda *args: None)
    return tmp_path


def test_probe_success_uses_download_options_without_files(probe_environment):
    url = "https://youtu.be/123"
    info = {"title": "範例", "duration": 12, "extractor": "youtube", "uploader": "作者",
            "is_live": False, "live_status": "not_live", "availability": "public", "webpage_url": url,
            "requested_downloads": [{"format_id": "137+140", "ext": "mp4", "requested_formats": [
                {"filesize": 1000}, {"filesize_approx": 200}]}]}
    with patch.object(pipeline.subprocess, "run", return_value=SimpleNamespace(returncode=0, stdout=json.dumps(info), stderr="")) as run, \
            patch.object(server, "get_queue", side_effect=AssertionError("must not create queue")):
        result = asyncio.run(server.probe_video_source(url))
    assert result == {"status": "ok", "title": "範例", "duration_seconds": 12, "extractor": "youtube",
                      "uploader": "作者", "is_live": False, "live_status": "not_live", "availability": "public",
                      "webpage_url": url, "estimated_filesize_bytes": 1200,
                      "selected_format": {"format_id": "137+140", "format": None, "ext": "mp4", "width": None,
                                          "height": None, "vcodec": None, "acodec": None}}
    command = run.call_args.args[0]
    assert command[command.index("--proxy") + 1] == "http://proxy.example:18443"
    assert command[-1] == url
    assert "--skip-download" in command and "--no-cache-dir" in command and "-B" in command
    assert "--output" not in command and "--write-info-json" not in command
    assert 0 < run.call_args.kwargs["timeout"] <= 45
    assert list(probe_environment.iterdir()) == []


@pytest.mark.parametrize("failure,code", [
    (SimpleNamespace(returncode=1, stdout="", stderr="Unable to connect to proxy"), "PROXY_UNREACHABLE"),
    (subprocess.TimeoutExpired("yt-dlp", 45), "TIMEOUT"),
    (SimpleNamespace(returncode=0, stdout="not json", stderr=""), "UNKNOWN"),
])
def test_probe_failure_without_files(probe_environment, failure, code):
    kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
    with patch.object(pipeline.subprocess, "run", **kwargs):
        result = asyncio.run(server.probe_video_source("https://youtu.be/123"))
    assert result["status"] == "error" and result["error_code"] == code
    assert result["error"] and result["raw_error"] and result["next_steps"]
    assert list(probe_environment.iterdir()) == []


def test_probe_reuses_url_validator_before_subprocess(probe_environment):
    with patch.object(server, "validate_source_url", side_effect=ValueError("影片 URL 無效。")) as validate, \
            patch.object(pipeline.subprocess, "run") as run:
        result = asyncio.run(server.probe_video_source("file:///private"))
    validate.assert_called_once_with("file:///private")
    run.assert_not_called()
    assert result["status"] == "error"


def test_facebook_cookie_probe_uses_memory_and_shared_options(probe_environment, monkeypatch):
    cookies = probe_environment / "existing.cookies.txt"
    original = ("# Netscape HTTP Cookie File\n"
                ".facebook.com\tTRUE\t/\tTRUE\t2147483647\tc_user\tPRIVATE_USER\n"
                ".example.com\tTRUE\t/\tTRUE\t2147483647\tx\tUNRELATED\n")
    cookies.write_text(original, encoding="utf-8")
    monkeypatch.setenv("YTDLP_FACEBOOK_COOKIE_FILE", str(cookies))
    responses = [SimpleNamespace(returncode=1, stdout="", stderr="Cannot parse data"),
                 SimpleNamespace(returncode=0, stdout='{"title":"範例"}', stderr="")]
    with patch.object(pipeline, "instagram_download_diagnostics", return_value={"chrome_impersonation": True}), \
            patch.object(pipeline.subprocess, "run", side_effect=responses) as run, \
            patch.object(pipeline.tempfile, "TemporaryDirectory", side_effect=AssertionError("no temp files")):
        result = asyncio.run(server.probe_video_source("https://www.facebook.com/reel/123"))
    assert result["status"] == "ok" and result["estimated_filesize_bytes"] is None
    command = run.call_args.args[0]
    assert "--impersonate" in command and "--cookies" not in command and "--proxy" not in command
    payload = json.loads(run.call_args.kwargs["input"])
    assert [cookie["value"] for cookie in payload] == ["PRIVATE_USER"]
    # Exercise the child script, including Cookie reconstruction, without yt-dlp/network/files.
    import yt_dlp
    parsed = yt_dlp.parse_options(command[4:])
    ydl = MagicMock()
    ydl.cookiejar = pipeline.http.cookiejar.CookieJar()
    ydl.extract_info.return_value = {"title": "範例"}
    ydl.sanitize_info.side_effect = lambda info: info
    with patch.object(sys, "argv", ["probe", *command[4:]]), \
            patch.object(sys, "stdin", io.StringIO(run.call_args.kwargs["input"])), \
            patch.object(sys, "stdout", io.StringIO()), \
            patch.object(yt_dlp, "parse_options", return_value=parsed), \
            patch.object(yt_dlp, "YoutubeDL") as constructor:
        constructor.return_value.__enter__.return_value = ydl
        exec(compile(pipeline.SOURCE_PROBE_SCRIPT, "<probe>", "exec"), {})
    opts = constructor.call_args.args[0]
    assert opts["cookiefile"] is None and opts["cachedir"] is False and opts["skip_download"] is True
    assert list(ydl.cookiejar)[0].value == "PRIVATE_USER"
    ydl.extract_info.assert_called_once_with("https://www.facebook.com/reel/123", download=False)
    assert cookies.read_text(encoding="utf-8") == original
    assert list(probe_environment.iterdir()) == [cookies]


def test_facebook_probe_redacts_cookie_errors(probe_environment, monkeypatch):
    cookies = probe_environment / "existing.cookies.txt"
    cookies.write_text("# Netscape HTTP Cookie File\n.facebook.com\tTRUE\t/\tTRUE\t2147483647\tc_user\tSECRET\n")
    monkeypatch.setenv("YTDLP_FACEBOOK_COOKIE_FILE", str(cookies))
    responses = [SimpleNamespace(returncode=1, stdout="", stderr="Cannot parse data"),
                 SimpleNamespace(returncode=1, stdout="", stderr="Cookie: SECRET\nAuthorization: Bearer HIDDEN\nCannot parse data")]
    with patch.object(pipeline, "instagram_download_diagnostics", return_value={"chrome_impersonation": False}), \
            patch.object(pipeline.subprocess, "run", side_effect=responses):
        result = asyncio.run(server.probe_video_source("https://www.facebook.com/reel/123"))
    assert result["error_code"] == "LOGIN_REQUIRED_OR_PRIVATE"
    assert "SECRET" not in json.dumps(result) and "HIDDEN" not in json.dumps(result)


def test_prepare_sync_error_keeps_old_fields_and_adds_classification():
    error = "ERROR: [facebook] 123: Cannot parse data"
    with patch.object(server, "prepare_video_locked", side_effect=RuntimeError(error)):
        result = asyncio.run(server.prepare_video_for_chatgpt("https://www.facebook.com/reel/123"))
    assert result.isError
    payload = result.structuredContent
    assert payload == {"status": "error", "error": error,
                       **pipeline.classify_download_error(error, "facebook.com")}
    server.PreparedVideoResult.model_validate(payload)


def test_prepare_failed_job_is_classified_on_poll(tmp_path):
    queue = JobQueue(tmp_path / "queue", autostart=False)
    try:
        operation = queue.submit("prepare", {"url": "https://www.facebook.com/reel/123"})
        with queue.connect() as db:
            db.execute("UPDATE jobs SET status='failed',error=? WHERE id=?", ("Cannot parse data", operation["operation_id"]))
        with patch.object(server, "get_queue", return_value=queue):
            result = asyncio.run(server.get_video_job(operation["operation_id"]))
        assert result["status"] == "failed" and result["error"] == "Cannot parse data"
        assert result["error_code"] == "LOGIN_REQUIRED_OR_PRIVATE"
        assert result["raw_error"] == "Cannot parse data" and result["retryable"] is False
    finally:
        queue.close()


def test_probe_annotations_and_error_instructions():
    tools = {tool.name: tool for tool in asyncio.run(server.mcp.list_tools())}
    assert tools["probe_video_source"].annotations.readOnlyHint is True
    assert tools["probe_video_source"].annotations.openWorldHint is True
    for name in ("probe_video_source", "prepare_video_for_chatgpt", "get_video_job"):
        assert "user_message" in tools[name].description and "next_steps" in tools[name].description
