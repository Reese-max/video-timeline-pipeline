#!/usr/bin/env bash
set -euo pipefail
umask 077
export PYTHONDONTWRITEBYTECODE=1

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
venv="${YTDLP_APP_VENV:-$repo/.venv}"
urls="${YTDLP_CANARY_URLS:-$repo/scripts/ytdlp-canary-urls.txt}"
dry_run=0
pre=()
while (($#)); do
    case "$1" in
        --venv|--urls)
            (($# >= 2)) || { printf '缺少參數：%s\n' "$1" >&2; exit 2; }
            if [[ "$1" == --venv ]]; then venv="$2"; else urls="$2"; fi
            shift 2 ;;
        --nightly) pre=(--pre); shift ;;
        --dry-run) dry_run=1; shift ;;
        --help) printf '用法：bash scripts/ytdlp_canary_update.sh [--venv 路徑] [--urls 檔案] [--nightly] [--dry-run]\n'; exit 0 ;;
        *) printf '未知參數：%s\n' "$1" >&2; exit 2 ;;
    esac
done
app="$venv/bin/python"
[[ -x "$app" && -f "$venv/pyvenv.cfg" && -r "$urls" ]] || {
    printf '應用程式 venv 或 canary 網址檔不存在。\n' >&2; exit 2;
}

# Linux flock also serializes manual runs against the timer; dry-run never writes the app venv.
lock_root="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}"
exec 9>"$lock_root/video-timeline-ytdlp-${UID}.lock"
flock -n 9 || { printf '另一個 yt-dlp canary 更新正在執行。\n' >&2; exit 2; }
work="$(mktemp -d)"
armed=0
previous=""
cleanup() {
    local result=$?
    trap - EXIT
    if ((armed)); then
        printf '升級失敗，正在回復 yt-dlp %s。\n' "$previous" >&2
        if "$app" -m pip install --disable-pip-version-check --no-index --find-links "$work/wheels" \
                --no-deps --force-reinstall "yt-dlp==$previous" \
                && [[ "$("$app" -m yt_dlp --version)" == "$previous" ]]; then
            printf '已回復 yt-dlp %s。\n' "$previous" >&2
            result=4
        else
            printf '回復失敗，需人工修復應用程式 venv。\n' >&2
            result=5
        fi
    fi
    rm -rf -- "$work"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 4' INT TERM

canaries() {
    "$1" - "$repo" "$urls" "${2:-run}" <<'PY'
import os
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, sys.argv[1])
import pipeline
pipeline.load_dotenv(Path(sys.argv[1]) / ".env")
urls = [line.split("#", 1)[0].strip() for line in Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()]
urls = [url for url in urls if url]
if not urls or any("REPLACE_" in url for url in urls):
    print("請先替換 Facebook canary 佔位網址；網址清單不得為空。", file=sys.stderr)
    sys.exit(2)
if any(not url.startswith(("https://", "http://")) for url in urls):
    print("Canary 網址必須使用 HTTP(S)。", file=sys.stderr)
    sys.exit(2)
if sys.argv[3] == "validate":
    sys.exit(0)
for index, url in enumerate(urls, 1):
    command = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-playlist",
               "--no-cookies", "--no-cookies-from-browser", "--simulate", "--quiet", "--no-warnings",
               "--socket-timeout", "15", "--retries", "1", "--extractor-retries", "1"]
    command += pipeline.ytdlp_resolution_options() + pipeline.youtube_proxy_options(url)
    if pipeline.is_facebook_url(url):
        command += ["--impersonate", "chrome"]
    # Do not echo extractor stderr: errors can expose signed URLs or proxy credentials.
    try:
        result = subprocess.run(command + ["--", url], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        print(f"Canary {index} 執行失敗或逾時。", file=sys.stderr)
        sys.exit(3)
    if result.returncode:
        print(f"Canary {index} 失敗（結束碼 {result.returncode}）。", file=sys.stderr)
        sys.exit(3)
    print(f"Canary {index} 通過（僅模擬，未下載）。")
PY
}

canaries "$app" validate
previous="$("$app" -m yt_dlp --version)"
printf '更新前 yt-dlp：%s\n' "$previous"
"$app" -m venv "$work/candidate"
candidate="$work/candidate/bin/python"
"$candidate" -m pip install --disable-pip-version-check --upgrade "${pre[@]}" 'yt-dlp[default,curl-cffi]'
version="$("$candidate" -m yt_dlp --version)"
printf '候選 yt-dlp：%s\n' "$version"
canaries "$candidate" || { result=$?; exit "$result"; }
# Cache both wheels before mutation so rollback needs no network. Dependencies stay unchanged.
"$candidate" -m pip download --disable-pip-version-check --no-deps --dest "$work/wheels" "yt-dlp==$previous"
"$candidate" -m pip download --disable-pip-version-check --no-deps --dest "$work/wheels" "yt-dlp==$version"
if ((dry_run)); then
    printf '預覽通過；應用程式 yt-dlp 維持 %s，未寫入應用程式 venv。\n' "$previous"
    exit 0
fi
armed=1
"$app" -m pip install --disable-pip-version-check --no-index --find-links "$work/wheels" \
    --no-deps --force-reinstall "yt-dlp==$version"
[[ "$("$app" -m yt_dlp --version)" == "$version" ]] || exit 4
canaries "$app" || exit 4
armed=0
printf '更新後 yt-dlp：%s（應用程式 canary 全部通過）。\n' "$version"
