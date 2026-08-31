from __future__ import annotations

import argparse
import array
import base64
import concurrent.futures
import difflib
import hashlib
import importlib.util
import io
import json
import math
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}


def load_dotenv(path: Path) -> None:
    """專案 .env 覆蓋繼承來的環境變數。

    setdefault 會讓全域環境變數贏過專案設定：像 MINIMAX_BASE_URL 這種通用名稱
    常被別的工具設成不相容的值（例如 .../anthropic），照著 README 建 .env 也修不掉。
    只有非空值才覆蓋，這樣直接複製 .env.example 不會把既有金鑰清成空字串。
    """
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and value:
            os.environ[key] = value


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path: Path, data: Any) -> None:
    content = json.dumps(data, ensure_ascii=False, indent=2)
    # 內容沒變就別動檔案：無條件覆寫會讓 mtime 前進，
    # 下游靠時間戳判斷「digest 是否過期」就會被誤導成要重跑。
    if path.is_file():
        try:
            if path.read_text(encoding="utf-8") == content:
                return
        except OSError:
            pass
    write_text(path, content)


def run_external(command: list[str], *, cwd: Path | None = None, stream: bool = False) -> str:
    print("$ " + subprocess.list2cmdline(command))
    if stream:
        # 下載／轉檔動輒數十分鐘，捕捉輸出等於全程無聲，看不出是在跑還是卡死。
        # 直接讓子行程寫到終端機；失敗時錯誤已經印出來了，不必再回傳 detail。
        sys.stdout.flush()
        completed = subprocess.run(command, cwd=cwd)
        if completed.returncode:
            raise RuntimeError(f"外部指令失敗（{completed.returncode}）：{command[0]}")
        return ""
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"外部指令失敗（{completed.returncode}）：{detail[-2000:]}")
    return completed.stdout



def run_external_capture(command: list[str], *, cwd: Path | None = None) -> tuple[str, str]:
    """執行外部指令並同時保留 stdout／stderr；抽幀需要從 showinfo 讀回原始 PTS。"""
    print("$ " + subprocess.list2cmdline(command))
    completed = subprocess.run(
        command,
        cwd=cwd,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"外部指令失敗（{completed.returncode}）：{detail[-2000:]}")
    return completed.stdout, completed.stderr

def require_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise RuntimeError(f"找不到 {name}，請先安裝並加入 PATH。")
    return path


def yt_dlp_command() -> list[str]:
    executable = shutil.which("yt-dlp")
    if executable:
        return [executable]
    if importlib.util.find_spec("yt_dlp"):
        return [sys.executable, "-m", "yt_dlp"]
    raise RuntimeError("找不到 yt-dlp；請執行 `python -m pip install yt-dlp`。")


def job_key(source: str, variant: str = "") -> str:
    material = source + "\x00" + variant
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:12]



PIPELINE_SCHEMA_VERSION = 2
ARTIFACT_SCHEMA_VERSION = 2
FRAME_EXTRACTOR_VERSION = "native-pts-scene-v2"
AUDIO_EXTRACTOR_VERSION = "audio-window-v2"
CAPTION_EXTRACTOR_VERSION = "youtube-caption-v2"
CAPTION_MERGE_CHARS = 320
CAPTION_MERGE_GAP = 2.0
PROMPT_VERSIONS = {
    "visual": "2026-08-31.visual-v2",
    "visual_review": "2026-08-31.visual-review-v1",
    "summary": "2026-08-31.summary-v2",
    "summary_chunk": "2026-08-31.summary-chunk-v1",
    "keywords": "2026-08-31.keywords-v1",
    "answer": "2026-08-31.answer-v1",
}
SUMMARY_CHUNK_CHARS = 180000
RRF_K = 60
SNIPPET_WIDTH = 40


class ArtifactValidationError(RuntimeError):
    """產物未通過結構、時間或來源驗證。"""


def read_json_file(path: Path) -> Any | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest_value(value: Any, length: int = 24) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()[:length]


def source_metadata_fingerprint(info: dict[str, Any], fallback: str) -> str:
    material = {
        "source": info.get("webpage_url") or info.get("original_url") or fallback,
        "id": info.get("id"),
        "duration": info.get("duration"),
        "language": info.get("language"),
        "upload_date": info.get("upload_date"),
        "timestamp": info.get("timestamp"),
    }
    return digest_value(material)


def file_fingerprint(path: Path, cache_path: Path | None = None) -> str:
    """用檔案內容指紋判斷本機影片是否真的變了；相同 stat 直接重用已算過的 hash。"""
    stat = path.stat()
    if cache_path:
        cached = read_json_file(cache_path)
        if (
            isinstance(cached, dict)
            and cached.get("size") == stat.st_size
            and cached.get("mtime_ns") == stat.st_mtime_ns
            and cached.get("sha256")
        ):
            return str(cached["sha256"])
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    result = digest.hexdigest()
    if cache_path:
        write_json(
            cache_path,
            {"schema_version": ARTIFACT_SCHEMA_VERSION, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns, "sha256": result},
        )
    return result


def stage_signature(
    stage: str,
    source_fingerprint: str,
    settings: dict[str, Any] | None = None,
    dependencies: list[str] | None = None,
) -> str:
    return digest_value(
        {
            "schema_version": PIPELINE_SCHEMA_VERSION,
            "stage": stage,
            "source_fingerprint": source_fingerprint,
            "settings": settings or {},
            "dependencies": dependencies or [],
        }
    )


def stage_cache_value(
    previous_stages: dict[str, Any],
    stage: str,
    path: Path,
    signature: str,
    validator: Any = None,
    refresh: bool = False,
) -> Any | None:
    """只有階段簽章、檔案內容與驗證都通過才重用，與整支影片完成狀態脫鉤。"""
    if refresh:
        return None
    record = previous_stages.get(stage) or {}
    if record.get("signature") != signature or record.get("status") != "completed":
        return None
    value = read_json_file(path)
    if value is None:
        return None
    if validator is not None and validator(value):
        return None
    return value


def write_stage_part(path: Path, stage: str, signature: str, result: Any, **metadata: Any) -> None:
    write_json(
        path,
        {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "stage": stage,
            "signature": signature,
            "result": result,
            **metadata,
        },
    )


def read_stage_part(path: Path, stage: str, signature: str) -> Any | None:
    value = read_json_file(path)
    if not isinstance(value, dict):
        return None
    if (
        value.get("schema_version") != ARTIFACT_SCHEMA_VERSION
        or value.get("stage") != stage
        or value.get("signature") != signature
        or "result" not in value
    ):
        return None
    return value["result"]


def set_stage_record(
    manifest: dict[str, Any],
    stage: str,
    signature: str,
    artifact: str,
    *,
    status: str = "completed",
    reused: bool = False,
    **metadata: Any,
) -> None:
    stages = manifest.setdefault("stages", {})
    stages[stage] = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "signature": signature,
        "artifact": artifact,
        "status": status,
        "reused": reused,
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **metadata,
    }


