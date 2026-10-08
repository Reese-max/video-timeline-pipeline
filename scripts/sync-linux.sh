#!/usr/bin/env bash
# Reuse sand-desktop supervision; one pull-based worker, no second MCP service.
set -eu
shared="${MCP_BASE_DIR:-/home/box/video-timeline-pipeline}"
home="${MCP_DEPLOY_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
shared="$(readlink -f "$shared")"
home="$(readlink -f "$home")"
stable="${home}/sync-linux.sh"
helper="${home}/deploy_linux.py"
umask 077
mkdir -p "$home"

once() {
    local host public
    host="$(timeout 15 /usr/bin/tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')" || return 1
    [[ "$host" =~ ^[a-zA-Z0-9.-]+\.ts\.net$ ]] || return 1
    public="https://${host}/video-timeline"
    # Independent health receipt still runs when CI is pending or failed.
    python3 - "$helper" "$shared" "$home" "$public" <<'PY'
import importlib.util, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("deployment", sys.argv[1])
d = importlib.util.module_from_spec(spec); spec.loader.exec_module(d)
shared, home, public = Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4]
result = {"time_utc": d.time.strftime("%Y-%m-%dT%H:%M:%SZ", d.time.gmtime()), "public_base": public}
try:
    local = d.http_json("http://127.0.0.1:8787/health")
    code = (home / "current").resolve()
    expected = local.get("runtime_sha256", {})
    if not expected or any(name not in d.RUNTIME_FILES for name in expected) or d.hashes(code, expected) != expected:
        raise RuntimeError("Runtime source inventory mismatch")
    d.verify_runtime("http://127.0.0.1:8787", expected, local.get("commit_sha"))
    d.verify_runtime(public, expected, local.get("commit_sha"))
    result.update(status="HEALTH_OK", commit_sha=local.get("commit_sha"), runtime_sha256=expected)
except Exception as error:
    result.update(status="HEALTH_FAILED", reason=type(error).__name__)
d.write_json(home / "last-health.json", result)
PY
    python3 -B "$helper" --shared-root "$shared" --deploy-home "$home" --activate --public-base "$public"
}

case "${1:-}" in
    --once) once ;;
    --run-loop)
        exec 8>"${home}/loop.lock"
        flock -n 8 || exit 0
        while true; do
            once || printf '%s Sync failed; retry in 300 seconds.\n' "$(date -Iseconds)" >&2
            sleep 300
        done
        ;;
    '')
        exec 9>"${home}/loop-start.lock"
        flock -w 15 9 || exit 1
        . /usr/local/bin/sand-desktop-supervise.sh
        pidfile="${home}/sync.pid"
        pid="$(cat "$pidfile" 2>/dev/null || true)"
        if ! [[ "$pid" =~ ^[0-9]+$ ]] || ! kill -0 "$pid" 2>/dev/null \
            || ! tr '\0' '\n' <"/proc/${pid}/cmdline" | grep -Fxq -- "$stable"; then
            nohup /bin/bash "$stable" --run-loop 9>&- </dev/null >>"${home}/sync.log" 2>&1 &
            pid="$!"
            printf '%s\n' "$pid" >"$pidfile"
        fi
        sand_desktop_register d1 video-timeline-git-sync 7 "${home}/sync.log" "$pid" "$pidfile" -- /usr/bin/env "MCP_BASE_DIR=$shared" "MCP_DEPLOY_HOME=$home" /bin/bash "$stable" --run-loop
        ;;
    *) echo 'Usage: sync-linux.sh [--once|--run-loop]' >&2; exit 2 ;;
esac
