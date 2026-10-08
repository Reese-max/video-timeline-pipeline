"""Run on Linux/WSL: python3 -m unittest discover -s tests -p test_media_tunnel.py -v."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest


@unittest.skipUnless(os.name == "posix" and shutil.which("bash"), "Linux/WSL bash required")
class MediaTunnelTests(unittest.TestCase):
    def test_launcher_rejects_stale_runtime_and_keeps_drive_available(self):
        script = (Path(__file__).resolve().parents[1] / "scripts/start-mcp-supervised.sh").read_text()
        selection = script[script.index('media_base="${MCP_MEDIA_BASE_URL:-}"'):script.index('process_matches()')]
        guard = script[script.index('if [ -n "${pid}" ]; then'):script.index('command=(/bin/bash')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "media-launcher"
            launcher.write_text("#!/bin/sh\nexit 1\n")
            launcher.chmod(0o700)
            urlfile = root / "media-url"
            urlfile.write_text("https://dead.trycloudflare.com\n")
            env = dict(os.environ, MCP_MEDIA_BASE_URL="", MCP_USE_QUICK_TUNNEL="1", media_launcher=str(launcher),
                       media_url_file=str(urlfile), public_base="https://mcp.example", group="test")
            result = subprocess.run(["bash", "-c", selection + 'printf "%s" "$media_base"'],
                                    env=env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, "https://mcp.example")
            self.assertIn("Drive handoff remains available", result.stderr)
            for actual, expected in (("https://old.example", 1), ("https://new.example", 0)):
                env.update(MCP_MEDIA_BASE_URL=actual, media_base="https://new.example")
                result = subprocess.run(["bash", "-c", 'pid="$$"\n' + guard], env=env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr)
                if expected:
                    self.assertIn("Running jobs were not interrupted", result.stderr)

    def test_launcher_defaults_to_stable_media_and_preserves_explicit_override(self):
        script = (Path(__file__).resolve().parents[1] / "scripts/start-mcp-supervised.sh").read_text()
        selection = script[script.index('media_base="${MCP_MEDIA_BASE_URL:-}"'):script.index('process_matches()')]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            launcher = root / "media-launcher"
            marker = root / "called"
            launcher.write_text('#!/bin/sh\ntouch "$marker"\nexit 1\n')
            launcher.chmod(0o700)
            urlfile = root / "media-url"
            urlfile.write_text("https://rotating.trycloudflare.com\n")
            for override, expected in (("", "https://mcp.example"), ("https://media.example", "https://media.example")):
                env = dict(os.environ, MCP_MEDIA_BASE_URL=override, MCP_USE_QUICK_TUNNEL="0",
                           media_launcher=str(launcher), media_url_file=str(urlfile),
                           public_base="https://mcp.example", group="test", marker=str(marker))
                result = subprocess.run(["bash", "-c", selection + 'printf "%s" "$media_base"'],
                                        env=env, capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, expected)
                self.assertFalse(marker.exists(), "Stable mode must not start or depend on a quick tunnel")

    def test_worker_recovers_dns_failure_without_restarting_a_failed_origin(self):
        script = Path(__file__).resolve().parents[1] / "scripts/start-mcp-media-tunnel-supervised.sh"
        for scenario in ("startup_dns", "runtime_dns", "origin_down", "rate_limit"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "cloudflared").write_text("""#!/usr/bin/env python3
import os, pathlib, time
root = pathlib.Path(os.environ['MCP_BASE_DIR'])
counter = root / 'launches'
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
print(f'https://tunnel-{n}.trycloudflare.com', flush=True)
while True: time.sleep(60)
""")
                (root / "curl").write_text("""#!/usr/bin/env python3
import os, pathlib, sys
root = pathlib.Path(os.environ['MCP_BASE_DIR'])
scenario = os.environ['TEST_SCENARIO']
if sys.argv[-1].startswith('http://127.0.0.1'):
    print('502' if scenario == 'origin_down' else '200', end='')
    sys.exit(0)
counter = root / 'probes'
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
if 'tunnel-1.' in sys.argv[-1]:
    if scenario == 'startup_dns' or (scenario == 'runtime_dns' and n > 1):
        print('000', end=''); sys.exit(6)
    if n > 1 and scenario in ('origin_down', 'rate_limit'):
        print('502' if scenario == 'origin_down' else '429', end=''); sys.exit(0)
print('200', end='')
""")
                (root / "sleep").write_text("#!/bin/sh\n/bin/sleep 0.01\n")
                for name in ("curl", "cloudflared", "sleep"):
                    (root / name).chmod(0o700)
                urlfile = root / "media-url"
                env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", MCP_BASE_DIR=str(root),
                           MCP_MEDIA_URL_FILE=str(urlfile), CLOUDFLARED_BIN=str(root / "cloudflared"),
                           MCP_MEDIA_STARTUP_ATTEMPTS="5", TEST_SCENARIO=scenario)
                process = subprocess.Popen(["bash", str(script), "test", "--worker"], env=env,
                                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        self.assertIsNone(process.poll(), "Worker exited unexpectedly")
                        url = urlfile.read_text().strip() if urlfile.exists() else ""
                        if scenario == "startup_dns":
                            self.assertNotIn("tunnel-1.", url, "Never publish an NXDOMAIN hostname")
                        probes = int((root / "probes").read_text() or "0") if (root / "probes").exists() else 0
                        if scenario.endswith("dns") and "tunnel-2." in url:
                            break
                        if scenario in ("origin_down", "rate_limit") and probes >= 6:
                            self.assertEqual((root / "launches").read_text(), "1")
                            self.assertIn("tunnel-1.", url)
                            break
                        time.sleep(0.02)
                    else:
                        self.fail(f"Worker did not reach expected state: {scenario}")
                finally:
                    process.terminate()
                    _, errors = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, errors.decode())
                self.assertFalse(urlfile.exists(), "Shutdown must remove the dead URL")


if __name__ == "__main__":
    unittest.main()
