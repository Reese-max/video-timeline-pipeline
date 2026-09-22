from __future__ import annotations

import asyncio
import csv
import ipaddress
import json
import math
import mimetypes
import os
import re
import shutil
import socket
import threading
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import pipeline
import drive_storage
from mcp import types
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict
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
MCP_MIN_FREE_BYTES = max(0, env_int("MCP_MIN_FREE_MB", 1024)) * 1024 * 1024
MCP_PUBLIC_BASE_URL = (
    os.getenv("MCP_PUBLIC_BASE_URL", "") or f"http://{MCP_HOST}:{MCP_PORT}"
).rstrip("/")
MCP_MEDIA_BASE_URL = (
    os.getenv("MCP_MEDIA_BASE_URL", "") or MCP_PUBLIC_BASE_URL
).rstrip("/")
UI_URI = "ui://video-timeline-pipeline/v8.html"
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
ANALYSIS_META = {
    "ui": {"visibility": ["model", "app"]},
    "openai/widgetAccessible": True,
    "openai/toolInvocation/invoking": "正在用 FFmpeg、Groq 與 MiniMax 分析影片…",
    "openai/toolInvocation/invoked": "影片分析完成",
}
DRIVE_META = {
    "ui": {"visibility": ["model", "app"]},
    "openai/widgetAccessible": True,
    "openai/toolInvocation/invoking": "正在把影片分段存到 Google Drive…",
    "openai/toolInvocation/invoked": "影片分段已存到 Google Drive",
}


class OpenAIFileReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    download_url: str
    file_id: str
    mime_type: str = ""
    file_name: str = ""


class VerifiedFileSummary(BaseModel):
    file_id: str
    mime_type: str | None = None
    file_name: str | None = None


class FileHandoffResult(BaseModel):
    status: Literal["files_received"]
    files_received: int
    files: list[VerifiedFileSummary]


class PreparedVideoAnalysisResult(BaseModel):
    status: Literal["complete", "partial", "error"]
    job_id: str = ""
    title: str = ""
    source_url: str = ""
    duration_seconds: float = 0.0
    source_size_bytes: int = 0
    source_sha256: str = ""
    audio_status: str = "error"
    visual_status: str = "error"
    summary: dict[str, Any] = {}
    transcript: dict[str, Any] = {}
    timeline: list[dict[str, Any]] = []
    errors: list[str] = []
    error: str | None = None


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


class DriveFileResult(BaseModel):
    file_id: str
    url: str
    file_name: str
    mime_type: str
    size_bytes: int
    sha256: str


class DriveUploadResult(BaseModel):
    status: Literal["drive_ready", "error"]
    job_id: str = ""
    folder_id: str = ""
    folder_url: str = ""
    files: list[DriveFileResult] = []
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
    if MCP_MAX_SEGMENTS > 0:
        max_segments = MCP_MAX_SEGMENTS if max_segments is None else min(max_segments, MCP_MAX_SEGMENTS)
    if duration <= 0:
        raise RuntimeError("影片沒有可用的長度資訊。")
    count = math.ceil(duration / segment_seconds)
    if max_segments is not None and count > max_segments:
        raise ValueError(
            f"影片長度 {duration:.0f} 秒需要 {count} 段，超過 max_segments={max_segments}；"
            "請提高 segment_seconds、設定較高的 max_segments，或省略 max_segments。"
            "省略參數仍受 MCP_MAX_SEGMENTS 伺服器上限限制。"
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
    if directory == "segments" and not re.fullmatch(r"segment_\d{3,}\.mp4", filename):
        return None
    if directory == "previews" and not re.fullmatch(r"preview_\d{3,}\.jpg", filename):
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
    if manifest.get("version") != 2:
        return False
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


def split_video(video_path: Path, segment_dir: Path, segment_seconds: int) -> list[tuple[Path, float, float]]:
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
            "-segment_list",
            str(segment_dir / "segments.csv"),
            "-segment_list_type",
            "csv",
            "-avoid_negative_ts",
            "disabled",
            "-reset_timestamps",
            "1",
            str(segment_dir / "segment_%03d.mp4"),
        ],
        stream=True,
    )
    segments = sorted(segment_dir.glob("segment_*.mp4"), key=lambda p: int(p.stem.split("_")[-1]))
    if not segments:
        raise RuntimeError("FFmpeg 沒有產生影片片段。")
    rows = list(csv.reader((segment_dir / "segments.csv").read_text(encoding="utf-8-sig").splitlines()))
    if len(rows) != len(segments):
        raise RuntimeError("FFmpeg 影片分段與實際時間清單數量不一致。")
    result = []
    previous_end = 0.0
    for path, row in zip(segments, rows, strict=True):
        if len(row) != 3 or Path(row[0]).name != path.name:
            raise RuntimeError("FFmpeg 影片分段時間清單格式錯誤。")
        start, end = float(row[1]), float(row[2])
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end
                and start >= previous_end - 0.001):
            raise RuntimeError("FFmpeg 影片分段時間無效或重疊。")
        result.append((path, start, end))
        previous_end = end
    return result


