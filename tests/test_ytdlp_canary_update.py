import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pipeline

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ytdlp_canary_update.sh"
BASH = str(Path("C:/Program Files/Git/bin/bash.exe")) if os.name == "nt" else shutil.which("bash")

FAKE_PYTHON = r'''#!/usr/bin/env bash
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
if [[ "$1" == - ]]; then
    # Execute the real heredoc; only the app has app_dependency. Never load real .env.
    PYTHONPATH="$root${PYTHONPATH:+:$PYTHONPATH}" exec "$REAL_PYTHON" -c \
        'import app_dependency, pipeline, sys; pipeline.load_dotenv = lambda path: None; exec(sys.stdin.read())' "${@:2}"
fi
case "$2" in
    venv)
        mkdir -p "$3/bin"
        cp "$0" "$3/bin/python"
        printf '2099.01.01\n' >"$3/version" ;;
    yt_dlp)
        if [[ "$3" == --version ]]; then cat "$root/version"; exit 0; fi
        printf 'canary:%s\n' "$(cat "$root/version")" >>"$FAKE_LOG"
        if [[ "$root" == */candidate && "$FAKE_SCENARIO" == candidatefail ]]; then exit 3; fi
        if [[ "$root" != */candidate && "$(cat "$root/version")" == 2099.01.01 && "$FAKE_SCENARIO" != success ]]; then exit 3; fi ;;
    pip)
        printf 'pip:%s:%s\n' "$root" "$*" >>"$FAKE_LOG"
        if [[ "$3" == install && "$root" == */candidate && "$FAKE_SCENARIO" == preinterrupt ]]; then
            kill -TERM "$PPID"; exit 0
        fi
        if [[ "$3" == install && "$root" != */candidate ]]; then
            value="${!#}"
            value="${value#yt-dlp==}"
            if [[ "$FAKE_SCENARIO" == rollbackfail && "$value" == 2026.01.01 ]]; then exit 1; fi
            printf '%s\n' "$value" >"$root/version"
            if [[ "$FAKE_SCENARIO" == postinterrupt && "$value" == 2099.01.01 ]]; then
                kill -TERM "$PPID"; exit 0
            fi
            [[ "$FAKE_SCENARIO" != installfail || "$value" != 2099.01.01 ]] || exit 1
        fi ;;
    *) exit 99 ;;
esac
'''


def canary_source():
    return SCRIPT.read_text(encoding="utf-8").split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]


