#!/usr/bin/env bash
set -u

group="${1:-d1}"
mode="${2:-}"
base="${MCP_BASE_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
log="${MCP_MEDIA_TUNNEL_LOG_FILE:-${base}/mcp-media-tunnel.log}"
pidfile="${MCP_MEDIA_TUNNEL_PID_FILE:-${base}/mcp-media-tunnel.pid}"
lockfile="${MCP_MEDIA_TUNNEL_LOCK_FILE:-${base}/mcp-media-tunnel.lock}"
urlfile="${MCP_MEDIA_URL_FILE:-/tmp/video-timeline-media-url}"
target="${MCP_MEDIA_TUNNEL_TARGET:-http://${MCP_HOST:-127.0.0.1}:${MCP_PORT:-8787}}"
cloudflared="${CLOUDFLARED_BIN:-$(command -v cloudflared 2>/dev/null || true)}"
script_path="$(readlink -f "${BASH_SOURCE[0]}")"

if [ "${mode}" = "--worker" ]; then
	[ -x "${cloudflared}" ] || exit 1
	tunnel_pid=""
	trap 'if [ -n "${tunnel_pid}" ]; then kill "${tunnel_pid}" 2>/dev/null || true; fi; rm -f "${urlfile}"; exit 0' INT TERM
	while :; do
		: >"${log}"
		"${cloudflared}" tunnel --url "${target}" --no-autoupdate >>"${log}" 2>&1 &
		tunnel_pid="$!"
		url=""
		for _ in $(seq 1 60); do
			if ! kill -0 "${tunnel_pid}" 2>/dev/null; then
				break
			fi
			url="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "${log}" | tail -n 1 || true)"
			if [ -n "${url}" ]; then
				printf '%s\n' "${url}" >"${urlfile}.tmp"
				mv -f "${urlfile}.tmp" "${urlfile}"
				break
			fi
			sleep 1
		done
		wait "${tunnel_pid}" || true
		tunnel_pid=""
		rm -f "${urlfile}"
		sleep 2
	done
fi

[ -n "${cloudflared}" ] || exit 1
[ -r /usr/local/bin/sand-desktop-supervise.sh ] || exit 1
# shellcheck source=/dev/null
. /usr/local/bin/sand-desktop-supervise.sh

exec 9>"${lockfile}"
flock -n 9 || exit 0

process_matches() {
	local pid="$1" cmdline="" cwd=""
	[ -n "${pid}" ] || return 1
	kill -0 "${pid}" >/dev/null 2>&1 || return 1
	cmdline="$(tr '\0' ' ' </proc/${pid}/cmdline 2>/dev/null || true)"
	cwd="$(readlink -f "/proc/${pid}/cwd" 2>/dev/null || true)"
	case "${cmdline}" in
		*"${script_path}"*"--worker"*) [ "${cwd}" = "${base}" ] && return 0 ;;
		*) return 1 ;;
	esac
}

registered="$(find /tmp/sand-desktop -path '*/video-timeline-media-tunnel.json' -print -quit 2>/dev/null || true)"
pid=""
if [ -r "${pidfile}" ]; then
	candidate="$(tr -dc '0-9' <"${pidfile}" 2>/dev/null || true)"
	if process_matches "${candidate}"; then
		pid="${candidate}"
	fi
fi

printf -v worker_script 'cd %q && exec /bin/bash %q %q --worker' \
	"${base}" "${script_path}" "${group}"
run_command=(/bin/bash -lc "${worker_script}")

if [ -z "${pid}" ]; then
	nohup "${run_command[@]}" </dev/null >>"${log}" 2>&1 &
	pid="$!"
	printf '%s\n' "${pid}" >"${pidfile}"
fi

if [ -z "${registered}" ] || [ ! -e "${registered}" ]; then
	sand_desktop_register "${group}" video-timeline-media-tunnel 5 "${log}" "${pid}" "${pidfile}" \
		-- "${run_command[@]}"
fi

for _ in $(seq 1 60); do
	if [ -s "${urlfile}" ] && grep -Eq '^https://[a-z0-9-]+\.trycloudflare\.com$' "${urlfile}"; then
		exit 0
	fi
	sleep 1
done
exit 1