def validate_transcript(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return ["不是 object"]
    if value.get("status") in {"skipped", "silent"}:
        return []
    segments = value.get("segments")
    if not isinstance(segments, list):
        return ["segments 不是陣列"]
    errors: list[str] = []
    previous_start = -1.0
    for index, segment in enumerate(segments):
        if not isinstance(segment, dict):
            errors.append(f"segments[{index}] 不是 object")
            continue
        try:
            start = float(segment["start"])
            end = float(segment["end"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"segments[{index}] 缺少有效時間")
            continue
        if start < 0 or end < start:
            errors.append(f"segments[{index}] 時間範圍錯誤")
        if start + 0.001 < previous_start:
            errors.append(f"segments[{index}] 未依時間排序")
        if not str(segment.get("text", "")).strip():
            errors.append(f"segments[{index}] 沒有文字")
        previous_start = start
    return errors


def validate_visual(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["視覺產物不是陣列"]
    errors: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            errors.append(f"visual[{index}] 不是 object")
            continue
        try:
            timestamp = float(item["timestamp"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"visual[{index}] 缺少有效 timestamp")
            continue
        if timestamp < 0:
            errors.append(f"visual[{index}] timestamp 小於 0")
        for key in ("screen_type", "visible_text", "visual_summary", "important_details"):
            if key not in item:
                errors.append(f"visual[{index}] 缺少 {key}")
        confidence = item.get("confidence")
        if confidence is not None:
            try:
                if not 0 <= float(confidence) <= 1:
                    errors.append(f"visual[{index}] confidence 超出 0 到 1")
            except (TypeError, ValueError):
                errors.append(f"visual[{index}] confidence 無效")
    return errors


def validate_timeline(value: Any) -> list[str]:
    if not isinstance(value, list):
        return ["時間軸不是陣列"]
    errors: list[str] = []
    previous = -1.0
    for index, event in enumerate(value):
        if not isinstance(event, dict):
            errors.append(f"timeline[{index}] 不是 object")
            continue
        try:
            timestamp = float(event["timestamp"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"timeline[{index}] 缺少 timestamp")
            continue
        if timestamp < previous:
            errors.append(f"timeline[{index}] 未依時間排序")
        previous = timestamp
        if event.get("type") not in {"speech", "visual"}:
            errors.append(f"timeline[{index}] type 無效")
    return errors


def validate_summary(value: Any, duration: float = 0.0) -> list[str]:
    if not isinstance(value, dict):
        return ["摘要不是 object"]
    errors: list[str] = []
    if not str(value.get("short_summary", "")).strip():
        errors.append("缺少 short_summary")
    for key in ("chapters", "steps", "tools_and_terms", "fact_check", "source_timestamps"):
        if not isinstance(value.get(key), list):
            errors.append(f"{key} 不是陣列")
    tolerance = max(5.0, duration * 0.01)
    for index, chapter in enumerate(value.get("chapters") or []):
        if not isinstance(chapter, dict):
            errors.append(f"chapters[{index}] 不是 object")
            continue
        try:
            start = float(chapter["start"])
            end = float(chapter["end"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"chapters[{index}] 缺少有效時間")
            continue
        if start < 0 or end < start or (duration > 0 and end > duration + tolerance):
            errors.append(f"chapters[{index}] 時間超出影片範圍")
        if not str(chapter.get("title", "")).strip():
            errors.append(f"chapters[{index}] 缺少 title")
    for index, item in enumerate(value.get("fact_check") or []):
        if not isinstance(item, dict):
            errors.append(f"fact_check[{index}] 不是 object")
            continue
        try:
            timestamp = float(item["timestamp"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"fact_check[{index}] 缺少 timestamp")
            continue
        if timestamp < 0 or (duration > 0 and timestamp > duration + tolerance):
            errors.append(f"fact_check[{index}] timestamp 超出範圍")
        if not str(item.get("claim", "")).strip() or not str(item.get("reason", "")).strip():
            errors.append(f"fact_check[{index}] 缺少 claim 或 reason")
    for index, timestamp in enumerate(value.get("source_timestamps") or []):
        try:
            numeric = float(timestamp)
        except (TypeError, ValueError):
            errors.append(f"source_timestamps[{index}] 無效")
            continue
        if numeric < 0 or (duration > 0 and numeric > duration + tolerance):
            errors.append(f"source_timestamps[{index}] 超出範圍")
    return errors

def marker_matches(marker: Path, signature: str) -> bool:
    """完成標記同時記錄當時的設定，設定變了就得重做。

    這讓 job 目錄只由來源決定：換 --frame-interval 只重抽畫面，
    已經抓好的字幕與已付費的逐字稿不會跟著作廢。
    """
    if not marker.is_file():
        return False
    try:
        return marker.read_text(encoding="utf-8").strip() == signature
    except OSError:
        return False


def artifact_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path.resolve())


def locate_source_video(job_dir: Path) -> Path:
    candidates = sorted(
        path
        for path in job_dir.glob("source.*")
        if path.suffix.lower() in VIDEO_EXTENSIONS
    )
    if not candidates:
        raise RuntimeError(f"下載完成但找不到影片檔：{job_dir}")
    return candidates[0]


def reuse_artifact(path: Path, required_key: str = "") -> Any | None:
    """載入既有的雲端產物；壞檔或空檔一律當作沒有，寧可重算也不要餵半份資料下去。

    required_key：模型偶爾不回 JSON，parse_json_object 就退回 {"raw": ...}。
    那種半成品若被當成有效快取，這支影片永遠不會重算——實測 142 支中有 2 支中招。
    """
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    if not value or (isinstance(value, dict) and value.get("status") == "skipped"):
        return None
    if required_key and isinstance(value, dict) and not value.get(required_key):
        return None
    return value


def source_title(job_dir: Path, video_path: Path) -> str:
    """yt-dlp 的 info.json 有真實標題；本機檔案就退回檔名。"""
    info_path = job_dir / "source.info.json"
    if info_path.is_file():
        try:
            title = str(json.loads(info_path.read_text(encoding="utf-8")).get("title", "")).strip()
            if title:
                return title
        except (json.JSONDecodeError, OSError):
            pass
    return video_path.stem


def download_video(url: str, job_dir: Path) -> Path:
    # 只信任完整標記：中斷會留下 source.f<id>.mp4 之類的分軌檔，
    # 直接復用會拿到無音訊的純視訊軌。重跑 yt-dlp 會自行續傳並清掉分軌。
    done = job_dir / ".download-complete"
    if done.is_file():
        return locate_source_video(job_dir)
    job_dir.mkdir(parents=True, exist_ok=True)
    command = yt_dlp_command() + [
        "--no-playlist",
        "--write-info-json",
        "--merge-output-format",
        "mp4",
        "--output",
        str(job_dir / "source.%(ext)s"),
        url,
    ]
    run_external(command, stream=True)
    video_path = locate_source_video(job_dir)
    done.touch()
    return video_path


def probe_video(video_path: Path) -> dict[str, Any]:
    output = run_external(
        [
            require_tool("ffprobe"),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video_path),
        ]
    )
    return json.loads(output)


def video_duration(probe: dict[str, Any]) -> float:
    value = probe.get("format", {}).get("duration")
    if value is not None:
        return float(value)
    for stream in probe.get("streams", []):
        if stream.get("codec_type") == "video" and stream.get("duration"):
            return float(stream["duration"])
    return 0.0



def extract_audio(
    video_path: Path,
    job_dir: Path,
    chunk_seconds: int,
    overlap_seconds: float = 2.0,
    source_fingerprint: str = "",
    duration_seconds: float = 0.0,
    refresh: bool = False,
    probe_data: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """以可恢復的固定視窗切音訊，保存每個片段的實際來源範圍。"""
    if chunk_seconds <= 0:
        raise ValueError("chunk_seconds 必須大於 0。")
    if overlap_seconds < 0 or overlap_seconds >= chunk_seconds:
        raise ValueError("overlap_seconds 必須介於 0 與 chunk_seconds 之間。")
    audio_dir = job_dir / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    done = audio_dir / ".complete"
    metadata_path = audio_dir / "chunks.json"
    source_fingerprint = source_fingerprint or digest_value(str(video_path))
    signature = stage_signature(
        "audio",
        source_fingerprint,
        {
            "extractor": AUDIO_EXTRACTOR_VERSION,
            "chunk_seconds": chunk_seconds,
            "overlap_seconds": round(overlap_seconds, 3),
        },
    )
    cached = read_json_file(metadata_path)
    cached_chunks = cached.get("chunks") if isinstance(cached, dict) else None
    if (
        not refresh
        and marker_matches(done, signature)
        and isinstance(cached, dict)
        and cached.get("schema_version") == ARTIFACT_SCHEMA_VERSION
        and cached.get("signature") == signature
        and isinstance(cached_chunks, list)
        and cached_chunks
        and all(Path(str(item.get("path", ""))).is_file() for item in cached_chunks if isinstance(item, dict))
    ):
        return [dict(item) for item in cached_chunks if isinstance(item, dict)]

    for stale in audio_dir.glob("audio_*.mp3"):
        stale.unlink()
    probe = probe_data or probe_video(video_path)
    streams = probe.get("streams") or []
    if not any(stream.get("codec_type") == "audio" for stream in streams if isinstance(stream, dict)):
        write_json(
            metadata_path,
            {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "signature": signature,
                "status": "silent",
                "chunks": [],
            },
        )
        write_text(done, signature)
        return []

    if duration_seconds <= 0:
        duration_seconds = video_duration(probe)
    if duration_seconds <= 0:
        raise RuntimeError("無法取得影片時長，無法建立音訊片段。")

    step = float(chunk_seconds) - float(overlap_seconds)
    chunks: list[dict[str, Any]] = []
    start = 0.0
    index = 0
    while start < duration_seconds - 0.05:
        end = min(duration_seconds, start + float(chunk_seconds))
        length = end - start
        if length <= 0.05:
            break
        output = audio_dir / f"audio_{index:04d}.mp3"
        run_external(
            [
                require_tool("ffmpeg"),
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{start:.3f}",
                "-i",
                str(video_path),
                "-t",
                f"{length:.3f}",
                "-map",
                "0:a:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "64k",
                str(output),
            ],
            stream=True,
        )
        if not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError(f"FFmpeg 沒有產生音訊片段：{output.name}")
        chunks.append(
            {
                "index": index,
                "path": str(output),
                "start": round(start, 3),
                "end": round(end, 3),
            }
        )
        index += 1
        if end >= duration_seconds - 0.05:
            break
        start += step

    if not chunks:
        raise RuntimeError("FFmpeg 沒有產生音訊片段。")
    write_json(
        metadata_path,
        {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "signature": signature,
            "chunk_seconds": chunk_seconds,
            "overlap_seconds": overlap_seconds,
            "chunks": chunks,
        },
    )
    write_text(done, signature)
    return chunks

def average_hash(image_path: Path) -> str:
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            gray = image.convert("L").resize((8, 8))
            get_pixels = getattr(gray, "get_flattened_data", gray.getdata)
            values = list(get_pixels())
    except ImportError as exc:
        raise RuntimeError("截圖去重需要 Pillow；請執行 `python -m pip install Pillow`。") from exc
    average = sum(values) / len(values)
    return "".join("1" if value >= average else "0" for value in values)


def hash_distance(left: str, right: str) -> int:
    return sum(a != b for a, b in zip(left, right))


def limit_items(items: list[Any], max_items: int) -> list[Any]:
    if max_items <= 0:
        raise ValueError("max_items 必須大於 0。")
    if len(items) <= max_items:
        return items
    if max_items == 1:
        return [items[0]]
    selected_indexes = {
        round(index * (len(items) - 1) / (max_items - 1))
        for index in range(max_items)
    }
    return [item for index, item in enumerate(items) if index in selected_indexes]


class OptionalOCR:
    def __init__(self, enabled: bool) -> None:
        self.engine = None
        self.reason = "未啟用"
        if not enabled:
            return
        if not importlib.util.find_spec("paddleocr"):
            self.reason = "未安裝 paddleocr"
            return
        try:
            from paddleocr import PaddleOCR

            try:
                self.engine = PaddleOCR(lang="ch")
            except TypeError:
                self.engine = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
            self.reason = "可用"
        except Exception as exc:  # optional dependency is allowed to fail closed
            self.reason = f"初始化失敗：{exc}"

    def read(self, image_path: Path) -> tuple[str, float | None]:
        if self.engine is None:
            return "", None
        try:
            result = self.engine.ocr(str(image_path), cls=True)
        except TypeError:
            result = self.engine.ocr(str(image_path))
        except Exception:
            return "", None
        texts: list[str] = []
        scores: list[float] = []
        for page in result or []:
            if not isinstance(page, list):
                continue
            for line in page:
                if not isinstance(line, (list, tuple)) or len(line) < 2:
                    continue
                detail = line[1]
                if isinstance(detail, (list, tuple)) and len(detail) >= 2:
                    texts.append(str(detail[0]))
                    try:
                        scores.append(float(detail[1]))
                    except (TypeError, ValueError):
                        pass
        return "\n".join(texts), (sum(scores) / len(scores) if scores else None)



def parse_showinfo_timestamps(log: str) -> list[float]:
    """讀取 FFmpeg showinfo 的 pts_time；這是原始畫面時間，不是輸出檔案序號。"""
    pattern = re.compile(r"showinfo.*?pts_time:([+-]?(?:\d+(?:\.\d*)?|\.\d+))")
    return [round(float(match.group(1)), 3) for match in pattern.finditer(log)]


def select_frame_candidates(candidates: list[dict[str, Any]], max_frames: int) -> list[dict[str, Any]]:
    """優先保留內容變化，再用時間均勻抽樣補足，避免固定上限吃掉重要畫面。"""
    if len(candidates) <= max_frames:
        return candidates
    priority = {
        "first_frame": 6,
        "text_change": 5,
        "visual_change": 4,
        "low_ocr_confidence": 3,
    }
    ranked = sorted(
        enumerate(candidates),
        key=lambda pair: (
            -max((priority.get(reason, 1) for reason in pair[1].get("reasons", [])), default=0),
            pair[1].get("timestamp", 0),
            pair[0],
        ),
    )
    keep_count = max(1, max_frames // 2)
    kept_indexes = {index for index, _ in ranked[:keep_count]}
    remaining = [index for index in range(len(candidates)) if index not in kept_indexes]
    need = max_frames - len(kept_indexes)
    if need > 0 and remaining:
        for selected in limit_items(remaining, min(need, len(remaining))):
            kept_indexes.add(selected)
    kept_indexes.add(0)
    kept_indexes.add(len(candidates) - 1)
    if len(kept_indexes) > max_frames:
        ordered = sorted(
            kept_indexes,
            key=lambda index: (
                -max((priority.get(reason, 1) for reason in candidates[index].get("reasons", [])), default=0),
                index,
            ),
        )
        kept_indexes = set(ordered[:max_frames])
    return [candidates[index] for index in sorted(kept_indexes)]


def extract_candidates(
    video_path: Path,
    job_dir: Path,
    interval: float,
    hash_threshold: int,
    max_frames: int,
    ocr_enabled: bool,
    ocr_threshold: float,
    scene_threshold: float = 0.3,
    source_fingerprint: str = "",
    refresh: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if interval <= 0:
        raise ValueError("interval 必須大於 0。")
    if max_frames <= 0:
        raise ValueError("max_frames 必須大於 0。")
    frames_dir = job_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    done = frames_dir / ".complete"
    timestamps_path = frames_dir / "timestamps.json"
    source_fingerprint = source_fingerprint or digest_value(str(video_path))
    signature = stage_signature(
        "frames",
        source_fingerprint,
        {
            "extractor": FRAME_EXTRACTOR_VERSION,
            "interval": interval,
            "hash_threshold": hash_threshold,
            "scene_threshold": scene_threshold,
            "max_frames": max_frames,
            "ocr": ocr_enabled,
        },
    )
    all_frames = sorted(frames_dir.glob("frame_*.jpg"))
    timestamp_data = read_json_file(timestamps_path)
    cached_timestamps = timestamp_data.get("timestamps") if isinstance(timestamp_data, dict) else None
    cache_valid = (
        not refresh
        and bool(all_frames)
        and marker_matches(done, signature)
        and isinstance(timestamp_data, dict)
        and timestamp_data.get("schema_version") == ARTIFACT_SCHEMA_VERSION
        and timestamp_data.get("signature") == signature
        and isinstance(cached_timestamps, list)
        and len(cached_timestamps) == len(all_frames)
    )
    if not cache_valid:
        for stale in all_frames:
            stale.unlink()
        timestamps_path.unlink(missing_ok=True)
        filter_value = (
            f"select='eq(n,0)+gt(scene,{scene_threshold:g})+"
            f"gte(t,prev_selected_t+{interval:g})',"
            "scale=1280:-2:force_original_aspect_ratio=decrease,showinfo"
        )
        _, stderr = run_external_capture(
            [
                require_tool("ffmpeg"),
                "-y",
                "-hide_banner",
                "-loglevel",
                "info",
                "-copyts",
                "-i",
                str(video_path),
                "-vf",
                filter_value,
                "-fps_mode",
                "vfr",
                "-q:v",
                "3",
                str(frames_dir / "frame_%06d.jpg"),
            ]
        )
        all_frames = sorted(frames_dir.glob("frame_*.jpg"))
        timestamps = parse_showinfo_timestamps(stderr)
        if not all_frames:
            raise RuntimeError("FFmpeg 沒有產生截圖。")
        if len(timestamps) != len(all_frames):
            raise RuntimeError(
                f"FFmpeg 畫面數與 PTS 數量不一致：{len(all_frames)} 張、{len(timestamps)} 個時間戳；"
                "為避免錯位，本次不產生候選畫面。"
            )
        cached_timestamps = timestamps
        write_json(
            timestamps_path,
            {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "signature": signature,
                "timestamps": timestamps,
                "timestamp_source": "ffmpeg-showinfo-pts_time",
            },
        )
    write_text(done, signature)

    ocr = OptionalOCR(ocr_enabled)
    candidates: list[dict[str, Any]] = []
    previous_hash = ""
    previous_text = ""
    for index, frame_path in enumerate(all_frames):
        image_hash = average_hash(frame_path)
        text, confidence = ocr.read(frame_path)
        reasons: list[str] = []
        distance = hash_distance(previous_hash, image_hash) if previous_hash else 64
        if not previous_hash:
            reasons.append("first_frame")
        elif distance >= hash_threshold:
            reasons.append("visual_change")
        if confidence is not None and confidence < ocr_threshold:
            reasons.append("low_ocr_confidence")
        if text and previous_text:
            similarity = difflib.SequenceMatcher(None, previous_text, text).ratio()
            if similarity < 0.7:
                reasons.append("text_change")
        if reasons:
            candidates.append(
                {
                    "timestamp": float(cached_timestamps[index]),
                    "path": str(frame_path),
                    "hash": image_hash,
                    "hash_distance": distance,
                    "ocr_text": text,
                    "ocr_confidence": confidence,
                    "reasons": reasons,
                }
            )
        previous_hash = image_hash
        if text:
            previous_text = text
    candidates = select_frame_candidates(candidates, max_frames)
    metadata = {
        "all_frame_count": len(all_frames),
        "candidate_count": len(candidates),
        "ocr": {"enabled": ocr_enabled, "status": ocr.reason},
        "method": "adaptive_scene_plus_interval_average_hash",
        "timestamp_source": "ffmpeg-showinfo-pts_time",
        "signature": signature,
    }
    return candidates, metadata

def steipete_command() -> list[str]:
    executable = shutil.which("summarize")
    if executable:
        return [executable]
    npx = shutil.which("npx")
    if npx:
        return [npx, "-y", "@steipete/summarize"]
    raise RuntimeError("找不到 summarize 或 npx；請安裝 Node 24+ 與 @steipete/summarize。")


def extract_steipete_candidates(
    video_path: Path,
    job_dir: Path,
    max_frames: int,
    scene_threshold: float,
    min_duration: float,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    slides_dir = job_dir / "steipete-slides"
    output = run_external(
        steipete_command()
        + [
            "slides",
            str(video_path),
            "--slides-ocr",
            "--slides-dir",
            str(slides_dir),
            "--slides-max",
            str(max_frames),
            "--slides-min-duration",
            str(min_duration),
            "--slides-scene-threshold",
            str(scene_threshold),
            "--json",
            "--render",
            "none",
        ]
    )
    payload = parse_json_object(output)
    slide_data = payload.get("slides", {})
    if not isinstance(slide_data, dict) or payload.get("ok") is False:
        raise RuntimeError(f"steipete slides 回應格式錯誤：{payload}")
    slides = slide_data.get("slides") or []
    candidates: list[dict[str, Any]] = []
    for slide in slides:
        if not isinstance(slide, dict) or not slide.get("imagePath"):
            continue
        image_path = Path(str(slide["imagePath"]))
        if not image_path.is_file():
            continue
        candidates.append(
            {
                "timestamp": float(slide.get("timestamp", 0)),
                "path": str(image_path),
                "hash": average_hash(image_path),
                "hash_distance": None,
                "ocr_text": str(slide.get("ocrText", "")),
                "ocr_confidence": slide.get("ocrConfidence"),
                "reasons": ["steipete_scene"],
            }
        )
    write_json(job_dir / "steipete-slides.json", payload)
    return candidates, {
        "method": "steipete/summarize slides",
        "all_frame_count": len(slides),
        "candidate_count": len(candidates),
        "ocr": {"enabled": True, "status": "steipete-tesseract"},
        "warnings": slide_data.get("warnings", []),
    }


def multipart_body(fields: dict[str, str], file_field: str, file_path: Path) -> tuple[bytes, str]:
    boundary = "----codex" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        )
    content_type = mimetypes.guess_type(file_path.name)[0] or "application/octet-stream"
    chunks.append(
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; '
            f'filename="{file_path.name}"\r\nContent-Type: {content_type}\r\n\r\n'
        ).encode()
    )
    chunks.append(file_path.read_bytes())
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


# Whisper 遇到靜默會硬生出 "you"、"Thank you." 之類的幻覺文字。
# 實測 85 分鐘直播（開頭近 20 分鐘無人聲）：幻覺段 no_speech_prob 全是 0.701，
# 688 個正常段最高只有 0.414，0.6 完美分離、零誤殺。
NO_SPEECH_PROB_LIMIT = 0.6


def is_silence(no_speech: Any) -> bool:
    return isinstance(no_speech, (int, float)) and not isinstance(no_speech, bool) and no_speech > NO_SPEECH_PROB_LIMIT


RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}
# urllib 預設送 Python-urllib/3.x，Groq 前面的 Cloudflare 會直接 403（error code 1010）。
USER_AGENT = "video-library-pipeline/1.0"


def retry_delay(exc: urllib.error.HTTPError, fallback: float) -> float:
    """尊重伺服器的 Retry-After；解析不出來就用退避值。"""
    header = exc.headers.get("Retry-After") if exc.headers else None
    if header:
        try:
            return max(0.0, min(float(header), 120.0))
        except ValueError:
            pass
    return fallback


def request_json(
    url: str,
    headers: dict[str, str],
    *,
    method: str,
    body: bytes | None = None,
    timeout: int,
    attempts: int = 4,
) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT, **headers}
    delay = 2.0
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in RETRY_STATUS and attempt < attempts:
                wait = retry_delay(exc, delay)
                print(f"! API HTTP {exc.code}，{wait:.0f} 秒後重試（{attempt}/{attempts - 1}）")
                time.sleep(wait)
                delay *= 2
                continue
            # 帶上 URL：base_url 被環境變數改掉時，光看「404 page not found」查不出原因。
            raise RuntimeError(f"API HTTP {exc.code}（{url}）：{detail[:1200]}") from exc
        # TimeoutError 不是 URLError 的子類，只接 URLError 會讓讀取逾時完全跳過重試
        except (urllib.error.URLError, TimeoutError) as exc:
            reason = getattr(exc, "reason", None) or type(exc).__name__
            if attempt < attempts:
                print(f"! API 網路錯誤（{reason}），{delay:.0f} 秒後重試（{attempt}/{attempts - 1}）")
                time.sleep(delay)
                delay *= 2
                continue
            raise RuntimeError(f"API 網路錯誤：{reason}") from exc
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"API 回應不是 JSON：{raw[:500]!r}") from exc


def post_json(url: str, headers: dict[str, str], body: bytes, timeout: int = 180) -> dict[str, Any]:
    return request_json(url, headers, method="POST", body=body, timeout=timeout)


def get_json(url: str, headers: dict[str, str]) -> dict[str, Any]:
    return request_json(url, headers, method="GET", timeout=30)



def groq_transcribe(
    audio_path: Path,
    api_key: str,
    model: str,
    language: str,
    prompt: str = "",
) -> dict[str, Any]:
    fields = {
        "model": model,
        "response_format": "verbose_json",
        "temperature": "0",
        "timestamp_granularities[]": "segment",
    }
    if language and language != "auto":
        fields["language"] = language
    if prompt:
        fields["prompt"] = prompt
    body, boundary = multipart_body(fields, "file", audio_path)
    return post_json(
        "https://api.groq.com/openai/v1/audio/transcriptions",
        {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        body,
    )


def normalize_chunk_specs(
    chunks: list[Any],
    default_chunk_seconds: int = 900,
) -> list[dict[str, Any]]:
    specs: list[dict[str, Any]] = []
    for index, item in enumerate(chunks):
        if isinstance(item, dict):
            path = Path(str(item["path"]))
            start = float(item.get("start", index * default_chunk_seconds))
            end = float(item.get("end", start + default_chunk_seconds))
            actual_index = int(item.get("index", index))
        else:
            path = Path(str(item))
            start = float(index * default_chunk_seconds)
            end = start + default_chunk_seconds
            actual_index = index
        specs.append({"index": actual_index, "path": path, "start": start, "end": end})
    return specs


def transcription_review_flags(result: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    segments = result.get("segments") or []
    if not isinstance(segments, list):
        return ["segments_missing"]
    for segment in segments:
        if not isinstance(segment, dict):
            flags.append("malformed_segment")
            continue
        text = str(segment.get("text", "")).strip()
        no_speech = segment.get("no_speech_prob")
        avg_logprob = segment.get("avg_logprob")
        compression = segment.get("compression_ratio")
        if text and is_silence(no_speech):
            flags.append("text_with_high_no_speech_prob")
        try:
            if avg_logprob is not None and float(avg_logprob) < -1.0:
                flags.append("low_avg_logprob")
        except (TypeError, ValueError):
            flags.append("invalid_avg_logprob")
        try:
            if compression is not None and float(compression) > 2.4:
                flags.append("high_compression_ratio")
        except (TypeError, ValueError):
            flags.append("invalid_compression_ratio")
    return sorted(set(flags))


def transcription_quality_score(result: dict[str, Any]) -> float:
    segments = result.get("segments") or []
    text = " ".join(str(item.get("text", "")) for item in segments if isinstance(item, dict))
    return float(len(text)) - 25.0 * len(transcription_review_flags(result))


def result_segments(
    result: dict[str, Any],
    spec: dict[str, Any],
    *,
    source_label: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    raw_segments = result.get("segments") or []
    if not raw_segments and result.get("text"):
        raw_segments = [{"start": 0.0, "end": spec["end"] - spec["start"], "text": result["text"]}]
    segments: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            continue
        text = str(raw.get("text", "")).strip()
        try:
            relative_start = float(raw.get("start", 0))
            relative_end = float(raw.get("end", relative_start))
        except (TypeError, ValueError):
            continue
        item = {
            "start": round(relative_start + spec["start"], 3),
            "end": round(max(relative_end, relative_start) + spec["start"], 3),
            "text": text,
            "chunk": spec["index"],
            "source_chunk_start": spec["start"],
            "source_label": source_label,
        }
        for key in ("avg_logprob", "compression_ratio", "no_speech_prob"):
            if key in raw:
                item[key] = raw[key]
        if text:
            flags: list[str] = []
            if is_silence(raw.get("no_speech_prob")):
                flags.append("text_with_high_no_speech_prob")
            try:
                if raw.get("avg_logprob") is not None and float(raw["avg_logprob"]) < -1.0:
                    flags.append("low_avg_logprob")
            except (TypeError, ValueError):
                flags.append("invalid_avg_logprob")
            try:
                if raw.get("compression_ratio") is not None and float(raw["compression_ratio"]) > 2.4:
                    flags.append("high_compression_ratio")
            except (TypeError, ValueError):
                flags.append("invalid_compression_ratio")
            if flags:
                item["review_required"] = sorted(set(flags))
            segments.append(item)
        elif is_silence(raw.get("no_speech_prob")):
            omitted.append(
                {
                    "start": item["start"],
                    "end": item["end"],
                    "chunk": spec["index"],
                    "no_speech_prob": raw.get("no_speech_prob"),
                    "reason": "no_text_high_no_speech_probability",
                }
            )
    return segments, omitted


def merge_overlapping_segments(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for current in sorted(segments, key=lambda item: (float(item["start"]), float(item["end"]))):
        if not merged:
            current["source_chunks"] = [current.get("chunk")]
            merged.append(current)
            continue
        previous = merged[-1]
        overlap = min(float(previous["end"]), float(current["end"])) - float(current["start"])
        left = " ".join(str(previous.get("text", "")).lower().split())
        right = " ".join(str(current.get("text", "")).lower().split())
        similar = (
            bool(left and right)
            and (
                left == right
                or left in right
                or right in left
                or difflib.SequenceMatcher(None, left, right).ratio() >= 0.85
            )
        )
        if overlap > 0 and similar:
            chosen = current if len(right) > len(left) else previous
            chosen["start"] = min(float(previous["start"]), float(current["start"]))
            chosen["end"] = max(float(previous["end"]), float(current["end"]))
            chosen["source_chunks"] = sorted(
                set(previous.get("source_chunks", []) + [current.get("chunk")])
            )
            if previous.get("review_required") or current.get("review_required"):
                chosen["review_required"] = sorted(
                    set(previous.get("review_required", []) + current.get("review_required", []))
                )
            merged[-1] = chosen
        else:
            current["source_chunks"] = [current.get("chunk")]
            merged.append(current)
    return merged


def aggregate_transcription_results(
    results: dict[int, dict[str, Any]],
    specs: list[dict[str, Any]],
    model: str,
    language: str,
    source_label: str,
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    omitted: list[dict[str, Any]] = []
    reviews: list[dict[str, Any]] = []
    detected = language
    by_index = {int(spec["index"]): spec for spec in specs}
    for index in sorted(results):
        payload = results[index]
        selected = payload.get("selected") or {}
        detected = selected.get("language") or detected
        chunk_segments, chunk_omitted = result_segments(
            selected, by_index[index], source_label=source_label
        )
        segments.extend(chunk_segments)
        omitted.extend(chunk_omitted)
        flags = payload.get("review") or []
        if flags:
            reviews.append({"chunk": index, "flags": flags, "attempts": payload.get("attempts", 1)})
    return {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "model": model,
        "language": detected,
        "segments": merge_overlapping_segments(segments),
        "review": reviews,
        "omitted_segments": omitted,
        "chunk_count": len(specs),
    }


def transcribe_all(
    chunks: list[Any],
    api_key: str,
    model: str,
    language: str,
    chunk_seconds: int,
    workers: int,
    stage_signature_value: str = "",
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    specs = normalize_chunk_specs(chunks, chunk_seconds)
    if not specs:
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "model": model,
            "language": language,
            "segments": [],
            "review": [],
            "omitted_segments": [],
            "chunk_count": 0,
        }
    part_dir = cache_dir or Path(tempfile.mkdtemp(prefix="transcript-parts-"))
    part_dir.mkdir(parents=True, exist_ok=True)

    def one(spec: dict[str, Any]) -> tuple[int, dict[str, Any], bool, str]:
        index = int(spec["index"])
        signature = stage_signature(
            "transcript_chunk",
            stage_signature_value or model,
            {
                "model": model,
                "language": language,
                "chunk": index,
                "start": spec["start"],
                "end": spec["end"],
            },
        )
        part_path = part_dir / f"chunk_{index:04d}.json"
        cached = None if refresh else read_stage_part(part_path, "transcript_chunk", signature)
        if isinstance(cached, dict) and "selected" in cached:
            return index, cached, True, signature
        raw = groq_transcribe(spec["path"], api_key, model, language)
        flags = transcription_review_flags(raw)
        selected = raw
        attempts = 1
        if flags:
            retry_prompt = (
                "請特別注意專有名詞、數字、版本號、否定詞與句首句尾；"
                "只根據音訊重新辨識，不要補寫聽不到的內容。"
            )
            retry = groq_transcribe(spec["path"], api_key, model, language, retry_prompt)
            attempts = 2
            if transcription_quality_score(retry) >= transcription_quality_score(raw):
                selected = retry
        payload = {"raw": raw, "selected": selected, "review": flags, "attempts": attempts}
        return index, payload, False, signature

    results: dict[int, dict[str, Any]] = {}
    errors: list[Exception] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        futures = [pool.submit(one, spec) for spec in specs]
        for future in concurrent.futures.as_completed(futures):
            try:
                index, payload, cached, signature = future.result()
                results[index] = payload
                if not cached:
                    write_stage_part(
                        part_dir / f"chunk_{index:04d}.json",
                        "transcript_chunk",
                        signature,
                        payload,
                        chunk=index,
                    )
            except Exception as exc:
                errors.append(exc)
    if errors:
        raise errors[0]
    return aggregate_transcription_results(results, specs, model, language, "groq")

def parse_caption_json3(payload: dict[str, Any]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for event in payload.get("events") or []:
        text = "".join(part.get("utf8", "") for part in (event.get("segs") or [])).strip()
        if not text:
            continue
        start = float(event.get("tStartMs", 0)) / 1000
        segments.append(
            {
                "start": round(start, 3),
                "end": round(start + float(event.get("dDurationMs", 0)) / 1000, 3),
                "text": text,
                "chunk": 0,
            }
        )
    return sorted(segments, key=lambda item: item["start"])


def merge_caption_segments(
    segments: list[dict[str, Any]],
    max_chars: int = CAPTION_MERGE_CHARS,
    max_gap: float = CAPTION_MERGE_GAP,
) -> list[dict[str, Any]]:
    """把逐行字幕併成段落。時間戳取該段第一行的開始，才能正確跳轉。"""
    merged: list[dict[str, Any]] = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        if (
            merged
            and len(merged[-1]["text"]) + len(text) + 1 <= max_chars
            and float(segment["start"]) - float(merged[-1]["end"]) <= max_gap
        ):
            merged[-1]["text"] += " " + text
            merged[-1]["end"] = segment["end"]
        else:
            merged.append(dict(segment, text=text))
    return merged



def youtube_info(url: str, job_dir: Path, refresh: bool = False) -> dict[str, Any]:
    """先取得來源 metadata，讓字幕選擇發生在完整影片下載前。"""
    job_dir.mkdir(parents=True, exist_ok=True)
    info_path = job_dir / "source.info.json"
    cached = read_json_file(info_path)
    if (
        not refresh
        and isinstance(cached, dict)
        and (cached.get("webpage_url") or cached.get("original_url")) == url
        and ("subtitles" in cached or "automatic_captions" in cached)
    ):
        return cached
    output = run_external(
        yt_dlp_command() + ["--skip-download", "--dump-single-json", "--no-playlist", url]
    )
    try:
        info = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"yt-dlp metadata 不是 JSON：{output[:500]!r}") from exc
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp metadata 不是 object。")
    write_json(info_path, info)
    return info


def _language_base(value: str) -> str:
    return value.lower().replace("_", "-").split("-", 1)[0]


def choose_caption_tracks(info: dict[str, Any], requested: str) -> list[dict[str, Any]]:
    manual = info.get("subtitles") or {}
    automatic = info.get("automatic_captions") or {}
    original = str(info.get("language") or "")
    tracks: list[dict[str, Any]] = []
    for kind, catalog in (("manual", manual), ("automatic", automatic)):
        if not isinstance(catalog, dict):
            continue
        for raw_language in catalog:
            language = str(raw_language)
            if "translat" in language.lower():
                continue
            tracks.append(
                {
                    "language": language,
                    "kind": kind,
                    "translation": False,
                    "original": bool(original and _language_base(language) == _language_base(original)),
                }
            )
    if not tracks:
        return []

    wanted = str(requested or "auto")
    if wanted != "auto":
        exact = [
            item
            for item in tracks
            if item["language"].lower() == wanted.lower()
            or _language_base(item["language"]) == _language_base(wanted)
        ]
        if exact:
            tracks = exact
        else:
            return []

    def rank(item: dict[str, Any]) -> tuple[int, int, int, str]:
        language = str(item["language"])
        requested_match = (
            1
            if wanted != "auto"
            and (
                language.lower() == wanted.lower()
                or _language_base(language) == _language_base(wanted)
            )
            else 0
        )
        original_match = 1 if item["original"] else 0
        manual_match = 1 if item["kind"] == "manual" else 0
        return (-requested_match, -original_match, -manual_match, language)

    tracks.sort(key=rank)
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in tracks:
        language = str(item["language"])
        if language in seen:
            continue
        seen.add(language)
        selected.append(item)
        if wanted != "auto" or len(selected) >= 3:
            break
    return selected


def caption_stage_signature(info: dict[str, Any], requested: str) -> str:
    tracks = choose_caption_tracks(info, requested)
    return stage_signature(
        "captions",
        source_metadata_fingerprint(info, str(info.get("webpage_url") or info.get("id") or "")),
        {
            "extractor": CAPTION_EXTRACTOR_VERSION,
            "requested_language": requested,
            "selected_tracks": tracks,
        },
    )


def caption_is_reliable(transcript: dict[str, Any], duration: float = 0.0) -> bool:
    segments = transcript.get("segments") or []
    if not isinstance(segments, list) or len(segments) == 0:
        return False
    text_length = sum(len(str(item.get("text", ""))) for item in segments if isinstance(item, dict))
    if text_length < 20:
        return False
    if duration <= 0:
        return True
    last_end = max(
        (float(item.get("end", 0)) for item in segments if isinstance(item, dict)),
        default=0.0,
    )
    return last_end >= min(duration * 0.35, max(30.0, duration - 5.0))


def fetch_youtube_transcript(url: str, job_dir: Path, language: str, refresh: bool = False) -> dict[str, Any] | None:
    """讀取可用字幕清單後選原文／自動字幕，避免 auto 被硬編成英文。"""
    info = youtube_info(url, job_dir, refresh=refresh)
    tracks = choose_caption_tracks(info, language)
    if not tracks:
        return None
    for stale in job_dir.glob("source.*.json3"):
        stale.unlink()
    requested_languages = ",".join(str(item["language"]) for item in tracks)
    try:
        run_external(
            yt_dlp_command()
            + [
                "--skip-download",
                "--write-info-json",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                requested_languages,
                "--sub-format",
                "json3",
                "--output",
                str(job_dir / "source.%(ext)s"),
                url,
            ],
            stream=True,
        )
    except RuntimeError as exc:
        detail = str(exc).lower()
        if any(token in detail for token in ("no subtitles", "subtitles are not available", "requested format")):
            return None
        raise

    files = sorted(job_dir.glob("source.*.json3"))
    if not files:
        return None
    by_language = {str(item["language"]): item for item in tracks}
    files_by_language: dict[str, Path] = {}
    for path in files:
        name = path.name
        language_name = name[len("source.") : -len(".json3")] if name.startswith("source.") else path.stem
        files_by_language.setdefault(language_name, path)
    selected_file = None
    selected_track: dict[str, Any] | None = None
    for track in tracks:
        language_name = str(track["language"])
        selected_file = files_by_language.get(language_name)
        if selected_file is None:
            selected_file = next(
                (
                    path
                    for available_language, path in files_by_language.items()
                    if _language_base(available_language) == _language_base(language_name)
                ),
                None,
            )
        if selected_file is not None:
            selected_track = track
            break
    if selected_file is None:
        selected_file = files[0]
        language_name = selected_file.name[len("source.") : -len(".json3")]
        selected_track = by_language.get(
            language_name,
            {"language": language_name, "kind": "unknown", "translation": False},
        )
    try:
        payload = json.loads(selected_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    segments = merge_caption_segments(parse_caption_json3(payload))
    if not segments:
        return None
    signature = caption_stage_signature(info, language)
    result = {
        "schema_version": ARTIFACT_SCHEMA_VERSION,
        "model": f"youtube-captions/{selected_track.get('language', language)}",
        "language": selected_track.get("language") or info.get("language") or language,
        "caption_source": selected_track,
        "signature": signature,
        "segments": segments,
    }
    write_json(
        job_dir / "caption-meta.json",
        {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "signature": signature,
            "source": selected_track,
            "available_tracks": tracks,
        },
    )
    return result


def transcribe_local(
    chunks: list[Any],
    model_size: str,
    language: str,
    chunk_seconds: int,
    stage_signature_value: str = "",
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> dict[str, Any]:
    """本機 faster-whisper；每段也寫入可續跑的產物。"""
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "本機轉錄需要 faster-whisper；或改用 transcribe-backend groq。"
        ) from exc
    if model_size not in _WHISPER_MODELS:
        device = os.getenv("WHISPER_DEVICE", "cpu")
        compute = os.getenv("WHISPER_COMPUTE", "float16" if device == "cuda" else "int8")
        _WHISPER_MODELS[model_size] = WhisperModel(model_size, device=device, compute_type=compute)
    model = _WHISPER_MODELS[model_size]
    specs = normalize_chunk_specs(chunks, chunk_seconds)
    part_dir = cache_dir or Path(tempfile.mkdtemp(prefix="transcript-parts-"))
    part_dir.mkdir(parents=True, exist_ok=True)
    results: dict[int, dict[str, Any]] = {}
    for spec in specs:
        index = int(spec["index"])
        signature = stage_signature(
            "transcript_chunk",
            stage_signature_value or model_size,
            {
                "model": f"faster-whisper/{model_size}",
                "language": language,
                "chunk": index,
                "start": spec["start"],
                "end": spec["end"],
            },
        )
        part_path = part_dir / f"chunk_{index:04d}.json"
        cached = None if refresh else read_stage_part(part_path, "transcript_chunk", signature)
        if isinstance(cached, dict) and "selected" in cached:
            results[index] = cached
            continue
        raw_segments: list[dict[str, Any]] = []
        result, info = model.transcribe(
            str(spec["path"]),
            language=None if language in ("", "auto") else language,
            vad_filter=True,
        )
        for segment in result:
            raw_segments.append(
                {
                    "start": segment.start,
                    "end": segment.end,
                    "text": segment.text,
                    "avg_logprob": getattr(segment, "avg_logprob", None),
                    "no_speech_prob": getattr(segment, "no_speech_prob", None),
                    "compression_ratio": getattr(segment, "compression_ratio", None),
                }
            )
        raw = {"language": getattr(info, "language", None) or language, "segments": raw_segments}
        payload = {"raw": raw, "selected": raw, "review": transcription_review_flags(raw), "attempts": 1}
        write_stage_part(part_path, "transcript_chunk", signature, payload, chunk=index)
        results[index] = payload
    return aggregate_transcription_results(
        results,
        specs,
        f"faster-whisper/{model_size}",
        language,
        "local",
    )


def image_data_url(image_path: Path, max_long_side: int = 1280, quality: int = 80) -> str:
    try:
        from PIL import Image

        with Image.open(image_path) as image:
            image = image.convert("RGB")
            image.thumbnail((max_long_side, max_long_side))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
            return f"data:image/jpeg;base64,{encoded}"
    except ImportError:
        encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
        mime = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
        return f"data:{mime};base64,{encoded}"

def minimax_endpoint() -> str:
    base_url = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1").rstrip("/")
    if base_url.endswith("/chat/completions"):
        return base_url
    return base_url + "/chat/completions"



def minimax_chat(
    messages: list[dict[str, Any]],
    api_key: str,
    model: str,
    max_tokens: int | None = None,
    timeout: int = 180,
    thinking: str | None = "adaptive",
) -> str:
    payload: dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.1}
    if max_tokens is not None and max_tokens > 0:
        payload["max_tokens"] = max_tokens
    if thinking in {"adaptive", "disabled"}:
        # MiniMax-M3 的 OpenAI 相容介面用 thinking 控制是否真的推理；
        # reasoning_split 只改輸出格式，不能取代這個欄位。
        payload["thinking"] = {"type": thinking}
    raw = post_json(
        minimax_endpoint(),
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        timeout,
    )
    choices = raw.get("choices") or []
    if not choices:
        raise RuntimeError(f"MiniMax 回應缺少 choices：{raw}")
    message = choices[0].get("message") or {}
    content = message.get("content", "")
    if isinstance(content, list):
        content = "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in {None, "text"}
        )
    if not content and message.get("reasoning_content"):
        content = message.get("reasoning_content")
    return re.sub(r"<think>.*?</think>", "", str(content), flags=re.DOTALL).strip()

def parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL)
    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else {"value": value}
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start >= 0 and end > start:
            try:
                value = json.loads(cleaned[start : end + 1])
                return value if isinstance(value, dict) else {"value": value}
            except json.JSONDecodeError:
                pass
    return {"raw": text}



def analyze_images(
    candidates: list[dict[str, Any]],
    api_key: str,
    model: str,
    workers: int = 1,
    stage_signature_value: str = "",
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    part_dir = cache_dir or Path(tempfile.mkdtemp(prefix="visual-parts-"))
    part_dir.mkdir(parents=True, exist_ok=True)

    def one(item: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any], bool, str]:
        index, candidate = item
        candidate_signature = stage_signature(
            "visual_frame",
            stage_signature_value or model,
            {
                "model": model,
                "index": index,
                "timestamp": candidate.get("timestamp"),
                "hash": candidate.get("hash"),
                "ocr_text": candidate.get("ocr_text", ""),
                "reasons": candidate.get("reasons", []),
                "prompt_version": PROMPT_VERSIONS["visual"],
            },
        )
        part_path = part_dir / f"visual_{index:04d}.json"
        cached = None if refresh else read_stage_part(part_path, "visual_frame", candidate_signature)
        if isinstance(cached, dict):
            return index, cached, True, candidate_signature

        local_ocr = str(candidate.get("ocr_text", "")).strip()
        high_detail = any(
            reason in {"text_change", "low_ocr_confidence"} for reason in candidate.get("reasons", [])
        )
        prompt = (
            "請分析這張影片截圖，只輸出 JSON object，不要 Markdown。"
            "欄位必須包含 screen_type、visible_text（字串陣列）、visual_summary、"
            "important_details（字串陣列）、confidence（0 到 1）。"
            "若看不清楚，保留空陣列並降低 confidence。"
            "描述文字一律用繁體中文，畫面上的原文照抄不要翻譯。"
        )
        if local_ocr:
            prompt += f"\n本地 OCR 初篩文字（僅供交叉確認）：{local_ocr}"
        response = minimax_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_data_url(
                                    Path(candidate["path"]),
                                    max_long_side=2048 if high_detail else 1280,
                                    quality=90 if high_detail else 80,
                                ),
                                "detail": "high" if high_detail else "default",
                            },
                        },
                    ],
                }
            ],
            api_key,
            model,
            max_tokens=1400,
            thinking="disabled",
        )
        observation = parse_json_object(response)
        observation.setdefault("screen_type", "未知畫面")
        observation.setdefault("visible_text", [])
        observation.setdefault("visual_summary", "")
        observation.setdefault("important_details", [])
        observation.setdefault("confidence", 0.0)
        observation["timestamp"] = candidate["timestamp"]
        observation["source_image"] = candidate["path"]
        observation["local_ocr_text"] = local_ocr
        observation["local_ocr_confidence"] = candidate.get("ocr_confidence")
        if not isinstance(observation.get("visible_text"), list):
            observation["visible_text"] = [str(observation["visible_text"])]
        if not isinstance(observation.get("important_details"), list):
            observation["important_details"] = [str(observation["important_details"])]
        return index, observation, False, candidate_signature

    results: dict[int, dict[str, Any]] = {}
    errors: list[Exception] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        futures = [pool.submit(one, item) for item in enumerate(candidates)]
        for future in concurrent.futures.as_completed(futures):
            try:
                index, observation, cached, signature = future.result()
                results[index] = observation
                if not cached:
                    write_stage_part(
                        part_dir / f"visual_{index:04d}.json",
                        "visual_frame",
                        signature,
                        observation,
                        frame=index,
                    )
            except Exception as exc:
                errors.append(exc)
    if errors:
        raise errors[0]
    return [results[index] for index in sorted(results)]


def visual_needs_review(observation: dict[str, Any]) -> list[str]:
    errors = validate_visual([observation])
    reasons = [f"schema:{error}" for error in errors]
    try:
        if float(observation.get("confidence", 0.0)) < 0.65:
            reasons.append("low_confidence")
    except (TypeError, ValueError):
        reasons.append("invalid_confidence")
    if not str(observation.get("visual_summary", "")).strip():
        reasons.append("empty_summary")
    return sorted(set(reasons))


def refine_visual_observations(
    candidates: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    api_key: str,
    model: str,
    workers: int = 1,
    stage_signature_value: str = "",
    cache_dir: Path | None = None,
    refresh: bool = False,
) -> list[dict[str, Any]]:
    part_dir = cache_dir or Path(tempfile.mkdtemp(prefix="visual-review-parts-"))
    part_dir.mkdir(parents=True, exist_ok=True)
    by_index = {index: item for index, item in enumerate(observations)}
    suspects = [
        index
        for index, observation in by_index.items()
        if visual_needs_review(observation)
    ]
    if not suspects:
        return observations

    def one(index: int) -> tuple[int, dict[str, Any], bool, str]:
        candidate = candidates[index]
        reasons = visual_needs_review(by_index[index])
        signature = stage_signature(
            "visual_review",
            stage_signature_value or model,
            {
                "model": model,
                "index": index,
                "timestamp": candidate.get("timestamp"),
                "hash": candidate.get("hash"),
                "reasons": reasons,
                "prompt_version": PROMPT_VERSIONS["visual_review"],
            },
        )
        path = part_dir / f"visual_review_{index:04d}.json"
        cached = None if refresh else read_stage_part(path, "visual_review", signature)
        if isinstance(cached, dict):
            return index, cached, True, signature
        prompt = (
            "這是影片畫面的第二次複核。只輸出 JSON object，重新確認可見文字、"
            "畫面類型、關鍵細節與信心度；不要依常識補齊看不清楚的內容。"
            f"\n需要複核的原因：{', '.join(reasons)}"
        )
        response = minimax_chat(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": image_data_url(
                                    Path(candidate["path"]),
                                    max_long_side=2048,
                                    quality=92,
                                ),
                                "detail": "high",
                            },
                        },
                    ],
                }
            ],
            api_key,
            model,
            max_tokens=1800,
            thinking="adaptive",
        )
        correction = parse_json_object(response)
        if not isinstance(correction, dict):
            correction = {}
        correction["timestamp"] = candidate["timestamp"]
        correction["source_image"] = candidate["path"]
        if not correction.get("visual_summary"):
            correction["visual_summary"] = by_index[index].get("visual_summary", "")
        return index, correction, False, signature

    corrections: dict[int, dict[str, Any]] = {}
    errors: list[Exception] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, min(workers, 4))) as pool:
        futures = [pool.submit(one, index) for index in suspects]
        for future in concurrent.futures.as_completed(futures):
            try:
                index, correction, cached, signature = future.result()
                corrections[index] = correction
                if not cached:
                    write_stage_part(
                        part_dir / f"visual_review_{index:04d}.json",
                        "visual_review",
                        signature,
                        correction,
                        frame=index,
                    )
            except Exception as exc:
                errors.append(exc)
    if errors:
        raise errors[0]
    refined: list[dict[str, Any]] = []
    for index, original in enumerate(observations):
        if index not in corrections:
            refined.append(original)
            continue
        merged = {**original, **corrections[index], "reviewed": True}
        merged["timestamp"] = original["timestamp"]
        merged["source_image"] = original.get("source_image")
        if not isinstance(merged.get("visible_text"), list):
            merged["visible_text"] = [str(merged["visible_text"])]
        if not isinstance(merged.get("important_details"), list):
            merged["important_details"] = [str(merged["important_details"])]
        refined.append(merged)
    return refined