def require_disk_space(additional_bytes: int = 0) -> None:
    MCP_JOBS_ROOT.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(MCP_JOBS_ROOT).free
    required = MCP_MIN_FREE_BYTES + additional_bytes
    if free < required:
        raise RuntimeError(f"MCP 磁碟空間不足：剩餘 {free // (1024 * 1024)} MiB，"
                           f"至少需要 {required // (1024 * 1024)} MiB；請先整理舊工作，本工具不會自動刪除影片。")


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
    if MCP_MAX_SEGMENTS > 0 and max_segments is None:
        max_segments = MCP_MAX_SEGMENTS
    job_id = pipeline.job_key(source_url, f"mcp-video-v1:{segment_seconds}:{max_segments}")
    job_dir = MCP_JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    try:
        video_path = pipeline.locate_source_video(job_dir)
    except RuntimeError:
        require_disk_space(MCP_MAX_SOURCE_BYTES)
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
    segment_bounds(duration, segment_seconds, max_segments)
    existing = read_manifest(job_dir)
    if existing and manifest_is_ready(existing, video_path, segment_seconds, max_segments):
        return existing

    require_disk_space(video_path.stat().st_size)
    pipeline.write_json(job_dir / "manifest.json", {"version": 2, "status": "preparing", "job_id": job_id})
    segments = split_video(video_path, job_dir / "segments", segment_seconds)
    if max_segments is not None and len(segments) > max_segments:
        raise RuntimeError(f"FFmpeg 產生 {len(segments)} 段，超過 max_segments={max_segments}。")
    previews_dir = job_dir / "previews"
    segment_rows = []
    for index, (segment_path, start, end) in enumerate(segments):
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
        "version": 2,
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
    return run_job(prepare_video_sync, url, segment_seconds, max_segments)


def run_job(operation, *args):
    if not JOB_LOCK.acquire(blocking=False):
        raise RuntimeError("MCP 正在處理另一個影片工作，請稍後重試；本次未加入背景佇列。")
    try:
        return operation(*args)
    finally:
        JOB_LOCK.release()


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


def analysis_error(error: Exception) -> str:
    detail = str(error).strip() or error.__class__.__name__
    for secret_name in ("GROQ_API_KEY", "MINIMAX_SUBSCRIPTION_KEY"):
        secret = os.getenv(secret_name, "").strip()
        if secret:
            detail = detail.replace(secret, "[redacted]")
    return detail


def prepared_job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{12}", job_id):
        raise ValueError("job_id 格式無效。")
    job_dir = (MCP_JOBS_ROOT / job_id).resolve()
    try:
        job_dir.relative_to(MCP_JOBS_ROOT.resolve())
    except ValueError as exc:
        raise ValueError("job_id 不在 MCP 工作目錄內。") from exc
    if not job_dir.is_dir():
        raise FileNotFoundError(f"找不到 MCP 工作：{job_id}")
    return job_dir


