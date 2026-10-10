"""Run the same offline release checks on Windows and Linux; missing tools fail."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    for tool in ("ffmpeg", "ffprobe", "node", "git"):
        if not shutil.which(tool):
            print(f"Missing required tool: {tool}", file=sys.stderr)
            return 1
    for module in ("mcp", "PIL", "yt_dlp", "tzdata", "opentimelineio"):
        if importlib.util.find_spec(module) is None:
            print(f"Missing dependency: {module}; install requirements.txt", file=sys.stderr)
            return 1
    with tempfile.TemporaryDirectory(prefix="video-offline-") as state:
        env = {**os.environ, "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1", "MCP_ROOT": state}
        for command in (
            ["ffmpeg", "-version"], ["ffprobe", "-version"], ["node", "--version"],
            [sys.executable, "-X", "utf8", "-B", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
            ["node", "tests/test_widget_handoff.cjs"],
            ["node", "tests/test_parallel_submission.cjs"],
            ["node", "tests/test_widget_analysis_report.cjs"],
        ):
            result = subprocess.run(command, cwd=root, env=env)
            if result.returncode:
                return result.returncode
        checkout = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, env=env,
                                  capture_output=True, text=True)
        if checkout.returncode == 0 and Path(checkout.stdout.strip()).resolve() == root:
            return subprocess.run(["git", "diff", "--check"], cwd=root, env=env).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
