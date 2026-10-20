#!/usr/bin/env bash
# VIDEO_GITOPS_PROTOCOL=1: one existing supervisor, stable data and atomic release pointer.
set -eu
group="${1:-d1}"
shared="${MCP_ROOT:-${MCP_BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}}"
shared="$(readlink -f "$shared")"
stable="${shared}/scripts/start-mcp-supervised.sh"
log="${MCP_LOG_FILE:-${shared}/mcp-server.log}"
pidfile="${MCP_PID_FILE:-${shared}/mcp-server.pid}"
exec 9>"${MCP_LOCK_FILE:-${shared}/mcp-supervisor.lock}"
flock -w 45 9 || exit 1

deploy="${MCP_DEPLOY_HOME:-}"
if [ -z "$deploy" ] && [ -r "${shared}/.runtime/gitops-home" ]; then
    deploy="$(cat "${shared}/.runtime/gitops-home")"
fi
base="$shared"
if [ -n "$deploy" ]; then
    [ -L "${deploy}/current" ] || { echo 'Missing release pointer; startup refused.' >&2; exit 1; }
    base="$(readlink -f "${deploy}/current")"
    case "$base" in "$shared"|"${deploy}/releases/"*) ;; *) echo 'Unsafe release pointer.' >&2; exit 1;; esac
fi
python="${base}/.venv/bin/python"
[ -x "$python" ] && [ -r "${base}/mcp_server.py" ] || exit 1

read_public_env() {
    # Same whitespace/quote rules as pipeline.load_dotenv; never execute .env.
    python3 - "${shared}/.env" "$1" <<'PY'
from pathlib import Path
import sys
path, wanted = Path(sys.argv[1]), sys.argv[2]
value = ""
for line in path.read_text().splitlines() if path.is_file() else []:
    if "=" in line and line.split("=", 1)[0].strip() == wanted:
        candidate = line.split("=", 1)[1].strip().strip("\"'")
        if candidate: value = candidate
print(value)
PY
}
public_base="${MCP_PUBLIC_BASE_URL:-$(read_public_env MCP_PUBLIC_BASE_URL)}"
media_launcher="${shared}/scripts/start-mcp-media-tunnel-supervised.sh"
media_url_file="${MCP_MEDIA_URL_FILE:-/tmp/video-timeline-media-url}"
if [ -z "${MCP_MEDIA_BASE_URL:-}" ] && [ -r "${shared}/.env" ]; then
    MCP_MEDIA_BASE_URL="$(read_public_env MCP_MEDIA_BASE_URL)"
fi
media_base="${MCP_MEDIA_BASE_URL:-}"
# Keep stable media by default; quick tunnels remain explicit opt-in.
if [ -z "${media_base}" ] && [ "${MCP_USE_QUICK_TUNNEL:-0}" != "1" ]; then
    media_base="${public_base}"
fi
if [ -z "${media_base}" ] && [ -x "${media_launcher}" ]; then
    if ! "${media_launcher}" "${group}" >/dev/null 2>&1; then
        printf 'Media tunnel unavailable; ignoring its stale URL. Drive handoff remains available.\n' >&2
        media_base="${public_base}"
    fi
fi
if [ -z "${media_base}" ] && [ -r "${media_url_file}" ]; then
    media_base="$(tr -d '\r\n' <"${media_url_file}")"
fi
media_base="${media_base:-${public_base}}"

process_matches() {
    local pid="$1" cmd cwd
    kill -0 "$pid" 2>/dev/null || return 1
    cmd="$(tr '\0' ' ' <"/proc/${pid}/cmdline" 2>/dev/null || true)"
    cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
    case "$cmd" in
        *"${base}/mcp_server.py"*) [ "$cwd" = "$base" ] ;;
        *"${stable}"*"--run-server"*) return 0 ;;
        *) return 1 ;;
    esac
}
pid=""
if [ -r "$pidfile" ]; then
    candidate="$(cat "$pidfile")"
    if [[ "$candidate" =~ ^[0-9]+$ ]] && [ "$candidate" != "$$" ] && process_matches "$candidate"; then pid="$candidate"; fi
fi

if [ "${2:-}" = '--run-server' ]; then
    [ -z "$pid" ] || exit 0
    [ -n "$public_base" ] || { echo 'Resolve the current MCP public URL before startup.' >&2; exit 1; }
    sha="$(basename "$base")"
    if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then sha="$(git -C "$base" rev-parse HEAD)"; fi
    printf '%s\n' "$$" >"$pidfile"
    cd "$base"
    exec 9>&-
    exec env MCP_ROOT="$shared" MCP_RELEASE_SHA="$sha" MCP_PUBLIC_BASE_URL="$public_base" MCP_MEDIA_BASE_URL="$media_base" \
        MCP_HOST="${MCP_HOST:-127.0.0.1}" MCP_PORT="${MCP_PORT:-8787}" "$python" "${base}/mcp_server.py"
fi

[ -r /usr/local/bin/sand-desktop-supervise.sh ] || exit 1
# shellcheck source=/dev/null
. /usr/local/bin/sand-desktop-supervise.sh
if [ -n "${pid}" ]; then
    running_media="$(tr '\0' '\n' </proc/${pid}/environ | sed -n 's/^MCP_MEDIA_BASE_URL=//p')"
    if [ -n "$running_media" ] && [ "$running_media" != "$media_base" ]; then
        printf 'Media URL changed; controlled reload and ChatGPT review required. Running jobs were not interrupted.\n' >&2
        exit 1
    fi
fi
command=(/bin/bash "$stable" "$group" --run-server)
if [ -z "$pid" ]; then
    nohup "${command[@]}" 9>&- </dev/null >>"$log" 2>&1 &
    pid="$!"
    printf '%s\n' "$pid" >"$pidfile"
fi
sand_desktop_register "$group" video-timeline-mcp 6 "$log" "$pid" "$pidfile" -- "${command[@]}"