def merge_timeline(transcript: dict[str, Any], observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for segment in transcript.get("segments", []):
        events.append(
            {
                "timestamp": segment["start"],
                "end": segment.get("end"),
                "type": "speech",
                "text": segment["text"],
                "source": {key: value for key, value in segment.items() if key not in {"start", "end", "text"}},
            }
        )
    for observation in observations:
        events.append({"timestamp": observation["timestamp"], "type": "visual", "data": observation})
    return sorted(events, key=lambda event: (event["timestamp"], event["type"]))


def local_observations(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "timestamp": candidate["timestamp"],
            "screen_type": "候選畫面",
            "visible_text": [line for line in str(candidate.get("ocr_text", "")).splitlines() if line],
            "visual_summary": "尚未執行 MiniMax 視覺理解。",
            "important_details": [],
            "confidence": candidate.get("ocr_confidence"),
            "source_image": candidate.get("path"),
            "local_ocr_text": candidate.get("ocr_text", ""),
            "local_ocr_confidence": candidate.get("ocr_confidence"),
        }
        for candidate in candidates
    ]



def timeline_chunks(
    timeline: list[dict[str, Any]],
    max_chars: int,
    overlap_events: int = 2,
) -> list[list[dict[str, Any]]]:
    """按事件邊界切分，不遺漏尾端；相鄰片段保留少量上下文重疊。"""
    if max_chars <= 0:
        raise ValueError("max_chars 必須大於 0。")
    if not timeline:
        return [[]]
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_size = 0
    for event in timeline:
        event_size = len(json.dumps(event, ensure_ascii=False)) + 1
        if current and current_size + event_size > max_chars:
            chunks.append(current)
            overlap = max(0, min(overlap_events, len(current)))
            current = current[-overlap:] if overlap else []
            current_size = sum(len(json.dumps(item, ensure_ascii=False)) + 1 for item in current)
        current.append(event)
        current_size += event_size
    if current:
        chunks.append(current)
    return chunks


def summary_prompt(events: list[dict[str, Any]], *, strict: bool = False) -> str:
    instruction = (
        "你是影片知識整理員。只根據提供的時間軸事件輸出 JSON object。"
        "欄位必須包含 short_summary、chapters、steps、tools_and_terms、fact_check、source_timestamps。"
        "chapters 每項必須有 title、start、end、summary；fact_check 每項必須有 timestamp、claim、reason。"
        "時間一律使用事件中的影片秒數；不要把相對序號當時間。"
        "不要捏造事件中沒有的內容，不確定的資訊放入 fact_check。"
        "所有說明文字用繁體中文，專有名詞可保留原文。"
    )
    if strict:
        instruction += (
            "上一次輸出格式不合格；這次只能輸出完整 JSON，所有陣列即使沒有內容也要保留。"
            "所有章節與查證時間都必須落在提供事件的時間範圍內。"
        )
    return instruction + "\n\n" + json.dumps(events, ensure_ascii=False)


