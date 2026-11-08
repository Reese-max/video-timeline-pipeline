#!/usr/bin/env python3
"""Auto-deploy video-timeline-pipeline main on this VM (run as root by systemd).

Flow: main SHA changed -> GitHub "Video Timeline CI" push run succeeded -> offline
unittest in a separate candidate checkout -> drain (refuse new jobs, wait for
queued/running = 0) -> checkout + restart video-mcp -> verify local/public health
and tools -> undrain.  Any failure after the switch rolls back to the previous SHA.
Exit 0 = up to date / deployed / deferred, 1 = failed (rolled back or needs review).
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import http.client
import ipaddress
import json
import os
import pwd
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path("/home/ubuntu/video-timeline-pipeline")
WORK = Path("/home/ubuntu/.video-autodeploy")
CANDIDATE = WORK / "candidate"
TEST_VENV = WORK / "venv"
STATE_DIR = Path("/var/lib/video-mcp-autodeploy")
DROPIN = Path("/etc/systemd/system/video-mcp.service.d/release.conf")
SERVICE = "video-mcp"
LOCAL = "http://127.0.0.1:8787"
API = "https://api.github.com/repos/Reese-max/video-timeline-pipeline"
WORKFLOW = "Video Timeline CI"
DRAIN_FLAG = REPO / ".video-state" / "deployment-draining"
JOBS_DB = REPO / ".video-state" / "jobs.sqlite3"
UBUNTU = pwd.getpwnam("ubuntu")
DRAIN_TIMEOUT = int(os.getenv("AUTODEPLOY_DRAIN_SECONDS", "10800"))


class Deferred(Exception):
    """Not an error: try again on the next timer run."""


def log(message: str) -> None:
    print(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message, flush=True)


def run(cmd: list[str], *, cwd: Path | None = None, user: bool = True, timeout: int = 600,
        env: dict | None = None) -> str:
    if user:
        cmd = ["runuser", "-u", "ubuntu", "--", "env", "HOME=/home/ubuntu",
               *[f"{k}={v}" for k, v in (env or {}).items()], *cmd]
        env = None
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                            env={**os.environ, **env} if env else None)
    if result.returncode:
        tail = (result.stdout + result.stderr).strip().splitlines()[-15:]
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(cmd[-6:])}\n" + "\n".join(tail))
    return result.stdout.strip()


def git(*args: str, cwd: Path = REPO, timeout: int = 120) -> str:
    return run(["git", *args], cwd=cwd, timeout=timeout, env={"GIT_TERMINAL_PROMPT": "0"})


def retry_network(action, what: str, attempts: int = 4, delay: int = 30):
    """Ride out short network/DNS blips (e.g. unattended-upgrades restarting systemd-resolved
    at the moment the timer fires).  Only for read/fetch calls; a lasting failure still fails the run."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        # git exit != 0, urllib/socket errors, truncated or malformed HTTP responses
        except (RuntimeError, OSError, http.client.HTTPException, subprocess.TimeoutExpired) as error:
            if attempt == attempts:
                raise
            wait = delay * attempt
            first = (str(error).splitlines() or [repr(error)])[0]
            log(f"{what} failed (attempt {attempt}/{attempts}), retrying in {wait}s: {first[:200]}")
            time.sleep(wait)


def http_json(url: str, timeout: int = 15) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   "User-Agent": "video-mcp-autodeploy"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode())


def public_json(url: str, timeout: int = 20) -> dict:
    """GET url the way an outside client does.  MagicDNS on this VM maps the Funnel name to
    our own tailnet IP (never touching the public relay), so resolve via public DNS and pin it."""
    parts = urllib.parse.urlsplit(url)
    for server in ("8.8.8.8", "1.1.1.1"):
        dig = subprocess.run(["dig", "+short", "+time=5", "+tries=1", f"@{server}", parts.hostname, "A"],
                             capture_output=True, text=True)
        ips = []
        for token in dig.stdout.split() if dig.returncode == 0 else []:  # timeouts print ";; ..." here
            try:
                ips.append(str(ipaddress.IPv4Address(token)))
            except ValueError:
                pass  # CNAME targets
        if ips:
            break
    else:
        raise RuntimeError(f"no public A record for {parts.hostname}")
    curl = subprocess.run(["curl", "-fsS", "-m", str(timeout), "-H", "Accept: application/json",
                           "-A", "video-mcp-autodeploy",
                           "--resolve", f"{parts.hostname}:{parts.port or 443}:{ips[0]}", url],
                          capture_output=True, text=True)
    if curl.returncode:
        raise RuntimeError(f"curl via {ips[0]} failed ({curl.returncode}): {curl.stderr.strip()[:200]}")
    return json.loads(curl.stdout)


