"""Durable bounded video queue. No provider credentials are stored in job inputs."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager

TERMINAL = {"completed", "failed", "cancelled", "expired", "interrupted"}
ATOMIC_REPLACE_ATTEMPTS = 5
ATOMIC_REPLACE_RETRY_SECONDS = 0.02
WINDOWS_TRANSIENT_REPLACE_ERRORS = {5, 32}  # access denied, sharing violation


class JobBusyError(RuntimeError):
    """No new operation started; retry after the deployment or writer releases."""


def parallel_job_limit():
    raw = os.getenv("MCP_MAX_PARALLEL_JOBS", "2")
    try:
        value = int(raw)
        if value < 1: raise ValueError
        return value
    except ValueError:
        logging.getLogger(__name__).warning("Invalid MCP_MAX_PARALLEL_JOBS=%r; using 2", raw)
        return 2


class WorkerSlots:
    """Shared admission for synchronous calls and the single durable scheduler."""
    def __init__(self):
        self.limit = parallel_job_limit()
        self.active = set()
        self.lock = threading.Lock()
        self.failed = False

    def acquire(self, key):
        with self.lock:
            if self.failed or key in self.active or len(self.active) >= self.limit: return False
            self.active.add(key)
            return True

    def release(self, key):
        with self.lock: self.active.remove(key)

    def snapshot(self):
        with self.lock: return set(self.active)


def validate_parent_writer_lease(work: Path, lease: dict, writer_path: Path) -> None:
    """Validate an internal queue delegation, never a caller-supplied MCP input."""
    work = Path(work)
    keys = {"owner_pid", "token", "writer_path", "operation_id", "operation", "job_id"}
    if not isinstance(lease, dict) or set(lease) != keys:
        raise ValueError("Invalid parent writer lease")
    ident = lease["operation_id"]
    token = lease["token"]
    if (not isinstance(ident, str) or len(ident) != 32 or any(c not in "0123456789abcdef" for c in ident)
            or work.name != ident or work.absolute() != work.resolve()
            or not isinstance(token, str) or len(token) != 32 or any(c not in "0123456789abcdef" for c in token)
            or lease["owner_pid"] != os.getppid() or os.getenv("VIDEO_JOB_WORKER") != "1"
            or lease["operation"] not in {"prepare", "analyze", "drive", "report"}
            or lease["writer_path"] != str(Path(writer_path).resolve())):
        raise ValueError("Invalid parent writer lease")
    file = work / "writer-lease.json"
    if file.is_symlink():
        raise ValueError("Invalid parent writer lease")
    owner = json.loads(file.read_text(encoding="utf-8"))
    if owner != lease:
        raise ValueError("Parent writer lease is no longer current")


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as out:
            if os.name != "nt": os.chmod(temp, 0o600)
            json.dump(value, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        for attempt in range(ATOMIC_REPLACE_ATTEMPTS):
            try:
                os.replace(temp, path)
                break
            except OSError as error:
                if (getattr(error, "winerror", None) not in WINDOWS_TRANSIENT_REPLACE_ERRORS
                        or attempt + 1 == ATOMIC_REPLACE_ATTEMPTS):
                    raise
                # A queue reader or scanner may briefly hold the destination
                # without delete sharing on Windows. Keep the retry bounded;
                # every other error and a persistent collision still fail.
                time.sleep(ATOMIC_REPLACE_RETRY_SECONDS)
    finally:
        temp.unlink(missing_ok=True)


def report_progress(stage: str, current: int, message: str) -> None:
    target = os.getenv("VIDEO_JOB_PROGRESS_FILE")
    if target:
        atomic_json(Path(target), {"stage": stage, "current": current, "total": 100,
                                  "message": message, "updated_at": time.time()})


def bind_windows_job(process: subprocess.Popen) -> None:
    """Use the Windows kernel for tree termination instead of spawning taskkill."""
    if os.name != "nt": return
    import ctypes
    from ctypes import wintypes as w
    class Limits(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                    ("flags", w.DWORD), ("min_working", ctypes.c_size_t), ("max_working", ctypes.c_size_t),
                    ("active_limit", w.DWORD), ("affinity", ctypes.c_size_t),
                    ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class Counters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
    class Extended(ctypes.Structure):
        _fields_ = [("limits", Limits), ("io", Counters), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]; api.CreateJobObjectW.restype = w.HANDLE
    api.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    api.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    api.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
    api.CloseHandle.argtypes = [w.HANDLE]
    handle = api.CreateJobObjectW(None, None)
    if not handle: raise OSError("Could not create worker job object")
    info = Extended(); info.limits.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not api.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not api.AssignProcessToJobObject(handle, int(process._handle)):
        api.CloseHandle(handle)
        raise OSError("Could not bind worker process tree")
    process._video_windows_job = (api, handle)


def stop_process_tree(process: subprocess.Popen) -> bool:
    job = getattr(process, "_video_windows_job", None)
    if job:
        api, handle = job
        api.TerminateJobObject(handle, 1)
        api.CloseHandle(handle)
        process._video_windows_job = None
    if process.poll() is not None: return True
    if os.name == "nt":
        if not job:
            try:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=5)
            except (OSError, subprocess.TimeoutExpired): pass
        if process.poll() is None:
            try: process.kill()
            except OSError: pass
    else:
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        except OSError: return False
    try: process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired): pass
    return process.poll() is not None


class JobQueue:
    def __init__(self, root: Path, *, worker: Path | None = None, timeout_seconds=3600,
                 max_queued=64, execution_lock=None, autostart=True,
                 paired_same_job=False, writer_context=None, writer_path: Path | None = None, idle_callback=None,
                 slots=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt": os.chmod(self.root, 0o700)
        self.db = self.root / "jobs.sqlite3"
        self.worker = worker or Path(__file__).with_name("job_worker.py")
        self.timeout_seconds = max(1, float(timeout_seconds))
        self.max_queued = max_queued
        self.execution_lock = execution_lock or threading.Lock()
        self.slots = slots or WorkerSlots()
        self.parallel_limit = self.slots.limit
        self._units = {}
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._process = None
        self._thread = None
        self._lease = None
        self._cleanup_failed = False
        self.idle_callback = idle_callback
        self._report_idle_failures = {}
        self.paired_same_job = bool(paired_same_job)
        if self.paired_same_job and (writer_context is None or writer_path is None):
            raise ValueError("Paired work requires a parent-owned writer lock")
        self.writer_context = writer_context
        self.writer_path = Path(writer_path).resolve() if writer_path is not None else None
        self._retained_contexts = []
        self._pair_processes = {}
        self._pair_process_lock = threading.Lock()
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, operation TEXT NOT NULL, inputs TEXT NOT NULL,
                input_hash TEXT NOT NULL, job_id TEXT, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, deadline REAL,
                result TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0)""")
            db.execute("CREATE INDEX IF NOT EXISTS queue_status ON jobs(status, created)")
        if os.name != "nt": os.chmod(self.db, 0o600)
        if autostart: self.start()

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.db, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def start(self):
        if self._thread is not None: return
        # Only one scheduler may own this state directory, even across processes.
        lease = (self.root / "scheduler.lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                lease.seek(0); lease.write(b"0"); lease.flush(); lease.seek(0)
                msvcrt.locking(lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lease.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError):
            lease.close()
            raise RuntimeError("Another video scheduler owns this state directory")
        self._lease = lease
        with self.connect() as db:
            # Prepare/analyze resume from their stage caches. Cloud writes need explicit retry.
            db.execute("UPDATE jobs SET status=CASE WHEN operation IN ('drive','report') THEN 'interrupted' ELSE 'queued' END, "
                       "updated=?, error='service_restarted', deadline=NULL WHERE status='running'", (time.time(),))
        self._thread = threading.Thread(target=self._loop, name="video-job-queue", daemon=True)
        self._thread.start()

    def submit(self, operation: str, inputs: dict, job_id: str | None = None) -> dict:
        if operation not in {"prepare", "analyze", "drive", "report"}: raise ValueError("Unknown operation")
        packed = json.dumps(inputs, sort_keys=True, separators=(",", ":"))
        key = hashlib.sha256(f"{operation}:{packed}".encode()).hexdigest()
        now = time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if (self.root / "deployment-draining").exists():
                raise JobBusyError("MCP 正在安全更新，請稍後重試；本次未加入背景佇列。")
            selection = "1=1" if operation == "report" else "status IN ('queued','running')"
            row = db.execute("SELECT id FROM jobs WHERE input_hash=? AND " + selection + " ORDER BY created LIMIT 1", (key,)).fetchone()
            if row:
                operation_id = row[0]
            else:
                count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
                if count >= self.max_queued: raise RuntimeError("Video job queue is full; retry later")
                operation_id = uuid.uuid4().hex
                db.execute("INSERT INTO jobs(id,operation,inputs,input_hash,job_id,status,created,updated) "
                           "VALUES(?,?,?,?,?,'queued',?,?)", (operation_id, operation, packed, key, job_id, now, now))
        self._wake.set()
        return self.get(operation_id)

    def _row(self, operation_id):
        if not isinstance(operation_id, str) or len(operation_id) != 32 or any(c not in '0123456789abcdef' for c in operation_id):
            raise ValueError("Invalid operation_id")
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (operation_id,)).fetchone()
        if row is None: raise ValueError("Unknown operation_id")
        return dict(row)

    def get(self, operation_id: str) -> dict:
        row = self._row(operation_id)
        payload = {"operation_id": row["id"], "operation": row["operation"], "status": row["status"],
                   "job_id": row["job_id"], "created_at": row["created"], "updated_at": row["updated"],
                   "attempts": row["attempts"]}
        if row["status"] == "queued":
            with self.connect() as db:
                queued = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created,rowid").fetchall()
            units, position = [], 0
            for item in queued:
                pairable = (self.paired_same_job and item["job_id"] and item["operation"] in {"analyze", "drive"}
                            and json.loads(item["inputs"]).get("job_id") == item["job_id"])
                mate = next((i for i, unit in enumerate(units) if pairable and unit ==
                             (item["job_id"], "drive" if item["operation"] == "analyze" else "analyze")), None)
                if mate is None:
                    position = len(units)
                    units.append((item["job_id"], item["operation"]) if pairable else None)
                else:
                    position = mate
                    units[mate] = None
                if item["id"] == operation_id: break
            payload.update(queue_position=position,
                           running_count=len(self.slots.snapshot()), parallel_limit=self.parallel_limit)
        if row["status"] == "completed":
            payload.update(result_kind={"prepare": "prepared", "analyze": "analysis", "drive": "drive", "report": "report"}[row["operation"]],
                           result=json.loads(row["result"]))
        else:
            payload["progress"] = {"stage": row["status"], "current": 0, "total": 100,
                                   "message": "等待處理" if row["status"] == "queued" else row["status"]}
            try:
                progress = json.loads((self.root / row["id"] / "progress.json").read_text(encoding="utf-8"))
                payload["progress"] = progress
            except (OSError, ValueError): pass
            if row["status"] in {"queued", "running"}: payload["retry_after_seconds"] = 2
            if row["error"]: payload["error"] = row["error"]
            if row["operation"] == "prepare" and row["error"] and row["status"] in {"failed", "expired"}:
                from pipeline import classify_download_error
                try:
                    inputs = json.loads(row["inputs"] or "{}")
                except (TypeError, ValueError):
                    inputs = {}
                url = inputs.get("url") if isinstance(inputs, dict) else ""
                payload.update(classify_download_error(row["error"], url if isinstance(url, str) else ""))
        if row["deadline"]: payload["deadline_at"] = row["deadline"]
        return payload

    def for_job(self, job_id):
        with self.connect() as db:
            rows = db.execute("SELECT id,status FROM jobs WHERE job_id=? ORDER BY created DESC", (job_id,)).fetchall()
        active = next((row for row in rows if row["status"] in {"queued", "running"}), None)
        pending = [self.get(row["id"]) for row in rows if row["status"] in {"queued", "running"}]
        return {"active_operation": self.get(active["id"]) if active else None,
                "latest_operation": self.get(rows[0]["id"]) if rows else None,
                "pending_operations": pending}

    def protected_job_ids(self):
        with self.connect() as db:
            return {row[0] for row in db.execute("SELECT job_id FROM jobs WHERE status IN ('queued','running') AND job_id IS NOT NULL")}

    def stats(self):
        with self.connect() as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT status,count(*) FROM jobs GROUP BY status")}
        return {"worker_limit": self.parallel_limit, "running_count": len(self.slots.snapshot()),
                "scheduler_alive": bool(self._thread and self._thread.is_alive()),
                "cleanup_failed": self._cleanup_failed, "counts": counts}

    def cancel(self, operation_id):
        self._row(operation_id)
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='cancelled',updated=?,error='cancelled_by_user' "
                       "WHERE id=? AND status IN ('queued','running')", (time.time(), operation_id))
        self._wake.set()
        return self.get(operation_id)

    def resume(self, operation_id):
        row = self._row(operation_id)
        if row["status"] in {"completed", "queued", "running"}: return self.get(operation_id)
        if row["operation"] == "report":
            with self.connect() as db:
                db.execute("UPDATE jobs SET status='queued',updated=?,error=NULL,deadline=NULL WHERE id=? AND status IN ('failed','cancelled','expired','interrupted')", (time.time(), operation_id))
            self._wake.set()
            return self.get(operation_id)
        return self.submit(row["operation"], json.loads(row["inputs"]), row["job_id"])

    def close(self):
        self._stop.set(); self._wake.set()
        if self._thread: self._thread.join(timeout=20)
        if self._thread and self._thread.is_alive():
            raise RuntimeError("Scheduler is still stopping; ownership lock retained")
        if self._cleanup_failed:
            raise RuntimeError("Worker cleanup incomplete; ownership lock retained")
        if self._lease:
            self._lease.close(); self._lease = None

    def _loop(self):
        while not self._stop.is_set():
            try:
                self._reap_units()
                if self._cleanup_failed: break
                if self.execution_lock.acquire(blocking=False):
                    try:
                        with self.connect() as db:
                            rows = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created,rowid").fetchall()
                        for row in rows:
                            key = row["job_id"] or row["id"]
                            if not self.slots.acquire(key): continue
                            context = None
                            try:
                                if self.writer_path is not None:
                                    from pipeline import file_writer_lock, job_writer_path
                                    with file_writer_lock(self.writer_path):
                                        context = self.writer_context(job_writer_path(self.writer_path.parent, key))
                                        context.__enter__()
                            except Exception:
                                self.slots.release(key)
                                continue  # An external job lock must not block other eligible jobs.
                            thread = threading.Thread(target=self._run_unit, args=(dict(row), context), daemon=True,
                                                      name="video-unit-" + row["id"])
                            self._units[key] = thread
                            try:
                                thread.start()
                            except Exception:
                                del self._units[key]
                                if context is not None: context.__exit__(None, None, None)
                                self.slots.release(key)
                                raise
                    finally:
                        self.execution_lock.release()
                self._notify_idle()
            except Exception:
                logging.getLogger(__name__).exception("Video scheduler iteration failed")
            self._wake.wait(.05); self._wake.clear()
        for thread in list(self._units.values()): thread.join()
        self._reap_units()

    def _reap_units(self):
        for key, thread in list(self._units.items()):
            if not thread.is_alive() and not self._cleanup_failed:
                del self._units[key]
                self.slots.release(key)

    def report_prerequisites_stopped(self, job_id):
        if self._cleanup_failed or job_id in self._units: return False
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM jobs WHERE job_id=? AND operation IN ('prepare','analyze','drive') AND status IN ('queued','running')", (job_id,)).fetchone()[0] == 0

    def _notify_idle(self):
        if self.idle_callback is not None and not self._stop.is_set() and not self._cleanup_failed:
            try:
                self.idle_callback(self)
            except Exception:
                logging.getLogger(__name__).exception("佇列閒置回呼失敗；排程將繼續。")

    def _run_unit(self, primary, context=None):
        # A unit retains its slot and OS lock until every owned process is stopped.
        from pipeline import job_writer_path
        threads, leases = [], []
        try:
            if self.writer_path is None:
                self._execute(primary)
                return
            path = job_writer_path(self.writer_path.parent, primary["job_id"] or primary["id"])
            def start(row):
                work = self.root / row["id"]
                lease = {"owner_pid": os.getpid(), "token": uuid.uuid4().hex,
                         "writer_path": str(path.resolve()), "operation_id": row["id"],
                         "operation": row["operation"], "job_id": row["job_id"]}
                lease_file = work / "writer-lease.json"
                atomic_json(lease_file, lease)
                leases.append(lease_file)
                thread = threading.Thread(target=self._execute, args=(row, lease), daemon=True,
                                          name="video-operation-" + row["id"])
                threads.append(thread)
                thread.start()

            start(primary)
            companion_started = False
            while any(thread.is_alive() for thread in threads):
                if (self.paired_same_job and not self._stop.is_set() and not companion_started
                        and primary["job_id"] and primary["operation"] in {"analyze", "drive"}
                        and json.loads(primary["inputs"]).get("job_id") == primary["job_id"]
                        and self._row(primary["id"])["status"] == "running"):
                    opposite = "drive" if primary["operation"] == "analyze" else "analyze"
                    with self.connect() as db:
                        candidates = db.execute("SELECT * FROM jobs WHERE status='queued' AND job_id=? AND operation=? "
                                                "ORDER BY created,rowid", (primary["job_id"], opposite)).fetchall()
                    mate = next((item for item in candidates if json.loads(item["inputs"]).get("job_id") == primary["job_id"]), None)
                    if mate:
                        companion_started = True
                        start(dict(mate))
                self._stop.wait(.05) if not self._stop.is_set() else time.sleep(.05)
        except Exception:
            if threads:
                self._stop.set()
                for thread in threads: thread.join()
            else:
                # A contended job remains queued; other units continue independently.
                self._stop.wait(.1)
        finally:
            if self._cleanup_failed or any(thread.is_alive() for thread in threads):
                self._cleanup_failed = True
                self.slots.failed = True
                self._stop.set()
                # Keep a strong reference: generator context finalization would unlock it.
                self._retained_contexts.append(context)
                return
            if context is not None:
                try:
                    for lease_file in leases: lease_file.unlink(missing_ok=True)
                    context.__exit__(None, None, None)
                except Exception:
                    self._cleanup_failed = True
                    self.slots.failed = True
                    self._stop.set()
                    self._retained_contexts.append(context)
            self._wake.set()

    def _execute(self, row, parent_writer_lease=None):
        operation_id = row["id"]
        work = self.root / operation_id
        deadline = time.time() + self.timeout_seconds
        with self.connect() as db:
            updated = db.execute("UPDATE jobs SET status='running',updated=?,deadline=?,attempts=attempts+1,error=NULL "
                                 "WHERE id=? AND status='queued'", (time.time(), deadline, operation_id)).rowcount
        if not updated: return
        output = work / "output.json"
        env = {**os.environ, "VIDEO_JOB_DEADLINE": str(deadline), "VIDEO_JOB_PROGRESS_FILE": str(work / "progress.json"),
               "VIDEO_JOB_WORKER": "1"}
        process = None
        try:
            work.mkdir(mode=0o700, exist_ok=True)
            task = {"operation": row["operation"], "inputs": json.loads(row["inputs"])}
            if parent_writer_lease is not None:
                task["parent_writer_lease"] = parent_writer_lease
            atomic_json(work / "input.json", task)
            output.unlink(missing_ok=True)
            command = [sys.executable, str(self.worker), str(work)]
            if os.name == "nt":
                # venv/Store launchers spawn Python before it can be assigned to the job.
                import ctypes
                from ctypes import wintypes as w
                api = ctypes.WinDLL("kernel32", use_last_error=True)
                api.GetModuleFileNameW.argtypes = [w.HMODULE, w.LPWSTR, w.DWORD]
                api.GetModuleFileNameW.restype = w.DWORD
                executable = ctypes.create_unicode_buffer(32768)
                if not api.GetModuleFileNameW(None, executable, len(executable)):
                    raise OSError("Cannot resolve the running Python executable")
                command[0] = executable.value
                env["__PYVENV_LAUNCHER__"] = sys.executable
                # Bind the Windows job before worker code can spawn any descendants.
                command[1:1] = ["-c", "import sys,runpy,os; assert sys.stdin.buffer.read(1)==b'1'; "
                                     f"sys.executable={executable.value!r}; os.environ['__PYVENV_LAUNCHER__']={sys.executable!r}; "
                                     "sys.argv=sys.argv[1:]; runpy.run_path(sys.argv[0],run_name='__main__')"]
            process = subprocess.Popen(command, stdin=subprocess.PIPE,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
                                       start_new_session=os.name != "nt",
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
            with self._pair_process_lock:
                self._pair_processes[operation_id] = process
            bind_windows_job(process)
            if os.name == "nt":
                process.stdin.write(b"1")
                process.stdin.flush()
            while process.poll() is None:
                status = self._row(operation_id)["status"]
                if status == "cancelled" or self._stop.is_set() or time.time() >= deadline:
                    break
                self._stop.wait(.1)
            current = self._row(operation_id)["status"]
            if current == "cancelled": return
            result = None
            if self._stop.is_set():
                status, error = ("interrupted" if row["operation"] in {"drive", "report"} else "queued"), "service_stopped"
            elif time.time() >= deadline:
                status, error = "expired", "工作已達總時間上限；可從已完成階段續跑。"
            else:
                try: outcome = json.loads(output.read_text(encoding="utf-8"))
                except (OSError, ValueError): outcome = {"error": "Worker exited without a valid result"}
                if outcome.get("ok"):
                    status, error, result = "completed", None, outcome["result"]
                else:
                    status = "expired" if outcome.get("expired") else "failed"
                    error = str(outcome.get("error") or "Worker failed")[:1000]
            with self.connect() as db:
                db.execute("UPDATE jobs SET status=?,updated=?,result=?,error=?,job_id=COALESCE(?,job_id) "
                           "WHERE id=? AND status='running'", (status, time.time(), json.dumps(result) if result is not None else None,
                           error, result.get("job_id") if isinstance(result, dict) else None, operation_id))
        except Exception as exc:
            with self.connect() as db:
                db.execute("UPDATE jobs SET status='failed',updated=?,error=? WHERE id=? AND status='running'",
                           (time.time(), f"Worker launch failed ({type(exc).__name__})", operation_id))
        finally:
            cleaned = process is None
            try:
                if process and process.stdin: process.stdin.close()
                if process:
                    cleaned = stop_process_tree(process)
                    if os.name != "nt":
                        # Reap descendants even if their direct parent already exited.
                        try: os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError: pass
            except Exception:
                cleaned = False
            if not cleaned:
                # Keep exclusive ownership and stop scheduling; never overlap an
                # unconfirmed live worker with a second heavy operation.
                self._stop.set()
                self._cleanup_failed = True
                self.slots.failed = True
                with self.connect() as db:
                    db.execute("UPDATE jobs SET error=? WHERE id=?", ("Worker cleanup incomplete; scheduler stopped", operation_id))
            if not self._cleanup_failed:
                with self._pair_process_lock:
                    self._pair_processes.pop(operation_id, None)