def analyze_prepared_sync(job_id: str) -> dict[str, Any]:
    job_dir = prepared_job_dir(job_id)
    manifest = read_manifest(job_dir)
    if not manifest:
        raise RuntimeError("影片尚未準備完成，請先呼叫 prepare_video_for_chatgpt。")
    video_path = pipeline.locate_source_video(job_dir)
    source_fingerprint = pipeline.file_fingerprint(video_path)
    analysis_dir = job_dir / "analysis"
    result_path = analysis_dir / "result.json"
    cached = pipeline.reuse_artifact(result_path)
    if (
        isinstance(cached, dict)
        and cached.get("status") == "complete"
        and cached.get("source_fingerprint") == source_fingerprint
    ):
        return cached

    require_disk_space(video_path.stat().st_size)
    analysis_dir.mkdir(parents=True, exist_ok=True)
    probe = pipeline.probe_video(video_path, analysis_dir / "source.probe.json")
    duration = pipeline.video_duration(probe)
    has_audio = any(stream.get("codec_type") == "audio" for stream in probe.get("streams", []))
    model = pipeline.minimax_model()
    minimax_key = pipeline.minimax_subscription_key()
    # 短片常用快速卡片切換；長片仍 capped at 40，避免每秒抽幀無界增長。
    max_frames = min(40, max(8, math.ceil(max(0.0, duration))))
    hash_threshold = 0 if duration <= 40 else 1
    transcript: dict[str, Any] = {
        "provider": "none",
        "language": "",
        "segments": [],
    }
    errors: list[str] = []
    audio_status = "skipped" if not has_audio else "pending"
    visual_status = "pending"

    def visual_stage() -> list[dict[str, Any]]:
        candidates, _ = pipeline.extract_candidates(
            video_path,
            analysis_dir,
            1.0,
            hash_threshold,
            max_frames,
            False,
            0.75,
        )
        return pipeline.analyze_images(
            candidates,
            minimax_key,
            model,
            workers=4,
            cache_dir=analysis_dir / "vision-cache",
        )

    def transcript_stage() -> dict[str, Any]:
        chunks = pipeline.extract_audio(
            video_path,
            analysis_dir,
            max(15, int(manifest.get("segment_seconds") or 120)),
        )
        backend = os.getenv("TRANSCRIBE_BACKEND", "groq").strip().lower()
        language = os.getenv("GROQ_LANGUAGE", "auto")
        if backend == "local":
            return pipeline.transcribe_local(chunks, os.getenv("WHISPER_MODEL", "base"), language)
        groq_key = os.getenv("GROQ_API_KEY", "").strip()
        if not groq_key:
            raise RuntimeError("音訊存在但未設定 GROQ_API_KEY。")
        transcript_result, _ = pipeline.transcribe_all(
            chunks,
            groq_key,
            os.getenv("GROQ_ASR_MODEL", "whisper-large-v3"),
            language,
            2,
            analysis_dir / "transcription-cache",
        )
        return transcript_result

    if has_audio:
        transcript_box: dict[str, Any] = {}

        def safe_transcript() -> None:
            try:
                transcript_box["value"] = transcript_stage()
            except Exception as error:
                transcript_box["error"] = analysis_error(error)

        visual_box: dict[str, Any] = {}

        def safe_visual() -> None:
            try:
                visual_box["value"] = visual_stage()
            except Exception as error:
                visual_box["error"] = analysis_error(error)

        pipeline.run_parallel_stages(safe_transcript, safe_visual)
        if "value" in transcript_box:
            transcript = transcript_box["value"]
            audio_status = "complete"
        else:
            audio_status = "error"
            errors.append(f"音訊：{transcript_box.get('error', '未知錯誤')}")
        observations = visual_box.get("value") or []
        if "value" in visual_box:
            visual_status = "complete"
        else:
            visual_status = "error"
            errors.append(f"畫面：{visual_box.get('error', '未知錯誤')}")
    else:
        try:
            observations = visual_stage()
            visual_status = "complete"
        except Exception as error:
            observations = []
            visual_status = "error"
            errors.append(f"畫面：{analysis_error(error)}")

    timeline = pipeline.merge_timeline(transcript, observations)
    if timeline:
        try:
            summary = pipeline.create_summary(
                timeline,
                minimax_key,
                model,
                120000,
                analysis_dir / "summary-cache",
                2,
            )
            summary_status = "complete"
        except Exception as error:
            summary = {"status": "error", "reason": analysis_error(error)}
            summary_status = "error"
            errors.append(f"摘要：{analysis_error(error)}")
    else:
        summary = {"status": "skipped", "reason": "沒有可供摘要的音訊或畫面事件。"}
        summary_status = "skipped"

    overall = (
        "complete"
        if visual_status == "complete"
        and audio_status in {"complete", "skipped"}
        and summary_status == "complete"
        else "partial"
        if timeline
        else "error"
    )
    result = {
        "status": overall,
        "job_id": job_id,
        "title": str((manifest.get("source") or {}).get("title") or video_path.stem),
        "source_url": str((manifest.get("source") or {}).get("url") or ""),
        "duration_seconds": round(duration, 3),
        "source_size_bytes": video_path.stat().st_size,
        "source_sha256": pipeline.file_sha256(video_path),
        "source_fingerprint": source_fingerprint,
        "audio_status": audio_status,
        "visual_status": visual_status,
        "summary": summary,
        "transcript": transcript,
        "timeline": timeline,
        "errors": errors,
    }
    if overall == "complete":
        pipeline.write_json(result_path, result)
    return result


