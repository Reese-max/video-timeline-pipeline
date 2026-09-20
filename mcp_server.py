from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import mimetypes
import os
import re
import socket
import threading
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

import pipeline
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, model_validator
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse


ROOT = Path(__file__).resolve().parent
pipeline.load_dotenv(ROOT / ".env")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} 必須是整數。") from exc


MCP_HOST = os.getenv("MCP_HOST", "127.0.0.1")
MCP_PORT = env_int("MCP_PORT", 8787)
MCP_ROOT = Path(os.getenv("MCP_ROOT", str(ROOT))).expanduser().resolve()
MCP_JOBS_ROOT = MCP_ROOT / "mcp_jobs"
MCP_MAX_SOURCE_BYTES = env_int("MCP_MAX_SOURCE_MB", 512) * 1024 * 1024
MCP_MAX_SEGMENT_SECONDS = env_int("MCP_MAX_SEGMENT_SECONDS", 600)
MCP_MAX_SEGMENTS = env_int("MCP_MAX_SEGMENTS", 0)
MCP_PUBLIC_BASE_URL = (
    os.getenv("MCP_PUBLIC_BASE_URL", "") or f"http://{MCP_HOST}:{MCP_PORT}"
).rstrip("/")
MCP_MEDIA_BASE_URL = (
    os.getenv("MCP_MEDIA_BASE_URL", "") or MCP_PUBLIC_BASE_URL
).rstrip("/")
UI_URI = "ui://video-timeline-pipeline/v2.html"
PUBLIC_URL = urlsplit(MCP_PUBLIC_BASE_URL)
MEDIA_URL = urlsplit(MCP_MEDIA_BASE_URL)
UI_ORIGIN = f"{PUBLIC_URL.scheme}://{PUBLIC_URL.netloc}"
PUBLIC_NETLOC = PUBLIC_URL.netloc
MEDIA_ORIGIN = f"{MEDIA_URL.scheme}://{MEDIA_URL.netloc}"
MEDIA_NETLOC = MEDIA_URL.netloc
ALLOWED_HOSTS = sorted(
    {
        f"{MCP_HOST}:{MCP_PORT}",
        f"127.0.0.1:{MCP_PORT}",
        f"localhost:{MCP_PORT}",
        f"[::1]:{MCP_PORT}",
        PUBLIC_NETLOC,
        MEDIA_NETLOC,
    }
    - {""}
)
ALLOWED_ORIGINS = sorted(
    {
        f"http://{MCP_HOST}:{MCP_PORT}",
        f"http://127.0.0.1:{MCP_PORT}",
        f"http://localhost:{MCP_PORT}",
        f"http://[::1]:{MCP_PORT}",
        UI_ORIGIN,
        MEDIA_ORIGIN,
    }
    - {""}
)
TRANSPORT_SECURITY = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=ALLOWED_HOSTS,
    allowed_origins=ALLOWED_ORIGINS,
)
# ponytail: global lock, per-job locks if concurrent public jobs become necessary.
JOB_LOCK = threading.Lock()

UI_META = {
    "openai/widgetDescription": "顯示影片分段並把分段檔上傳到 ChatGPT，供後續影片摘要使用。",
    "ui": {
        "domain": UI_ORIGIN,
        "prefersBorder": True,
        "csp": {
            "connectDomains": sorted({UI_ORIGIN, MEDIA_ORIGIN} - {""}),
            "resourceDomains": sorted({UI_ORIGIN, MEDIA_ORIGIN} - {""}),
        },
    },
    "openai/widgetDomain": UI_ORIGIN,
    "openai/widgetCSP": {
        "connect_domains": sorted({UI_ORIGIN, MEDIA_ORIGIN} - {""}),
        "resource_domains": sorted({UI_ORIGIN, MEDIA_ORIGIN} - {""}),
    },
}
TOOL_META = {
    "ui": {"resourceUri": UI_URI},
    "openai/outputTemplate": UI_URI,
    "openai/toolInvocation/invoking": "正在下載並切割影片…",
    "openai/toolInvocation/invoked": "影片分段已準備好",
}
FILE_HANDOFF_META = {
    "ui": {"visibility": ["model", "app"]},
    "openai/widgetAccessible": True,
    "openai/fileParams": ["files"],
    "openai/toolInvocation/invoking": "正在接收影片檔案…",
    "openai/toolInvocation/invoked": "影片檔案已交接",
}


class OpenAIFileReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    download_url: str
    file_id: str
    mime_type: str = ""
    file_name: str = ""

    @model_validator(mode="before")
    @classmethod
    def accept_resource_link_url(cls, value):
        if isinstance(value, str):
            return {
                "download_url": value,
                "file_id": f"url_{sha256(value.encode('utf-8')).hexdigest()[:24]}",
            }
        return value


class VerifiedFileSummary(BaseModel):
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None


class FileHandoffResult(BaseModel):
    status: Literal["files_received"]
    files_received: int
    files: list[VerifiedFileSummary]


class PreparedVideoSegment(BaseModel):
    index: int
    filename: str
    preview_filename: str | None = None
    start_seconds: float
    end_seconds: float
    size_bytes: int
    mime_type: str
    url: str
    preview_url: str | None = None


class PreparedVideoResult(BaseModel):
    status: Literal["ready", "error"]
    job_id: str | None = None
    title: str | None = None
    source_url: str | None = None
    duration_seconds: float | None = None
    segment_seconds: int | None = None
    segments: list[PreparedVideoSegment] | None = None
    error: str | None = None


def normalize_file_references(files: list[dict]) -> list[dict]:
    if not files:
        raise ValueError("至少需要一個影片檔案引用。")

    normalized = []
    seen_ids = set()
    for index, item in enumerate(files):
        if not isinstance(item, dict):
            raise ValueError(f"files[{index}] 必須是檔案引用物件。")
        file_id = str(item.get("file_id") or "").strip()
        if not file_id:
            raise ValueError(f"files[{index}].file_id 必填。")
        if file_id in seen_ids:
            raise ValueError(f"files[{index}].file_id 重複。")
        download_url = str(item.get("download_url") or "").strip()
        parsed = urlsplit(download_url)
        if parsed.scheme.lower() != "https" or not parsed.netloc:
            raise ValueError(f"files[{index}].download_url 必須是 HTTPS URL。")
        if parsed.username or parsed.password:
            raise ValueError(f"files[{index}].download_url 不可包含帳號或密碼。")
        seen_ids.add(file_id)
        normalized.append(
            {
                "file_id": file_id,
                "download_url": download_url,
                "mime_type": item.get("mime_type"),
                "file_name": item.get("file_name"),
            }
        )
    return normalized