def generate_summary_for_events(
    events: list[dict[str, Any]],
    api_key: str,
    model: str,
    duration: float,
) -> dict[str, Any]:
    prompt = summary_prompt(events)
    summary = parse_json_object(
        minimax_chat(
            [{"role": "user", "content": prompt}],
            api_key,
            model,
            timeout=900,
            thinking="adaptive",
        )
    )
    errors = validate_summary(summary, duration)
    if errors:
        print(f"! 摘要結構驗證失敗，重試一次：{'; '.join(errors[:4])}")
        summary = parse_json_object(
            minimax_chat(
                [{"role": "user", "content": summary_prompt(events, strict=True)}],
                api_key,
                model,
                timeout=900,
                thinking="adaptive",
            )
        )
        errors = validate_summary(summary, duration)
    if errors:
        raise ArtifactValidationError("摘要未通過驗證：" + "; ".join(errors[:8]))
    return summary


def merge_summary_parts(
    parts: list[dict[str, Any]],
    api_key: str,
    model: str,
    duration: float,
) -> dict[str, Any]:
    compact_parts = [
        {
            "chunk": index,
            "short_summary": part.get("short_summary", ""),
            "chapters": part.get("chapters", []),
            "steps": part.get("steps", []),
            "tools_and_terms": part.get("tools_and_terms", []),
            "fact_check": part.get("fact_check", []),
            "source_timestamps": part.get("source_timestamps", []),
        }
        for index, part in enumerate(parts)
    ]
    prompt = (
        "你是影片知識庫總編。請整合下列各章節摘要，輸出完整 JSON object。"
        "必須包含 short_summary、chapters、steps、tools_and_terms、fact_check、source_timestamps。"
        "保留所有重要章節與原始時間，不可只保留前面的片段；不可捏造。"
        "所有說明文字用繁體中文，專有名詞可保留原文。"
        "\n\n"
        + json.dumps(compact_parts, ensure_ascii=False)
    )
    summary = parse_json_object(
        minimax_chat(
            [{"role": "user", "content": prompt}],
            api_key,
            model,
            timeout=900,
            thinking="adaptive",
        )
    )
    errors = validate_summary(summary, duration)
    if errors:
        print(f"! 全片摘要結構驗證失敗，重試一次：{'; '.join(errors[:4])}")
        summary = parse_json_object(
            minimax_chat(
                [{"role": "user", "content": prompt + "\n只能輸出通過欄位與時間驗證的 JSON。"}],
                api_key,
                model,
                timeout=900,
                thinking="adaptive",
            )
        )
        errors = validate_summary(summary, duration)
    if errors:
        raise ArtifactValidationError("全片摘要未通過驗證：" + "; ".join(errors[:8]))
    source_timestamps = {
        round(float(timestamp), 3)
        for part in parts
        for timestamp in (part.get("source_timestamps") or [])
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool)
    }
    for timestamp in summary.get("source_timestamps") or []:
        if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
            source_timestamps.add(round(float(timestamp), 3))
    summary["source_timestamps"] = sorted(source_timestamps)
    summary["coverage"] = {"mode": "hierarchical", "chunks": len(parts)}
    return summary


def summary_input(timeline: list[dict[str, Any]], max_chars: int) -> list[dict[str, Any]]:
    # 舊呼叫端仍可取得第一段；正式摘要流程使用 timeline_chunks，不會靜默捨棄尾端。
    return timeline_chunks(timeline, max_chars)[0]


def create_summary(
    timeline: list[dict[str, Any]],
    api_key: str,
    model: str,
    max_chars: int,
    duration: float = 0.0,
    stage_signature_value: str = "",
    cache_dir: Path | None = None,
    workers: int = 2,
    refresh: bool = False,
) -> dict[str, Any]:
    if not timeline:
        return {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "short_summary": "影片中沒有可用的語音或畫面事件。",
            "chapters": [],
            "steps": [],
            "tools_and_terms": [],
            "fact_check": [],
            "source_timestamps": [],
            "coverage": {"mode": "empty", "chunks": 0},
        }
    chunk_limit = min(max_chars, SUMMARY_CHUNK_CHARS)
    chunks = timeline_chunks(timeline, chunk_limit)
    if len(chunks) == 1:
        summary = generate_summary_for_events(chunks[0], api_key, model, duration)
        summary["schema_version"] = ARTIFACT_SCHEMA_VERSION
        summary["coverage"] = {"mode": "single_pass", "chunks": 1, "event_count": len(timeline)}
        return summary

    part_dir = cache_dir or Path(tempfile.mkdtemp(prefix="summary-parts-"))
    part_dir.mkdir(parents=True, exist_ok=True)
    parts: list[dict[str, Any]] = []
    for index, events in enumerate(chunks):
        chunk_config_signature = stage_signature(
            "summary_chunk_config",
            model,
            {
                "model": model,
                "prompt": PROMPT_VERSIONS["summary_chunk"],
                "duration": round(duration, 3),
            },
        )
        signature = stage_signature(
            "summary_chunk",
            chunk_config_signature,
            {
                "chunk": index,
                "events": digest_value(events),
            },
        )
        path = part_dir / f"summary_{index:04d}.json"
        cached = None if refresh else read_stage_part(path, "summary_chunk", signature)
        if isinstance(cached, dict) and not validate_summary(cached, duration):
            parts.append(cached)
            continue
        part = generate_summary_for_events(events, api_key, model, duration)
        write_stage_part(path, "summary_chunk", signature, part, chunk=index)
        parts.append(part)
    summary = merge_summary_parts(parts, api_key, model, duration)
    summary["schema_version"] = ARTIFACT_SCHEMA_VERSION
    summary["coverage"]["event_count"] = len(timeline)
    summary["coverage"]["chunk_sizes"] = [len(events) for events in chunks]
    return summary

def source_link(url: Any, timestamp: Any) -> str:
    """把來源 URL 加上時間戳，讓每筆結果都能直接跳到影片的那一秒。

    認不出的網域就原樣回傳——寧可少個參數，也不要生出打不開的連結。
    """
    if not url or not isinstance(url, str):
        return ""
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
        return url
    seconds = max(0, int(timestamp))
    if not seconds:
        return url
    joiner = "&" if "?" in url else "?"
    if "youtube.com" in url or "youtu.be" in url:
        return f"{url}{joiner}t={seconds}s"
    if "facebook.com" in url or "fb.watch" in url:
        return f"{url}{joiner}t={seconds}"
    return url


def format_timestamp(value: Any) -> str:
    total = max(0, int(float(value or 0)))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def render_knowledge_markdown(
    job_dir: Path,
    source: dict[str, Any],
    video_path: Path,
    transcript: dict[str, Any],
    observations: list[dict[str, Any]],
    timeline: list[dict[str, Any]],
    summary: dict[str, Any] | None,
) -> Path:
    # 用真實影片標題；退回檔名的話每支影片在 Open Notebook 裡都叫「source」，認不出來。
    title = source.get("title") or Path(source.get("resolved_video") or video_path.name).stem
    lines = [f"# {title}", "", "## 來源", ""]
    if source.get("url"):
        lines.append(f"- URL：{source['url']}")
    lines.extend(
        [
            f"- 影片檔：`{video_path.name}`",
            f"- 工作目錄：`{job_dir.name}`",
            "- 內容管線：yt-dlp／FFmpeg／Groq Whisper／steipete 或 native／MiniMax",
            "",
        ]
    )

    if summary and summary.get("short_summary"):
        lines.extend(["## 摘要", "", str(summary["short_summary"]), ""])
    elif summary and summary.get("status") == "skipped":
        lines.extend(["## 摘要", "", "尚未執行雲端摘要。", ""])

    chapters = (summary or {}).get("chapters") or []
    if chapters:
        lines.extend(["## 章節", ""])
        for chapter in chapters:
            if not isinstance(chapter, dict):
                continue
            start = format_timestamp(chapter.get("start"))
            end = format_timestamp(chapter.get("end"))
            jump = source_link(source.get("url"), chapter.get("start"))
            label = f"[`{start}–{end}`]({jump})" if jump else f"`{start}–{end}`"
            lines.append(f"- {label} **{chapter.get('title', '未命名')}**：{chapter.get('summary', '')}")
        lines.append("")

    steps = (summary or {}).get("steps") or []
    if steps:
        lines.extend(["## 操作步驟", ""])
        lines.extend(f"{index}. {step}" for index, step in enumerate(steps, 1))
        lines.append("")

    terms = (summary or {}).get("tools_and_terms") or []
    if terms:
        lines.extend(["## 工具與專有名詞", "", ", ".join(map(str, terms)), ""])

    fact_checks = (summary or {}).get("fact_check") or []
    if fact_checks:
        lines.extend(["## 待查證資訊", ""])
        for item in fact_checks:
            if isinstance(item, dict):
                # 待查證的東西最需要能一鍵跳回原片確認
                jump = source_link(source.get("url"), item.get("timestamp"))
                stamp = format_timestamp(item.get("timestamp"))
                label = f"[`{stamp}`]({jump})" if jump else f"`{stamp}`"
                lines.append(f"- {label} {item.get('claim', '')}；原因：{item.get('reason', '')}")
        lines.append("")

    lines.extend(["## 時間軸", ""])
    if not timeline:
        lines.append("尚未產生時間軸事件。")
    for event in timeline:
        timestamp = format_timestamp(event.get("timestamp"))
        jump = source_link(source.get("url"), event.get("timestamp"))
        stamp = f"[`{timestamp}`]({jump})" if jump else f"`{timestamp}`"
        if event.get("type") == "speech":
            lines.append(f"- {stamp} 語音：{event.get('text', '')}")
            continue
        data = event.get("data") or {}
        screen_type = data.get("screen_type", "畫面")
        visual_summary = data.get("visual_summary", "")
        ocr_text = data.get("local_ocr_text") or "／".join(data.get("visible_text") or [])
        detail = f"{screen_type}"
        if visual_summary:
            detail += f"；{visual_summary}"
        if ocr_text:
            detail += f"；OCR：{ocr_text}"
        image_name = Path(str(data.get("source_image", ""))).name
        if image_name:
            detail += f"（畫面：`{image_name}`）"
        lines.append(f"- {stamp} 畫面：{detail}")

    lines.extend(
        [
            "",
            "## 處理狀態",
            "",
            f"- 語音段落：{len(transcript.get('segments', []))}",
            f"- 視覺觀察：{len(observations)}",
            "- 本檔案由影片資料庫管線產生，可重新執行以更新內容。",
            "",
        ]
    )
    output = job_dir / "knowledge.md"
    write_text(output, "\n".join(lines).rstrip() + "\n")
    return output


def open_notebook_sync(
    knowledge_path: Path,
    base_url: str,
    notebook_id: str,
    password: str,
    sources_path: str,
    wait_seconds: int,
    title: str = "",
    replace_source_id: str = "",
) -> dict[str, Any]:
    base_url = base_url.rstrip("/")
    sources_url = base_url + "/" + sources_path.strip("/")
    auth_headers = {"Authorization": f"Bearer {password}", "X-Password": password}

    replaced = False
    if replace_source_id:
        # 重跑同一支影片時先撤掉上次的條目，否則 notebook 會積出一排同名 source。
        try:
            request_json(
                f"{base_url}/api/sources/{replace_source_id}",
                auth_headers,
                method="DELETE",
                timeout=30,
            )
            replaced = True
        except RuntimeError as exc:
            # 使用者可能已經在 UI 手動刪掉了，那不是錯誤。
            if "HTTP 404" not in str(exc):
                raise

    fields = {"type": "upload", "notebook_id": notebook_id, "async_processing": "true"}
    if title:
        # 不傳的話 Open Notebook 拿檔名當標題，整個 notebook 會是一排 knowledge.md
        fields["title"] = title
    body, boundary = multipart_body(fields, "file", knowledge_path)
    headers = {**auth_headers, "Content-Type": f"multipart/form-data; boundary={boundary}"}
    response = post_json(sources_url, headers, body)
    source_id = response.get("id") or ""
    command_id = response.get("command_id") or response.get("commandId")
    if not command_id:
        return {"status": "accepted", "source_id": source_id, "replaced": replaced, "response": response}

    deadline = time.monotonic() + max(0, wait_seconds)
    # 實測 v1-latest：/commands/{id} 是 404，正確端點是 /api/commands/jobs/{id}，
    # 且 id 必須保留 command: 前綴（去掉會 500）。
    command_url = f"{base_url}/api/commands/jobs/{command_id}"
    while time.monotonic() < deadline:
        status_data = get_json(command_url, auth_headers)
        status = str(
            status_data.get("status")
            or status_data.get("state")
            or (status_data.get("command") or {}).get("status", "")
        ).lower()
        if status in {"completed", "complete", "success", "succeeded", "done"}:
            return {
                "status": "completed",
                "command_id": command_id,
                "source_id": source_id,
                "replaced": replaced,
                "response": status_data,
            }
        if status in {"failed", "error", "cancelled", "canceled"}:
            raise RuntimeError(f"Open Notebook source processing 失敗：{status_data}")
        time.sleep(2)
    return {"status": "pending", "command_id": command_id, "source_id": source_id, "replaced": replaced}


def flatten_text(value: Any) -> str:
    """把巢狀的 summary／visual 觀察攤成一串可搜尋文字。"""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return " ".join(flatten_text(item) for item in value.values())
    if isinstance(value, list):
        return " ".join(flatten_text(item) for item in value)
    return ""



def index_connection(root: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(root / "library.db")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS videos (
            job TEXT PRIMARY KEY,
            title TEXT,
            source TEXT,
            status TEXT,
            duration REAL,
            indexed_at TEXT,
            categories TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS events USING fts5(
            job UNINDEXED,
            timestamp UNINDEXED,
            kind UNINDEXED,
            text,
            tokenize='trigram'
        );
        """
    )
    required_video_columns = {"job", "title", "source", "status", "duration", "indexed_at", "categories"}
    video_columns = {row[1] for row in connection.execute("PRAGMA table_info(videos)")}
    if video_columns and not required_video_columns.issubset(video_columns):
        connection.execute("DROP TABLE videos")
        connection.execute(
            """
            CREATE TABLE videos (
                job TEXT PRIMARY KEY, title TEXT, source TEXT, status TEXT,
                duration REAL, indexed_at TEXT, categories TEXT
            )
            """
        )
    required_vector_columns = {"text_hash", "model", "dim", "vector"}
    vector_columns = {row[1] for row in connection.execute("PRAGMA table_info(vectors)")}
    if vector_columns and not required_vector_columns.issubset(vector_columns):
        connection.execute("DROP TABLE vectors")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS vectors (
            text_hash TEXT,
            model TEXT,
            dim INTEGER,
            vector BLOB,
            PRIMARY KEY (text_hash, model)
        )
        """
    )
    return connection

def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]



def index_job(connection: sqlite3.Connection, job_dir: Path) -> int:
    manifest_path = job_dir / "manifest.json"
    if not manifest_path.is_file():
        return 0
    manifest = read_json_file(manifest_path)
    if not isinstance(manifest, dict):
        return 0
    job = job_dir.name
    if (manifest.get("status") or "") == "failed":
        old_texts = [
            str(row[0])
            for row in connection.execute("SELECT text FROM events WHERE job = ?", (job,)).fetchall()
        ]
        connection.execute("DELETE FROM events WHERE job = ?", (job,))
        connection.execute("DELETE FROM videos WHERE job = ?", (job,))
        # 向量由不同 job 共用；只有確認沒有其他事件仍使用同一文字時才刪除。
        for text in set(old_texts):
            if connection.execute("SELECT 1 FROM events WHERE text = ? LIMIT 1", (text,)).fetchone() is None:
                connection.execute("DELETE FROM vectors WHERE text_hash = ?", (text_hash(text),))
        return 0

    source = manifest.get("source") or {}
    connection.execute("DELETE FROM events WHERE job = ?", (job,))
    connection.execute(
        "INSERT OR REPLACE INTO videos VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            job,
            source.get("title") or "",
            source.get("url") or source.get("video") or "",
            manifest.get("status") or "",
            (manifest.get("video") or {}).get("duration_seconds"),
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            json.dumps(manifest.get("categories") or [], ensure_ascii=False),
        ),
    )

    rows: list[tuple[str, Any, str, str]] = []
    if source.get("title"):
        rows.append((job, 0, "title", str(source["title"])))
    timeline = read_json_file(job_dir / "timeline.json") or []
    if isinstance(timeline, list):
        for event in timeline:
            if not isinstance(event, dict):
                continue
            if event.get("type") == "speech":
                text = str(event.get("text", "")).strip()
            else:
                text = flatten_text(event.get("data")).strip()
            if text:
                rows.append((job, event.get("timestamp"), str(event.get("type", "")), text))

    summary = read_json_file(job_dir / "summary.json")
    if isinstance(summary, dict) and summary.get("status") != "skipped":
        text = flatten_text(summary).strip()
        if text:
            rows.append((job, 0, "summary", text))
        for chapter in summary.get("chapters") or []:
            if not isinstance(chapter, dict) or not chapter.get("title"):
                continue
            detail = f"{chapter['title']}：{chapter.get('summary', '')}".strip("：")
            rows.append((job, chapter.get("start") or 0, "chapter", detail))

    connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", rows)
    return len(rows)


def build_index(root: Path) -> tuple[Path, int, int]:
    connection = index_connection(root)
    try:
        job_dirs = sorted(path.parent for path in root.glob("jobs/*/manifest.json"))
        job_names = {path.name for path in job_dirs}
        if job_names:
            placeholders = ",".join("?" for _ in job_names)
            params = tuple(sorted(job_names))
            connection.execute(f"DELETE FROM events WHERE job NOT IN ({placeholders})", params)
            connection.execute(f"DELETE FROM videos WHERE job NOT IN ({placeholders})", params)
        else:
            connection.execute("DELETE FROM events")
            connection.execute("DELETE FROM videos")
        total = sum(index_job(connection, job_dir) for job_dir in job_dirs)
        connection.commit()
        indexed = connection.execute("SELECT count(*) FROM videos").fetchone()[0]
    finally:
        connection.close()
    return root / "library.db", indexed, total

def make_snippet(text: str, query: str, width: int = SNIPPET_WIDTH) -> str:
    """LIKE 掃描沒有 FTS5 的 snippet()，自己截一段並把命中詞框起來。"""
    position = text.lower().find(query.lower())
    if position < 0:
        return text[: width * 2]
    end_of_hit = position + len(query)
    start = max(0, position - width)
    end = min(len(text), end_of_hit + width)
    prefix = "… " if start > 0 else ""
    suffix = " …" if end < len(text) else ""
    return f"{prefix}{text[start:position]}《{text[position:end_of_hit]}》{text[end_of_hit:end]}{suffix}"