def analysis_tool_result(result: dict[str, Any]) -> types.CallToolResult:
    status = str(result.get("status") or "error")
    summary = result.get("summary") or {}
    headline = str(summary.get("short_summary") or "沒有產生摘要。")
    text = (
        f"MCP 已實際讀取準備好的 MP4（{result.get('source_size_bytes', 0)} bytes；"
        f"SHA-256 {result.get('source_sha256', '')}）。"
        f"伺服器端畫面={result.get('visual_status')}、音訊={result.get('audio_status')}、"
        f"摘要={status}。\n{headline}"
    )
    if result.get("errors"):
        text += "\n" + "；".join(str(item) for item in result["errors"])
    return types.CallToolResult(
        isError=status == "error",
        structuredContent=result,
        content=[types.TextContent(type="text", text=text)],
    )


def analysis_tool_error_result(error: Exception) -> types.CallToolResult:
    detail = analysis_error(error)
    return types.CallToolResult(
        isError=True,
        structuredContent={"status": "error", "error": detail},
        content=[types.TextContent(type="text", text=f"影片分析失敗：{detail}")],
    )


def drive_tool_result(result: dict[str, Any]) -> types.CallToolResult:
    files = result.get("files") or []
    # Drive view URLs are authenticated HTML, not materializable MP4 resources.
    return types.CallToolResult(
        structuredContent=result,
        content=[
            types.TextContent(
                type="text",
                text=(
                    f"已將 {len(files)} 個 MP4 分段存到 Google Drive。"
                    "這些是 Drive 檔案引用，不是 ChatGPT 原生附件；"
                    "files[].url 是檢視頁，不是 MP4 下載網址。"
                    "請用 Google Drive connector 對各網址執行 fetch"
                    "（download_raw_file=true、include_base64=false），取得原始檔後再摘要。"
                ),
            ),
        ],
    )


