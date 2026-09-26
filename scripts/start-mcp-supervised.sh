#!/usr/bin/env bash
set -u

group="${1:-d1}"
base="${MCP_BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
log="${MCP_LOG_FILE:-${base}/mcp-server.log}"
pidfile="${MCP_PID_FILE:-${base}/mcp-server.pid}"
lockfile="${MCP_LOCK_FILE:-${base}/mcp-supervisor.lock}"
public_base="${MCP_PUBLIC_BASE_URL:-https://cursor-grokbot-1.tail930e77.ts.net/video-timeline}"
media_url_file="${MCP_MEDIA_URL_FILE:-/tmp/video-timeline-media-url}"
host="${MCP_HOST:-127.0.0.1}"
port="${MCP_PORT:-8787}"
python="${base}/.venv/bin/python"
server="${base}/mcp_server.py"
media_launcher="${base}/scripts/start-mcp-media-tunnel-supervised.sh"

[ -x "${python}" ] || exit 1
[ -r "${server}" ] || exit 1

if [ -r /usr/local/bin/sand-desktop-supervise.sh ]; then
	# shellcheck source=/dev/null
	. /usr/local/bin/sand-desktop-supervise.sh
else
	exit 1
fi

media_base="${MCP_MEDIA_BASE_URL:-}"
if [ -z "${media_base}" ] && [ -x "${media_launcher}" ]; then
	if ! "${media_launcher}" "${group}" >/dev/null 2>&1; then
		printf 'Media tunnel unavailable; ignoring its stale URL. Starting MCP with its public origin so Drive handoff remains available.\n' >&2
		media_base="${public_base}"
	fi
fi
if [ -z "${media_base}" ] && [ -r "${media_url_file}" ]; then
	media_base="$(tr -d '\r\n' <"${media_url_file}" 2>/dev/null || true)"
fi
media_base="${media_base:-${public_base}}"

process_matches() {
	local pid="$1"
	local cmdline=""
	local cwd=""
	[ -n "${pid}" ] || return 1
	kill -0 "${pid}" >/dev/null 2>&1 || return 1
	cmdline="$(tr '\0' ' ' </proc/${pid}/cmdline 2>/dev/null || true)"
	cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
	case "${cmdline}" in
		*"mcp_server.py"*) [ "${cwd}" = "${base}" ] && return 0 ;;
		*) return 1 ;;
	esac
}

exec 9>"${lockfile}"
flock -n 9 || exit 0

pid=""
if [ -r "${pidfile}" ]; then
	candidate="$(tr -dc '0-9' <"${pidfile}" 2>/dev/null || true)"
	if process_matches "${candidate}"; then
		pid="${candidate}"
	fi
fi

if [ -n "${pid}" ]; then
	running_media="$(tr '\0' '\n' </proc/${pid}/environ | sed -n 's/^MCP_MEDIA_BASE_URL=//p')"
	if [ "${running_media}" != "${media_base}" ]; then
		printf 'Media URL changed: reload this MCP, refresh ChatGPT actions, and prepare a new widget. Running jobs were not interrupted.\n' >&2
		exit 1
	fi
fi

printf -v run_script 'cd %q && exec env MCP_PUBLIC_BASE_URL=%q MCP_MEDIA_BASE_URL=%q MCP_HOST=%q MCP_PORT=%q %q %q' \
	"${base}" "${public_base}" "${media_base}" "${host}" "${port}" "${python}" "${server}"
run_command=(/bin/bash -lc "${run_script}")

if [ -z "${pid}" ]; then
	nohup "${run_command[@]}" 9>&- </dev/null >>"${log}" 2>&1 &
	pid="$!"
	printf '%s\n' "${pid}" >"${pidfile}"
fi

sand_desktop_register "${group}" video-timeline-mcp 6 "${log}" "${pid}" "${pidfile}" \
	-- "${run_command[@]}"
