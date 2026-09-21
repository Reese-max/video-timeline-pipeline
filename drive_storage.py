"""Minimal Google Drive resumable upload support for prepared MP4 segments.

Credentials stay outside the repository. The MCP host may use a short-lived
access token for development, or refresh-token OAuth for a long-running host.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_CHUNK_BYTES = 1024 * 1024


class DriveNotConfigured(RuntimeError):
    """Raised when the MCP host has no Google Drive credential configuration."""


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def configured() -> bool:
    if _env("GOOGLE_DRIVE_ACCESS_TOKEN"):
        return True
    return all(
        _env(name)
        for name in (
            "GOOGLE_DRIVE_REFRESH_TOKEN",
            "GOOGLE_DRIVE_CLIENT_ID",
            "GOOGLE_DRIVE_CLIENT_SECRET",
        )
    )


def _access_token() -> str:
    direct = _env("GOOGLE_DRIVE_ACCESS_TOKEN")
    if direct:
        return direct

    required = {
        name: _env(name)
        for name in (
            "GOOGLE_DRIVE_REFRESH_TOKEN",
            "GOOGLE_DRIVE_CLIENT_ID",
            "GOOGLE_DRIVE_CLIENT_SECRET",
        )
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise DriveNotConfigured(
            "Google Drive 未設定；請設定 GOOGLE_DRIVE_REFRESH_TOKEN、"
            "GOOGLE_DRIVE_CLIENT_ID、GOOGLE_DRIVE_CLIENT_SECRET，"
            "或僅供短期測試的 GOOGLE_DRIVE_ACCESS_TOKEN。"
        )

    body = urllib.parse.urlencode(
        {
            "client_id": required["GOOGLE_DRIVE_CLIENT_ID"],
            "client_secret": required["GOOGLE_DRIVE_CLIENT_SECRET"],
            "refresh_token": required["GOOGLE_DRIVE_REFRESH_TOKEN"],
            "grant_type": "refresh_token",
        }
    ).encode("ascii")
    request = urllib.request.Request(
        OAUTH_TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Google Drive OAuth 更新失敗。") from exc
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Google Drive OAuth 沒有回傳 access_token。")
    return token


def _api_json(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Google Drive API 失敗（HTTP {exc.code}）。") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Google Drive API 無法連線或回傳無效 JSON。") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Google Drive API 回傳格式無效。")
    return result


def _create_folder(token: str, name: str, parent_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        metadata["parents"] = [parent_id]
    query = urllib.parse.urlencode(
        {"supportsAllDrives": "true", "fields": "id,name,mimeType,webViewLink"}
    )
    result = _api_json("POST", f"{DRIVE_FILES_URL}?{query}", token, metadata)
    if not result.get("id"):
        raise RuntimeError("Google Drive 建立工作資料夾時沒有回傳 file id。")
    return result


def _upload_file(
    token: str,
    path: Path,
    name: str,
    parent_id: str,
    mime_type: str = "video/mp4",
) -> dict[str, Any]:
    size = path.stat().st_size
    metadata: dict[str, Any] = {"name": name, "mimeType": mime_type}
    if parent_id:
        metadata["parents"] = [parent_id]
    query = urllib.parse.urlencode(
        {
            "uploadType": "resumable",
            "supportsAllDrives": "true",
            "fields": "id,name,mimeType,size,webViewLink",
        }
    )
    init_request = urllib.request.Request(
        f"{DRIVE_UPLOAD_URL}?{query}",
        data=json.dumps(metadata, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
            "X-Upload-Content-Type": mime_type,
            "X-Upload-Content-Length": str(size),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(init_request, timeout=30) as response:
            upload_url = str(response.headers.get("Location") or "").strip()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Google Drive 初始化上傳失敗（HTTP {exc.code}）。") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Google Drive 初始化上傳無法連線。") from exc
    if not upload_url:
        raise RuntimeError("Google Drive 沒有回傳 resumable upload URL。")

    parsed = urllib.parse.urlsplit(upload_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("Google Drive 回傳的 upload URL 不安全。")
    connection = http.client.HTTPSConnection(parsed.netloc, timeout=120)
    request_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    try:
        connection.putrequest("PUT", request_path)
        connection.putheader("Authorization", f"Bearer {token}")
        connection.putheader("Content-Type", mime_type)
        connection.putheader("Content-Length", str(size))
        connection.endheaders()
        with path.open("rb") as source:
            while chunk := source.read(UPLOAD_CHUNK_BYTES):
                connection.send(chunk)
        response = connection.getresponse()
        raw = response.read()
    except (OSError, TimeoutError) as exc:
        raise RuntimeError("Google Drive resumable upload 無法完成。") from exc
    finally:
        connection.close()
    if response.status not in {200, 201}:
        raise RuntimeError(f"Google Drive 上傳失敗（HTTP {response.status}）。")
    try:
        result = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError("Google Drive 上傳完成但回傳格式無效。") from exc
    if not isinstance(result, dict) or not result.get("id"):
        raise RuntimeError("Google Drive 上傳完成但沒有回傳 file id。")
    if not result.get("webViewLink"):
        raise RuntimeError("Google Drive 上傳完成但沒有回傳可供 connector 使用的檢視網址。")
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(UPLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_name(value: str) -> str:
    normalized = re.sub(r"[^\w .()\-]+", "_", value, flags=re.UNICODE).strip(" .")
    return normalized[:96] or "video"


def upload_prepared_segments(
    job_dir: Path,
    job_id: str,
    title: str,
    source_fingerprint: str,
    segment_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    """Upload prepared MP4s once and cache only non-secret Drive metadata."""

    if not configured():
        raise DriveNotConfigured(
            "Google Drive fallback 尚未設定；MCP 不會把影片假裝成 Drive 檔案。"
        )
    if not segment_rows:
        raise ValueError("沒有可上傳的影片分段。")

    cache_path = job_dir / "drive-manifest.json"
    local_rows = []
    for row in segment_rows:
        filename = str(row.get("filename") or "")
        path = (job_dir / "segments" / filename).resolve()
        if path.parent != (job_dir / "segments").resolve() or not path.is_file():
            raise FileNotFoundError(f"找不到影片分段：{filename}")
        local_rows.append(
            {
                "filename": filename,
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )

    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = None
    if (
        isinstance(cached, dict)
        and cached.get("status") == "drive_ready"
        and cached.get("source_fingerprint") == source_fingerprint
        and cached.get("local_segments") == local_rows
        and len(cached.get("files") or []) == len(local_rows)
        and all(item.get("file_id") and item.get("url") for item in cached["files"])
    ):
        return cached

    token = _access_token()
    parent_id = _env("GOOGLE_DRIVE_FOLDER_ID")
    folder = _create_folder(token, f"Video Timeline - {_safe_name(title)} - {job_id}", parent_id)
    files = []
    for local in local_rows:
        result = _upload_file(
            token,
            job_dir / "segments" / local["filename"],
            local["filename"],
            str(folder["id"]),
        )
        files.append(
            {
                "file_id": str(result["id"]),
                "url": str(result["webViewLink"]),
                "file_name": str(result.get("name") or local["filename"]),
                "mime_type": str(result.get("mimeType") or "video/mp4"),
                "size_bytes": int(result.get("size") or local["size_bytes"]),
                "sha256": local["sha256"],
            }
        )
    payload = {
        "status": "drive_ready",
        "job_id": job_id,
        "folder_id": str(folder["id"]),
        "folder_url": str(folder.get("webViewLink") or ""),
        "source_fingerprint": source_fingerprint,
        "local_segments": local_rows,
        "files": files,
    }
    cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return payload