def drive_tool_error_result(error: Exception) -> types.CallToolResult:
    detail = str(error).strip() or error.__class__.__name__
    return types.CallToolResult(
        isError=True,
        structuredContent={"status": "error", "error": detail},
        content=[types.TextContent(type="text", text=f"Google Drive 交接失敗：{detail}")],
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
    const getOpenAI = () => window.openai;
    const pendingRequests = new Map();
    let nextRequestId = 1;
    let data = null;
    let jobVersion = 0;
    let running = false;
    let startedJobId = null;
    let bridgePromise = null;
    const $ = (id) => document.getElementById(id);
    const formatTime = (seconds) => {
      const value = Math.max(0, Number(seconds) || 0);
      const minutes = Math.floor(value / 60);
      const rest = Math.floor(value % 60).toString().padStart(2, "0");
      return `${minutes}:${rest}`;
    };
    const normalize = (value) => value?.structuredContent || value || {};
    const setStatus = (value) => { $("status").textContent = value; };
    const checkJob = (version) => {
      if (version !== jobVersion) throw new Error("影片工作已切換，忽略舊交接結果。");
    };

    function request(method, params) {
      const id = nextRequestId++;
      return new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          pendingRequests.delete(id);
          reject(new Error(`${method} bridge timeout`));
        }, method === "tools/call" ? 120000 : 15000);
        pendingRequests.set(id, { resolve, reject, timer });
        window.parent.postMessage({ jsonrpc: "2.0", id, method, params }, "*");
      });
    }

    async function initializeBridge() {
      const result = await request("ui/initialize", {
        protocolVersion: "2026-01-26",
        appCapabilities: {},
        clientInfo: { name: "video-timeline-pipeline", version: "8" },
      });
      window.parent.postMessage({
        jsonrpc: "2.0",
        method: "ui/notifications/initialized",
        params: {},
      }, "*");
      return result;
    }

    function ensureBridge() {
      bridgePromise ||= initializeBridge();
      return bridgePromise;
    }

    async function callTool(name, args, version) {
      const host = getOpenAI();
      checkJob(version);
      return host?.callTool
        ? host.callTool(name, args)
        : ensureBridge().then(() => {
          checkJob(version);
          return request("tools/call", { name, arguments: args });
        });
    }

    async function sendFollowUp(prompt, version) {
      const host = getOpenAI();
      checkJob(version);
      return host?.sendFollowUpMessage
        ? host.sendFollowUpMessage({ prompt })
        : ensureBridge().then(() => {
          checkJob(version);
          return request("ui/message", { content: [{ type: "text", text: prompt }] });
        });
    }

    async function waitForOpenAI(timeout = 5000) {
      const deadline = Date.now() + timeout;
      while (!getOpenAI() && Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 100));
      }
      return getOpenAI();
    }

    function render(value) {
      const next = normalize(value);
      // Handoff results are data-only; they are not a newly prepared video.
      if (["drive_ready", "files_received"].includes(next.status)) return false;
      if (!data || next.job_id !== data.job_id) {
        jobVersion += 1;
        startedJobId = null;
        getOpenAI()?.setWidgetState?.({
          modelContent: next.job_id
            ? `目前影片「${next.title || "未命名"}」（job_id=${next.job_id}）尚待檔案交接；請勿沿用先前影片或檔案。`
            : "尚未取得目前影片的檔案交接資訊；請勿沿用先前影片或檔案。",
          privateContent: { jobId: next.job_id || null },
        });
        setStatus("");
      }
      data = next;
      const segments = Array.isArray(data.segments) ? data.segments : [];
      $("title").textContent = data.title || "影片時間軸";
      $("summary").textContent = segments.length
        ? `${formatTime(data.duration_seconds)} · ${segments.length} 段 · 每段約 ${data.segment_seconds} 秒`
        : "尚未取得影片分段。";
      $("upload").disabled = !segments.length || running;
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
      return true;
    }

    async function upload(host, url, name, type, version) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 30000);
      let blob;
      try {
        const response = await fetch(url, { credentials: "omit", signal: controller.signal });
        checkJob(version);
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        blob = await response.blob();
      } catch (error) {
        throw new Error(`下載 ${name} 失敗：${error.name === "AbortError" ? "逾時" : error.message}`);
      } finally {
        clearTimeout(timer);
      }
      checkJob(version);
      const result = await host.uploadFile(new File([blob], name, { type }), { library: false });
      checkJob(version);
      const fileId = result?.fileId || result?.file_id;
      if (!fileId) throw new Error(`${name} 沒有取得 file_id`);
      const download = await host.getFileDownloadUrl({ fileId });
      checkJob(version);
      const downloadUrl = download?.downloadUrl || download?.download_url;
      if (!downloadUrl) throw new Error(`${name} 沒有取得 download_url`);
      return { file_id: fileId, download_url: downloadUrl, mime_type: type, file_name: name };
    }

    async function driveAndAsk(job, version) {
      checkJob(version);
      setStatus("正在把所有 MP4 分段存到 Google Drive，完成後交由 connector 讀取…");
      const drive = normalize(await callTool("upload_prepared_video_to_drive", { job_id: job.job_id }, version));
      checkJob(version);
      if (drive.status !== "drive_ready" || !Array.isArray(drive.files) || drive.files.length !== job.segments.length) {
        throw new Error(drive.error || "Google Drive 沒有確認所有 MP4 分段");
      }
      if (drive.job_id !== job.job_id) throw new Error("Google Drive 回傳工作不符，停止檔案交接。");
      const entries = drive.files.map((file, index) =>
        `${index + 1}. ${file.file_name} | file_id=${file.file_id} | ${file.size_bytes} bytes | ${file.url}`
      ).join("\n");
      const modelContent = [
        `影片「${job.title || "未命名"}」的 ${drive.files.length} 個 MP4 分段已由 MCP 存到 Google Drive。`,
        "Drive 檔案清單：",
        entries,
      ].join("\n");
      getOpenAI()?.setWidgetState?.({
        modelContent,
        privateContent: { jobId: job.job_id, driveFileIds: drive.files.map((file) => file.file_id) },
      });
      await sendFollowUp([
        modelContent,
        "請使用已連線的 Google Drive connector，對上列每個 Drive 檔案執行原始檔 fetch（download_raw_file=true、include_base64=false）。",
        "確認 connector 回傳 video/mp4 的實際檔案引用後，再逐一讀取影片畫面與音訊並摘要。",
        "不要把 Drive URL、檔名或 metadata 當成影片內容；若任何分段無法取得原始檔，請明確標示。",
      ].join("\n"), version);
      checkJob(version);
      setStatus(`已將 ${drive.files.length} 個 MP4 存到 Google Drive，已請 ChatGPT 透過 Drive connector 讀取。`);
    }

    async function uploadAndAsk(job, version) {
      $("upload").disabled = true;
      const host = await waitForOpenAI();
      checkJob(version);
      if (!host?.uploadFile || !host?.getFileDownloadUrl) {
        await driveAndAsk(job, version);
        return;
      }
      $("upload").disabled = true;
      const files = [];
      const failures = [];
      for (let index = 0; index < job.segments.length; index += 1) {
        const segment = job.segments[index];
        setStatus(`上傳影片片段 ${index + 1}/${job.segments.length}…`);
        try {
          files.push(await upload(host, segment.url, segment.filename, segment.mime_type || "video/mp4", version));
          checkJob(version);
        } catch (error) {
          checkJob(version);
          failures.push(`${segment.filename}: ${error.message}`);
        }
      }
      if (failures.length) {
        try {
          await driveAndAsk(job, version);
        } catch (driveError) {
          throw new Error(`原生附件交接失敗：${failures.join("；")}；Google Drive 交接失敗：${driveError.message}`);
        }
        return;
      }
      setStatus(`正在確認 ${files.length} 個 ChatGPT 檔案引用…`);
      const handoff = normalize(await callTool("ingest_video_segments", { files }, version));
      checkJob(version);
      if (handoff.status !== "files_received" || Number(handoff.files_received) !== files.length) {
        throw new Error("MCP 未確認所有影片檔案引用");
      }
      const videoIds = files.map((file) => file.file_id);
      const modelContent = [
        `影片「${job.title || "未命名"}」已由 video-timeline-pipeline 分成 ${job.segments.length} 段。`,
        `檔案交接工具已確認 ${files.length} 個影片檔案。`,
        "請先讀取所有影片附件，再依每段的時間範圍整理重點、關鍵數字、結論與不確定處；不要把未讀取的內容猜成事實。",
      ].join("\n");
      host.setWidgetState?.({
        modelContent,
        privateContent: { jobId: job.job_id, videoIds },
      });
      const prompt = `${modelContent}\n請現在直接讀取檔案交接工具提供的影片分段，並以繁體中文摘要。每個重點附上影片時間範圍；若某段無法讀取，明確標示。`;
      await sendFollowUp(prompt, version);
      checkJob(version);
      setStatus(`已確認 ${files.length} 個檔案，已請 ChatGPT 讀取影片並開始摘要。`);
      $("upload").disabled = false;
    }

    async function startUpload() {
      if (!data?.segments?.length || running || startedJobId === data.job_id) return;
      const job = data;
      const version = jobVersion;
      running = true;
      startedJobId = job.job_id;
      try {
        await uploadAndAsk(job, version);
      } catch (error) {
        if (version !== jobVersion) return;
        startedJobId = null;
        setStatus(`處理失敗：${error.message}；尚未開始摘要。`);
        $("upload").disabled = false;
      } finally {
        running = false;
        // ponytail: submitted uploads may finish; remote cancellation needs server support.
        if (version !== jobVersion) queueMicrotask(startUpload);
      }
    }

    $("upload").addEventListener("click", () => startUpload());
    window.addEventListener("message", (event) => {
      if (event.source !== window.parent) return;
      const message = event.data;
      if (message?.jsonrpc === "2.0" && message.id !== undefined && pendingRequests.has(message.id)) {
        const pending = pendingRequests.get(message.id);
        pendingRequests.delete(message.id);
        clearTimeout(pending.timer);
        if (message.error) pending.reject(new Error(message.error.message || "MCP bridge error"));
        else pending.resolve(message.result);
        return;
      }
      if (message?.jsonrpc === "2.0" && message.method === "ui/notifications/tool-result") {
        if (render(message.params)) queueMicrotask(startUpload);
      }
    }, { passive: true });
    ensureBridge().catch(() => {});
    render(getOpenAI()?.toolOutput);
    queueMicrotask(startUpload);
  </script>
