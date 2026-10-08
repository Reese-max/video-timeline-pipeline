#!/usr/bin/env bash
# Called by the existing sandbox boot entry after successful file hydration.
set -eu
shared="${MCP_BASE_DIR:-/home/box/video-timeline-pipeline}"
[ -r "${shared}/.runtime/gitops-home" ] || exit 0
home="$(cat "${shared}/.runtime/gitops-home")"
export MCP_BASE_DIR="$shared" MCP_DEPLOY_HOME="$home"
[ -x "${home}/sync-linux.sh" ] || exit 1
/bin/bash "${shared}/scripts/start-mcp-supervised.sh" d1
/bin/bash "${home}/sync-linux.sh"