class YtdlpCanaryUpdateTests(unittest.TestCase):
    @unittest.skipIf(not BASH or os.name == "nt", "bash -n 需可用的 Linux Bash")
    def test_bash_syntax(self):
        result = subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipIf(not BASH or os.name == "nt", "Bash 交易整合測試需 Linux（本 Windows 沙箱禁止啟動 Bash）")
    def test_updater_transaction(self):
        for scenario, dry, expected, version in [
            ("success", False, 0, "2099.01.01"), ("success", True, 0, "2026.01.01"),
            ("candidatefail", False, 3, "2026.01.01"), ("postfail", False, 4, "2026.01.01"),
            ("installfail", False, 4, "2026.01.01"), ("rollbackfail", False, 5, "2099.01.01"),
            ("preinterrupt", False, 1, "2026.01.01"), ("postinterrupt", False, 4, "2026.01.01"),
        ]:
            with self.subTest(scenario=scenario, dry=dry), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                app = root / "app"
                (app / "bin").mkdir(parents=True)
                (app / "bin/python").write_text(FAKE_PYTHON, encoding="utf-8", newline="\n")
                (app / "version").write_text("2026.01.01\n", encoding="utf-8", newline="\n")
                (app / "pyvenv.cfg").write_text("home = fake\n", encoding="utf-8", newline="\n")
                (app / "app_dependency.py").write_text("# Only installed in the app environment.\n", encoding="utf-8")
                urls = root / "urls.txt"
                urls.write_text("https://youtu.be/example\n", encoding="utf-8")
                before = {str(p.relative_to(app)): p.read_bytes() for p in app.rglob("*") if p.is_file()}
                env = {**os.environ, "REAL_PYTHON": sys.executable,
                       "FAKE_SCENARIO": scenario, "FAKE_LOG": str(root / "calls.log"),
                       "TMPDIR": str(root), "XDG_RUNTIME_DIR": str(root)}
                command = [BASH, "-c", 'chmod +x "$1/bin/python"; bash "$2" --venv "$1" --urls "$3" "${@:4}"',
                           "test", str(app), str(SCRIPT), str(urls)]
                if dry:
                    command.append("--dry-run")
                result = subprocess.run(command, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30)
                self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
                self.assertEqual((app / "version").read_text().strip(), version)
                if dry or scenario in {"candidatefail", "preinterrupt"}:
                    self.assertEqual(before, {str(p.relative_to(app)): p.read_bytes() for p in app.rglob("*") if p.is_file()})
                log = (root / "calls.log").read_text()
                if scenario != "preinterrupt":
                    self.assertIn("canary:2099.01.01", log)
                if expected == 4:
                    self.assertIn("--no-index", log)
                    self.assertIn("yt-dlp==2026.01.01", log)

    def test_canary_commands_use_app_options_and_never_download(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            urls = root / "urls.txt"
            urls.write_text("# comment\nhttps://youtu.be/example # inline\nhttps://www.facebook.com/reel/123\n", encoding="utf-8")
            candidate = str(root / "candidate/bin/python")
            with patch.object(sys, "argv", ["-", str(SCRIPT.parents[1]), str(urls), "run", candidate]), \
                    patch.object(pipeline, "load_dotenv"), \
                    patch.dict(os.environ, {"YTDLP_YOUTUBE_PROXY": "http://proxy.test:8080", "YTDLP_MAX_HEIGHT": "720"}), \
                    patch.object(subprocess, "run") as run:
                run.return_value.returncode = 0
                exec(compile(canary_source(), str(SCRIPT), "exec"), {})
            youtube, facebook = [call.args[0] for call in run.call_args_list]
            self.assertEqual(youtube[youtube.index("--proxy") + 1], "http://proxy.test:8080")
            self.assertNotIn("--proxy", facebook)
            self.assertEqual(facebook[facebook.index("--impersonate") + 1], "chrome")
            for command in (youtube, facebook):
                self.assertEqual(command[:3], [candidate, "-m", "yt_dlp"])
                self.assertIn("--simulate", command)
                self.assertIn("--no-cookies", command)
                self.assertEqual(command[command.index("-S") + 1], "res:720")

    def test_canary_orchestration_runs_under_app_interpreter(self):
        # Also runs on Windows, where the Bash integration tests cannot execute.
        function = SCRIPT.read_text(encoding="utf-8").split("canaries() {\n", 1)[1].split("<<'PY'", 1)[0]
        self.assertEqual(function.strip(), '"$app" - "$repo" "$urls" "${2:-run}" "$1"')

    def test_canary_input_refuses_incomplete_plan(self):
        for content in ["# empty\n", "https://www.facebook.com/watch/?v=REPLACE_WITH_PUBLIC_VIDEO_ID\n", "file:///private\n"]:
            with self.subTest(content=content), tempfile.TemporaryDirectory() as directory:
                urls = Path(directory) / "urls.txt"
                urls.write_text(content, encoding="utf-8")
                with patch.object(sys, "argv", ["-", str(SCRIPT.parents[1]), str(urls), "validate"]), \
                        patch.object(pipeline, "load_dotenv"), patch.object(subprocess, "run") as run:
                    with self.assertRaises(SystemExit) as error:
                        exec(compile(canary_source(), str(SCRIPT), "exec"), {})
                self.assertEqual(error.exception.code, 2)
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
