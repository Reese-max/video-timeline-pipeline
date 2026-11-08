"""Conservative local media retention, quota preflights and expiring links.

No cleanup happens on construction or capacity checks. Only explicitly marked
jobs are eligible, and adoption starts a fresh retention period. Callers must
serialize writers and pass their active job IDs to cleanup. This is a preflight
quota, not an operating-system filesystem quota.
"""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import threading
import time


JOB_PATTERN = re.compile(r"[0-9a-f]{12}\Z")
MEDIA_PATTERN = re.compile(
    r"(?:segments/[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.mp4|"
    r"previews/[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.jpg)\Z"
)
REPORT_PATTERN = re.compile(r"reports/[0-9a-f]{64}/A_server_analysis\.md\Z")
MARKER_NAME = ".media-lifecycle.json"


class StoragePolicyError(ValueError):
    """A path or lifecycle record cannot safely be managed."""


class StorageCapacityError(StoragePolicyError):
    def __init__(self, code: str, details: dict):
        self.code = code
        self.details = details
        super().__init__(f"Media storage capacity exceeded: {code}")


def _timestamp(value: float | int | None) -> float:
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (float, int)):
        raise ValueError("Timestamp must be a finite nonnegative number")
    if not math.isfinite(value) or value < 0:
        raise ValueError("Timestamp must be a finite nonnegative number")
    return float(value)


def _job_id(value: str) -> str:
    if not isinstance(value, str) or not JOB_PATTERN.fullmatch(value):
        raise StoragePolicyError("Expected a lowercase 12-hex job ID")
    return value


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    )


def _check_ancestors(path: Path) -> None:
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if _is_link(info):
            raise StoragePolicyError("Symlink or reparse-point paths are forbidden")


def _read_regular(path: Path, limit: int) -> bytes:
    before = path.lstat()
    if _is_link(before) or not stat.S_ISREG(before.st_mode):
        raise StoragePolicyError("Expected an ordinary file")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        after = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise StoragePolicyError("File changed while opening")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise StoragePolicyError("File is larger than its allowed limit")
        return data
    finally:
        os.close(fd)