def mcp_tool_names(base: str) -> set[str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    def post(payload: dict) -> dict:
        request = urllib.request.Request(base + "/mcp", json.dumps(payload).encode(), headers)
        with urllib.request.urlopen(request, timeout=20) as response:
            if response.headers.get("Mcp-Session-Id"):
                headers["Mcp-Session-Id"] = response.headers["Mcp-Session-Id"]
            raw = response.read().decode()
        if raw.lstrip().startswith(("event:", "data:")):
            raw = "\n".join(line[5:].strip() for line in raw.splitlines() if line.startswith("data:"))
        result = json.loads(raw) if raw else {}
        if result.get("error"):
            raise RuntimeError("MCP error: " + json.dumps(result["error"])[:200])
        return result.get("result", {})

    post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "autodeploy", "version": "1"}}})
    post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    names = {tool["name"] for tool in post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).get("tools", [])}
    if not names:
        raise RuntimeError("MCP tools/list returned no tools")
    return names


def ci_passed(sha: str) -> bool:
    runs = http_json(f"{API}/actions/runs?head_sha={sha}&event=push&branch=main&per_page=50")
    runs = [r for r in runs.get("workflow_runs", []) if r.get("name") == WORKFLOW and r.get("head_sha") == sha]
    if not runs:
        return False
    latest = max(runs, key=lambda r: (r.get("run_number", 0), r.get("run_attempt", 0)))
    log(f"CI {WORKFLOW} run {latest.get('id')} attempt {latest.get('run_attempt')}: "
        f"{latest.get('status')}/{latest.get('conclusion')}")
    return latest.get("status") == "completed" and latest.get("conclusion") == "success"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def prepare_candidate(sha: str) -> None:
    run(["mkdir", "-p", str(WORK)])
    if not (CANDIDATE / ".git").exists():
        git("clone", "--quiet", "https://github.com/Reese-max/video-timeline-pipeline", str(CANDIDATE),
            cwd=WORK, timeout=300)
    retry_network(lambda: git("fetch", "--quiet", "origin", "main", cwd=CANDIDATE, timeout=300), "candidate git fetch")
    git("checkout", "--quiet", "--force", "--detach", sha, cwd=CANDIDATE)
    git("clean", "-qfdx", cwd=CANDIDATE)
    if git("rev-parse", "HEAD", cwd=CANDIDATE) != sha:
        raise RuntimeError("Candidate checkout SHA mismatch")
    if not (TEST_VENV / "bin" / "python").exists():
        run(["python3", "-m", "venv", str(TEST_VENV)], timeout=300)
    run([str(TEST_VENV / "bin" / "pip"), "install", "--quiet", "-r", "requirements.txt"],
        cwd=CANDIDATE, timeout=1200)
    with tempfile.TemporaryDirectory(prefix="video-offline-") as state:
        os.chown(state, UBUNTU.pw_uid, UBUNTU.pw_gid)
        log("running offline unittest suite in candidate")
        run([str(TEST_VENV / "bin" / "python"), "-X", "utf8", "-B", "-m", "unittest", "discover",
             "-s", "tests", "-p", "test_*.py"], cwd=CANDIDATE, timeout=3600,
            env={"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "MCP_ROOT": state})
    log("candidate tests passed")


def active_jobs() -> int:
    db = sqlite3.connect(JOBS_DB.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        return db.execute("SELECT count(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
    finally:
        db.close()


def set_drain_flag() -> None:
    with DRAIN_FLAG.open("x", encoding="utf-8") as stream:
        os.fchown(stream.fileno(), UBUNTU.pw_uid, UBUNTU.pw_gid)
        stream.write(json.dumps({"by": "video-mcp-autodeploy", "pid": os.getpid()}))


def drain() -> None:
    try:
        set_drain_flag()
    except FileExistsError:
        raise RuntimeError("Drain flag already exists; manual review required") from None
    try:
        wait_drained()
    except Exception:
        DRAIN_FLAG.unlink(missing_ok=True)  # nothing switched yet: just resume accepting jobs
        raise


def rss_active() -> int:
    """video-rss.service runs pipeline.py from this checkout. Its ExecCondition skips new runs once the
    drain flag exists, so only a run already in progress has to finish before the switch.
    A running oneshot reports "activating" (is-active exits 3), so read ActiveState; a missing unit is "inactive"."""
    result = subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", "video-rss.service"],
                            capture_output=True, text=True, timeout=30)
    state = result.stdout.strip()
    if result.returncode != 0 or state not in {"inactive", "failed", "activating", "active", "deactivating", "reloading"}:
        raise Deferred(f"cannot read video-rss state ({state or result.returncode}); retry next run")
    return int(state not in {"inactive", "failed"})


def wait_drained() -> None:
    deadline = time.time() + DRAIN_TIMEOUT
    while True:
        health = http_json(LOCAL + "/health")
        counts = health.get("jobs", {}).get("counts", {})
        busy = counts.get("queued", 0) + counts.get("running", 0) + active_jobs() + rss_active()
        if not busy and health.get("writer_busy") is False and health.get("deployment_draining") is True:
            log("drained: no queued/running jobs")
            return
        if time.time() > deadline:
            raise Deferred(f"still {busy} active jobs after {DRAIN_TIMEOUT}s; undrained, retry next run")
        time.sleep(30)


FREEZE = STATE_DIR / "rollback-freeze.txt"  # root-owned dir: no ubuntu-controlled symlinks


def switch_to(sha: str, *, restore_freeze: bool = False) -> None:
    """Check out sha and restart.  Forward switches snapshot the venv before changing it;
    rollbacks reinstall that exact snapshot so upgraded packages are pinned back."""
    pip = str(REPO / ".venv" / "bin" / "pip")
    before = file_hash(REPO / "requirements.txt")
    # No network here: deploy() fetched the target before draining, and a rollback returns to the
    # commit that was checked out before the switch.
    git("checkout", "--quiet", "--detach", sha)
    if restore_freeze and FREEZE.exists():
        log("restoring service venv from pre-deploy snapshot")
        run([pip, "install", "--quiet", "-r", str(FREEZE)], cwd=REPO, timeout=1200)
    elif not restore_freeze and file_hash(REPO / "requirements.txt") != before:
        log("requirements.txt changed; snapshotting then updating service venv")
        FREEZE.write_text(run([pip, "freeze", "--exclude-editable"], cwd=REPO), encoding="utf-8")
        run([pip, "install", "--quiet", "-r", "requirements.txt"], cwd=REPO, timeout=1200)
    DROPIN.parent.mkdir(parents=True, exist_ok=True)
    DROPIN.write_text(f"[Service]\nEnvironment=\"MCP_RELEASE_SHA={sha}\"\n", encoding="utf-8")
    run(["systemctl", "daemon-reload"], user=False)
    run(["systemctl", "restart", SERVICE], user=False)


def wait_health(sha: str, *, draining: bool | None, seconds: int = 90) -> dict:
    deadline, last = time.time() + seconds, "no response"
    while time.time() < deadline:
        try:
            health = http_json(LOCAL + "/health", timeout=5)
            jobs = health.get("jobs", {})
            ok = (health.get("status") == "ok" and health.get("service") == "video-timeline-pipeline"
                  and health.get("commit_sha") == sha and jobs.get("scheduler_alive") is True
                  and jobs.get("cleanup_failed") is False
                  and (draining is None or health.get("deployment_draining") is draining))
            if ok:
                return health
            last = json.dumps({k: health.get(k) for k in ("status", "commit_sha", "deployment_draining")})
        except Exception as error:  # service still starting
            last = repr(error)
        time.sleep(3)
    raise RuntimeError(f"health not ready for {sha[:7]}: {last}")


def verify_public(health: dict, sha: str) -> None:
    public = (health.get("public_base_url") or "").rstrip("/")
    if not public:
        raise RuntimeError("public_base_url missing from health")
    for attempt in range(5):
        try:
            remote = public_json(public + "/health", timeout=20)
            if remote.get("commit_sha") == sha:
                return
            last = f"public commit_sha={remote.get('commit_sha')}"
        except Exception as error:
            last = repr(error)
        time.sleep(5)
    raise RuntimeError("public endpoint check failed: " + last)


def write_status(**fields) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    (STATE_DIR / "last.json").write_text(json.dumps(
        {"time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}, indent=2), encoding="utf-8")


def deploy(target: str, current: str) -> None:
    old_health = http_json(LOCAL + "/health")
    old_tools = mcp_tool_names(LOCAL)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    FREEZE.unlink(missing_ok=True)
    # Fetch before draining so a network blip never stretches the window in which jobs are refused.
    retry_network(lambda: git("fetch", "--quiet", "origin", "main", timeout=300), "git fetch")
    drain()
    try:
        log(f"switching {current[:7]} -> {target[:7]}")
        switch_to(target)
        health = wait_health(target, draining=True)
        missing = old_tools - mcp_tool_names(LOCAL)
        if missing:
            raise RuntimeError("MCP tools disappeared: " + ", ".join(sorted(missing)))
        if os.getenv("AUTODEPLOY_TEST_FAIL_AFTER_SWITCH") == "1":
            raise RuntimeError("test hook: forced failure after switch")
        verify_public(health, target)
        DRAIN_FLAG.unlink(missing_ok=True)
        wait_health(target, draining=False, seconds=30)
    except Exception as error:
        log(f"deploy failed, rolling back to {current[:7]}: {error}")
        if not DRAIN_FLAG.exists():
            set_drain_flag()
        switch_to(current, restore_freeze=True)
        wait_health(current, draining=True)
        DRAIN_FLAG.unlink(missing_ok=True)
        wait_health(current, draining=False, seconds=30)
        verify_public(old_health, current)
        write_status(result="ROLLED_BACK", attempted=target, running=current, error=str(error)[:500])
        error.rolled_back = True
        raise
    write_status(result="DEPLOYED", running=target, previous=current)
    log(f"deployed {target[:7]} (previous {current[:7]})")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="CI gate + candidate tests only; no drain/restart")
    parser.add_argument("--force-sha", help="redeploy this SHA even if it equals the running one (testing)")
    args = parser.parse_args()
    with open("/run/video-mcp-autodeploy.lock", "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("another autodeploy run is active; skipping")
            return 0
        try:
            if git("status", "--porcelain"):  # tracked edits or non-ignored untracked files
                raise RuntimeError("service checkout has local modifications or untracked files; manual review required")
            if DRAIN_FLAG.exists():
                raise RuntimeError("drain flag present before run; manual review required")
            current = git("rev-parse", "HEAD")
            target = args.force_sha or retry_network(
                lambda: git("ls-remote", "origin", "refs/heads/main"), "git ls-remote").split()[0]
            log(f"running {current[:7]}, main {target[:7]}")
            if target == current and not args.force_sha:
                write_status(result="UP_TO_DATE", running=current)
                log("up to date")
                return 0
            if not retry_network(lambda: ci_passed(target), "CI status query"):
                raise Deferred(f"CI for {target[:7]} not successful yet")
            prepare_candidate(target)
            if args.check:
                write_status(result="CHECK_PASSED", running=current, candidate=target)
                return 0
            deploy(target, current)
            return 0
        except Deferred as reason:
            log(f"deferred: {reason}")
            write_status(result="DEFERRED", reason=str(reason))
            return 0
        except Exception as error:
            log(f"FAILED: {error}")
            if not getattr(error, "rolled_back", False):
                write_status(result="FAILED", error=str(error)[:500])
            return 1


if __name__ == "__main__":
    sys.exit(main())