def validate_source_url(value: str) -> str:
    value = pipeline.require_http_url(value, "影片 URL")
    if len(value) > 2048:
        raise ValueError("影片 URL 不可超過 2048 個字元。")
    parsed = urlsplit(value)
    if parsed.username or parsed.password:
        raise ValueError("影片 URL 不可包含帳號或密碼。")
    try:
        host = (parsed.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        parsed.port
    except (UnicodeError, ValueError) as exc:
        raise ValueError("影片 URL 的主機或連接埠無效。") from exc
    if not host or host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError("為避免 SSRF，不接受本機或 .local 影片 URL。")

    try:
        addresses = {
            ipaddress.ip_address(item[4][0])
            for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        }
    except OSError:
        addresses = set()
    if addresses and any(not address.is_global for address in addresses):
        raise ValueError("為避免 SSRF，影片 URL 必須解析到公開 IP。")
    return value


def segment_bounds(
    duration: float, segment_seconds: int, max_segments: int | None
) -> list[tuple[float, float]]:
    if not 15 <= segment_seconds <= MCP_MAX_SEGMENT_SECONDS:
        raise ValueError(f"segment_seconds 必須介於 15 和 {MCP_MAX_SEGMENT_SECONDS} 秒。")
    if max_segments is not None and max_segments < 1:
        raise ValueError("max_segments 必須是正整數，或省略以取消上限。")
    if MCP_MAX_SEGMENTS > 0 and max_segments is not None and max_segments > MCP_MAX_SEGMENTS:
        raise ValueError(f"max_segments 不可超過 {MCP_MAX_SEGMENTS}。")
    if duration <= 0:
        raise RuntimeError("影片沒有可用的長度資訊。")
    count = math.ceil(duration / segment_seconds)
    if max_segments is not None and count > max_segments:
        raise ValueError(
            f"影片長度 {duration:.0f} 秒需要 {count} 段，超過 max_segments={max_segments}；"
            "請提高 segment_seconds、設定較高的 max_segments，或省略 max_segments。"
        )
    return [
        (start, min(duration, start + segment_seconds))
        for start in (index * segment_seconds for index in range(count))
    ]


def media_url(job_id: str, relative_name: str) -> str:
    escaped = "/".join(part for part in Path(relative_name).parts if part not in {".", ".."})
    return f"{MCP_MEDIA_BASE_URL}/media/{job_id}/{escaped}"


def safe_media_path(job_id: str, relative_name: str) -> Path | None:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        return None
    relative = Path(relative_name)
    if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 2:
        return None
    directory, filename = relative.parts
    if directory == "segments" and not re.fullmatch(r"segment_\d{3}\.mp4", filename):
        return None
    if directory == "previews" and not re.fullmatch(r"preview_\d{3}\.jpg", filename):
        return None
    if directory not in {"segments", "previews"}:
        return None
    job_dir = (MCP_JOBS_ROOT / job_id).resolve()
    candidate = (job_dir / relative).resolve()
    try:
        candidate.relative_to(job_dir)
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def read_manifest(job_dir: Path) -> dict | None:
    path = job_dir / "manifest.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("status") == "ready" else None


def manifest_is_ready(
    manifest: dict, video_path: Path, segment_seconds: int, max_segments: int | None
) -> bool:
    if manifest.get("segment_seconds") != segment_seconds or manifest.get("max_segments") != max_segments:
        return False
    source = manifest.get("source") or {}
    if source.get("fingerprint") != pipeline.file_fingerprint(video_path):
        return False
    for item in manifest.get("segments") or []:
        if not (video_path.parent / "segments" / str(item.get("filename", ""))).is_file():
            return False
        preview = item.get("preview_filename")
        if preview and not (video_path.parent / "previews" / str(preview)).is_file():
            return False
    return bool(manifest.get("segments"))


def split_video(video_path: Path, segment_dir: Path, segment_seconds: int) -> list[Path]:
    segment_dir.mkdir(parents=True, exist_ok=True)
    for stale in segment_dir.glob("segment_*.mp4"):
        stale.unlink()
    pipeline.run_external(
        [
            pipeline.require_tool("ffmpeg"),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "28",
            "-c:a",
            "aac",
            "-b:a",
            "96k",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{segment_seconds})",
            "-f",
            "segment",
            "-segment_time",
            str(segment_seconds),
            "-reset_timestamps",
            "1",
            str(segment_dir / "segment_%03d.mp4"),
        ],
        stream=True,
    )
    segments = sorted(segment_dir.glob("segment_*.mp4"))
    if not segments:
        raise RuntimeError("FFmpeg 沒有產生影片片段。")
    return segments