def like_pattern(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def search_index(root: Path, query: str, limit: int, category: str = "") -> list[dict[str, Any]]:
    db_path = root / "library.db"
    if not db_path.is_file():
        raise RuntimeError(f"索引不存在：{db_path}；先執行 `--index`。")

    if not query.strip():
        raise RuntimeError("搜尋字串不可為空。")

    # categories 存成 JSON 陣列，用 LIKE 比對就夠——分類名是固定清單，不會誤匹配
    category_clause = " AND videos.categories LIKE ?" if category else ""
    scan = len(query) < 3
    if scan:
        # ponytail: trigram 索引窗口是 3 字元，「AI」「影片」這種兩字詞 MATCH 一定落空，
        # 改掃全表。實測 5 萬列 80 ms（含下面的排序），線性成長；
        # 真的大到會慢再換 jieba 斷詞另建 bigram 欄位。
        where = "events.text LIKE ? ESCAPE '\\'"
        # LIKE 沒有 FTS5 的 rank。自己排：標題命中最能代表整支影片，摘要次之，
        # 逐字稿／畫面最後；同級再比出現次數（SQLite 無 occurrence 函式，用 replace 的長度差算）。
        order = (
            "CASE events.kind WHEN 'title' THEN 0 WHEN 'summary' THEN 1 ELSE 2 END, "
            "(length(events.text) - length(replace(lower(events.text), ?, ''))) DESC, "
            "events.rowid"
        )
        snippet_column = "events.text"
        # 參數順序必須跟 SQL 中出現的順序一致：WHERE → 分類 → ORDER BY → LIMIT
        category_params: tuple[Any, ...] = (f"%{category}%",) if category else ()
        params: tuple[Any, ...] = (like_pattern(query),) + category_params + (query.lower(), limit)
    else:
        # 整段當 phrase 查，否則使用者輸入的 - 或 " 會被當成 FTS5 運算子而語法錯誤。
        where = "events.text MATCH ?"
        order = "rank"
        snippet_column = f"snippet(events, 3, '《', '》', ' … ', {SNIPPET_WIDTH})"
        category_params = (f"%{category}%",) if category else ()
        params = ('"' + query.replace('"', '""') + '"',) + category_params + (limit,)

    connection = index_connection(root)
    try:
        cursor = connection.execute(
            f"""
            SELECT videos.title, videos.source, events.job, events.timestamp,
                   events.kind, {snippet_column}, events.text
            FROM events JOIN videos ON videos.job = events.job
            WHERE {where}{category_clause}
            ORDER BY {order}
            LIMIT ?
            """,
            params,
        )
        return [
            {
                "title": row[0],
                "source": row[1],
                "job": row[2],
                "timestamp": row[3],
                "kind": row[4],
                "snippet": make_snippet(row[5], query) if scan else row[5],
                "text": row[6],  # 完整原文：snippet 只有 16 字窗口，餵給 LLM 不夠用
                "mode": "scan" if scan else "index",
            }
            for row in cursor
        ]
    finally:
        connection.close()


_LOCAL_EMBEDDERS: dict[str, Any] = {}


def embed_texts_local(texts: list[str], model: str) -> list[list[float]]:
    """本機 sentence-transformers。免費、離線、內容不外傳。"""
    if model not in _LOCAL_EMBEDDERS:
        try:
            import torch
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "本機嵌入需要 sentence-transformers 與 torch；"
                "或改用 `--embed-backend openrouter`。"
            ) from exc
        device = "cuda" if torch.cuda.is_available() else "cpu"
        _LOCAL_EMBEDDERS[model] = SentenceTransformer(model, device=device)
    encoded = _LOCAL_EMBEDDERS[model].encode(texts, normalize_embeddings=False)
    return [vector.tolist() for vector in encoded]


def embed_texts(texts: list[str], api_key: str, model: str, backend: str = "openrouter") -> list[list[float]]:
    if backend == "local":
        return embed_texts_local(texts, model)
    return embed_texts_openrouter(texts, api_key, model)


