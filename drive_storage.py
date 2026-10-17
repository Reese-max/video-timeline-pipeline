"""Minimal Google Drive resumable upload support for prepared MP4 segments.

Credentials stay outside the repository. The MCP host may use a short-lived
access token for development, or refresh-token OAuth for a long-running host.
Drive also keeps a verified copy of the whole source video, so local media can be
removed after analysis and downloaded again on demand.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import mimetypes
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from pipeline import write_json


DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
FOLDER_MIME = "application/vnd.google-apps.folder"
UPLOAD_CHUNK_BYTES = 1024 * 1024
# ponytail: one MCP process; use a shared lease before adding multiple workers.
UPLOAD_LOCK = threading.Lock()
_DRIVE_ID = re.compile(r"[A-Za-z0-9_-]+")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SEGMENT_NAME = re.compile(r"segment_\d{3,}\.mp4")
_SOURCE_NAME = re.compile(r"source\.[A-Za-z0-9]{1,8}")


class DriveNotConfigured(RuntimeError):
    """Raised when the MCP host has no Google Drive credential configuration."""


class DriveAPIError(RuntimeError):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"Google Drive API 失敗（HTTP {status}）。")


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
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None,
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
        raise DriveAPIError(exc.code) from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError("Google Drive API 無法連線或回傳無效 JSON。") from exc
    if not isinstance(result, dict):
        raise RuntimeError("Google Drive API 回傳格式無效。")
    return result


def _get_file(token: str, file_id: str) -> dict[str, Any] | None:
    query = urllib.parse.urlencode({"supportsAllDrives": "true",
        "fields": "id,name,mimeType,size,sha256Checksum,webViewLink,trashed,parents,capabilities(canDownload)"})
    try:
        return _api_json("GET", f"{DRIVE_FILES_URL}/{urllib.parse.quote(file_id, safe='')}?{query}", token)
    except DriveAPIError as exc:
        if exc.status == 404:
            return None
        raise  # A permissions/network error is not permission to create duplicates.


def _new_id(token: str) -> str:
    result = _api_json("GET", f"{DRIVE_FILES_URL}/generateIds?count=1&space=drive&type=files", token)
    ids = result.get("ids") or []
    if len(ids) != 1 or not re.fullmatch(r"[A-Za-z0-9_-]+", str(ids[0])):
        raise RuntimeError("Google Drive 沒有回傳有效的預先配置檔案 ID。")
    return str(ids[0])


def _create_folder(token: str, name: str, parent_id: str, file_id: str | None = None) -> dict[str, Any]:
    metadata: dict[str, Any] = {"name": name, "mimeType": FOLDER_MIME}
    if file_id:
        metadata["id"] = file_id
    if parent_id:
        metadata["parents"] = [parent_id]
    query = urllib.parse.urlencode(
        {"supportsAllDrives": "true", "fields": "id,name,mimeType,webViewLink"}
    )
    try:
        result = _api_json("POST", f"{DRIVE_FILES_URL}?{query}", token, metadata)
    except DriveAPIError as exc:
        if exc.status != 409 or not file_id:
            raise
        result = _get_file(token, file_id) or {}
    if not result.get("id"):
        raise RuntimeError("Google Drive 建立工作資料夾時沒有回傳 file id。")
    return result


def _upload_file(
    token: str,
    path: Path,
    name: str,
    parent_id: str,
    mime_type: str = "video/mp4",
    *,
    file_id: str | None = None,
) -> dict[str, Any]:
    size = path.stat().st_size
    if not size:
        raise ValueError("Google Drive 不接受空的影片分段。")
    metadata: dict[str, Any] = {"name": name, "mimeType": mime_type}
    if file_id:
        metadata["id"] = file_id
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
        if exc.code == 409 and file_id:
            return _get_file(token, file_id) or {}
        raise RuntimeError(f"Google Drive 初始化上傳失敗（HTTP {exc.code}）。") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Google Drive 初始化上傳無法連線。") from exc
    if not upload_url:
        raise RuntimeError("Google Drive 沒有回傳 resumable upload URL。")

    parsed = urllib.parse.urlsplit(upload_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("Google Drive 回傳的 upload URL 不安全。")
    request_path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    offset, failures, query_status = 0, 0, False
    while True:
        connection = http.client.HTTPSConnection(parsed.netloc, timeout=120)
        length = 0 if query_status else min(UPLOAD_CHUNK_BYTES, size - offset)
        status, received = None, ""
        try:
            connection.putrequest("PUT", request_path)
            connection.putheader("Authorization", f"Bearer {token}")
            connection.putheader("Content-Type", mime_type)
            connection.putheader("Content-Length", str(length))
            connection.putheader("Content-Range", f"bytes */{size}" if query_status
                                 else f"bytes {offset}-{offset + length - 1}/{size}")
            connection.endheaders()
            if length:
                with path.open("rb") as source:
                    source.seek(offset)
                    connection.send(source.read(length))
            response = connection.getresponse()
            raw = response.read()
            status = response.status
            received = response.getheader("Range") or ""
        except (OSError, http.client.HTTPException):
            pass  # The server may have committed bytes: query before retransmitting.
        finally:
            connection.close()
        if status in {200, 201}:
            break
        if status == 409 and file_id:
            return _get_file(token, file_id) or {}
        if status == 308:
            match = re.fullmatch(r"bytes=0-(\d+)", received)
            next_offset = int(match[1]) + 1 if match else 0
            if (received and not match) or not 0 <= next_offset < size:
                raise RuntimeError("Google Drive 續傳進度無效。")
            if not query_status and next_offset <= offset:
                raise RuntimeError("Google Drive 上傳沒有進展。")
            offset, query_status = next_offset, False
            continue
        detail = f"HTTP {status}" if status is not None else "連線中斷"
        if (status is None or status == 429 or 500 <= status < 600) and failures < 3:
            time.sleep(2 ** failures)
            failures += 1
            query_status = True
            continue
        raise RuntimeError(f"Google Drive 上傳失敗（{detail}；已重試 {failures} 次）。")
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


def _source_mime(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed if guessed and guessed.startswith("video/") else "video/mp4"


def _local_source(path: Path) -> dict[str, Any]:
    info = path.stat()
    return {"filename": path.name, "size_bytes": info.st_size, "sha256": _sha256(path),
            "mtime_ns": info.st_mtime_ns}


def _matches_file(remote: dict | None, file_id: str, local: dict, folder_id: str,
                  mime_type: str = "video/mp4") -> bool:
    return bool(remote and str(remote.get("id")) == file_id and not remote.get("trashed")
                and remote.get("mimeType") == mime_type
                and str(remote.get("size")) == str(local["size_bytes"])
                and remote.get("sha256Checksum") == local["sha256"]
                and folder_id in remote.get("parents", []) and remote.get("webViewLink"))


def upload_prepared_segments(
    job_dir: Path,
    job_id: str,
    title: str,
    source_fingerprint: str,
    segment_rows: list[dict[str, Any]],
    source_path: Path | None = None,
) -> dict[str, Any]:
    if not UPLOAD_LOCK.acquire(blocking=False):
        raise RuntimeError("Google Drive 正在處理另一個上傳，請稍後重試。")
    try:
        return _upload_prepared_segments(job_dir, job_id, title, source_fingerprint, segment_rows, source_path)
    finally:
        UPLOAD_LOCK.release()


def _upload_prepared_segments(
    job_dir: Path, job_id: str, title: str, source_fingerprint: str,
    segment_rows: list[dict[str, Any]], source_path: Path | None = None,
) -> dict[str, Any]:
    """Persist IDs before writes; recheck Drive before reusing any cached file."""

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

    local_source = None
    if source_path is not None:
        if source_path.parent != job_dir or not _SOURCE_NAME.fullmatch(source_path.name) or not source_path.is_file():
            raise FileNotFoundError(f"找不到來源影片：{source_path.name}")
        local_source = _local_source(source_path)

    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cached = None
    token = _access_token()
    parent_id = _env("GOOGLE_DRIVE_FOLDER_ID")
    reusable = (
        isinstance(cached, dict)
        and cached.get("source_fingerprint") == source_fingerprint
        and cached.get("local_segments") == local_rows
        and cached.get("parent_id", parent_id) == parent_id
        # Manifests written before source backups existed stay reusable.
        and cached.get("local_source") in (None, local_source)
    )
    state = cached if reusable else {
        "job_id": job_id, "parent_id": parent_id,
        "source_fingerprint": source_fingerprint,
        "local_segments": local_rows,
        "files": [],
    }
    # Invalidate ready before remote checks, including checks that fail with 403/5xx.
    state["status"] = "uploading"
    write_json(cache_path, state)
    folder_id = str(state.get("folder_id") or "")
    folder = _get_file(token, folder_id) if folder_id else None
    if folder and (folder.get("trashed") or folder.get("mimeType") != FOLDER_MIME
                   or (parent_id and parent_id not in folder.get("parents", []))):
        folder = None
        folder_id = ""
    if not folder:
        # A created but now deleted folder needs a new ID; a reserved ID survives a crash.
        if state.get("folder_created") or state.get("folder_url"):
            folder_id = ""
        folder_id = folder_id or _new_id(token)
        state.update(folder_id=folder_id, folder_created=False, folder_url="", files=[])
        write_json(cache_path, state)
        folder = _create_folder(token, f"Video Timeline - {_safe_name(title)} - {job_id}", parent_id, folder_id)
        if str(folder.get("id")) != folder_id:
            raise RuntimeError("Google Drive 回傳的資料夾 ID 與預先配置的 ID 不符。")
    state.update(folder_id=folder_id, folder_created=True,
                 folder_url=str(folder.get("webViewLink") or ""), parent_id=parent_id)
    write_json(cache_path, state)
    previous = {item["file_name"]: item for item in state.get("files", []) if item.get("file_name")}
    files = []
    for local in local_rows:
        saved = previous.get(local["filename"], {})
        file_id = str(saved.get("file_id") or "")
        remote = _get_file(token, file_id) if file_id else None
        if not _matches_file(remote, file_id, local, folder_id):
            # Never overwrite a changed, moved, or trashed user file.
            if remote or saved.get("verified") or saved.get("url"):
                file_id = ""
            file_id = file_id or _new_id(token)
            pending = {"file_id": file_id, "file_name": local["filename"], "verified": False}
            state["files"] = files + [pending] + [previous[r["filename"]] for r in local_rows[len(files)+1:]
                                                  if r["filename"] in previous]
            write_json(cache_path, state)
            _upload_file(token, job_dir / "segments" / local["filename"], local["filename"], folder_id,
                         file_id=file_id)
            remote = _get_file(token, file_id)
            if not _matches_file(remote, file_id, local, folder_id):
                raise RuntimeError("Google Drive 上傳後的檔案大小、SHA-256 或資料夾驗證失敗。")
        files.append({"file_id": file_id, "url": remote["webViewLink"],
                      "file_name": local["filename"], "mime_type": "video/mp4",
                      "size_bytes": local["size_bytes"], "sha256": local["sha256"], "verified": True})
        state["files"] = files + [previous[r["filename"]] for r in local_rows[len(files):]
                                  if r["filename"] in previous]
        write_json(cache_path, state)
    state["files"] = files
    if source_path is not None and local_source is not None:
        _upload_source_backup(token, state, cache_path, source_path, local_source, folder_id)
    state.update(status="drive_ready", files=files)
    write_json(cache_path, state)
    return state


def _upload_source_backup(token: str, state: dict, cache_path: Path, source_path: Path,
                          local_source: dict, folder_id: str) -> None:
    """Keep a verified copy of the whole source video; never overwrite a changed user file."""
    mime_type = _source_mime(source_path)
    saved = state.get("source_file") if isinstance(state.get("source_file"), dict) else {}
    file_id = str(saved.get("file_id") or "")
    remote = _get_file(token, file_id) if file_id else None
    if not _matches_file(remote, file_id, local_source, folder_id, mime_type):
        if remote or saved.get("verified") or saved.get("url"):
            file_id = ""
        file_id = file_id or _new_id(token)
        state.update(local_source=local_source,
                     source_file={"file_id": file_id, "file_name": local_source["filename"], "verified": False})
        write_json(cache_path, state)
        _upload_file(token, source_path, local_source["filename"], folder_id, mime_type, file_id=file_id)
        remote = _get_file(token, file_id)
        if not _matches_file(remote, file_id, local_source, folder_id, mime_type):
            raise RuntimeError("Google Drive 原片上傳後的大小、SHA-256 或資料夾驗證失敗。")
    state.update(local_source=local_source, source_file={
        "file_id": file_id, "url": remote["webViewLink"], "file_name": local_source["filename"],
        "mime_type": mime_type, "size_bytes": local_source["size_bytes"], "sha256": local_source["sha256"],
        "mtime_ns": local_source["mtime_ns"], "verified": True})
    write_json(cache_path, state)


def backup_complete(state: Any, segment_rows: list[dict[str, Any]], source_fingerprint: str) -> bool:
    """Local check that Drive holds verified copies of the source and of every segment."""
    if not isinstance(state, dict) or state.get("status") != "drive_ready":
        return False
    if state.get("source_fingerprint") != source_fingerprint or not _DRIVE_ID.fullmatch(str(state.get("folder_id") or "")):
        return False
    source = state.get("source_file")
    if not (isinstance(source, dict) and source.get("verified") is True
            and _DRIVE_ID.fullmatch(str(source.get("file_id") or ""))
            and _SOURCE_NAME.fullmatch(str(source.get("file_name") or ""))
            and _SHA256.fullmatch(str(source.get("sha256") or ""))
            and str(source.get("mime_type") or "").startswith("video/")
            and type(source.get("size_bytes")) is int and source["size_bytes"] > 0
            and type(source.get("mtime_ns")) is int
            and f"{source['size_bytes']}:{source['mtime_ns']}" == source_fingerprint):
        return False
    names = [str(row.get("filename") or "") for row in segment_rows if isinstance(row, dict)]
    local, files = state.get("local_segments"), state.get("files")
    if (not names or len(names) != len(segment_rows) or not isinstance(local, list) or not isinstance(files, list)
            or len(local) != len(names) or len(files) != len(names)):
        return False
    for name, row, saved in zip(names, local, files):
        if (not isinstance(row, dict) or not isinstance(saved, dict) or not _SEGMENT_NAME.fullmatch(name)
                or row.get("filename") != name or saved.get("file_name") != name
                or saved.get("verified") is not True
                or not _DRIVE_ID.fullmatch(str(saved.get("file_id") or ""))
                or not _SHA256.fullmatch(str(row.get("sha256") or ""))
                or type(row.get("size_bytes")) is not int or row["size_bytes"] <= 0):
            return False
    return True


def _backup_targets(job_dir: Path, state: dict) -> list[tuple[Path, str, int, str]]:
    """Segments first, source last: a restored source completes the prepared media set."""
    targets = [(job_dir / "segments" / row["filename"], saved["file_id"], row["size_bytes"], row["sha256"])
               for row, saved in zip(state["local_segments"], state["files"])]
    source = state["source_file"]
    targets.append((job_dir / source["file_name"], source["file_id"], source["size_bytes"], source["sha256"]))
    return targets


def backup_relative_paths(state: dict) -> set[str]:
    """Job-relative paths of every file Drive holds a verified copy of."""
    return ({"segments/" + row["filename"] for row in state["local_segments"]}
            | {state["source_file"]["file_name"]})


def local_matches_backup(job_dir: Path, state: dict) -> bool:
    """Every local copy that still exists is byte-identical to its Drive backup."""
    for path, _file_id, size, digest in _backup_targets(job_dir, state):
        if path.is_symlink():
            return False
        if not path.exists():
            continue
        if not path.is_file() or path.stat().st_size != size or _sha256(path) != digest:
            return False
    return True


def verify_remote_backup(state: dict) -> bool:
    """Drive still holds every backed-up file untouched (not trashed, same bytes, same folder)."""
    token = _access_token()
    folder_id = str(state["folder_id"])
    folder = _get_file(token, folder_id)
    if not folder or folder.get("trashed") or folder.get("mimeType") != FOLDER_MIME:
        return False

    def usable(remote: dict | None, file_id: str, local: dict, mime_type: str = "video/mp4") -> bool:
        capabilities = (remote or {}).get("capabilities") or {}
        return (_matches_file(remote, file_id, local, folder_id, mime_type)
                and capabilities.get("canDownload") is not False)

    if not all(usable(_get_file(token, saved["file_id"]), saved["file_id"], row)
               for row, saved in zip(state["local_segments"], state["files"])):
        return False
    # Source last: keep the gap between this check and deleting the local source short.
    source = state["source_file"]
    return usable(_get_file(token, source["file_id"]), source["file_id"], source, source["mime_type"])


def _download_file(token: str, file_id: str, destination: Path, size: int, digest: str,
                   mtime_ns: int | None = None) -> None:
    query = urllib.parse.urlencode({"alt": "media", "supportsAllDrives": "true"})
    request = urllib.request.Request(
        f"{DRIVE_FILES_URL}/{urllib.parse.quote(file_id, safe='')}?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    temporary = destination.with_name(destination.name + ".drive-restore.part")
    hasher, written = hashlib.sha256(), 0
    try:
        with urllib.request.urlopen(request, timeout=120) as response, temporary.open("wb") as output:
            while True:
                chunk = response.read(UPLOAD_CHUNK_BYTES)
                if not chunk:
                    break
                written += len(chunk)
                if written > size:
                    raise RuntimeError("Google Drive 取回的檔案比備份紀錄大。")
                hasher.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if written != size or hasher.hexdigest() != digest:
            raise RuntimeError("Google Drive 取回的檔案大小或 SHA-256 與備份紀錄不符。")
        if mtime_ns is not None:
            # Publish the source with its prepared fingerprint already in place.
            os.utime(temporary, ns=(mtime_ns, mtime_ns))
        os.replace(temporary, destination)
    except urllib.error.HTTPError as exc:
        raise DriveAPIError(exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise RuntimeError("Google Drive 取回影片無法連線。") from exc
    finally:
        temporary.unlink(missing_ok=True)


def restore_prepared_media(job_dir: Path, state: dict) -> list[str]:
    """Download missing source/segments from a verified backup; returns restored names.

    An existing file that differs from its backup may be newer, never-backed-up
    media, so restore refuses instead of overwriting it.
    """
    token = _access_token()
    (job_dir / "segments").mkdir(parents=True, exist_ok=True)
    source = state["source_file"]
    source_path = job_dir / source["file_name"]
    restored = []
    for path, file_id, size, digest in _backup_targets(job_dir, state):
        if path.is_symlink():
            raise RuntimeError("拒絕把 Drive 備份寫入符號連結。")
        if path.exists():
            if not path.is_file() or path.stat().st_size != size or _sha256(path) != digest:
                raise RuntimeError(f"本機已有與 Drive 備份不同的 {path.name}，不覆寫。")
            continue
        _download_file(token, file_id, path, size, digest,
                       source["mtime_ns"] if path == source_path else None)
        restored.append(path.name)
    # Same bytes as the backup: the prepared manifest fingerprints the source by size and mtime_ns.
    os.utime(source_path, ns=(source["mtime_ns"], source["mtime_ns"]))
    return restored


def _matches_report(remote: dict | None, file_id: str, report: dict, folder_id: str) -> bool:
    return bool(remote and str(remote.get("id")) == file_id and not remote.get("trashed")
                and remote.get("name") == "A_server_analysis.md" and remote.get("mimeType") == "text/markdown"
                and str(remote.get("size")) == str(report["size_bytes"])
                and remote.get("sha256Checksum") == report["report_sha256"]
                and remote.get("parents") == [folder_id] and remote.get("webViewLink"))


def upload_analysis_report(job_dir: Path, report: dict, segments: dict) -> dict:
    """A distinct FIFO task: verify the original segment folder; never replace user files."""
    if not UPLOAD_LOCK.acquire(blocking=False):
        raise RuntimeError("Google Drive 正在處理另一個上傳，請稍後重試。")
    try:
        if not configured():
            raise DriveNotConfigured("Google Drive fallback 尚未設定；報告未上傳。")
        if report.get("summary_status") != "caller_submitted" or segments.get("status") != "drive_ready":
            raise ValueError("Report or segments are not ready")
        if report.get("job_id") != segments.get("job_id") or report.get("source_fingerprint") != segments.get("source_fingerprint"):
            raise ValueError("Report and segment source differ")
        digest = str(report.get("report_sha256") or "")
        folder_id = str(segments.get("folder_id") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not re.fullmatch(r"[A-Za-z0-9_-]+", folder_id):
            raise ValueError("Invalid report or folder identity")
        path = job_dir / "reports" / digest / "A_server_analysis.md"
        if any(p.is_symlink() for p in (job_dir, path.parent.parent, path.parent, path)):
            raise ValueError("Report path is not owned")
        if _sha256(path) != digest or path.stat().st_size != report["size_bytes"]:
            raise ValueError("Report bytes changed")
        local = segments.get("local_segments")
        files = segments.get("files")
        if not isinstance(local, list) or not local or not isinstance(files, list) or len(local) != len(files):
            raise ValueError("Segment verification is missing")
        names = set()
        for row, saved in zip(local, files):
            if not isinstance(row, dict) or not isinstance(saved, dict):
                raise ValueError("Invalid segment verification shape")
            filename = row.get("filename")
            if (not isinstance(filename, str) or not re.fullmatch(r"segment_\d{3,}\.mp4", filename)
                    or filename in names or saved.get("file_name") != filename or saved.get("verified") is not True
                    or not re.fullmatch(r"[A-Za-z0-9_-]+", str(saved.get("file_id") or ""))
                    or not re.fullmatch(r"[0-9a-f]{64}", str(row.get("sha256") or ""))
                    or type(row.get("size_bytes")) is not int or row["size_bytes"] <= 0):
                raise ValueError("Invalid segment verification metadata")
            names.add(filename)
            raw_segment = job_dir / "segments" / filename
            segment = raw_segment.resolve()
            if (raw_segment.is_symlink() or segment.parent != (job_dir / "segments").resolve()
                    or not segment.is_file() or _sha256(segment) != row["sha256"]
                    or segment.stat().st_size != row["size_bytes"]):
                raise ValueError("Original segment bytes changed; no transport")
        token = _access_token()
        folder = _get_file(token, folder_id)
        parent_id = str(segments.get("parent_id") or "")
        if (not folder or folder.get("trashed") or folder.get("mimeType") != FOLDER_MIME
                or str(folder.get("id")) != folder_id or (parent_id and parent_id not in folder.get("parents", []))
                or parent_id != _env("GOOGLE_DRIVE_FOLDER_ID")):
            raise ValueError("Original segment folder changed; no report write")
        for row, saved in zip(local, files):
            if not _matches_file(_get_file(token, saved["file_id"]), saved["file_id"], row, folder_id):
                raise ValueError("Original MP4 verification changed; no report write")
        key = hashlib.sha256(json.dumps({k: report[k] for k in ("job_id", "analysis_sha256", "report_sha256")}, sort_keys=True).encode() + folder_id.encode()).hexdigest()
        state_path = path.parent / ("drive-" + key + ".json")
        binding = {k: report[k] for k in ("job_id", "analysis_sha256", "report_sha256", "source_sha256", "source_fingerprint", "size_bytes")}
        binding.update(folder_id=folder_id, file_name="A_server_analysis.md", mime_type="text/markdown")
        if state_path.exists():
            if state_path.is_symlink():
                raise ValueError("Report upload state is not owned")
            state = json.loads(state_path.read_bytes())
            if not isinstance(state, dict) or any(state.get(k) != v for k, v in binding.items()) or not re.fullmatch(r"[A-Za-z0-9_-]+", str(state.get("file_id") or "")):
                raise ValueError("Unknown existing report upload state")
        else:
            state = {**binding, "file_id": _new_id(token), "status": "reserved", "verified": False}
            write_json(state_path, state)  # ID is durable BEFORE any POST/PUT.
        file_id = state["file_id"]
        remote = _get_file(token, file_id)
        if not _matches_report(remote, file_id, report, folder_id):
            if remote or state.get("verified"):
                raise ValueError("Existing report was moved, changed or deleted; never overwrite")
            state.update(status="uploading")
            write_json(state_path, state)
            _upload_file(token, path, "A_server_analysis.md", folder_id, "text/markdown", file_id=file_id)
            remote = _get_file(token, file_id)
            if not _matches_report(remote, file_id, report, folder_id):
                raise ValueError("Uploaded report identity/bytes/parent verification failed")
        if _sha256(path) != digest or path.stat().st_size != report["size_bytes"]:
            raise ValueError("Report changed during upload")
        state.update(status="report_ready", verified=True, url=str(remote["webViewLink"]))
        write_json(state_path, state)
        return state
    finally:
        UPLOAD_LOCK.release()