def extract_preview(segment_path: Path, preview_path: Path) -> bool:
    preview_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        pipeline.run_external(
            [
                pipeline.require_tool("ffmpeg"),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "0.2",
                "-i",
                str(segment_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=640:-2",
                "-q:v",
                "4",
                str(preview_path),
            ]
        )
    except RuntimeError:
        return False
    return preview_path.is_file()


def prepare_video_sync(url: str, segment_seconds: int, max_segments: int | None) -> dict:
    source_url = validate_source_url(url)
    segment_bounds(1, segment_seconds, max_segments)
    job_id = pipeline.job_key(source_url, f"mcp-video-v1:{segment_seconds}:{max_segments}")
    job_dir = MCP_JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        video_path = pipeline.locate_source_video(job_dir)
    except RuntimeError:
        video_path = pipeline.download_video(
            source_url,
            job_dir,
            private_url=True,
            max_filesize=MCP_MAX_SOURCE_BYTES,
            max_duration=(segment_seconds * max_segments if max_segments is not None else None),
        )
    if video_path.stat().st_size > MCP_MAX_SOURCE_BYTES:
        raise RuntimeError(f"影片超過 MCP_MAX_SOURCE_MB={MCP_MAX_SOURCE_BYTES // 1024 // 1024}。")

    probe = pipeline.probe_video(video_path, job_dir / "source.probe.json")
    if not any(stream.get("codec_type") == "video" for stream in probe.get("streams", [])):
        raise RuntimeError("來源沒有視訊串流。")
    duration = pipeline.video_duration(probe)
    bounds = segment_bounds(duration, segment_seconds, max_segments)
    existing = read_manifest(job_dir)
    if existing and manifest_is_ready(existing, video_path, segment_seconds, max_segments):
        return existing

    segments = split_video(video_path, job_dir / "segments", segment_seconds)
    if max_segments is not None and len(segments) > max_segments:
        raise RuntimeError(f"FFmpeg 產生 {len(segments)} 段，超過 max_segments={max_segments}。")
    previews_dir = job_dir / "previews"
    segment_rows = []
    for index, segment_path in enumerate(segments):
        start, end = bounds[min(index, len(bounds) - 1)]
        preview_name = f"preview_{index:03d}.jpg"
        preview_path = previews_dir / preview_name
        has_preview = extract_preview(segment_path, preview_path)
        segment_rows.append(
            {
                "index": index,
                "filename": segment_path.name,
                "preview_filename": preview_name if has_preview else None,
                "start_seconds": round(start, 3),
                "end_seconds": round(end, 3),
                "size_bytes": segment_path.stat().st_size,
                "mime_type": "video/mp4",
            }
        )

    manifest = {
        "version": 1,
        "status": "ready",
        "job_id": job_id,
        "source": {
            "url": pipeline.public_url(source_url),
            "title": pipeline.source_title(job_dir, video_path),
            "duration_seconds": round(duration, 3),
            "size_bytes": video_path.stat().st_size,
            "fingerprint": pipeline.file_fingerprint(video_path),
        },
        "segment_seconds": segment_seconds,
        "max_segments": max_segments,
        "segments": segment_rows,
    }
    pipeline.write_json(job_dir / "manifest.json", manifest)
    return manifest


def prepare_video_locked(url: str, segment_seconds: int, max_segments: int | None) -> dict:
    with JOB_LOCK:
        return prepare_video_sync(url, segment_seconds, max_segments)


def tool_payload(manifest: dict) -> dict:
    job_id = str(manifest["job_id"])
    source = manifest["source"]
    segments = []
    for item in manifest["segments"]:
        row = {
            "index": item["index"],
            "filename": item["filename"],
            "preview_filename": item.get("preview_filename"),
            "start_seconds": item["start_seconds"],
            "end_seconds": item["end_seconds"],
            "size_bytes": item["size_bytes"],
            "mime_type": item["mime_type"],
            "url": media_url(job_id, f"segments/{item['filename']}"),
        }
        if item.get("preview_filename"):
            row["preview_url"] = media_url(job_id, f"previews/{item['preview_filename']}")
        segments.append(row)
    return {
        "status": "ready",
        "job_id": job_id,
        "title": source["title"],
        "source_url": source["url"],
        "duration_seconds": source["duration_seconds"],
        "segment_seconds": manifest["segment_seconds"],
        "segments": segments,
    }


def tool_result(manifest: dict) -> types.CallToolResult:
    payload = tool_payload(manifest)
    links = []
    for segment in payload["segments"]:
        title = f"{segment['start_seconds']:.0f}s–{segment['end_seconds']:.0f}s"
        links.append(
            types.ResourceLink(
                type="resource_link",
                name=segment["filename"],
                title=title,
                uri=segment["url"],
                description="影片分段；ChatGPT 可將此公開 MP4 連結實體化為附件。",
                mimeType="video/mp4",
                size=segment["size_bytes"],
            )
        )
        if segment.get("preview_url"):
            links.append(
                types.ResourceLink(
                    type="resource_link",
                    name=f"preview-{segment['index']}.jpg",
                    title=f"{title} 代表影格",
                    uri=segment["preview_url"],
                    description="影片分段的代表影格；當 host 不支援 MP4 上傳時供視覺核對。",
                    mimeType="image/jpeg",
                )
            )
    return types.CallToolResult(
        structuredContent=payload,
        content=[
            types.TextContent(
                type="text",
                text=(
                    f"已準備 {len(payload['segments'])} 個影片分段（{payload['duration_seconds']:.1f} 秒）。"
                    "請先完成檔案交接，再開始摘要；若 host 不支援檔案上傳，請回報尚未分析，不要只根據 metadata 宣稱完成。"
                ),
            ),
            *links,
        ],
        _meta={"job_id": payload["job_id"]},
    )


def tool_error_result(error: Exception) -> types.CallToolResult:
    detail = str(error).strip() or error.__class__.__name__
    return types.CallToolResult(
        isError=True,
        structuredContent={"status": "error", "error": detail},
        content=[
            types.TextContent(
                type="text",
                text=(
                    f"影片準備失敗：{detail}\n"
                    "尚未產生影片分段；請確認來源可由無登入的公開 yt-dlp 下載，"
                    "以及 MCP 服務目前仍在線。"
                ),
            )
        ],
    )


WIDGET_HTML = r"""<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Video timeline</title>
  <style>
    :root { color-scheme: light dark; font-family: system-ui, sans-serif; }
    body { margin: 0; padding: 16px; }
    main { display: grid; gap: 12px; }
    h2, p { margin: 0; }
    .muted { opacity: .72; font-size: .9rem; }
    button { border: 0; border-radius: 10px; padding: 10px 14px; cursor: pointer; font-weight: 650; }
    button:disabled { cursor: wait; opacity: .55; }
    ol { margin: 0; padding-left: 22px; display: grid; gap: 8px; }
    li { display: grid; gap: 4px; }
    a { overflow-wrap: anywhere; }
    img, video { width: 100%; max-height: 220px; border-radius: 10px; background: #111; object-fit: contain; }
    #status { min-height: 1.2em; }
  </style>
</head>
<body>
  <main>
    <h2 id="title">影片時間軸</h2>
    <p id="summary" class="muted">等待影片分段資料…</p>
    <button id="upload" type="button" disabled>上傳影片片段並摘要</button>
    <p id="status" role="status" aria-live="polite"></p>
    <ol id="segments"></ol>
  </main>
  <script>
    const openai = window.openai;
    let data = null;
    const $ = (id) => document.getElementById(id);
    const formatTime = (seconds) => {
      const value = Math.max(0, Number(seconds) || 0);
      const minutes = Math.floor(value / 60);
      const rest = Math.floor(value % 60).toString().padStart(2, "0");
      return `${minutes}:${rest}`;
    };
    const normalize = (value) => value?.structuredContent || value || {};
    const setStatus = (value) => { $("status").textContent = value; };

    function render(value) {
      data = normalize(value);
      const segments = Array.isArray(data.segments) ? data.segments : [];
      $("title").textContent = data.title || "影片時間軸";
      $("summary").textContent = segments.length
        ? `${formatTime(data.duration_seconds)} · ${segments.length} 段 · 每段約 ${data.segment_seconds} 秒`
        : "尚未取得影片分段。";
      $("upload").disabled = !segments.length;
      const list = $("segments");
      list.replaceChildren();
      for (const segment of segments) {
        const item = document.createElement("li");
        const label = document.createElement("span");
        label.textContent = `${formatTime(segment.start_seconds)}–${formatTime(segment.end_seconds)} · ${segment.filename}`;
        item.append(label);
        if (segment.preview_url) {
          const image = document.createElement("img");
          image.alt = `代表影格：${segment.filename}`;
          image.loading = "lazy";
          image.src = segment.preview_url;
          item.append(image);
        }
        const link = document.createElement("a");
        link.href = segment.url;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = "開啟分段";
        item.append(link);
        list.append(item);
      }
    }

    async function upload(url, name, type) {
      const response = await fetch(url, { credentials: "omit" });
      if (!response.ok) throw new Error(`下載 ${name} 失敗（HTTP ${response.status}）`);
      const blob = await response.blob();
      const result = await openai.uploadFile(new File([blob], name, { type }), { library: false });
      const fileId = result?.fileId || result?.file_id;
      if (!fileId) throw new Error(`${name} 沒有取得 file_id`);
      const download = await openai.getFileDownloadUrl({ fileId });
      const downloadUrl = download?.downloadUrl || download?.download_url;
      if (!downloadUrl) throw new Error(`${name} 沒有取得 download_url`);
      return { file_id: fileId, download_url: downloadUrl, mime_type: type, file_name: name };
    }

    async function uploadAndAsk() {
      if (!data?.segments?.length) return;
      if (!openai?.uploadFile || !openai?.getFileDownloadUrl || !openai?.callTool) {
        setStatus("此 host 不支援完整檔案交接（需要 uploadFile、getFileDownloadUrl、callTool）；尚未開始摘要。\n");
        return;
      }
      $("upload").disabled = true;
      const files = [];
      const failures = [];
      for (let index = 0; index < data.segments.length; index += 1) {
        const segment = data.segments[index];
        setStatus(`上傳影片片段 ${index + 1}/${data.segments.length}…`);
        try {
          files.push(await upload(segment.url, segment.filename, segment.mime_type || "video/mp4"));
        } catch (error) {
          failures.push(`${segment.filename}: ${error.message}`);
        }
      }
      if (failures.length) {
        setStatus(`檔案交接失敗：${failures.join("；")}；尚未開始摘要。`);
        $("upload").disabled = false;
        return;
      }
      setStatus(`正在確認 ${files.length} 個 ChatGPT 檔案引用…`);
      const handoff = normalize(await openai.callTool("ingest_video_segments", { files }));
      if (handoff.status !== "files_received" || Number(handoff.files_received) !== files.length) {
        throw new Error("MCP 未確認所有影片檔案引用");
      }
      const videoIds = files.map((file) => file.file_id);
      const modelContent = [
        `影片「${data.title || "未命名"}」已由 video-timeline-pipeline 分成 ${data.segments.length} 段。`,
        `檔案交接工具已確認 ${files.length} 個影片檔案。`,
        "請先讀取所有影片附件，再依每段的時間範圍整理重點、關鍵數字、結論與不確定處；不要把未讀取的內容猜成事實。",
      ].join("\n");
      openai.setWidgetState?.({
        modelContent,
        privateContent: { jobId: data.job_id, videoIds },
      });
      const prompt = `${modelContent}\n請現在直接讀取檔案交接工具提供的影片分段，並以繁體中文摘要。每個重點附上影片時間範圍；若某段無法讀取，明確標示。`;
      if (openai.sendFollowUpMessage) {
        await openai.sendFollowUpMessage({ prompt });
        setStatus(`已確認 ${files.length} 個檔案，已請 ChatGPT 讀取影片並開始摘要。`);
      } else {
        setStatus(`已確認 ${files.length} 個檔案；此 host 沒有自動追問功能，尚未開始摘要。`);
      }
      $("upload").disabled = false;
    }

    $("upload").addEventListener("click", () => uploadAndAsk().catch((error) => {
      setStatus(`處理失敗：${error.message}；尚未開始摘要。`);
      $("upload").disabled = false;
    }));
    render(openai?.toolOutput);
    window.addEventListener("message", (event) => {
      if (event.source !== window.parent) return;
      const message = event.data;
      if (message?.jsonrpc === "2.0" && message.method === "ui/notifications/tool-result") {
        render(message.params);
      }
    }, { passive: true });
  </script>
</body>
</html>"""


mcp = FastMCP(
    "video-timeline-pipeline",
    instructions=(
        "Use prepare_video_for_chatgpt when the user gives a public video URL and wants a summary. "
        "The tool downloads and segments the video; the ChatGPT widget then uploads the segments. "
        "Do not claim that a video was analyzed until the widget upload and model response succeed."
    ),
    host=MCP_HOST,
    port=MCP_PORT,
    streamable_http_path="/mcp",
    transport_security=TRANSPORT_SECURITY,
)


@mcp.resource(
    UI_URI,
    name="video_timeline_widget",
    title="Video timeline uploader",
    description="Upload prepared video segments to ChatGPT for summarization.",
    mime_type="text/html;profile=mcp-app",
    meta=UI_META,
)
def video_timeline_widget() -> str:
    return WIDGET_HTML


@mcp.tool(
    name="prepare_video_for_chatgpt",
    title="Prepare video for ChatGPT",
    description=(
        "Use this when the user provides a public video URL and wants ChatGPT to summarize its content. "
        "Download the single video, split it into bounded MP4 segments, and return segment links for the "
        "ChatGPT widget to upload. Supports public http(s) URLs accepted by yt-dlp; does not analyze the "
        "video by itself."
    ),
    annotations=types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    meta=TOOL_META,
    structured_output=True,
)
async def prepare_video_for_chatgpt(
    url: str,
    segment_seconds: int = 120,
    max_segments: int | None = None,
) -> Annotated[types.CallToolResult, PreparedVideoResult]:
    try:
        manifest = await asyncio.to_thread(prepare_video_locked, url, segment_seconds, max_segments)
    except Exception as error:
        return tool_error_result(error)
    return tool_result(manifest)


@mcp.tool(
    name="ingest_video_segments",
    title="Receive video segments for analysis",
    description=(
        "Call this after prepare_video_for_chatgpt when ChatGPT has uploaded the MP4 segments. "
        "Pass the files in the same order as the prepared segment list. "
        "Some ChatGPT web hosts pass an approved MCP resource-link URL string instead of the full "
        "OpenAI file object; accept that URL as the file reference and do not reject it only for its type. "
        "This tool only confirms file references; read the attached files before making any visual "
        "or audio claims, and fail closed when the references are missing."
    ),
    annotations=types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    meta=FILE_HANDOFF_META,
    structured_output=True,
)
async def ingest_video_segments(
    files: list[OpenAIFileReference | str],
) -> FileHandoffResult:
    references = normalize_file_references(
        [OpenAIFileReference.model_validate(item).model_dump() for item in files]
    )
    return FileHandoffResult(
        status="files_received",
        files_received=len(references),
        files=[
            VerifiedFileSummary(
                file_id=item["file_id"],
                mime_type=item.get("mime_type"),
                file_name=item.get("file_name"),
            )
            for item in references
        ],
    )


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "video-timeline-pipeline"})


@mcp.custom_route("/media/{job_id}/{filename:path}", methods=["GET", "HEAD"], include_in_schema=False)
async def media(request: Request):
    relative_name = request.path_params["filename"]
    path = safe_media_path(request.path_params["job_id"], relative_name)
    if path is None:
        return PlainTextResponse("not found", status_code=404)
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    response = FileResponse(path, media_type=media_type)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    response.headers["Cache-Control"] = "public, max-age=3600"
    response.headers["Content-Disposition"] = f'attachment; filename="{path.name}"'
    return response


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