def embed_texts_openrouter(texts: list[str], api_key: str, model: str) -> list[list[float]]:
    """OpenRouter 的 embeddings 端點。模型清單查不到它、文件也沒寫，但實測可用。"""
    response = post_json(
        os.getenv("EMBED_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/embeddings",
        {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json.dumps({"model": model, "input": texts}, ensure_ascii=False).encode("utf-8"),
    )
    items = response.get("data") or []
    if len(items) != len(texts):
        raise RuntimeError(f"embedding 回傳數量不符：送 {len(texts)} 筆、收 {len(items)} 筆")
    return [item["embedding"] for item in items]


def pack_vector(vector: list[float]) -> bytes:
    return array.array("f", vector).tobytes()


def unpack_vector(blob: bytes) -> array.array:
    vector = array.array("f")
    vector.frombytes(blob)
    return vector


def similarity_cutoff(top_score: float, threshold: float | None = None) -> float:
    if threshold is not None:
        return threshold
    return max(SIMILARITY_FLOOR, top_score * SIMILARITY_RATIO)


def cosine(left: Any, right: Any) -> float:
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    return dot / (left_norm * right_norm) if left_norm and right_norm else 0.0


# 免費額度算「呼叫次數」不算 token，所以批次要盡量塞滿。
# 實測 OpenRouter embeddings 上限：256 可用，512 回 "exceeds maximum allowed batch size"。
# 4,203 筆片段：batch=32 要 132 次，batch=256 只要 17 次。
EMBED_BATCH_SIZE = 256


def build_embeddings(
    root: Path, api_key: str, model: str, backend: str = "openrouter", batch_size: int = EMBED_BATCH_SIZE
) -> tuple[int, int]:
    """只補算這個模型還沒算過的片段：重跑不重複付費，換模型則自動重算。"""
    connection = index_connection(root)
    try:
        have = {row[0] for row in connection.execute("SELECT text_hash FROM vectors WHERE model = ?", (model,))}
        seen: set[str] = set()
        pending: list[tuple[str, str]] = []
        for (text,) in connection.execute("SELECT text FROM events WHERE length(text) > 0"):
            digest = text_hash(text)
            if digest in have or digest in seen:
                continue  # 內容相同的片段共用一份向量，不重複付費
            seen.add(digest)
            pending.append((digest, text))

        done = 0
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            vectors = embed_texts([text for _, text in batch], api_key, model, backend)
            connection.executemany(
                "INSERT OR REPLACE INTO vectors VALUES (?, ?, ?, ?)",
                [
                    (digest, model, len(vector), pack_vector(vector))
                    for (digest, _), vector in zip(batch, vectors)
                ],
            )
            connection.commit()
            done += len(batch)
            print(f"  已嵌入 {done}/{len(pending)}")
        total = connection.execute("SELECT count(*) FROM vectors WHERE model = ?", (model,)).fetchone()[0]
        return done, total
    finally:
        connection.close()


# 各家模型的分數尺度差很多：bge-m3 無關項也有 0.33，nemotron 無關項只有 0.004。
# 寫死絕對門檻必定誤殺其中一種，改成「最高分的一半」再加一個很低的地板。
# 0.10 有實測依據：nemotron vl 對無關查詢最高只到 0.089，對正確命中是 0.158 起。
SIMILARITY_FLOOR = 0.10
SIMILARITY_RATIO = 0.5
# OpenRouter 預設走 :free（實測 cost=0）；本機走 bge-m3（HuggingFace 大小寫敏感）。
DEFAULT_EMBED_MODEL = {"openrouter": "nvidia/nemotron-3-embed-1b:free", "local": "BAAI/bge-m3"}



def semantic_search(
    root: Path,
    query: str,
    api_key: str,
    model: str,
    limit: int,
    backend: str = "openrouter",
    threshold: float | None = None,
    category: str = "",
) -> list[dict[str, Any]]:
    if not query.strip():
        raise RuntimeError("搜尋字串不可為空。")
    if limit <= 0:
        raise ValueError("limit 必須大於 0。")
    db_path = root / "library.db"
    if not db_path.is_file():
        raise RuntimeError(f"索引不存在：{db_path}；先執行 --index。")
    query_vector = embed_texts([query], api_key, model, backend)[0]
    connection = index_connection(root)
    try:
        stored = dict(
            connection.execute(
                "SELECT text_hash, vector FROM vectors WHERE model = ?", (model,)
            )
        )
        clauses = ["events.text IS NOT NULL"]
        params: list[Any] = []
        if category:
            clauses.append("videos.categories LIKE ?")
            params.append(f"%{category}%")
        rows = [
            (title, source, job, timestamp, kind, text, stored[text_hash(text)])
            for title, source, job, timestamp, kind, text in connection.execute(
                f"""
                SELECT videos.title, videos.source, events.job, events.timestamp,
                       events.kind, events.text
                FROM events JOIN videos ON videos.job = events.job
                WHERE {' AND '.join(clauses)}
                """,
                params,
            )
            if text_hash(text) in stored
        ]
    finally:
        connection.close()
    if not rows:
        raise RuntimeError(f"索引裡沒有 {model} 的向量；先執行 --embed。")
    scored = sorted(
        (
            {
                "title": row[0],
                "source": row[1],
                "job": row[2],
                "timestamp": row[3],
                "kind": row[4],
                "text": row[5],
                "snippet": row[5][:160],
                "score": cosine(query_vector, unpack_vector(row[6])),
                "mode": "semantic",
            }
            for row in rows
        ),
        key=lambda item: item["score"],
        reverse=True,
    )
    if not scored:
        return []
    cutoff = similarity_cutoff(scored[0]["score"], threshold)
    return [item for item in scored if item["score"] >= cutoff][:limit]


def hybrid_search(
    root: Path,
    query: str,
    api_key: str,
    embed_key: str,
    embed_model: str,
    embed_backend: str,
    limit: int,
    category: str = "",
    threshold: float | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """以 RRF 合併精確詞與語意結果；不同分數尺度不直接相加。"""
    if not query.strip():
        raise RuntimeError("搜尋字串不可為空。")
    lexical: list[dict[str, Any]] = []
    keywords: list[str] = []
    direct = search_index(root, query, max(limit * 4, 20), category)
    lexical.extend(direct)
    if not direct or len(query.strip()) > 12 or any(char.isspace() for char in query):
        try:
            keywords = question_keywords(
                query,
                api_key,
                os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
            )
        except RuntimeError:
            keywords = []
        for keyword in keywords:
            if keyword == query:
                continue
            try:
                lexical.extend(search_index(root, keyword, max(limit * 3, 12), category))
            except RuntimeError:
                continue
    if not keywords:
        keywords = [query]

    semantic = semantic_search(
        root,
        query,
        embed_key,
        embed_model,
        max(limit * 4, 20),
        embed_backend,
        threshold,
        category,
    )
    sources = [lexical, semantic]
    merged: dict[tuple[Any, ...], dict[str, Any]] = {}
    for source_rank, results in enumerate(sources):
        for rank, hit in enumerate(results, 1):
            key = (hit["job"], round(float(hit.get("timestamp") or 0), 3), hit["kind"])
            item = merged.setdefault(key, {**hit, "fusion_score": 0.0})
            item["fusion_score"] += 1.0 / (RRF_K + rank)
            if source_rank == 0:
                item["lexical_rank"] = rank
            else:
                item["semantic_rank"] = rank
                if "score" in hit:
                    item["score"] = hit["score"]
            item["modes"] = sorted(set(item.get("modes", []) + [hit.get("mode", "")]))
    ranked = sorted(
        merged.values(),
        key=lambda item: (
            -float(item["fusion_score"]),
            item.get("lexical_rank", 10**9),
            item.get("semantic_rank", 10**9),
        ),
    )
    return ranked[:limit], keywords


def question_keywords(question: str, api_key: str, model: str) -> list[str]:
    prompt = (
        "從問題抽出 2 到 5 個用於全文檢索的關鍵詞，只輸出 JSON："
        '{"keywords": ["...", "..."]}。'
        "關鍵詞要是會實際出現在影片逐字稿或畫面文字裡的名詞或動詞，"
        "去掉疑問詞與禮貌用語。"
        "\n\n問題：" + question
    )
    try:
        data = parse_json_object(
            minimax_chat(
                [{"role": "user", "content": prompt}],
                api_key,
                model,
                max_tokens=300,
                thinking="disabled",
            )
        )
        keywords = [
            str(item).strip()
            for item in (data.get("keywords") or [])
            if str(item).strip()
        ]
    except RuntimeError:
        keywords = []
    return keywords or [question]


def expand_hit_context(
    root: Path,
    hits: list[dict[str, Any]],
    window_seconds: float = 20.0,
) -> list[dict[str, Any]]:
    """為命中的原始事件補上同片段的相鄰語音與畫面證據。"""
    if not hits:
        return hits
    connection = index_connection(root)
    expanded: list[dict[str, Any]] = []
    try:
        for hit in hits:
            item = dict(hit)
            kind = str(item.get("kind") or "")
            timestamp = item.get("timestamp")
            try:
                point = float(timestamp)
            except (TypeError, ValueError):
                point = None
            if point is None or kind not in {"speech", "visual", "chapter"}:
                expanded.append(item)
                continue
            rows = connection.execute(
                """
                SELECT timestamp, kind, text
                FROM events
                WHERE job = ? AND timestamp BETWEEN ? AND ?
                  AND kind IN ('speech', 'visual')
                ORDER BY timestamp, rowid
                """,
                (
                    item.get("job"),
                    max(0.0, point - window_seconds),
                    point + window_seconds,
                ),
            ).fetchall()
            seen: set[tuple[Any, ...]] = set()
            context_rows: list[str] = []
            for row_timestamp, row_kind, row_text in rows:
                key = (row_timestamp, row_kind, row_text)
                if key in seen:
                    continue
                seen.add(key)
                context_rows.append(
                    f"{format_timestamp(row_timestamp)}（{row_kind}）：{row_text}"
                )
            if context_rows:
                item["context_text"] = " | ".join(context_rows)
            expanded.append(item)
    finally:
        connection.close()
    return expanded


def answer_question(
    root: Path,
    question: str,
    api_key: str,
    model: str,
    limit: int,
    embed_key: str = "",
    embed_model: str = "",
    embed_backend: str = "",
    category: str = "",
) -> dict[str, Any]:
    if embed_backend:
        hits, keywords = hybrid_search(
            root,
            question,
            api_key,
            embed_key,
            embed_model,
            embed_backend,
            limit,
            category,
        )
    else:
        direct = search_index(root, question, max(limit * 3, 20), category)
        if direct:
            keywords = [question]
            hits = direct[:limit]
        else:
            keywords = question_keywords(question, api_key, model)
            per_keyword = [search_index(root, keyword, limit, category) for keyword in keywords]
            seen: set[tuple[Any, ...]] = set()
            hits = []
            for rank in range(limit):
                for results in per_keyword:
                    if rank >= len(results):
                        continue
                    hit = results[rank]
                    key = (hit["job"], hit["timestamp"], hit["kind"])
                    if key in seen:
                        continue
                    seen.add(key)
                    hits.append(hit)
                    if len(hits) >= limit:
                        break
                if len(hits) >= limit:
                    break
    if not hits:
        return {"question": question, "keywords": keywords, "answer": "索引裡找不到相關片段。", "citations": []}

    context_hits = expand_hit_context(root, hits)
    context = "\n".join(
        f"[{index}] 《{hit['title'] or hit['job']}》 {format_timestamp(hit['timestamp'])} "
        f"（{hit['kind']}）：{hit.get('context_text') or hit['text']}"
        for index, hit in enumerate(context_hits, 1)
    )
    prompt = (
        "你是影片知識庫的問答助手。只根據下列片段回答問題，"
        "不要引入片段以外的知識；片段不足以回答就直說找不到。"
        "回答用繁體中文，並在每個論點後標註引用編號如 [1]、[3]。"
        "摘要或章節只能用來定位，涉及事實時優先引用 speech 或 visual 原始事件。"
        f"\n\n問題：{question}\n\n片段：\n{context}"
    )
    answer = minimax_chat(
        [{"role": "user", "content": prompt}],
        api_key,
        model,
        thinking="adaptive",
    )
    return {"question": question, "keywords": keywords, "answer": answer, "citations": hits}

def build_digest(root: Path, api_key: str, model: str, max_chars: int = 90000) -> Path:
    """把每支影片的摘要合成一份全頻道總覽。

    單支影片的 chapters／steps／術語表在 summary.json 裡已經有了，
    這裡補的是它們看不到的東西：主題怎麼分群、哪些術語反覆出現、該照什麼順序看。
    """
    entries = load_video_entries(root)
    if not entries:
        raise RuntimeError("沒有可用的摘要；先跑完整流程（不加 --local-only／--dry-run）再產生總覽。")

    # 有分類就由程式碼分群：模型每次都會漏掉不同的幾支，而 GROUP BY 不會。
    catalog = reuse_artifact(root / "categories.json") or {}
    order = [str(c) for c in (catalog.get("categories") or [])]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in entries:
        for category in item["categories"] or ["未分類"]:
            grouped.setdefault(category, []).append(item)
    if grouped and any(item["categories"] for item in entries):
        lines = []
        for category in order + [c for c in grouped if c not in order]:
            members = grouped.get(category)
            if not members:
                continue
            lines.append(f"\n### {category}（{len(members)} 支）\n")
            for item in sorted(members, key=lambda x: -float(x["minutes"] or 0)):
                brief = (item["summary"] or "").split("。")[0][:70]
                lines.append(f"- [{item['title']}]({item['url']}) · {item['minutes']} 分 — {brief}")
        grouping = "\n".join(lines)
        prompt = (
            "下面是已經分好類的影片清單，分類與歸屬都已確定。請用繁體中文輸出 Markdown，"
            "**不要重複列出影片清單**，只要：\n"
            "1. 一段整體描述，說明這批影片涵蓋什麼\n"
            "2. `## 反覆出現的工具與術語`：跨影片出現多次的詞，標註出現在哪幾支\n"
            "3. `## 建議觀看順序`：依分類排出從入門到進階的路徑並說明理由\n"
            "4. `## 涵蓋落差`：這批影片沒談到、但主題上應該要有的內容\n\n"
            + json.dumps(
                [
                    {"category": c, "titles": [m["title"] for m in grouped[c]], "terms": [t for m in grouped[c] for t in m["terms"]][:20]}
                    for c in grouped
                ],
                ensure_ascii=False,
            )[:max_chars]
        )
        report = minimax_chat([{"role": "user", "content": prompt}], api_key, model, timeout=900)
        header = f"# 影片知識庫總覽\n\n- 影片數：{len(entries)}\n- 分類數：{len(grouped)}\n\n"
        output = root / "digest.md"
        write_text(output, header + report.rstrip() + "\n\n## 主題分群\n" + grouping + "\n")
        return output

    # 影片夠多時 prompt 會爆掉。截斷要明說截了多少——悄悄砍掉會讓人以為總覽涵蓋全部。
    dropped = 0
    while len(json.dumps(entries, ensure_ascii=False)) > max_chars and len(entries) > 1:
        entries.pop()
        dropped += 1
    if dropped:
        print(f"! 摘要總量超過 {max_chars} 字元上限，本次總覽只涵蓋前 {len(entries)} 支，略過 {dropped} 支")

    prompt = (
        "你是知識庫編輯。下面是同一批影片各自的摘要，請輸出一份 Markdown 總覽，"
        "全文用繁體中文，專有名詞保留原文。必須包含：\n"
        "1. 一段整體描述，說明這批影片涵蓋什麼\n"
        "2. `## 主題分群`：把影片按主題歸類，每群列出影片與一句話說明；"
        "影片標題一律寫成 Markdown 連結 `[標題](url)`，url 用資料裡的欄位，不可杜撰\n"
        "3. `## 反覆出現的工具與術語`：跨影片出現多次的詞，標註出現在哪幾支\n"
        "4. `## 建議觀看順序`：從入門到進階排序並說明理由\n"
        "5. `## 涵蓋落差`：這批影片沒談到、但主題上應該要有的內容\n"
        "只根據提供的摘要，不要臆測影片沒有的內容。\n\n"
        + json.dumps(entries, ensure_ascii=False, indent=2)
    )
    # 總覽要輸出上百個標題與連結，reasoning 模型會跑很久，180 秒的預設值不夠
    report = minimax_chat([{"role": "user", "content": prompt}], api_key, model, timeout=900)

    # 模型會自行取捨，實測 142 支裡漏掉 8 支且毫無提示。與其重跑，不如補在末尾——
    # 「總覽」漏了東西卻不說，比列得不夠漂亮嚴重得多。
    missing = [item for item in entries if item["title"] and item["title"][:20] not in report]
    if missing:
        print(f"! 模型的分群漏掉 {len(missing)} 支，已補列在總覽末尾")
        report = report.rstrip() + "\n\n## 未列入上述分群的影片\n\n" + "\n".join(
            f"- [{item['title']}]({item['url']}) — {item['summary'] or ''}"[:300] for item in missing
        )

    header = f"# 影片知識庫總覽\n\n- 影片數：{len(entries)}\n\n"
    output = root / "digest.md"
    write_text(output, header + report.rstrip() + "\n")
    return output


CATEGORY_BATCH = 20


def load_video_entries(root: Path) -> list[dict[str, Any]]:
    """讀出所有已完成影片的標題與摘要，分類與總覽都用這份資料。"""
    entries: list[dict[str, Any]] = []
    for manifest_path in sorted(root.glob("jobs/*/manifest.json")):
        summary = reuse_artifact(manifest_path.parent / "summary.json", "short_summary")
        if summary is None:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if manifest.get("status") != "completed":
            continue
        source = manifest.get("source") or {}
        entries.append(
            {
                "job": manifest_path.parent.name,
                "title": source.get("title") or manifest_path.parent.name,
                "url": source.get("url") or source.get("video"),
                "minutes": round(float((manifest.get("video") or {}).get("duration_seconds") or 0) / 60, 1),
                "summary": summary.get("short_summary"),
                "chapters": [
                    chapter.get("title")
                    for chapter in (summary.get("chapters") or [])
                    if isinstance(chapter, dict)
                ][:6],
                "terms": (summary.get("tools_and_terms") or [])[:8],
                "categories": manifest.get("categories") or [],
            }
        )
    return entries


def categorize_library(root: Path, api_key: str, model: str) -> tuple[list[str], int]:
    """兩階段分類。

    一次全丟給模型分群，它每次都會漏掉不同的幾支（實測 142 支漏 5–8 支且無提示）。
    改成先產出固定的分類清單，再分批把影片歸進去——模型只能從清單裡選，
    標籤不會發散（實測自由標註時 12 支就長出 20 種標籤，只有 4 個被重用）。
    """
    entries = load_video_entries(root)
    if not entries:
        raise RuntimeError("沒有可用的摘要；先跑完整流程再分類。")

    catalog_path = root / "categories.json"
    existing = reuse_artifact(catalog_path) or {}
    categories = [str(c) for c in (existing.get("categories") or []) if str(c).strip()]
    catalog_is_new = not categories
    if not categories:
        overview = [{"title": item["title"], "summary": (item["summary"] or "")[:120]} for item in entries]
        prompt = (
            "看完這批影片，歸納出 8 到 12 個分類，只輸出 JSON：{\"categories\": [\"...\", ...]}。"
            "分類要粒度一致、彼此可區分、合起來能涵蓋全部影片；用繁體中文，技術名詞保留原文。"
            "不要用「其他」「雜項」這種收納類。\n\n" + json.dumps(overview, ensure_ascii=False)
        )
        data = parse_json_object(minimax_chat([{"role": "user", "content": prompt}], api_key, model, timeout=600))
        categories = [str(c).strip() for c in (data.get("categories") or []) if str(c).strip()]
        if not categories:
            raise RuntimeError("模型沒有回傳分類清單。")
        write_json(catalog_path, {"categories": categories})
        print(f"· 產生 {len(categories)} 個分類：{'、'.join(categories)}")
    else:
        print(f"· 沿用既有的 {len(categories)} 個分類（刪掉 categories.json 可重新產生）")

    # 分類體系沒變的話只歸類還沒分類的：新增一支影片不該重跑全部 142 支。
    # 想整批重來就刪掉 categories.json，那會重新產生體系並全部重歸。
    targets = entries if catalog_is_new else [item for item in entries if not item["categories"]]
    if not targets:
        print(f"· {len(entries)} 支影片都已分類，無需處理")
        return categories, 0
    print(f"· 待歸類 {len(targets)} 支（共 {len(entries)} 支）")

    assigned = 0
    for start in range(0, len(targets), CATEGORY_BATCH):
        batch = targets[start : start + CATEGORY_BATCH]
        payload = [
            {"job": item["job"], "title": item["title"], "summary": (item["summary"] or "")[:200]}
            for item in batch
        ]
        prompt = (
            "把每支影片歸進下列分類，每支選 1 到 2 個，**只能從清單裡挑**，不可自創：\n"
            + json.dumps(categories, ensure_ascii=False)
            + '\n\n只輸出 JSON：{"results": [{"job": "...", "categories": ["..."], "level": "入門|中階|進階"}]}\n\n'
            + json.dumps(payload, ensure_ascii=False)
        )
        data = parse_json_object(
            minimax_chat(
                [{"role": "user", "content": prompt}],
                api_key,
                model,
                max_tokens=2000,
                timeout=600,
                thinking="disabled",
            )
        )
        by_job = {str(r.get("job")): r for r in (data.get("results") or []) if isinstance(r, dict)}
        for item in batch:
            result = by_job.get(item["job"])
            if not result:
                continue
            picked = [str(c) for c in (result.get("categories") or []) if str(c) in categories]
            if not picked:
                continue
            manifest_path = root / "jobs" / item["job"] / "manifest.json"
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            manifest["categories"] = picked
            manifest["level"] = str(result.get("level") or "")
            write_json(manifest_path, manifest)
            assigned += 1
        print(f"  已歸類 {min(start + CATEGORY_BATCH, len(targets))}/{len(targets)}")
    return categories, assigned


def list_videos(root: Path, category: str = "") -> int:
    """列出影片。沒有這個就只能開 digest.md 或自己查 DB。"""
    db_path = root / "library.db"
    if not db_path.is_file():
        raise RuntimeError(f"索引不存在：{db_path}；先執行 `--index`。")
    connection = index_connection(root)
    try:
        rows = connection.execute(
            "SELECT title, source, duration, categories FROM videos ORDER BY duration DESC"
        ).fetchall()
    finally:
        connection.close()

    grouped: dict[str, list[tuple[Any, ...]]] = {}
    for title, source, duration, raw in rows:
        try:
            cats = json.loads(raw or "[]") or ["未分類"]
        except json.JSONDecodeError:
            cats = ["未分類"]
        for name in cats:
            if category and category not in name:
                continue
            grouped.setdefault(name, []).append((title, source, duration))

    shown = 0
    for name in sorted(grouped, key=lambda k: -len(grouped[k])):
        members = grouped[name]
        print(f"\n## {name}（{len(members)} 支）")
        for title, source, duration in members:
            print(f"  {float(duration or 0)/60:5.1f} 分  {title}")
            print(f"          {source}")
            shown += 1
    return shown


def show_video(root: Path, keyword: str) -> bool:
    """依關鍵字找出影片並印出摘要與章節。

    知道標題卻要先查出 12 碼 hash 才能打開 knowledge.md，這一步不該由人來做。
    """
    matches = [item for item in load_video_entries(root) if keyword.lower() in (item["title"] or "").lower()]
    if not matches:
        print(f"找不到標題含「{keyword}」的影片。")
        return False
    if len(matches) > 1:
        print(f"符合的有 {len(matches)} 支，請給更明確的關鍵字：")
        for item in matches[:10]:
            print(f"  - {item['title']}")
        return False

    item = matches[0]
    job_dir = root / "jobs" / item["job"]
    summary = reuse_artifact(job_dir / "summary.json", "short_summary") or {}
    print(f"# {item['title']}")
    print(f"  {item['url']}")
    print(f"  {item['minutes']} 分   分類：{'、'.join(item['categories']) or '未分類'}")
    print(f"\n## 摘要\n{summary.get('short_summary', '')}")
    chapters = [c for c in (summary.get("chapters") or []) if isinstance(c, dict)]
    if chapters:
        print("\n## 章節")
        for chapter in chapters:
            jump = source_link(item["url"], chapter.get("start"))
            print(f"  [{format_timestamp(chapter.get('start'))}] {chapter.get('title', '')}")
            print(f"      {jump}")
    terms = summary.get("tools_and_terms") or []
    if terms:
        print(f"\n## 工具與術語\n  {'、'.join(map(str, terms))}")
    print(f"\n完整內容：{job_dir / 'knowledge.md'}")
    return True



def audit_library(root: Path) -> list[str]:
    """健檢：檢查 job、階段產物、索引與向量是否一致。"""
    issues: list[str] = []
    jobs = sorted(path.parent for path in root.glob("jobs/*/manifest.json"))
    if not jobs:
        return ["沒有任何 job；先跑一次管線。"]

    completed: set[str] = set()
    failed: list[str] = []
    broken_summary: list[str] = []
    for job in jobs:
        manifest = read_json_file(job / "manifest.json")
        if not isinstance(manifest, dict):
            issues.append(f"{job.name}：manifest.json 讀不了")
            continue
        status = manifest.get("status") or ""
        if status == "failed":
            failed.append(job.name)
            continue
        if status not in {"completed", "local-only", "dry-run"}:
            issues.append(f"{job.name}：狀態停在 {status}（可能中途被中斷）")
        if status == "completed":
            completed.add(job.name)
            summary = read_json_file(job / "summary.json")
            if not isinstance(summary, dict) or validate_summary(summary, float((manifest.get("video") or {}).get("duration_seconds") or 0)):
                broken_summary.append(job.name)

    print(f"  影片：{len(jobs)} 支（completed {len(completed)}、failed {len(failed)}）")
    if failed:
        issues.append(f"{len(failed)} 支處理失敗：{', '.join(failed[:3])}{' …' if len(failed) > 3 else ''}")
    if broken_summary:
        issues.append(
            f"{len(broken_summary)} 支的摘要未通過結構或時間驗證；"
            "修正來源後重跑即可補齊"
        )

    db_path = root / "library.db"
    if not db_path.is_file():
        issues.append("尚未建立索引 → python pipeline.py --index")
        return issues

    connection = index_connection(root)
    try:
        indexed = {row[0] for row in connection.execute("SELECT job FROM videos")}
        events = connection.execute("SELECT count(*) FROM events").fetchone()[0]
        lengths = [row[0] for row in connection.execute("SELECT length(text) FROM events WHERE kind='speech'")]
        event_hashes = {
            text_hash(str(row[0]))
            for row in connection.execute("SELECT text FROM events WHERE length(text) > 0")
        }
        vectors = dict(connection.execute("SELECT model, count(*) FROM vectors GROUP BY model"))
    finally:
        connection.close()

    print(f"  索引：{len(indexed)} 支、{events} 筆片段")
    missing = completed - indexed
    stale = indexed - {path.name for path in jobs}
    if missing:
        issues.append(f"{len(missing)} 支已完成但不在索引裡 → python pipeline.py --index")
    if stale:
        issues.append(f"索引裡有 {len(stale)} 支已不存在於 jobs/ → python pipeline.py --index")

    if lengths:
        lengths.sort()
        median = lengths[len(lengths) // 2]
        print(f"  片段長度中位數：{median} 字元")
        if median < 60:
            issues.append(
                f"speech 片段中位數僅 {median} 字元，可能仍有碎片化問題"
            )

    if vectors:
        for model, count in vectors.items():
            print(f"  向量：{count} / {len(event_hashes)} 個唯一片段（{model}）")
            if count < len(event_hashes):
                issues.append(
                    f"{len(event_hashes) - count} 個唯一片段還沒有 {model} 的向量 → python pipeline.py --embed"
                )
    elif event_hashes:
        issues.append("尚未建立向量，--semantic 無法使用 → python pipeline.py --embed")
    return issues

def self_test() -> None:
    assert hash_distance("0000", "0000") == 0
    assert hash_distance("0000", "1111") == 4
    assert limit_items([1, 2, 3], 1) == [1]
    assert parse_json_object("```json\n{\"ok\": true}\n```")["ok"] is True
    timeline = merge_timeline(
        {"segments": [{"start": 3, "end": 4, "text": "語音"}]},
        [{"timestamp": 1, "screen_type": "畫面"}],
    )
    assert [event["timestamp"] for event in timeline] == [1, 3]
    assert format_timestamp(65.2) == "00:01:05"

    # 可溯源：搜尋結果要能直接跳到影片的那一秒
    youtube = "https://www.youtube.com/watch?v=abc"
    assert source_link(youtube, 3457) == youtube + "&t=3457s"
    assert source_link("https://youtu.be/abc", 90.7) == "https://youtu.be/abc?t=90s"  # 無 query 用 ?
    assert source_link("https://www.facebook.com/x/videos/1", 42) == "https://www.facebook.com/x/videos/1?t=42"
    assert source_link(youtube, 0) == youtube  # 0 秒不必加參數
    assert source_link("D:\\local\\clip.mp4", 30) == "D:\\local\\clip.mp4"  # 認不出就別亂加
    assert source_link(youtube, None) == youtube and source_link(youtube, True) == youtube
    assert source_link(None, 10) == ""
    assert local_observations([{"timestamp": 2, "ocr_text": "OCR"}])[0]["local_ocr_text"] == "OCR"

    # 字幕合併：逐行字幕併成段落，時間戳取第一行的開始
    rows = [
        {"start": 0.0, "end": 1.5, "text": "What's up folks?", "chunk": 0},
        {"start": 1.5, "end": 3.0, "text": "I'm going to build", "chunk": 0},
        {"start": 3.0, "end": 4.5, "text": "an MCP server.", "chunk": 0},
        {"start": 60.0, "end": 61.0, "text": "隔了很久之後的另一段", "chunk": 0},
    ]
    joined = merge_caption_segments(rows, max_chars=100, max_gap=2.0)
    assert len(joined) == 2, joined
    assert joined[0]["text"] == "What's up folks? I'm going to build an MCP server."
    assert joined[0]["start"] == 0.0 and joined[0]["end"] == 4.5
    assert joined[1]["start"] == 60.0  # 間隔超過 max_gap 就另起一段
    assert len(merge_caption_segments(rows, max_chars=20)) == 4  # 字數上限也會斷開
    assert merge_caption_segments([{"start": 0, "end": 1, "text": "  "}]) == []
    assert parse_caption_json3({"events": [{"tStartMs": 2000, "dDurationMs": 900, "segs": [{"utf8": "hi"}]}]}) == [
        {"start": 2.0, "end": 2.9, "text": "hi", "chunk": 0}
    ]

    # 靜默幻覺過濾：實測幻覺段 0.701、正常段最高 0.414
    assert is_silence(0.701) and is_silence(0.61)
    assert not is_silence(0.414) and not is_silence(0.028) and not is_silence(0.0)
    assert not is_silence(None) and not is_silence("0.9") and not is_silence(True)

    assert retry_delay(urllib.error.HTTPError("u", 429, "", {"Retry-After": "7"}, None), 2.0) == 7.0
    assert retry_delay(urllib.error.HTTPError("u", 429, "", {"Retry-After": "999"}, None), 2.0) == 120.0
    assert retry_delay(urllib.error.HTTPError("u", 500, "", {}, None), 3.0) == 3.0

    # 併發回收後必須還原輸入順序，否則時間軸會跟錯圖。
    # 讓越前面的圖睡越久，完成順序必定反轉：沒排序就會被抓到。
    count = 6
    original_chat, original_data_url = minimax_chat, image_data_url
    globals()["image_data_url"] = lambda path, **kwargs: "data:image/jpeg;base64,x"
    globals()["minimax_chat"] = lambda messages, *rest, **kwargs: (
        lambda tag: (time.sleep((count - int(tag[3:])) * 0.02), json.dumps({"tag": tag}))[1]
    )(messages[0]["content"][0]["text"].rsplit("：", 1)[-1])
    try:
        observations = analyze_images(
            [{"timestamp": i, "path": f"f{i}.jpg", "ocr_text": f"OCR{i}"} for i in range(count)],
            "key",
            "model",
            workers=4,
        )
    finally:
        globals()["minimax_chat"], globals()["image_data_url"] = original_chat, original_data_url
    assert [item["tag"] for item in observations] == [f"OCR{i}" for i in range(count)]
    assert [item["timestamp"] for item in observations] == list(range(count))

    assert flatten_text({"a": ["x", {"b": "y"}]}) == "x y"

    # 完成標記要綁設定：換了 --frame-interval 就必須重抽，沿用舊截圖會讓時間戳錯位
    with tempfile.TemporaryDirectory() as temporary:
        marker = Path(temporary) / ".complete"
        assert not marker_matches(marker, "interval=10")  # 還沒有標記
        write_text(marker, "interval=10")
        assert marker_matches(marker, "interval=10")
        assert not marker_matches(marker, "interval=15")  # 設定變了

    assert unpack_vector(pack_vector([1.0, -0.5, 0.25])).tolist() == [1.0, -0.5, 0.25]
    assert abs(cosine([1.0, 0.0], [1.0, 0.0]) - 1.0) < 1e-6
    assert abs(cosine([1.0, 0.0], [0.0, 1.0])) < 1e-6
    assert abs(cosine([1.0, 0.0], [-1.0, 0.0]) + 1.0) < 1e-6
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0  # 零向量不可除以零
    assert abs(cosine([3.0, 4.0], [30.0, 40.0]) - 1.0) < 1e-6  # 只看方向不看長度
    assert DEFAULT_EMBED_MODEL["local"] != DEFAULT_EMBED_MODEL["openrouter"]  # HF 大小寫敏感

    # 門檻要跟著模型的分數尺度走：bge-m3 命中約 0.6、nemotron 命中約 0.29，
    # 寫死絕對值必定誤殺其中一種。
    assert similarity_cutoff(0.60) == 0.30
    assert similarity_cutoff(0.29) == 0.145
    assert similarity_cutoff(0.05) == SIMILARITY_FLOOR  # 全部都很低時由地板接手
    assert similarity_cutoff(0.60, 0.9) == 0.9  # 明確指定就照指定的來

    # 舊版 vectors 表要被重建而不是沿用：沒有 model 欄位、或主鍵只有 event_id
    # （後者會讓第二顆模型 REPLACE 掉第一顆的向量，兩顆無法共存）
    assert text_hash("abc") == text_hash("abc") and text_hash("abc") != text_hash("abd")
    for legacy_schema, legacy_row in (
        ("CREATE TABLE vectors (event_id INTEGER PRIMARY KEY, dim INTEGER, vector BLOB)", (1, 3, b"\x00")),
        (
            "CREATE TABLE vectors (event_id INTEGER, model TEXT, dim INTEGER, vector BLOB, PRIMARY KEY (event_id, model))",
            (1, "m", 3, b"\x00"),
        ),
    ):
        with tempfile.TemporaryDirectory() as temporary:
            legacy_root = Path(temporary)
            legacy = sqlite3.connect(legacy_root / "library.db")
            legacy.execute(legacy_schema)
            legacy.execute(
                "INSERT INTO vectors VALUES (" + ",".join("?" * len(legacy_row)) + ")", legacy_row
            )
            legacy.commit()
            legacy.close()
            migrated = index_connection(legacy_root)
            try:
                # 綁 events.rowid 的舊 schema 一律重建：rowid 會隨 --index 重新分配
                assert "text_hash" in {row[1] for row in migrated.execute("PRAGMA table_info(vectors)")}
                assert migrated.execute("SELECT count(*) FROM vectors").fetchone()[0] == 0
                # 同一段文字在兩顆模型下必須能共存
                migrated.execute("INSERT INTO vectors VALUES ('h1', 'a', 2, x'0000')")
                migrated.execute("INSERT INTO vectors VALUES ('h1', 'b', 2, x'1111')")
                assert migrated.execute("SELECT count(*) FROM vectors").fetchone()[0] == 2
            finally:
                migrated.close()
    with tempfile.TemporaryDirectory() as temporary:
        index_root = Path(temporary)
        fake_job = index_root / "jobs" / "abc123456789"
        fake_job.mkdir(parents=True)
        write_json(
            fake_job / "manifest.json",
            {
                "status": "completed",
                "source": {"url": "https://example.com/v", "title": "時間軸合併教學"},
                "video": {"duration_seconds": 12.0},
            },
        )
        write_json(
            fake_job / "timeline.json",
            [
                {"timestamp": 3.0, "type": "speech", "text": "這段說明時間軸合併的做法"},
                {"timestamp": 5.0, "type": "visual", "data": {"visual_summary": "終端機畫面"}},
                {"timestamp": 7.0, "type": "visual", "data": {"visual_summary": "合併前後對照，合併結果如圖"}},
            ],
        )
        assert build_index(index_root)[1:] == (1, 4)
        assert search_index(index_root, "時間軸", 10)[0]["title"] == "時間軸合併教學"
        assert search_index(index_root, "終端機", 10)[0]["kind"] == "visual"
        assert search_index(index_root, "完全不相干的字串", 10) == []
        assert search_index(index_root, 'a-b "c OR d', 10) == []  # FTS5 運算子不可炸
        assert build_index(index_root)[1:] == (1, 4)  # 重跑不重複灌

        # 短於 3 字元的查詢 trigram 一定落空，必須自動回退全表掃描
        short = search_index(index_root, "畫面", 10)
        assert short and short[0]["mode"] == "scan", short
        assert "《畫面》" in short[0]["snippet"], short[0]["snippet"]
        assert search_index(index_root, "教學", 10)[0]["kind"] == "title"
        assert search_index(index_root, "zz", 10) == []
        assert search_index(index_root, "%", 10) == []  # LIKE 萬用字元要被逃脫

        # 掃描模式沒有 rank，排序得自己來：標題優先，同級比出現次數
        ranked = search_index(index_root, "合併", 10)
        assert [item["kind"] for item in ranked] == ["title", "visual", "speech"], ranked
        assert "《合併》前後對照" in ranked[1]["snippet"], ranked[1]["snippet"]
        for blank in ("", "   "):
            try:
                search_index(index_root, blank, 10)
                raise AssertionError("空查詢必須報錯")
            except RuntimeError:
                pass

    print("self-test: OK")



def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="影片語音＋畫面時間軸摘要管線")
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--url", help="YouTube／Facebook 等 yt-dlp 支援的影片 URL")
    source.add_argument("--video", help="本機影片路徑")
    source.add_argument("--url-file", type=Path, help="批次 URL 清單；每行一個影片 URL")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--dry-run", action="store_true", help="只做本地處理，不下載或呼叫雲端 API")
    parser.add_argument("--local-only", action="store_true", help="下載／本地處理，但跳過所有雲端 API")
    parser.add_argument("--chunk-seconds", type=int, default=int(os.getenv("CHUNK_SECONDS", "300")))
    parser.add_argument(
        "--chunk-overlap-seconds",
        type=float,
        default=float(os.getenv("CHUNK_OVERLAP_SECONDS", "2")),
        help="相鄰音訊片段的重疊秒數，避免切掉句首句尾",
    )
    parser.add_argument("--workers", type=int, default=2, help="每條服務的併發數，單一服務最多 4")
    parser.add_argument("--language", default=None, help="逐字稿語言；auto 交給字幕／Whisper 自動偵測")
    parser.add_argument(
        "--transcribe-backend",
        choices=("groq", "local", "youtube"),
        default=os.getenv("TRANSCRIBE_BACKEND", "groq"),
        help="優先使用字幕；無字幕時走 groq 或 local",
    )
    parser.add_argument(
        "--caption-fallback",
        choices=("groq", "local"),
        default=os.getenv("CAPTION_FALLBACK_BACKEND", "groq"),
        help="youtube／字幕前置失敗時的轉錄後端",
    )
    parser.add_argument("--ignore-captions", action="store_true", help="忽略平台字幕，強制走音訊轉錄")
    parser.add_argument("--whisper-model", default=os.getenv("WHISPER_MODEL", "base"), help="本機 whisper 模型大小")
    parser.add_argument(
        "--groq-model",
        default=os.getenv("GROQ_MODEL", "whisper-large-v3"),
        help="Groq 轉錄模型；保留 whisper-large-v3 以優先完整性",
    )
    parser.add_argument("--frame-interval", type=float, default=10.0)
    parser.add_argument("--hash-threshold", "--phash-threshold", dest="hash_threshold", type=int, default=8)
    parser.add_argument("--slide-extractor", choices=("native", "steipete"), default="native")
    parser.add_argument("--scene-threshold", type=float, default=0.3)
    parser.add_argument("--min-slide-duration", type=float, default=2.0)
    parser.add_argument("--max-vision-frames", type=int, default=40)
    parser.add_argument("--ocr", action="store_true", help="若已安裝 PaddleOCR，啟用本地 OCR 初篩")
    parser.add_argument(
        "--summary-max-chars",
        type=int,
        default=800000,
        help="單次摘要可用的時間軸字元上限；超過會自動分章節",
    )
    parser.add_argument(
        "--sync-open-notebook",
        action="store_true",
        help="將 knowledge.md 匯入 Open Notebook",
    )
    parser.add_argument("--notebook-id", default=os.getenv("OPEN_NOTEBOOK_NOTEBOOK_ID", ""))
    parser.add_argument("--open-notebook-url", default=os.getenv("OPEN_NOTEBOOK_URL", "http://127.0.0.1:5055"))
    parser.add_argument("--open-notebook-sources-path", default=os.getenv("OPEN_NOTEBOOK_SOURCES_PATH", "/api/sources"))
    parser.add_argument("--open-notebook-wait-seconds", type=int, default=300)
    parser.add_argument("--index", action="store_true", help="掃描 jobs/ 重建本地搜尋索引 library.db")
    parser.add_argument("--search", help="在本地索引全文搜尋（跑過的影片會自動入索引）")
    parser.add_argument("--search-limit", type=int, default=20)
    parser.add_argument("--ask", help="用索引內容回答問題並附引用（需要 MINIMAX_API_KEY）")
    parser.add_argument("--ask-limit", type=int, default=12, help="--ask 餵給模型的片段上限")
    parser.add_argument("--digest", action="store_true", help="彙整所有影片摘要成 digest.md 全頻道總覽")
    parser.add_argument("--embed", action="store_true", help="替索引片段補算向量（需 OPENROUTER_API_KEY）")
    parser.add_argument("--semantic", action="store_true", help="--search／--ask 使用混合精確詞＋語意檢索")
    parser.add_argument(
        "--embed-backend",
        choices=("openrouter", "local"),
        default=os.getenv("EMBED_BACKEND", "openrouter"),
        help="local 走本機 sentence-transformers（免費、離線、內容不外傳）",
    )
    parser.add_argument("--embed-model", default=os.getenv("EMBED_MODEL"), help="不指定則依 backend 取預設")
    parser.add_argument(
        "--min-similarity",
        type=float,
        default=None,
        help="語意檢索的相似度下限；不指定則自動取最高分的一半",
    )
    parser.add_argument(
        "--no-video",
        action="store_true",
        help="不下載影片、不抽幀，只靠字幕建索引；之後重跑同一支會自動補上畫面分析",
    )
    parser.add_argument("--list", action="store_true", help="列出所有影片（可搭配 --category 篩選）")
    parser.add_argument("--show", help="依標題關鍵字顯示某支影片的摘要與章節")
    parser.add_argument("--category", default="", help="限定分類；可搭配 --list、--search 或 --ask")
    parser.add_argument("--categorize", action="store_true", help="產生固定分類並歸類所有影片")
    parser.add_argument("--audit", action="store_true", help="健檢知識庫：找出壞掉的摘要、落後的索引與向量")
    parser.add_argument("--refresh", action="store_true", help="忽略既有階段快取，重新呼叫需要更新的工作")
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()

def read_url_file(path: Path) -> list[str]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    urls: list[str] = []
    seen: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and line not in seen:
            seen.add(line)
            urls.append(line)
    if not urls:
        raise ValueError(f"URL 清單沒有可處理的項目：{path}")
    return urls


def run_batch(args: argparse.Namespace) -> Path:
    url_file = Path(args.url_file).expanduser().resolve()
    urls = read_url_file(url_file)
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    batch_dir = root / "batches" / job_key(str(url_file), "\n".join(urls))
    batch_dir.mkdir(parents=True, exist_ok=True)
    items: list[dict[str, Any]] = []
    for index, url in enumerate(urls, 1):
        item_args = argparse.Namespace(**vars(args))
        item_args.url = url
        item_args.video = None
        item_args.url_file = None
        try:
            manifest_path = run_pipeline(item_args)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            items.append(
                {
                    "index": index,
                    "url": url,
                    "status": manifest.get("status", "unknown"),
                    "manifest": artifact_path(manifest_path, root),
                }
            )
        except Exception as exc:
            items.append({"index": index, "url": url, "status": "failed", "error": str(exc)})

    failures = sum(item["status"] == "failed" for item in items)
    status = "failed" if failures == len(items) else "partial" if failures else "completed"
    output = batch_dir / "batch-manifest.json"
    write_json(
        output,
        {
            "schema_version": 1,
            "status": status,
            "source_file": str(url_file),
            "count": len(items),
            "failed": failures,
            "items": items,
        },
    )
    return output



def validate_frame_artifact(value: Any) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("frames"), list):
        return ["畫面候選產物格式錯誤"]
    errors: list[str] = []
    for index, item in enumerate(value["frames"]):
        if not isinstance(item, dict):
            errors.append(f"frames[{index}] 不是 object")
            continue
        try:
            if float(item["timestamp"]) < 0:
                errors.append(f"frames[{index}] timestamp 小於 0")
        except (KeyError, TypeError, ValueError):
            errors.append(f"frames[{index}] 缺少有效 timestamp")
        if not item.get("path"):
            errors.append(f"frames[{index}] 缺少 path")
        elif not Path(str(item["path"])).is_file():
            errors.append(f"frames[{index}] path 不存在")
    return errors


def validate_audio_artifact(value: Any) -> list[str]:
    if not isinstance(value, dict) or not isinstance(value.get("chunks"), list):
        return ["音訊片段產物格式錯誤"]
    errors: list[str] = []
    for index, item in enumerate(value["chunks"]):
        if not isinstance(item, dict):
            errors.append(f"chunks[{index}] 不是 object")
            continue
        try:
            start = float(item["start"])
            end = float(item["end"])
        except (KeyError, TypeError, ValueError):
            errors.append(f"chunks[{index}] 缺少有效時間")
            continue
        if start < 0 or end <= start or not item.get("path"):
            errors.append(f"chunks[{index}] 時間或路徑錯誤")
        elif not Path(str(item["path"])).is_file():
            errors.append(f"chunks[{index}] path 不存在")
    return errors


def run_pipeline(args: argparse.Namespace) -> Path:
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    load_dotenv(Path(__file__).resolve().parent / ".env")
    load_dotenv(root / ".env")
    args.notebook_id = args.notebook_id or os.getenv("OPEN_NOTEBOOK_NOTEBOOK_ID", "")
    if args.open_notebook_url == "http://127.0.0.1:5055":
        args.open_notebook_url = os.getenv("OPEN_NOTEBOOK_URL", args.open_notebook_url)
    if args.open_notebook_sources_path == "/api/sources":
        args.open_notebook_sources_path = os.getenv("OPEN_NOTEBOOK_SOURCES_PATH", args.open_notebook_sources_path)

    language = args.language if args.language is not None else os.getenv("GROQ_LANGUAGE", "auto")
    if args.chunk_seconds <= 0 or args.frame_interval <= 0 or args.max_vision_frames <= 0:
        raise ValueError("chunk-seconds、frame-interval 與 max-vision-frames 必須大於 0。")
    if args.chunk_overlap_seconds < 0 or args.chunk_overlap_seconds >= args.chunk_seconds:
        raise ValueError("chunk-overlap-seconds 必須介於 0 與 chunk-seconds 之間。")
    if not 0.1 <= args.scene_threshold <= 1.0 or args.min_slide_duration < 0:
        raise ValueError("scene-threshold 必須介於 0.1 到 1.0，min-slide-duration 不可小於 0。")
    if args.summary_max_chars <= 0:
        raise ValueError("summary-max-chars 必須大於 0。")
    if args.url is None and args.video is None:
        raise ValueError("請提供 --url 或 --video。")

    source_name = args.url or str(Path(args.video).expanduser().resolve())
    job_dir = root / "jobs" / job_key(source_name)
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = job_dir / "manifest.json"
    previous = read_json_file(manifest_path) or {}
    previous_stages = previous.get("stages") if isinstance(previous.get("stages"), dict) else {}
    previous_sync = previous.get("open_notebook") if isinstance(previous.get("open_notebook"), dict) else {}
    previous_source_id = str(
        previous_sync.get("source_id")
        or ((previous_sync.get("response") or {}).get("result") or {}).get("source_id")
        or ""
    )

    artifact_config = {
        "chunk_seconds": args.chunk_seconds,
        "chunk_overlap_seconds": args.chunk_overlap_seconds,
        "frame_interval": args.frame_interval,
        "hash_threshold": args.hash_threshold,
        "slide_extractor": args.slide_extractor,
        "scene_threshold": args.scene_threshold,
        "min_slide_duration": args.min_slide_duration,
        "max_vision_frames": args.max_vision_frames,
        "ocr": args.ocr,
        "summary_max_chars": args.summary_max_chars,
        "language": language,
        "transcribe_backend": args.transcribe_backend,
        "caption_fallback": args.caption_fallback,
        "ignore_captions": args.ignore_captions,
    }
    manifest: dict[str, Any] = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "status": "running",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source": {"url": args.url, "video": args.video},
        "job_dir": artifact_path(job_dir, root),
        "config": {
            **artifact_config,
            "workers": max(1, min(args.workers, 4)),
            "notebook_id": args.notebook_id or None,
        },
        "stages": {},
    }
    if previous_source_id:
        manifest["open_notebook"] = {**previous_sync, "source_id": previous_source_id}
    for preserved in ("categories", "level"):
        if previous.get(preserved):
            manifest[preserved] = previous[preserved]

    def persist() -> None:
        write_json(manifest_path, manifest)

    def record(
        stage: str,
        signature: str,
        artifact: Path,
        *,
        status: str = "completed",
        reused: bool = False,
        **metadata: Any,
    ) -> None:
        set_stage_record(
            manifest,
            stage,
            signature,
            artifact_path(artifact, root),
            status=status,
            reused=reused,
            **metadata,
        )
        persist()

    persist()

    # URL 的字幕 metadata 在完整影片下載前取得；dry-run 則不碰網路。
    info: dict[str, Any] = {}
    metadata_fp = digest_value(source_name)
    caption_transcript: dict[str, Any] | None = None
    caption_signature = ""
    caption_reused = False
    caption_error = ""
    if args.url and not args.dry_run and not args.ignore_captions:
        try:
            info = youtube_info(args.url, job_dir, refresh=args.refresh)
            metadata_fp = source_metadata_fingerprint(info, args.url)
            caption_signature = caption_stage_signature(info, language)
            cached_caption = stage_cache_value(
                previous_stages,
                "transcript",
                job_dir / "transcript.json",
                caption_signature,
                validate_transcript,
                args.refresh,
            )
            if (
                isinstance(cached_caption, dict)
                and cached_caption.get("caption_source")
                and caption_is_reliable(cached_caption, float(info.get("duration") or 0.0))
            ):
                caption_transcript = cached_caption
                caption_reused = True
            else:
                fetched_caption = fetch_youtube_transcript(
                    args.url, job_dir, language, refresh=args.refresh
                )
                if fetched_caption and caption_is_reliable(
                    fetched_caption, float(info.get("duration") or 0.0)
                ):
                    caption_transcript = fetched_caption
        except RuntimeError as exc:
            caption_error = str(exc)
            print(f"! 字幕前置失敗，稍後改走媒體／語音流程：{caption_error}")
            if args.no_video:
                raise

    if caption_error:
        manifest.setdefault("warnings", []).append("字幕前置失敗：" + caption_error)

    if args.url and args.dry_run:
        manifest["status"] = "dry-run"
        manifest["next"] = "移除 --dry-run 後會先嘗試字幕，再下載必要的媒體。"
        persist()
        return manifest_path

    video_path: Path
    duration = 0.0
    media_fp = metadata_fp
    probe: dict[str, Any] = {}
    audio_chunks: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    frame_meta: dict[str, Any] = {"method": "not-run", "candidate_count": 0}
    frame_signature = ""
    audio_signature = ""

    try:
        if args.no_video:
            if not args.url:
                raise ValueError("--no-video 只支援 --url。")
            if caption_transcript is None:
                raise RuntimeError("這支影片沒有可用字幕，--no-video 無法處理；請改用完整流程。")
            duration = float(info.get("duration") or 0.0)
            if duration <= 0:
                duration = max(
                    (float(item.get("end", 0)) for item in caption_transcript.get("segments", [])),
                    default=0.0,
                )
            video_path = Path("captions-only")
            manifest["source"]["title"] = str(info.get("title") or job_dir.name)
            manifest["source"]["metadata_fingerprint"] = metadata_fp
            manifest["video"] = {"duration_seconds": duration, "streams": []}
            frame_signature = stage_signature(
                "frames",
                metadata_fp,
                {"extractor": "captions-only", "version": PIPELINE_SCHEMA_VERSION},
            )
            audio_signature = stage_signature(
                "audio",
                metadata_fp,
                {"extractor": "captions-only", "version": PIPELINE_SCHEMA_VERSION},
            )
            frame_meta = {"method": "captions-only", "candidate_count": 0}
            write_json(
                job_dir / "frame_candidates.json",
                {"frames": [], "meta": frame_meta},
            )
            record(
                "frames",
                frame_signature,
                job_dir / "frame_candidates.json",
                status="skipped",
                metadata="no-video",
            )
            audio_metadata_path = job_dir / "audio" / "chunks.json"
            audio_metadata_path.parent.mkdir(parents=True, exist_ok=True)
            write_json(
                audio_metadata_path,
                {
                    "schema_version": ARTIFACT_SCHEMA_VERSION,
                    "signature": audio_signature,
                    "status": "skipped",
                    "chunks": [],
                },
            )
            record(
                "audio",
                audio_signature,
                audio_metadata_path,
                status="skipped",
                metadata="no-video",
            )
        else:
            if args.url:
                video_path = download_video(args.url, job_dir)
            else:
                video_path = Path(args.video).expanduser().resolve()
                if not video_path.is_file():
                    raise FileNotFoundError(video_path)
            probe = probe_video(video_path)
            duration = video_duration(probe)
            media_fp = file_fingerprint(video_path, job_dir / "source.fingerprint.json")
            manifest["source"]["resolved_video"] = artifact_path(video_path, root)
            manifest["source"]["title"] = source_title(job_dir, video_path)
            manifest["source"]["metadata_fingerprint"] = metadata_fp
            manifest["source"]["media_fingerprint"] = media_fp
            manifest["video"] = {
                "duration_seconds": duration,
                "streams": [
                    {
                        "codec_type": stream.get("codec_type"),
                        "codec_name": stream.get("codec_name"),
                    }
                    for stream in probe.get("streams", [])
                ],
            }
            if info:
                manifest["source"]["platform_metadata"] = {
                    key: info.get(key)
                    for key in ("id", "webpage_url", "language", "duration", "upload_date")
                    if info.get(key) is not None
                }

            frame_settings = {
                "extractor": FRAME_EXTRACTOR_VERSION if args.slide_extractor == "native" else "steipete",
                "interval": args.frame_interval,
                "hash_threshold": args.hash_threshold,
                "scene_threshold": args.scene_threshold,
                "min_slide_duration": args.min_slide_duration,
                "max_frames": args.max_vision_frames,
                "ocr": args.ocr,
            }
            frame_signature = stage_signature("frames", media_fp, frame_settings)
            audio_signature = stage_signature(
                "audio",
                media_fp,
                {
                    "extractor": AUDIO_EXTRACTOR_VERSION,
                    "chunk_seconds": args.chunk_seconds,
                    "overlap_seconds": args.chunk_overlap_seconds,
                },
            )
            frame_cache = stage_cache_value(
                previous_stages,
                "frames",
                job_dir / "frame_candidates.json",
                frame_signature,
                validate_frame_artifact,
                args.refresh,
            )
            audio_cache = (
                stage_cache_value(
                    previous_stages,
                    "audio",
                    job_dir / "audio" / "chunks.json",
                    audio_signature,
                    validate_audio_artifact,
                    args.refresh,
                )
                if not caption_transcript
                else None
            )
            if isinstance(frame_cache, dict):
                candidates = [dict(item) for item in frame_cache.get("frames") or []]
                frame_meta = dict(frame_cache.get("meta") or {})
                record(
                    "frames",
                    frame_signature,
                    job_dir / "frame_candidates.json",
                    reused=True,
                    count=len(candidates),
                )
            if isinstance(audio_cache, dict):
                audio_chunks = [dict(item) for item in audio_cache.get("chunks") or []]
                record(
                    "audio",
                    audio_signature,
                    job_dir / "audio" / "chunks.json",
                    reused=True,
                    count=len(audio_chunks),
                )

            extraction_jobs: dict[str, Any] = {}
            if not isinstance(frame_cache, dict):
                extraction_jobs["frames"] = (
                    (extract_steipete_candidates, ())
                    if args.slide_extractor == "steipete"
                    else (extract_candidates, ())
                )
            if not caption_transcript and not isinstance(audio_cache, dict):
                extraction_jobs["audio"] = (extract_audio, ())

            if extraction_jobs:
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, len(extraction_jobs))) as pool:
                    futures: dict[Any, str] = {}
                    for name in extraction_jobs:
                        if name == "frames":
                            if args.slide_extractor == "steipete":
                                future = pool.submit(
                                    extract_steipete_candidates,
                                    video_path,
                                    job_dir,
                                    args.max_vision_frames,
                                    args.scene_threshold,
                                    args.min_slide_duration,
                                )
                            else:
                                future = pool.submit(
                                    extract_candidates,
                                    video_path,
                                    job_dir,
                                    args.frame_interval,
                                    args.hash_threshold,
                                    args.max_vision_frames,
                                    args.ocr,
                                    0.75,
                                    args.scene_threshold,
                                    media_fp,
                                    args.refresh,
                                )
                        else:
                            future = pool.submit(
                                extract_audio,
                                video_path,
                                job_dir,
                                args.chunk_seconds,
                                args.chunk_overlap_seconds,
                                media_fp,
                                duration,
                                args.refresh,
                                probe,
                            )
                        futures[future] = name
                    results: dict[str, Any] = {}
                    errors: list[Exception] = []
                    for future in concurrent.futures.as_completed(futures):
                        name = futures[future]
                        try:
                            results[name] = future.result()
                        except Exception as exc:
                            errors.append(exc)
                    if errors:
                        raise errors[0]
                    if "frames" in results:
                        candidates, frame_meta = results["frames"]
                        write_json(
                            job_dir / "frame_candidates.json",
                            {"frames": candidates, "meta": frame_meta},
                        )
                        record(
                            "frames",
                            frame_signature,
                            job_dir / "frame_candidates.json",
                            count=len(candidates),
                        )
                    if "audio" in results:
                        audio_chunks = results["audio"]
                        record(
                            "audio",
                            audio_signature,
                            job_dir / "audio" / "chunks.json",
                            count=len(audio_chunks),
                        )
            if caption_transcript:
                audio_metadata_path = job_dir / "audio" / "chunks.json"
                audio_metadata_path.parent.mkdir(parents=True, exist_ok=True)
                write_json(
                    audio_metadata_path,
                    {
                        "schema_version": ARTIFACT_SCHEMA_VERSION,
                        "signature": audio_signature,
                        "status": "skipped",
                        "chunks": [],
                    },
                )
                record(
                    "audio",
                    audio_signature,
                    audio_metadata_path,
                    status="skipped",
                    metadata="reliable-captions",
                )
            manifest["artifacts"] = {
                "audio_chunks": [
                    artifact_path(Path(str(item["path"])), root) for item in audio_chunks
                ],
                "frame_candidates": artifact_path(job_dir / "frame_candidates.json", root),
            }

        if args.no_video:
            manifest["artifacts"] = {
                "audio_chunks": [],
                "frame_candidates": artifact_path(job_dir / "frame_candidates.json", root),
            }
        persist()

        # 逐字稿與視覺階段相互獨立，未命中的階段同時執行。
        transcript: dict[str, Any]
        observations: list[dict[str, Any]]
        transcript_signature = ""
        transcript_reused = False
        visual_signature = ""
        visual_reused = False

        if caption_transcript is not None:
            transcript = caption_transcript
            transcript_signature = caption_signature or str(caption_transcript.get("signature") or "")
            transcript_reused = caption_reused
            if not transcript_signature:
                transcript_signature = stage_signature(
                    "transcript",
                    metadata_fp,
                    {"backend": "youtube-captions", "language": language},
                )
            write_json(job_dir / "transcript.json", transcript)
            record(
                "transcript",
                transcript_signature,
                job_dir / "transcript.json",
                reused=transcript_reused,
                backend="youtube-captions",
            )
        elif args.dry_run or args.local_only:
            transcript = {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "status": "skipped",
                "reason": "dry-run" if args.dry_run else "local-only",
                "model": None,
                "language": language,
                "segments": [],
            }
            transcript_signature = stage_signature(
                "transcript",
                media_fp,
                {"backend": transcript["reason"], "language": language},
            )
            write_json(job_dir / "transcript.json", transcript)
            record(
                "transcript",
                transcript_signature,
                job_dir / "transcript.json",
                status="skipped",
            )
        else:
            backend = args.transcribe_backend
            if backend == "youtube":
                backend = args.caption_fallback
            model_name = args.groq_model if backend == "groq" else f"faster-whisper/{args.whisper_model}"
            transcript_signature = stage_signature(
                "transcript",
                media_fp,
                {
                    "backend": backend,
                    "model": model_name,
                    "language": language,
                    "review_version": "targeted-v1",
                },
                [audio_signature],
            )
            cached_transcript = stage_cache_value(
                previous_stages,
                "transcript",
                job_dir / "transcript.json",
                transcript_signature,
                validate_transcript,
                args.refresh,
            )
            if isinstance(cached_transcript, dict):
                transcript = cached_transcript
                transcript_reused = True
                print("· 重用既有逐字稿")
                write_json(job_dir / "transcript.json", transcript)
                record(
                    "transcript",
                    transcript_signature,
                    job_dir / "transcript.json",
                    reused=True,
                    backend=backend,
                )
            else:
                if not audio_chunks:
                    transcript = {
                        "schema_version": ARTIFACT_SCHEMA_VERSION,
                        "status": "silent",
                        "model": model_name,
                        "language": language,
                        "segments": [],
                        "review": [],
                        "omitted_segments": [],
                    }
                elif backend == "groq":
                    if not os.getenv("GROQ_API_KEY"):
                        raise RuntimeError(
                            "需要 GROQ_API_KEY；或指定 --caption-fallback local／--transcribe-backend local。"
                        )
                    transcript = None  # type: ignore[assignment]
                else:
                    transcript = None  # type: ignore[assignment]

                visual_signature = stage_signature(
                    "visual",
                    media_fp,
                    {
                        "model": os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
                        "prompt": PROMPT_VERSIONS["visual"],
                        "review_prompt": PROMPT_VERSIONS["visual_review"],
                    },
                    [frame_signature, digest_value(candidates)],
                )
                cached_visual = (
                    stage_cache_value(
                        previous_stages,
                        "visual",
                        job_dir / "visual.json",
                        visual_signature,
                        validate_visual,
                        args.refresh,
                    )
                    if not args.no_video
                    else []
                )
                if isinstance(cached_visual, list):
                    observations = cached_visual
                    visual_reused = True
                elif args.no_video:
                    observations = []
                elif args.dry_run or args.local_only:
                    observations = local_observations(candidates)
                elif transcript is None:
                    observations = None  # type: ignore[assignment]
                else:
                    observations = None  # type: ignore[assignment]

                jobs: dict[str, Any] = {}
                if transcript is None:
                    if backend == "groq":
                        jobs["transcript"] = ("groq",)
                    else:
                        jobs["transcript"] = ("local",)
                if not isinstance(cached_visual, list) and not args.no_video and not (args.dry_run or args.local_only):
                    jobs["visual"] = ("visual",)
                if jobs:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=min(2, len(jobs))) as pool:
                        futures: dict[Any, str] = {}
                        for name, kind in jobs.items():
                            if name == "transcript":
                                if kind[0] == "groq":
                                    futures[
                                        pool.submit(
                                            transcribe_all,
                                            audio_chunks,
                                            os.environ["GROQ_API_KEY"],
                                            args.groq_model,
                                            language,
                                            args.chunk_seconds,
                                            args.workers,
                                            transcript_signature,
                                            job_dir / "transcript-parts",
                                            args.refresh,
                                        )
                                    ] = name
                                else:
                                    futures[
                                        pool.submit(
                                            transcribe_local,
                                            audio_chunks,
                                            args.whisper_model,
                                            language,
                                            args.chunk_seconds,
                                            transcript_signature,
                                            job_dir / "transcript-parts",
                                            args.refresh,
                                        )
                                    ] = name
                            else:
                                futures[
                                    pool.submit(
                                        analyze_images,
                                        candidates,
                                        os.environ["MINIMAX_API_KEY"],
                                        os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
                                        args.workers,
                                        frame_signature,
                                        job_dir / "visual-parts",
                                        args.refresh,
                                    )
                                ] = name
                        stage_results: dict[str, Any] = {}
                        errors: list[Exception] = []
                        for future in concurrent.futures.as_completed(futures):
                            name = futures[future]
                            try:
                                stage_results[name] = future.result()
                            except Exception as exc:
                                errors.append(exc)
                        if errors:
                            raise errors[0]
                        if "transcript" in stage_results:
                            transcript = stage_results["transcript"]
                            write_json(job_dir / "transcript.json", transcript)
                            record(
                                "transcript",
                                transcript_signature,
                                job_dir / "transcript.json",
                                backend=backend,
                            )
                        if "visual" in stage_results:
                            observations = refine_visual_observations(
                                candidates,
                                stage_results["visual"],
                                os.environ["MINIMAX_API_KEY"],
                                os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
                                args.workers,
                                frame_signature,
                                job_dir / "visual-review-parts",
                                args.refresh,
                            )
                            write_json(job_dir / "visual.json", observations)
                            record(
                                "visual",
                                visual_signature,
                                job_dir / "visual.json",
                                count=len(observations),
                            )
                if not isinstance(observations, list):
                    observations = []
                if "visual" not in manifest.get("stages", {}):
                    write_json(job_dir / "visual.json", observations)
                    if args.no_video:
                        record(
                            "visual",
                            visual_signature or stage_signature("visual", media_fp, {"mode": "no-video"}),
                            job_dir / "visual.json",
                            status="skipped",
                        )
                    elif args.dry_run or args.local_only:
                        record(
                            "visual",
                            visual_signature or stage_signature("visual", media_fp, {"mode": "local-only"}),
                            job_dir / "visual.json",
                            status="skipped",
                        )
                    elif isinstance(cached_visual, list):
                        record(
                            "visual",
                            visual_signature,
                            job_dir / "visual.json",
                            reused=True,
                            count=len(observations),
                        )
                if transcript is None:
                    raise RuntimeError("逐字稿階段沒有產生結果。")

        if "visual" not in manifest.get("stages", {}):
            if args.no_video:
                observations = []
                visual_signature = stage_signature("visual", metadata_fp, {"mode": "no-video"})
                write_json(job_dir / "visual.json", observations)
                record(
                    "visual",
                    visual_signature,
                    job_dir / "visual.json",
                    status="skipped",
                )
            else:
                visual_signature = visual_signature or stage_signature(
                    "visual",
                    media_fp,
                    {"model": os.getenv("MINIMAX_MODEL", "MiniMax-M3"), "prompt": PROMPT_VERSIONS["visual"]},
                    [frame_signature, digest_value(candidates)],
                )
                cached_visual = stage_cache_value(
                    previous_stages,
                    "visual",
                    job_dir / "visual.json",
                    visual_signature,
                    validate_visual,
                    args.refresh,
                )
                if isinstance(cached_visual, list):
                    observations = cached_visual
                    record(
                        "visual",
                        visual_signature,
                        job_dir / "visual.json",
                        reused=True,
                        count=len(observations),
                    )
                elif args.dry_run or args.local_only:
                    observations = local_observations(candidates)
                    write_json(job_dir / "visual.json", observations)
                    record(
                        "visual",
                        visual_signature,
                        job_dir / "visual.json",
                        status="skipped",
                        count=len(observations),
                    )
                else:
                    api_key = os.getenv("MINIMAX_API_KEY", "")
                    if not api_key:
                        raise RuntimeError("視覺分析需要 MINIMAX_API_KEY；或使用 --local-only。")
                    observations = analyze_images(
                        candidates,
                        api_key,
                        os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
                        args.workers,
                        visual_signature,
                        job_dir / "visual-parts",
                        args.refresh,
                    )
                    observations = refine_visual_observations(
                        candidates,
                        observations,
                        api_key,
                        os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
                        args.workers,
                        frame_signature,
                        job_dir / "visual-review-parts",
                        args.refresh,
                    )
                    write_json(job_dir / "visual.json", observations)
                    record(
                        "visual",
                        visual_signature,
                        job_dir / "visual.json",
                        count=len(observations),
                    )

        if "artifacts" not in manifest:
            manifest["artifacts"] = {}
        manifest["artifacts"].update(
            {
                "transcript": artifact_path(job_dir / "transcript.json", root),
                "visual": artifact_path(job_dir / "visual.json", root),
            }
        )
        persist()

        timeline_signature = stage_signature(
            "timeline",
            media_fp,
            {"version": PIPELINE_SCHEMA_VERSION},
            [digest_value(transcript), digest_value(observations)],
        )
        cached_timeline = stage_cache_value(
            previous_stages,
            "timeline",
            job_dir / "timeline.json",
            timeline_signature,
            validate_timeline,
            args.refresh,
        )
        if isinstance(cached_timeline, list):
            timeline = cached_timeline
            record(
                "timeline",
                timeline_signature,
                job_dir / "timeline.json",
                reused=True,
                count=len(timeline),
            )
        else:
            timeline = merge_timeline(transcript, observations)
            write_json(job_dir / "timeline.json", timeline)
            if validate_timeline(timeline):
                raise ArtifactValidationError(
                    "時間軸未通過驗證：" + "; ".join(validate_timeline(timeline)[:8])
                )
            record(
                "timeline",
                timeline_signature,
                job_dir / "timeline.json",
                count=len(timeline),
            )
        manifest["artifacts"]["timeline"] = artifact_path(job_dir / "timeline.json", root)

        if args.dry_run or args.local_only:
            summary = {
                "schema_version": ARTIFACT_SCHEMA_VERSION,
                "status": "skipped",
                "reason": "dry-run" if args.dry_run else "local-only",
                "chapters": [],
                "steps": [],
                "tools_and_terms": [],
                "fact_check": [],
                "source_timestamps": [],
            }
            summary_signature = stage_signature(
                "summary",
                media_fp,
                {"mode": summary["reason"]},
                [timeline_signature],
            )
            write_json(job_dir / "summary.json", summary)
            record(
                "summary",
                summary_signature,
                job_dir / "summary.json",
                status="skipped",
            )
        else:
            summary_signature = stage_signature(
                "summary",
                media_fp,
                {
                    "model": os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
                    "prompt": PROMPT_VERSIONS["summary"],
                    "max_chars": args.summary_max_chars,
                },
                [timeline_signature],
            )
            cached_summary = stage_cache_value(
                previous_stages,
                "summary",
                job_dir / "summary.json",
                summary_signature,
                lambda value: validate_summary(value, duration),
                args.refresh,
            )
            if isinstance(cached_summary, dict):
                summary = cached_summary
                record(
                    "summary",
                    summary_signature,
                    job_dir / "summary.json",
                    reused=True,
                )
            else:
                if not os.getenv("MINIMAX_API_KEY") and timeline:
                    raise RuntimeError("完整流程需要 MINIMAX_API_KEY。")
                summary = create_summary(
                    timeline,
                    os.getenv("MINIMAX_API_KEY", ""),
                    os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
                    args.summary_max_chars,
                    duration,
                    summary_signature,
                    job_dir / "summary-parts",
                    args.workers,
                    args.refresh,
                )
                errors = validate_summary(summary, duration)
                if errors:
                    raise ArtifactValidationError("摘要未通過驗證：" + "; ".join(errors[:8]))
                write_json(job_dir / "summary.json", summary)
                record(
                    "summary",
                    summary_signature,
                    job_dir / "summary.json",
                    model=os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
                )
            manifest["models"] = {
                "transcript": transcript.get("model"),
                "visual": os.getenv("MINIMAX_MODEL", "MiniMax-M3") if observations else None,
                "summary": os.getenv("MINIMAX_MODEL", "MiniMax-M3") if timeline else None,
            }
        manifest["artifacts"]["summary"] = artifact_path(job_dir / "summary.json", root)
        persist()

        # 最終 gate：只有所有結構、時間與來源欄位通過才會宣告完成。
        if not (args.dry_run or args.local_only):
            errors = validate_transcript(transcript) + validate_visual(observations) + validate_timeline(timeline)
            errors += validate_summary(summary, duration)
            if errors:
                raise ArtifactValidationError("完成驗證失敗：" + "; ".join(errors[:12]))

        knowledge_path = render_knowledge_markdown(
            job_dir,
            manifest["source"],
            video_path,
            transcript,
            observations,
            timeline,
            summary,
        )
        manifest["artifacts"]["knowledge_markdown"] = artifact_path(knowledge_path, root)

        if args.sync_open_notebook:
            if args.dry_run or args.local_only:
                manifest["open_notebook"] = {"status": "skipped", "reason": "dry-run/local-only"}
            else:
                password = os.getenv("OPEN_NOTEBOOK_PASSWORD", "")
                if not args.notebook_id or not password:
                    raise RuntimeError("同步 Open Notebook 需要 --notebook-id 與 OPEN_NOTEBOOK_PASSWORD。")
                manifest["open_notebook"] = open_notebook_sync(
                    knowledge_path,
                    args.open_notebook_url,
                    args.notebook_id,
                    password,
                    args.open_notebook_sources_path,
                    args.open_notebook_wait_seconds,
                    manifest["source"].get("title", ""),
                    previous_source_id,
                )
        manifest["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        manifest["status"] = "completed" if not args.dry_run and not args.local_only else (
            "dry-run" if args.dry_run else "local-only"
        )
        persist()

        connection = index_connection(root)
        try:
            index_job(connection, job_dir)
            connection.commit()
        finally:
            connection.close()
        return manifest_path
    except Exception as exc:
        manifest["status"] = "failed"
        manifest["error"] = str(exc)
        manifest["failed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        persist()
        raise


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
        return 0
    if args.list or args.show:
        root = args.root.resolve()
        try:
            if args.list:
                total = list_videos(root, args.category)
                print(f"\n共 {total} 支" + (f"（分類含「{args.category}」）" if args.category else ""))
            if args.show and not show_video(root, args.show):
                return 1
        except (RuntimeError, sqlite3.Error) as exc:
            print(f"錯誤：{exc}", file=sys.stderr)
            return 1
        return 0
    if args.audit:
        print("=== 知識庫健檢 ===")
        problems = audit_library(args.root.resolve())
        if problems:
            print(f"\n發現 {len(problems)} 個問題：")
            for problem in problems:
                print(f"  ! {problem}")
            return 1
        print("\n無異常。")
        return 0
    if args.index or args.search is not None or args.ask or args.digest or args.embed or args.categorize:
        root = args.root.resolve()
        load_dotenv(Path(__file__).resolve().parent / ".env")
        load_dotenv(root / ".env")
        try:
            if args.index:
                db_path, video_count, event_count = build_index(root)
                print(f"索引完成：{db_path}（{video_count} 支影片、{event_count} 筆片段）")

            embed_model = args.embed_model or DEFAULT_EMBED_MODEL[args.embed_backend]
            embed_key = os.getenv("OPENROUTER_API_KEY", "")
            if (args.embed or args.semantic) and args.embed_backend == "openrouter" and not embed_key:
                raise RuntimeError("OpenRouter 後端需要 OPENROUTER_API_KEY，或改用 --embed-backend local。")
            if args.embed:
                added, total = build_embeddings(root, embed_key, embed_model, args.embed_backend)
                print(f"向量完成：新增 {added} 筆，索引共 {total} 個唯一片段（{args.embed_backend}／{embed_model}）")

            if args.search is not None:
                if args.semantic:
                    hits, keywords = hybrid_search(
                        root,
                        args.search,
                        os.getenv("MINIMAX_API_KEY", ""),
                        embed_key,
                        embed_model,
                        args.embed_backend,
                        args.search_limit,
                        args.category,
                        args.min_similarity,
                    )
                    mode = f"混合檢索：{args.embed_backend}／{embed_model}"
                    if keywords and keywords != [args.search]:
                        print(f"輔助關鍵詞：{'、'.join(keywords)}")
                else:
                    hits = search_index(root, args.search, args.search_limit, args.category)
                    mode = "全表掃描：查詢短於 3 字元" if len(args.search) < 3 else "索引"
                    if args.category:
                        mode += f"，限定「{args.category}」"
                print(f"命中 {len(hits)} 筆（{mode}）：")
                for hit in hits:
                    label = hit["title"] or hit["job"]
                    score = (
                        f" 融合分數={hit['fusion_score']:.4f}"
                        if "fusion_score" in hit
                        else f" 相似度={hit['score']:.3f}" if "score" in hit else ""
                    )
                    print(f"-{score} [{format_timestamp(hit['timestamp'])}] {label}（{hit['kind']}）")
                    print(f"    {hit['snippet']}")
                    print(f"    {source_link(hit['source'], hit['timestamp'])}")

            if args.categorize:
                if not os.getenv("MINIMAX_API_KEY"):
                    raise RuntimeError("--categorize 需要 MINIMAX_API_KEY。")
                cats, assigned = categorize_library(
                    root,
                    os.environ["MINIMAX_API_KEY"],
                    os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
                )
                print(f"分類完成：{len(cats)} 個分類，{assigned} 支影片已歸類（記得重跑 --index）")

            if args.digest:
                if not os.getenv("MINIMAX_API_KEY"):
                    raise RuntimeError("--digest 需要 MINIMAX_API_KEY。")
                digest_path = build_digest(
                    root,
                    os.environ["MINIMAX_API_KEY"],
                    os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
                )
                print(f"總覽已產生：{digest_path}")

            if args.ask:
                if not os.getenv("MINIMAX_API_KEY"):
                    raise RuntimeError("--ask 需要 MINIMAX_API_KEY。")
                result = answer_question(
                    root,
                    args.ask,
                    os.environ["MINIMAX_API_KEY"],
                    os.getenv("MINIMAX_MODEL", "MiniMax-M3"),
                    args.ask_limit,
                    embed_key,
                    embed_model,
                    args.embed_backend if args.semantic else "",
                    args.category,
                )
                print(f"檢索詞：{'、'.join(result['keywords'])}")
                print()
                print(result["answer"])
                if result["citations"]:
                    print("\n引用：")
                    for index, hit in enumerate(result["citations"], 1):
                        label = hit["title"] or hit["job"]
                        print(f"  [{index}] {label} {format_timestamp(hit['timestamp'])}（{hit['kind']}）")
                        print(f"      {source_link(hit['source'], hit['timestamp'])}")
                        excerpt = " ".join(str(hit.get("text") or "").split())
                        if excerpt:
                            print(f"      「{excerpt[:160]}{'…' if len(excerpt) > 160 else ''}」")
        except (RuntimeError, sqlite3.Error) as exc:
            print(f"錯誤：{exc}", file=sys.stderr)
            return 1
        return 0
    try:
        if args.url_file:
            manifest = run_batch(args)
            print(f"batch-manifest: {manifest}")
        else:
            manifest = run_pipeline(args)
            print(f"manifest: {manifest}")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