class StoragePolicy:
    def __init__(self, root, max_total_bytes: int, max_job_bytes: int,
                 retention_seconds: int, min_free_bytes: int):
        for name, value in {
            "max_total_bytes": max_total_bytes, "max_job_bytes": max_job_bytes,
            "retention_seconds": retention_seconds, "min_free_bytes": min_free_bytes,
        }.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a nonnegative integer")
        if not max_total_bytes or not max_job_bytes:
            raise ValueError("Storage quotas must be positive")
        self.root = Path(os.path.abspath(root))
        _check_ancestors(self.root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise StoragePolicyError("Storage root must be an ordinary directory")
        self._root_identity = (info.st_dev, info.st_ino)
        self.max_total_bytes = max_total_bytes
        self.max_job_bytes = max_job_bytes
        self.retention_seconds = retention_seconds
        self.min_free_bytes = min_free_bytes
        self._lock = threading.RLock()
        self._lock_depth = threading.local()

    @contextmanager
    def _guard(self):
        """Serialize lifecycle decisions across instances, threads and workers.

        Keep the lock file permanently: unlinking a lock can create two lock
        domains while an older process still holds its original inode.
        """
        with self._lock:
            depth = getattr(self._lock_depth, "value", 0)
            if depth:
                self._lock_depth.value = depth + 1
                try:
                    yield
                finally:
                    self._lock_depth.value -= 1
                return
            path = self._root() / ".media-lifecycle.lock"
            _check_ancestors(path)
            fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            acquired = False
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or _is_link(info):
                    raise StoragePolicyError("Lifecycle lock must be an ordinary file")
                if os.name == "nt" and info.st_size == 0:
                    os.write(fd, b"0")
                deadline = time.monotonic() + 15
                while True:
                    try:
                        if os.name == "nt":
                            import msvcrt
                            os.lseek(fd, 0, os.SEEK_SET)
                            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        acquired = True
                        break
                    except (OSError, BlockingIOError):
                        if time.monotonic() >= deadline:
                            raise StoragePolicyError("Media lifecycle is busy; retry later") from None
                        time.sleep(0.02)
                current = path.lstat()
                if _is_link(current) or (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise StoragePolicyError("Lifecycle lock changed")
                self._root()
                self._lock_depth.value = 1
                try:
                    yield
                finally:
                    self._lock_depth.value = 0
            finally:
                if acquired:
                    if os.name == "nt":
                        import msvcrt
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _root(self) -> Path:
        _check_ancestors(self.root)
        info = self.root.lstat()
        if (info.st_dev, info.st_ino) != self._root_identity or not stat.S_ISDIR(info.st_mode):
            raise StoragePolicyError("Storage root changed")
        return self.root

    def _job(self, job_id: str, *, missing_ok=False) -> Path:
        path = self._root() / _job_id(job_id)
        try:
            info = path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return path
            raise StoragePolicyError("Job does not exist") from None
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise StoragePolicyError("Job must be an ordinary directory")
        return path

    @staticmethod
    def _scan(path: Path) -> tuple[int, bool]:
        total, unsafe = 0, False
        pending = [path]
        while pending:
            folder = pending.pop()
            info = folder.lstat()
            if _is_link(info) or not stat.S_ISDIR(info.st_mode):
                unsafe = True
                continue
            with os.scandir(folder) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if _is_link(info):
                        unsafe = True
                    elif stat.S_ISDIR(info.st_mode):
                        pending.append(Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        total += info.st_size
                    else:
                        unsafe = True
        return total, unsafe

    @staticmethod
    def _marker(path: Path) -> dict | None:
        try:
            value = json.loads(_read_regular(path / MARKER_NAME, 4096))
        except FileNotFoundError:
            return None
        except (ValueError, OSError) as exc:
            raise StoragePolicyError("Invalid lifecycle marker") from exc
        if not isinstance(value, dict) or value.get("version") != 1 or value.get("job_id") != path.name:
            raise StoragePolicyError("Invalid lifecycle marker")
        for key in ("managed_at", "last_used_at", "protected_until"):
            try:
                _timestamp(value[key])
            except (KeyError, ValueError) as exc:
                raise StoragePolicyError("Invalid lifecycle marker") from exc
        return value

    def _write_marker(self, path: Path, value: dict) -> None:
        self._job(path.name)
        if os.name == "posix":
            root_fd = os.open(self._root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            job_fd = None
            temporary_name = MARKER_NAME + "." + secrets.token_hex(8) + ".tmp"
            try:
                info = os.fstat(root_fd)
                if (info.st_dev, info.st_ino) != self._root_identity:
                    raise StoragePolicyError("Storage root changed")
                job_fd = os.open(path.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
                fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=job_fd)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(value, stream, sort_keys=True, separators=(",", ":"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary_name, MARKER_NAME, src_dir_fd=job_fd, dst_dir_fd=job_fd)
                os.fsync(job_fd)
            finally:
                if job_fd is not None:
                    try:
                        os.unlink(temporary_name, dir_fd=job_fd)
                    except FileNotFoundError:
                        pass
                    os.close(job_fd)
                os.close(root_fd)
            return
        temporary = path / (MARKER_NAME + "." + secrets.token_hex(8) + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            self._job(path.name)
            os.replace(temporary, path / MARKER_NAME)
        finally:
            temporary.unlink(missing_ok=True)

    def mark_managed(self, job_id: str, now=None) -> dict:
        """Explicitly adopt a job; refresh retention when an existing job is reused."""
        now = _timestamp(now)
        with self._guard():
            path = self._job(job_id)
            _, unsafe = self._scan(path)
            if unsafe:
                raise StoragePolicyError("Unsafe entry inside job")
            marker = self._marker(path) or {
                "version": 1, "job_id": job_id, "managed_at": now,
                "last_used_at": now, "protected_until": 0,
            }
            marker["last_used_at"] = max(now, marker["last_used_at"])
            self._write_marker(path, marker)
            return dict(marker)

    def protect_until(self, job_id: str, expires, *, link: bool = False) -> dict:
        """Persist the latest live link expiry before returning its URL.

        Unmanaged legacy jobs already have indefinite protection and are not
        silently adopted by this operation. ``link`` marks a playback URL, kept
        separately so Drive-backed cleanup waits only for issued links.
        """
        expires = _timestamp(expires)
        with self._guard():
            path = self._job(job_id)
            marker = self._marker(path)
            if marker is None:
                return {"managed": False, "protected_until": expires}
            marker["protected_until"] = max(expires, marker["protected_until"])
            if link:
                marker["link_until"] = max(expires, _timestamp(marker.get("link_until", 0)))
            self._write_marker(path, marker)
            return {"managed": True, "protected_until": marker["protected_until"]}

    def usage(self, protected_job_ids=(), now=None) -> dict:
        now = _timestamp(now)
        protected = {_job_id(job) for job in protected_job_ids}
        with self._guard():
            root = self._root()
            jobs, ignored = [], []
            for path in sorted(root.iterdir()):
                if not JOB_PATTERN.fullmatch(path.name):
                    ignored.append(path.name)
                    continue
                row = {"job_id": path.name, "bytes": 0, "managed": False,
                       "expires_at": None, "protected_until": 0, "eligible": False}
                try:
                    self._job(path.name)
                    row["bytes"], unsafe = self._scan(path)
                    if unsafe:
                        raise StoragePolicyError("Unsafe entry inside job")
                    marker = self._marker(path)
                    if marker is None:
                        row["reason"] = "unmanaged"
                    else:
                        row.update(managed=True,
                                   expires_at=max(marker["managed_at"], marker["last_used_at"]) + self.retention_seconds,
                                   protected_until=marker["protected_until"])
                        if path.name in protected:
                            row["reason"] = "active"
                        elif now < marker["protected_until"]:
                            row["reason"] = "live_share"
                        elif now < row["expires_at"]:
                            row["reason"] = "retained"
                        else:
                            row.update(eligible=True, reason="expired_managed_job")
                except (OSError, StoragePolicyError):
                    row["reason"] = "unsafe_or_unreadable"
                jobs.append(row)
            return {"total_bytes": sum(row["bytes"] for row in jobs),
                    "job_count": len(jobs),
                    "managed_job_count": sum(row["managed"] for row in jobs),
                    "jobs": jobs, "ignored_entries": ignored,
                    "free_bytes": shutil.disk_usage(root).free}

    def ensure_capacity(self, job_id: str, additional_bytes: int = 0,
                        protected_job_ids=(), now=None) -> dict:
        if isinstance(additional_bytes, bool) or not isinstance(additional_bytes, int) or additional_bytes < 0:
            raise ValueError("additional_bytes must be a nonnegative integer")
        with self._guard():
            self._job(job_id, missing_ok=True)
            report = self.usage(protected_job_ids=protected_job_ids, now=now)
            report["requested_bytes"] = additional_bytes
            current = next((row for row in report["jobs"] if row["job_id"] == job_id), None)
            if any(row["reason"] == "unsafe_or_unreadable" for row in report["jobs"]):
                raise StorageCapacityError("unsafe_storage", report)
            if (current["bytes"] if current else 0) + additional_bytes > self.max_job_bytes:
                raise StorageCapacityError("job_quota", report)
            if report["total_bytes"] + additional_bytes > self.max_total_bytes:
                raise StorageCapacityError("total_quota", report)
            if report["free_bytes"] - additional_bytes < self.min_free_bytes:
                raise StorageCapacityError("free_space", report)
            return report

    def cleanup(self, dry_run=True, protected_job_ids=(), now=None) -> dict:
        """Legacy lifecycle plan only. Whole-job deletion is permanently disabled.

        Use media_retention.py for completed-only media deletion; records must survive.
        """
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be boolean")
        now = _timestamp(now)
        protected = tuple(protected_job_ids)
        with self._guard():
            report = self.usage(protected_job_ids=protected, now=now)
            candidates = [dict(row) for row in report["jobs"] if row["eligible"]]
            result = {"dry_run": dry_run, "candidates": candidates,
                      "deleted_job_ids": [], "reclaimed_bytes": 0,
                      "skipped": [row for row in report["jobs"] if not row["eligible"]]}
            if dry_run or not candidates:
                return result
            result["skipped"].extend({"job_id": row["job_id"], "reason": "guarded_retention_required"}
                                     for row in candidates)
            return result


class MediaSigner:
    """HMAC URLs bind job, relative media path and expiry, never HTTP method."""

    def __init__(self, secret_file, max_ttl_seconds: int = 86400):
        if isinstance(max_ttl_seconds, bool) or not isinstance(max_ttl_seconds, int) or max_ttl_seconds <= 0:
            raise ValueError("max_ttl_seconds must be a positive integer")
        self.max_ttl_seconds = max_ttl_seconds
        self.secret_file = Path(os.path.abspath(secret_file))
        _check_ancestors(self.secret_file)
        self.secret_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = self.secret_file.with_name(self.secret_file.name + "." + secrets.token_hex(8) + ".tmp")
        try:
            if not self.secret_file.exists():
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
                with os.fdopen(fd, "wb") as stream:
                    if os.name == "posix":
                        os.fchmod(stream.fileno(), 0o600)
                    stream.write(secrets.token_bytes(32))
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    # Publish a complete key without replacing a concurrent winner.
                    os.link(temporary, self.secret_file)
                except FileExistsError:
                    pass
        finally:
            temporary.unlink(missing_ok=True)
        info = self.secret_file.lstat()
        if os.name == "posix" and stat.S_IMODE(info.st_mode) != 0o600:
            raise StoragePolicyError("Signing key must have mode 0600")
        self._key = _read_regular(self.secret_file, 32)
        if len(self._key) != 32:
            raise StoragePolicyError("Signing key must contain exactly 32 bytes")

    @staticmethod
    def _fields(job_id, relative_name, expires) -> tuple[str, str, int]:
        _job_id(job_id)
        if not isinstance(relative_name, str) or not (MEDIA_PATTERN.fullmatch(relative_name) or REPORT_PATTERN.fullmatch(relative_name)):
            raise ValueError("Only segment MP4, preview JPEG and canonical analysis report paths may be signed")
        if isinstance(expires, str):
            if not re.fullmatch(r"[1-9][0-9]{0,11}", expires):
                raise ValueError("Expiry must be a positive canonical integer")
            expires = int(expires)
        if isinstance(expires, bool) or not isinstance(expires, int) or not 0 < expires < 10**12:
            raise ValueError("Expiry must be a positive canonical integer")
        return job_id, relative_name, expires

    def _signature(self, fields) -> str:
        job_id, relative_name, expires = fields
        body = f"media-v1\n{job_id}\n{relative_name}\n{expires}".encode("ascii")
        return hmac.new(self._key, body, hashlib.sha256).hexdigest()

    def sign(self, job_id: str, relative_name: str, expires: int, *, now=None) -> str:
        fields = self._fields(job_id, relative_name, expires)
        now = _timestamp(now)
        if not now < fields[2] <= now + self.max_ttl_seconds:
            raise ValueError("Expiry is outside the permitted sharing window")
        return self._signature(fields)

    def verify(self, job_id: str, relative_name: str, expires, signature: str, *, now=None) -> bool:
        try:
            fields = self._fields(job_id, relative_name, expires)
            now = _timestamp(now)
            if not now < fields[2] <= now + self.max_ttl_seconds:
                return False
            if not isinstance(signature, str) or not re.fullmatch(r"[0-9a-f]{64}", signature):
                return False
            return hmac.compare_digest(self._signature(fields), signature)
        except (TypeError, ValueError, OverflowError):
            return False
