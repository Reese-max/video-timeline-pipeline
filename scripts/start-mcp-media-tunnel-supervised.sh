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
startup_attempts="${MCP_MEDIA_STARTUP_ATTEMPTS:-300}"
case "${startup_attempts}" in ''|*[!0-9]*) exit 1 ;; esac
[ "${startup_attempts}" -ge 1 ] && [ "${startup_attempts}" -le 900 ] || exit 1
script_path="$(readlink -f "${BASH_SOURCE[0]}")"
probe() {
	curl --silent --output /dev/null --write-out '%{http_code}' \
		--connect-timeout 5 --max-time 10 "${1}/health"
}

if [ "${mode}" = "--worker" ]; then
	[ -x "${cloudflared}" ] || exit 1
	command -v curl >/dev/null || exit 1
	tunnel_pid=""
	stop_tunnel() {
		if [ -n "${tunnel_pid}" ]; then
			kill "${tunnel_pid}" 2>/dev/null || true
			wait "${tunnel_pid}" 2>/dev/null || true
			tunnel_pid=""
		fi
		rm -f "${urlfile}"
	}
	trap 'stop_tunnel; exit 0' INT TERM
	while :; do
		rm -f "${urlfile}"
		: >"${log}"
		"${cloudflared}" tunnel --url "${target}" --no-autoupdate >>"${log}" 2>&1 &
		tunnel_pid="$!"
		url=""
		# New quick-tunnel names can remain negatively cached at the host resolver.
		# Allow propagation before replacing a newly registered, otherwise healthy tunnel.
		for _ in $(seq 1 "${startup_attempts}"); do
			if ! kill -0 "${tunnel_pid}" 2>/dev/null; then
				break
			fi
			url="$(grep -Eo 'https://[a-z0-9-]+\.trycloudflare\.com' "${log}" | tail -n 1 || true)"
			# The origin may not be running yet. An HTTP response proves DNS/TLS,
			# not media delivery; the steady-state check below also requires /health.
			if [ -n "${url}" ] && probe "${url}" >/dev/null; then
				printf '%s\n' "${url}" >"${urlfile}.tmp"
				mv -f "${urlfile}.tmp" "${urlfile}"
				break
			fi
			sleep 1
		done
		failures=0
		while [ -s "${urlfile}" ] && kill -0 "${tunnel_pid}" 2>/dev/null; do
			sleep 30
			status="$(probe "${url}")" || status="000"
			local_status="$(probe "${target}")" || local_status="000"
			if [ "${status}" != "000" ] && { [ "${status}" = "200" ] || [ "${local_status}" != "200" ] || [ "${status}" = "429" ]; }; then
				failures=0
			else
				failures=$((failures + 1))
				printf 'Media health failed: public=%s origin=%s attempt=%s/3\n' "${status}" "${local_status}" "${failures}" >>"${log}"
			fi
			[ "${failures}" -lt 3 ] || break
		done
		stop_tunnel
		# ponytail: quick tunnels remain development-only; use a named tunnel for a stable URL/SLA.
		sleep 30
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
	nohup "${run_command[@]}" 9>&- </dev/null >>"${log}" 2>&1 &
	pid="$!"
	printf '%s\n' "${pid}" >"${pidfile}"
fi

if [ -z "${registered}" ] || [ ! -e "${registered}" ]; then
	sand_desktop_register "${group}" video-timeline-media-tunnel 5 "${log}" "${pid}" "${pidfile}" \
		-- "${run_command[@]}"
fi

for _ in $(seq 1 60); do
	if [ -s "${urlfile}" ] && grep -Eq '^https://[a-z0-9-]+\.trycloudflare\.com$' "${urlfile}" && probe "$(tr -d '\r\n' <"${urlfile}")" >/dev/null; then
		exit 0
	fi
	sleep 1
done
exit 1
