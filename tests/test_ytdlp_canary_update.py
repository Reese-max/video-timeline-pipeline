import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

import pytest
import pipeline

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ytdlp_canary_update.sh"
BASH = str(Path("C:/Program Files/Git/bin/bash.exe")) if os.name == "nt" else shutil.which("bash")

FAKE_PYTHON = r'''#!/usr/bin/env bash
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$1" == - ]]; then
    cat >/dev/null
    [[ "$4" == validate ]] && exit 0
    printf 'canary:%s\n' "$(cat "$root/version")" >>"$FAKE_LOG"
    if [[ "$root" == */candidate && "$FAKE_SCENARIO" == candidatefail ]]; then exit 3; fi
    if [[ "$root" != */candidate && "$(cat "$root/version")" == 2099.01.01 && "$FAKE_SCENARIO" != success ]]; then exit 3; fi
    exit 0
fi
case "$2" in
    venv)
        mkdir -p "$3/bin"
        cp "$0" "$3/bin/python"
        printf '2099.01.01\n' >"$3/version" ;;
    yt_dlp) cat "$root/version" ;;
    pip)
        printf 'pip:%s:%s\n' "$root" "$*" >>"$FAKE_LOG"
        if [[ "$3" == install && "$root" != */candidate ]]; then
            value="${!#}"
            value="${value#yt-dlp==}"
            if [[ "$FAKE_SCENARIO" == rollbackfail && "$value" == 2026.01.01 ]]; then exit 1; fi
            printf '%s\n' "$value" >"$root/version"
            [[ "$FAKE_SCENARIO" != installfail || "$value" != 2099.01.01 ]] || exit 1
        fi ;;
    *) exit 99 ;;
esac
'''


@pytest.mark.skipif(not BASH or os.name == "nt", reason="bash -n 需可用的 Linux Bash")
def test_bash_syntax():
    result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not BASH or os.name == "nt", reason="Bash 交易整合測試需 Linux（本 Windows 沙箱禁止啟動 Bash）")
@pytest.mark.parametrize("scenario,dry,expected,version", [
    ("success", False, 0, "2099.01.01"), ("success", True, 0, "2026.01.01"),
    ("candidatefail", False, 3, "2026.01.01"), ("postfail", False, 4, "2026.01.01"),
    ("installfail", False, 4, "2026.01.01"), ("rollbackfail", False, 5, "2099.01.01")])
def test_updater_transaction(tmp_path, scenario, dry, expected, version):
    app = tmp_path / "app"
    (app / "bin").mkdir(parents=True)
    (app / "bin/python").write_text(FAKE_PYTHON, encoding="utf-8", newline="\n")
    (app / "version").write_text("2026.01.01\n", encoding="utf-8", newline="\n")
    (app / "pyvenv.cfg").write_text("home = fake\n", encoding="utf-8", newline="\n")
    urls = tmp_path / "urls.txt"
    urls.write_text("https://youtu.be/example\n", encoding="utf-8")
    before = {str(p.relative_to(app)): p.read_bytes() for p in app.rglob("*") if p.is_file()}
    env = {**os.environ, "FAKE_SCENARIO": scenario, "FAKE_LOG": str(tmp_path / "calls.log"),
           "TMPDIR": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path)}
    command = [BASH, "-c", 'chmod +x "$1/bin/python"; bash "$2" --venv "$1" --urls "$3" "${@:4}"',
               "test", str(app), str(SCRIPT), str(urls)]
    if dry:
        command.append("--dry-run")
    result = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
    assert result.returncode == expected, result.stdout + result.stderr
    assert (app / "version").read_text().strip() == version
    if dry or scenario == "candidatefail":
        assert before == {str(p.relative_to(app)): p.read_bytes() for p in app.rglob("*") if p.is_file()}
    log = (tmp_path / "calls.log").read_text()
    assert "canary:2099.01.01" in log
    if expected == 4:
        assert "--no-index" in log and "yt-dlp==2026.01.01" in log


def canary_source():
    return SCRIPT.read_text(encoding="utf-8").split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


def test_canary_commands_use_app_options_and_never_download(tmp_path):
    urls = tmp_path / "urls.txt"
    urls.write_text("# comment\nhttps://youtu.be/example # inline\nhttps://www.facebook.com/reel/123\n", encoding="utf-8")
    with patch.object(sys, "argv", ["-", str(SCRIPT.parents[1]), str(urls), "run"]), \
            patch.object(pipeline, "load_dotenv"), \
            patch.dict(os.environ, {"YTDLP_YOUTUBE_PROXY": "http://proxy.test:8080", "YTDLP_MAX_HEIGHT": "720"}), \
            patch.object(subprocess, "run") as run:
        run.return_value.returncode = 0
        exec(compile(canary_source(), str(SCRIPT), "exec"), {})
    youtube, facebook = [call.args[0] for call in run.call_args_list]
    assert youtube[youtube.index("--proxy") + 1] == "http://proxy.test:8080"
    assert "--proxy" not in facebook
    assert facebook[facebook.index("--impersonate") + 1] == "chrome"
    for command in (youtube, facebook):
        assert "--simulate" in command and "--no-cookies" in command
        assert command[command.index("-S") + 1] == "res:720"


@pytest.mark.parametrize("content", ["# empty\n", "https://www.facebook.com/watch/?v=REPLACE_WITH_PUBLIC_VIDEO_ID\n", "file:///private\n"])
def test_canary_input_refuses_incomplete_plan(tmp_path, content):
    urls = tmp_path / "urls.txt"
    urls.write_text(content, encoding="utf-8")
    with patch.object(sys, "argv", ["-", str(SCRIPT.parents[1]), str(urls), "validate"]), \
            patch.object(pipeline, "load_dotenv"), patch.object(subprocess, "run") as run:
        with pytest.raises(SystemExit) as error:
            exec(compile(canary_source(), str(SCRIPT), "exec"), {})
    assert error.value.code == 2
    run.assert_not_called()