</body>
</html>"""


mcp = FastMCP(
    "video-timeline-pipeline",
    instructions=(
        "Use prepare_video_for_chatgpt when the user gives a public video URL and wants a summary. "
        "The tool downloads and segments the video; the ChatGPT widget then uploads the segments. "
        "If native file download or upload is unavailable or fails, the widget tries the configured "
        "Google Drive handoff and asks the Google Drive connector to fetch the raw MP4s. "
        "If Drive fails, stop and report the error; do not substitute server-side analysis. "
        "Do not claim that ChatGPT itself read the MP4 unless the file handoff and model response succeed."
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
    name="analyze_prepared_video",
    title="Analyze prepared video",
    description=(
        "Use this after prepare_video_for_chatgpt when the ChatGPT host cannot upload MP4 files. "
        "It reads the prepared local MP4 bytes on the MCP host, uses FFmpeg to extract audio and "
        "frames, Groq or faster-whisper for speech, and MiniMax (including MiniMax-M3 when configured) "
        "for visual analysis and the final timeline summary. The result is a server-side analysis; "
        "do not describe it as ChatGPT directly reading the video attachment."
    ),
    annotations=types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    meta=ANALYSIS_META,
    structured_output=True,
)
async def analyze_prepared_video(
    job_id: str,
) -> Annotated[types.CallToolResult, PreparedVideoAnalysisResult]:
    try:
        result = await asyncio.to_thread(run_job, analyze_prepared_sync, job_id)
    except Exception as error:
        return analysis_tool_error_result(error)
    return analysis_tool_result(result)


@mcp.tool(
    name="upload_prepared_video_to_drive",
    title="Store prepared video in Google Drive",
    description=(
        "Use this when prepare_video_for_chatgpt has produced MP4 segments but the ChatGPT host "
        "cannot download or upload the native attachments, or the user requests Drive. "
        "Upload the actual prepared MP4 bytes to a configured private "
        "Google Drive folder and return observed Drive file IDs and view URLs. The model must then "
        "use the connected Google Drive connector to fetch each raw MP4 before analyzing it. "
        "The returned view URLs are metadata, not directly downloadable MP4 attachments. A Drive "
        "URL alone is never treated as proof that ChatGPT received or read the video bytes."
    ),
    annotations=types.ToolAnnotations(
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=True,
    ),
    meta=DRIVE_META,
    structured_output=True,
)
async def upload_prepared_video_to_drive(
    job_id: str,
) -> Annotated[types.CallToolResult, DriveUploadResult]:
    try:
        result = await asyncio.to_thread(run_job, upload_prepared_drive_sync, job_id)
    except Exception as error:
        return drive_tool_error_result(error)
    return drive_tool_result(result)


def upload_prepared_drive_sync(job_id: str) -> dict:
    job_dir = prepared_job_dir(job_id)
    manifest = read_manifest(job_dir)
    if not manifest:
        raise RuntimeError("影片尚未準備完成，請先呼叫 prepare_video_for_chatgpt。")
    source = manifest.get("source") or {}
    return drive_storage.upload_prepared_segments(
        job_dir, job_id, str(source.get("title") or job_id),
        str(source.get("fingerprint") or ""), list(manifest.get("segments") or []))


@mcp.tool(
    name="ingest_video_segments",
    title="Receive video segments for analysis",
    description=(
        "Call this after prepare_video_for_chatgpt when ChatGPT has uploaded the MP4 segments. "
        "Pass the files in the same order as the prepared segment list. "
        "Each item must be the complete OpenAI file object containing file_id and download_url. "
        "A public segment URL or MCP resource link alone is not proof that ChatGPT received the MP4 "
        "bytes and must be rejected. This tool only confirms file references; read the attached files "
        "before making any visual or audio claims, and fail closed when the references are missing."
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
    files: list[OpenAIFileReference],
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
