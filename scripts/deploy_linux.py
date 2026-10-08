"""Pull tested main releases without changing the developer checkout or its data."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import select
import signal
import socket
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit
import uuid

REPOSITORY = "Reese-max/video-timeline-pipeline"
WORKFLOW = ".github/workflows/cost-timezone.yml"
RUNTIME_FILES = ("mcp_server.py", "pipeline.py", "job_worker.py", "job_queue.py", "execution_control.py",
                 "media_retention.py", "media_lifecycle.py", "drive_storage.py", "cost_tracker.py",
                 "scoring.py", "notifications.py", "web/widget.html", "requirements.txt")


class Deferred(RuntimeError):
    pass


def write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, path)


def receipt(home: Path, status: str, **values) -> dict:
    value = {"time_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "status": status, **values}
    write_json(home / "last-result.json", value)
    with (home / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
    return value


def command(args: list[str], *, cwd: Path | None = None, env: dict | None = None,
            log: Path | None = None, timeout: int = 1800) -> bytes:
    # No child output enters a receipt: authentication failures may contain secrets.
    if log:
        with log.open("ab") as output:
            result = subprocess.run(args, cwd=cwd, env=env, stdout=output,
                                    stderr=subprocess.STDOUT, timeout=timeout)
    else:
        result = subprocess.run(args, cwd=cwd, env=env, capture_output=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{Path(args[0]).name} exited {result.returncode}; see private deployment log")
    return b"" if log else result.stdout


def ci_success(runs: list[dict], sha: str) -> dict:
    matching = [run for run in runs if run.get("head_sha") == sha
                and run.get("head_branch") == "main" and run.get("event") == "push"
                and run.get("name") == "Video Timeline CI"
                and str(run.get("path", "")).split("@")[0] == WORKFLOW]
    if not matching:
        raise Deferred("CI_MISSING_EXACT_MAIN_PUSH")
    latest = max(matching, key=lambda run: (run.get("updated_at", ""),
                                           int(run.get("id", 0)), int(run.get("run_attempt", 1))))
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise Deferred("CI_LATEST_ATTEMPT_NOT_SUCCESS")
    if int(latest.get("id", 0)) <= 0 or int(latest.get("run_attempt", 1)) <= 0:
        raise Deferred("CI_ID_UNKNOWN")
    return {key: latest.get(key) for key in
            ("id", "run_attempt", "head_sha", "head_branch", "event", "name", "path",
             "status", "conclusion", "html_url", "updated_at")}


def github_ci(sha: str) -> dict:
    endpoint = (f"repos/{REPOSITORY}/actions/workflows/cost-timezone.yml/runs"
                f"?head_sha={sha}&branch=main&event=push&per_page=100")
    data = json.loads(command(["gh", "api", endpoint], timeout=30))
    # More than one page is unexpected for one SHA; do not silently miss a rerun.
    if data.get("total_count", 0) > 100:
        raise Deferred("CI_PAGINATION_REQUIRES_REVIEW")
    return ci_success(data.get("workflow_runs", []), sha)


def clean_environment(**values) -> dict:
    permitted = ("PATH", "HOME", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "LC_ALL",
                 "SSL_CERT_FILE", "SSL_CERT_DIR", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY")
    env = {key: os.environ[key] for key in permitted if key in os.environ}
    return {**env, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", **values}


def hashes(root: Path, names) -> dict[str, str]:
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in names}


def unpack_code(archive: bytes, destination: Path) -> list[str]:
    excluded = {"analyses", "jobs", "mcp_jobs", ".video-state", ".runtime", ".venv",
                "cookies", "secrets", "batches", "rss", "canary", "node_modules"}
    names = []
    with tarfile.open(fileobj=io.BytesIO(archive)) as source:
        for member in source.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or not path.parts:
                raise RuntimeError("Unsafe archive path")
            if (path.parts[0] in excluded or (path.name.startswith(".env") and path.name != ".env.example")
                    or ".bak-" in path.name):
                continue
            if not member.isdir() and not member.isfile():
                raise RuntimeError("Release contains a link or special file")
            if path.suffix in {".db", ".sqlite", ".sqlite3", ".log", ".mp4", ".wav", ".mp3"}:
                if not member.name.startswith("tests/fixtures/"):
                    continue
            if member.isfile():
                target = destination / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.extractfile(member) as stream, target.open("xb") as output:
                    output.write(stream.read())
                target.chmod(member.mode & 0o777)
                names.append(member.name)
    return names


def http_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("Health HTTP status is not 200")
        return json.load(response)


def mcp_tools(base: str) -> list[dict]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def post(payload):
        request = urllib.request.Request(base.rstrip("/") + "/mcp", json.dumps(payload).encode(), headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.headers.get("Mcp-Session-Id"):
                headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
            raw = response.read().decode()
            if response.headers.get("Content-Type", "").startswith("text/event-stream"):
                raw = "\n".join(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
            result = json.loads(raw) if raw else {}
            if result.get("error"):
                raise RuntimeError("MCP protocol returned an error")
            return result.get("result", {})

    result = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "video-gitops-smoke", "version": "1"}}})
    if result.get("serverInfo", {}).get("name") != "video-timeline-pipeline":
        raise RuntimeError("MCP server identity mismatch")
    headers["MCP-Protocol-Version"] = result["protocolVersion"]
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})["tools"]


def schema_compatible(old: dict, new: dict, *, output: bool = False) -> bool:
    # Descriptions may change; existing argument/output constraints must still hold.
    if old.get("type") != new.get("type") or old.get("enum") != new.get("enum"):
        return False
    if old.get("$ref") != new.get("$ref"):
        return False
    old_required, new_required = set(old.get("required", [])), set(new.get("required", []))
    if not (old_required.issubset(new_required) if output else new_required.issubset(old_required)):
        return False
    for name, value in old.get("properties", {}).items():
        if name not in new.get("properties", {}) or not schema_compatible(value, new["properties"][name], output=output):
            return False
    for key in ("$defs", "definitions"):
        for name, value in old.get(key, {}).items():
            if name not in new.get(key, {}) or not schema_compatible(value, new[key][name], output=output):
                return False
    if isinstance(old.get("items"), dict):
        if not isinstance(new.get("items"), dict) or not schema_compatible(old["items"], new["items"], output=output):
            return False
    elif old.get("items") != new.get("items"):
        return False
    for key in ("anyOf", "allOf", "oneOf"):
        if len(old.get(key, [])) != len(new.get(key, [])):
            return False
        if any(not schema_compatible(a, b, output=output) for a, b in zip(old.get(key, []), new.get(key, []))):
            return False
    handled = {"type", "enum", "$ref", "properties", "required", "items", "$defs", "definitions",
               "anyOf", "allOf", "oneOf", "description", "title", "examples"}
    for key in (old.keys() | new.keys()) - handled:
        if old.get(key) != new.get(key):
            return False
    return True


def tools_compatible(old: list[dict], new: list[dict]) -> None:
    by_name = {tool["name"]: tool for tool in new}
    if len(by_name) != len(new) or not old or not new:
        raise RuntimeError("Missing or duplicate MCP tools")
    for before in old:
        after = by_name.get(before["name"])
        if not after or not schema_compatible(before["inputSchema"], after["inputSchema"]):
            raise RuntimeError("Incompatible MCP input schema: " + before["name"])
        if bool(before.get("outputSchema")) != bool(after.get("outputSchema")):
            raise RuntimeError("MCP output schema removed")
        if before.get("outputSchema") and not schema_compatible(before["outputSchema"], after["outputSchema"], output=True):
            raise RuntimeError("Incompatible MCP output schema: " + before["name"])
        prior_annotations = {key: item for key, item in before.get("annotations", {}).items() if key != "title"}
        new_annotations = {key: item for key, item in after.get("annotations", {}).items() if key != "title"}
        if prior_annotations != new_annotations:
            raise RuntimeError("MCP tool annotations changed: " + before["name"])


def verify_runtime(base: str, expected: dict, sha: str | None = None) -> dict:
    health = http_json(base.rstrip("/") + "/health")
    if health.get("status") != "ok" or health.get("service") != "video-timeline-pipeline":
        raise RuntimeError("Runtime health identity mismatch")
    if sha and health.get("commit_sha") != sha:
        raise RuntimeError("Runtime commit SHA mismatch")
    if any(health.get("runtime_sha256", {}).get(name) != digest for name, digest in expected.items()):
        raise RuntimeError("Runtime source hash mismatch")
    if (health.get("jobs", {}).get("scheduler_alive") is not True
            or health.get("jobs", {}).get("cleanup_failed") is not False):
        raise RuntimeError("Runtime job scheduler is unhealthy")
    return health


def wait_runtime(base: str, expected: dict, sha: str | None, seconds: int = 30) -> dict:
    deadline = time.monotonic() + seconds
    while True:
        try:
            return verify_runtime(base, expected, sha)
        except (OSError, ValueError, RuntimeError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)


def prepare(mirror: Path, home: Path, shared: Path, sha: str, ci: dict) -> tuple[Path, dict]:
    release = home / "releases" / sha
    prepared = release / "prepared.json"
    if prepared.is_file():
        value = json.loads(prepared.read_text())
        if value.get("sha") != sha or hashes(release, value["source_sha256"]) != value["source_sha256"]:
            raise RuntimeError("Prepared release source changed")
        return release, value
    if release.exists():
        raise RuntimeError("Incomplete release preserved; inspect it before retrying")
    release.mkdir(parents=True, mode=0o700)
    archive = command(["git", "--git-dir", str(mirror), "archive", "--format=tar", sha])
    files = unpack_code(archive, release)
    source_hashes = hashes(release, files)
    log = release / "offline-tests.log"
    python = release / ".venv" / "bin" / "python"
    command([sys.executable, "-m", "venv", str(release / ".venv")], env=clean_environment(), log=log)
    command([str(python), "-m", "pip", "install", "-r", "requirements.txt"], cwd=release,
            env=clean_environment(), log=log)
    command([str(python), "-X", "utf8", "-B", "scripts/check_offline.py"], cwd=release,
            env=clean_environment(), log=log)
    if hashes(release, files) != source_hashes:
        raise RuntimeError("Offline checks modified release source")
    with tempfile.TemporaryDirectory(prefix="video-release-smoke-") as temporary:
        with socket.socket() as reserve:
            reserve.bind(("127.0.0.1", 0))
            port = reserve.getsockname()[1]
        base = f"http://127.0.0.1:{port}"
        env = clean_environment(MCP_ROOT=temporary, MCP_HOST="127.0.0.1", MCP_PORT=str(port),
                                MCP_PUBLIC_BASE_URL=base, MCP_MEDIA_BASE_URL=base, MCP_RELEASE_SHA=sha)
        with log.open("ab") as output:
            server = subprocess.Popen([str(python), "-B", "mcp_server.py"], cwd=release,
                                      env=env, stdout=output, stderr=output, start_new_session=True)
            try:
                health = wait_runtime(base, hashes(release, RUNTIME_FILES), sha)
                tools = mcp_tools(base)
                if not tools or health.get("jobs", {}).get("counts"):
                    raise RuntimeError("Isolated MCP smoke state is not empty")
            finally:
                if server.poll() is None:
                    os.killpg(server.pid, signal.SIGTERM)
                    try:
                        server.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        os.killpg(server.pid, signal.SIGKILL)
                        server.wait(timeout=5)
    value = {"sha": sha, "ci": ci, "source_sha256": source_hashes,
             "runtime_sha256": hashes(release, RUNTIME_FILES), "tools": tools,
             "offline_test_exit": 0, "isolated_smoke": True,
             "archive_sha256": hashlib.sha256(archive).hexdigest()}
    # Attach production credentials only after all isolated tests and smoke calls pass.
    if (shared / ".env").is_file():
        (release / ".env").symlink_to(shared / ".env")
    write_json(prepared, value)
    return release, value


@contextlib.contextmanager
def locked(path: Path):
    import fcntl
    if path.is_symlink():
        raise RuntimeError("Lock path is a symlink")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Deferred("LOCK_BUSY: " + path.name) from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def atomic_current(link: Path, target: Path | None) -> None:
    if link.exists() and not link.is_symlink():
        raise RuntimeError("Current release pointer is not a symlink")
    if target is None:
        link.unlink(missing_ok=True)
        return
    temporary = link.with_name(".current." + uuid.uuid4().hex)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, link)


def rollback_smoke() -> dict:
    with tempfile.TemporaryDirectory(prefix="video-rollback-smoke-") as name:
        home = Path(name)
        old, new = home / "old", home / "new"
        old.mkdir(); new.mkdir()
        current = home / "current"
        atomic_current(current, old)
        try:
            atomic_current(current, new)
            raise RuntimeError("injected failed candidate verification")
        except RuntimeError:
            atomic_current(current, old)
        assert current.resolve() == old.resolve()
        return {"status": "ROLLBACK_SMOKE_OK", "production_verified": False}


def stop_verified(shared: Path, code: Path) -> None:
    pid = int((shared / "mcp-server.pid").read_text().strip())
    if pid <= 1 or not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("Verified process signaling requires Linux pidfds")
    try:
        descriptor = os.pidfd_open(pid)
    except ProcessLookupError:
        return  # No process exists to signal; the launcher must still verify its port.
    try:
        process = Path("/proc") / str(pid)
        args = process.joinpath("cmdline").read_bytes().split(b"\0")
        expected = str(code.resolve() / "mcp_server.py").encode()
        if process.joinpath("cwd").resolve() != code.resolve() or expected not in args:
            raise RuntimeError("PID/cwd/server identity mismatch; no signal sent")
        if select.select([descriptor], [], [], 0)[0]:
            raise RuntimeError("Verified server already exited")
        signal.pidfd_send_signal(descriptor, signal.SIGTERM)
        if not select.select([descriptor], [], [], 30)[0]:
            raise RuntimeError("Server did not exit after TERM; no forced termination")
    finally:
        os.close(descriptor)


def switch_idle(shared: Path, home: Path, target: Path | None, code: Path, *, allow_unhealthy=False) -> None:
    with locked(shared / "mcp-supervisor.lock"), locked(shared / "mcp_jobs" / ".writer.lock"):
        database = shared / ".video-state" / "jobs.sqlite3"
        db = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=0)
        try:
            try:
                db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if getattr(error, "sqlite_errorcode", 0) & 0xff in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                    raise Deferred("QUEUE_TRANSACTION_BUSY") from None
                raise
            count = db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
            if count:
                raise Deferred("ACTIVE_VIDEO_WORK")
            try:
                health = http_json("http://127.0.0.1:8787/health")
            except (OSError, ValueError):
                if not allow_unhealthy:
                    raise
                health = {}  # DB + writer locks + candidate drain still prove no active work.
            if not allow_unhealthy:
                jobs = health.get("jobs", {})
                if (health.get("status") != "ok" or health.get("service") != "video-timeline-pipeline"
                        or not isinstance(health.get("writer_busy"), bool)
                        or not isinstance(jobs.get("counts"), dict)
                        or jobs.get("scheduler_alive") is not True or jobs.get("cleanup_failed") is not False):
                    raise RuntimeError("Runtime queue ownership/health is unknown")
                inventory = health.get("runtime_sha256", {})
                if (not inventory or any(name not in RUNTIME_FILES for name in inventory)
                        or hashes(code, inventory) != inventory):
                    raise RuntimeError("Runtime source changed before switch")
            if health.get("writer_busy") or any(
                health.get("jobs", {}).get("counts", {}).get(status, 0) for status in ("queued", "running")
            ):
                raise Deferred("ACTIVE_VIDEO_WORK")
            # Hold DB/writer locks through verified old-process exit. The prior runtime
            # may not yet understand the draining flag during its first migration.
            current = home / "current"
            prior = current.resolve() if current.is_symlink() else None
            atomic_current(current, target)
            try:
                stop_verified(shared, code)
            except Exception:
                atomic_current(current, prior)
                raise
        finally:
            db.rollback()
            db.close()


def activate(shared: Path, home: Path, release: Path, value: dict, public: str) -> dict:
    launcher = shared / "scripts" / "start-mcp-supervised.sh"
    if "VIDEO_GITOPS_PROTOCOL=1" not in launcher.read_text():
        raise Deferred("SUPERVISOR_PROTOCOL_NOT_INSTALLED")
    registry = json.loads(Path("/tmp/sand-desktop/d1/video-timeline-mcp.json").read_text())
    if str(launcher) not in registry.get("argv", []) or "--run-server" not in registry.get("argv", []):
        raise Deferred("STABLE_SUPERVISOR_REGISTRY_NOT_INSTALLED")
    current = home / "current"
    if not current.is_symlink():
        raise Deferred("CURRENT_BOOTSTRAP_POINTER_MISSING")
    previous = current.resolve()
    if previous != shared and not previous.is_relative_to(home / "releases"):
        raise RuntimeError("Current release points outside release home")
    old_code = previous or shared
    old_health = http_json("http://127.0.0.1:8787/health")
    old_public = old_health.get("public_base_url")
    if not old_public:
        pid = int((shared / "mcp-server.pid").read_text().strip())
        process = Path("/proc") / str(pid)
        if process.joinpath("cwd").resolve() != old_code.resolve():
            raise RuntimeError("Public endpoint process identity mismatch")
        old_public = next((item.split(b"=", 1)[1].decode() for item in
                           process.joinpath("environ").read_bytes().split(b"\0")
                           if item.startswith(b"MCP_PUBLIC_BASE_URL=")), "")
    if old_public.rstrip("/") != public:
        raise Deferred("PUBLIC_ENDPOINT_CHANGED_CONNECTOR_REVIEW_REQUIRED")
    old_tools = mcp_tools("http://127.0.0.1:8787")
    tools_compatible(old_tools, value["tools"])
    if previous == release.resolve():
        local = verify_runtime("http://127.0.0.1:8787", value["runtime_sha256"], value["sha"])
        verify_runtime(public, value["runtime_sha256"], value["sha"])
        tools_compatible(old_tools, mcp_tools(public))
        if local.get("deployment_draining") or (shared / ".video-state" / "deployment-draining").exists():
            raise Deferred("CURRENT_RELEASE_DRAINING_REQUIRES_REVIEW")
        return {"sha": value["sha"], "already_current": True, "public_base": public,
                "runtime_sha256": local["runtime_sha256"], "public_verified": True}
    old_hashes = old_health.get("runtime_sha256", {})
    if not old_hashes or any(name not in RUNTIME_FILES for name in old_hashes):
        raise RuntimeError("Invalid old runtime source hash inventory")
    if hashes(old_code, old_hashes) != old_hashes:
        raise RuntimeError("Old runtime/disk source mismatch")
    flag = shared / ".video-state" / "deployment-draining"
    try:
        with flag.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({"sha": value["sha"], "pid": os.getpid()}))
    except FileExistsError:
        raise Deferred("EXISTING_DRAIN_FLAG_REQUIRES_REVIEW") from None
    env = {**os.environ, "MCP_BASE_DIR": str(shared), "MCP_DEPLOY_HOME": str(home)}

    def start():
        command(["/bin/bash", str(launcher), "d1"], cwd=shared, env=env, timeout=30)

    switched = safe_to_resume = False
    try:
        switch_idle(shared, home, release, old_code)
        switched = True
        start()
        local = wait_runtime("http://127.0.0.1:8787", value["runtime_sha256"], value["sha"])
        if not local.get("deployment_draining"):
            raise RuntimeError("Candidate runtime does not enforce drain protocol")
        tools_compatible(old_tools, mcp_tools("http://127.0.0.1:8787"))
        remote = verify_runtime(public, value["runtime_sha256"], value["sha"])
        tools_compatible(old_tools, mcp_tools(public))
        safe_to_resume = True
        return {"sha": value["sha"], "previous_release": str(previous) if previous else None,
                "public_base": public, "runtime_sha256": local["runtime_sha256"],
                "public_verified": remote["status"] == "ok"}
    except Exception as error:
        if switched:
            # If a candidate cannot be verified, its identity still must be proven
            # before stopping it. Unknown/dead processes stay fail-closed for review.
            switch_idle(shared, home, previous, release, allow_unhealthy=True)
            start()
            wait_runtime("http://127.0.0.1:8787", old_hashes, old_health.get("commit_sha"))
            tools_compatible(old_tools, mcp_tools("http://127.0.0.1:8787"))
            verify_runtime(public, old_hashes, old_health.get("commit_sha"))
            tools_compatible(old_tools, mcp_tools(public))
            safe_to_resume = True
            rollback = receipt(home, "ROLLED_BACK", attempted_sha=value["sha"],
                               previous_release=str(previous), previous_commit_sha=old_health.get("commit_sha"),
                               runtime_sha256=old_hashes, public_base=public, rollback_verified=True)
            write_json(home / "last-rollback.json", rollback)
        elif isinstance(error, Deferred):
            # No process was switched; its existing work must continue normally.
            safe_to_resume = True
        raise
    finally:
        # An unsuccessful rollback must keep admissions closed for investigation.
        if safe_to_resume:
            flag.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared-root", type=Path, default=Path("/home/box/video-timeline-pipeline"))
    parser.add_argument("--deploy-home", type=Path, default=Path("/home/box/video-timeline-deploy"))
    parser.add_argument("--repo-url", default=f"https://github.com/{REPOSITORY}.git")
    parser.add_argument("--public-base", help="live discovered MCP public base; required for activation")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--prepare-only", action="store_true")
    mode.add_argument("--activate", action="store_true")
    mode.add_argument("--rollback-smoke", action="store_true")
    args = parser.parse_args(argv)
    if args.rollback_smoke:
        print(json.dumps(rollback_smoke()))
        return 0
    if sys.platform != "linux":
        parser.error("deployment runs only on Linux; rollback-smoke is portable")
    if args.repo_url not in {f"https://github.com/{REPOSITORY}.git", f"git@github.com:{REPOSITORY}.git"}:
        parser.error("repository must be Reese-max/video-timeline-pipeline")
    shared, home = args.shared_root.resolve(), args.deploy_home.resolve()
    if shared == home or home.is_relative_to(shared) or shared.is_relative_to(home):
        parser.error("deploy home and shared checkout must be separate")
    if not shared.is_dir():
        parser.error("shared checkout does not exist")
    if args.activate:
        args.public_base = (args.public_base or "").strip().strip("'\"").rstrip("/")
        address = urlsplit(args.public_base)
        if (address.scheme != "https" or not address.hostname or address.username or address.password
                or address.query or address.fragment):
            parser.error("activation requires the currently discovered HTTPS public base without credentials")
    os.umask(0o077)
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        with locked(home / "deploy.lock"):
            sha = command(["git", "ls-remote", args.repo_url, "refs/heads/main"], timeout=30).decode().split()[0]
            if not re.fullmatch(r"[a-f0-9]{40}", sha):
                raise RuntimeError("GitHub main SHA is invalid")
            ci = github_ci(sha)
            if args.check_only:
                result = receipt(home, "CI_ELIGIBLE", sha=sha, ci=ci, deployed=False)
            else:
                mirror = home / "mirror.git"
                if not mirror.exists():
                    command(["git", "init", "--bare", str(mirror)])
                    command(["git", "--git-dir", str(mirror), "remote", "add", "origin", args.repo_url])
                if command(["git", "--git-dir", str(mirror), "remote", "get-url", "origin"]).decode().strip() != args.repo_url:
                    raise RuntimeError("Bare mirror remote does not match the authorized repository")
                command(["git", "--git-dir", str(mirror), "fetch", "--no-tags", "origin", "main"], timeout=300)
                if command(["git", "--git-dir", str(mirror), "rev-parse", "FETCH_HEAD"]).decode().strip() != sha:
                    raise Deferred("MAIN_MOVED_DURING_FETCH")
                release, value = prepare(mirror, home, shared, sha, ci)
                if args.activate:
                    latest = command(["git", "ls-remote", args.repo_url, "refs/heads/main"], timeout=30).decode().split()[0]
                    if latest != sha:
                        raise Deferred("MAIN_MOVED_DURING_PREPARATION")
                    ci = github_ci(sha)
                    result = receipt(home, "DEPLOYED", ci=ci, **activate(shared, home, release, value, args.public_base))
                else:
                    result = receipt(home, "PREPARED", sha=sha, release=str(release), ci=ci, deployed=False)
    except Deferred as error:
        result = receipt(home, "DEFERRED", reason=str(error), deployed=False)
    except Exception as error:
        result = receipt(home, "FAILED", sha=locals().get("sha"),
                         reason=type(error).__name__ + ": " + str(error), deployed=False)
        print(json.dumps(result, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
