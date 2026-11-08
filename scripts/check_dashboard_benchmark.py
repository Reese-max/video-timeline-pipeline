"""Offline Dashboard HTTP/RSS benchmark: record once, then rerun with --check.

Only synthetic TemporaryDirectory data is used. TestClient measures ASGI/server
work, not browser rendering or TCP/TLS latency. No model or network is allowed.
"""
from __future__ import annotations

import argparse
import builtins
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, closing
import ctypes
import datetime as dt
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "dashboard-benchmark.json"
SIZES = (1000, 10000)
# The saved ungated 10k baseline was 240/393 ms and 84 MiB: allow 3-5x headroom.
# These are local regression gates, not production/browser response-time claims.
LIMITS = {"route_p95_ms": 1000, "concurrent_p95_ms": 2000, "peak_rss_mib": 256}
HEAVY = {"torch", "whisper", "faster_whisper", "sentence_transformers", "transformers"}


def memory():
    """OS process high-water RSS; the fixture build is deliberately included."""
    if os.name == "nt":
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (name, ctypes.c_size_t) for name in ("peak", "current", "qpp", "qp", "qnp", "qn", "pf", "ppf")]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        api = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
        api.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        info = Counters()
        info.cb = ctypes.sizeof(info)
        if not api(kernel.GetCurrentProcess(), ctypes.byref(info), info.cb):
            raise ctypes.WinError(ctypes.get_last_error())
        current, peak = info.current, info.peak
    else:
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * (1 if sys.platform == "darwin" else 1024)
        current = int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE") if sys.platform.startswith("linux") else peak
    return {"rss_mib": round(current / 1048576, 3), "peak_rss_mib": round(peak / 1048576, 3)}


def machine():
    if os.name == "nt":
        class Status(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ("total", "available", "page", "free_page", "virtual", "free_virtual", "extended")]
        state = Status()
        state.length = ctypes.sizeof(state)
        if not ctypes.WinDLL("kernel32", use_last_error=True).GlobalMemoryStatusEx(ctypes.byref(state)):
            raise ctypes.WinError(ctypes.get_last_error())
        ram = state.total
    else:
        ram = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    return {"os": platform.platform(), "python": sys.version, "sqlite": sqlite3.sqlite_version,
            "cpu": platform.processor(), "logical_cpus": os.cpu_count(), "ram_mib": round(ram / 1048576, 1)}


def timing(values):
    return {"samples": len(values), "median_ms": round(statistics.median(values), 3),
            "p95_ms": round(sorted(values)[math.ceil(len(values) * .95) - 1], 3), "max_ms": round(max(values), 3)}


def seed(root, size, services):
    services.initialize(root)  # Reuse the real schemas, never a benchmark-only schema.
    stamp = dt.datetime.now(dt.timezone.utc).isoformat()
    with closing(sqlite3.connect(root / "library.db")) as db, db:
        db.executemany("INSERT INTO videos VALUES (?,?,?,?,?,?,?)", (
            (f"bench{i:06d}", f"Benchmark video {i:06d}", f"https://www.youtube.com/watch?v=bench{i:06d}",
             "completed", 3600, stamp, json.dumps(["AI" if i % 2 == 0 else "AI tools"])) for i in range(size)))
        db.executemany("INSERT INTO video_catalog VALUES (?,?,?,?,?)", (
            (f"bench{i:06d}", "youtube", f"Researcher {i % 5}", stamp, stamp) for i in range(size)))
        db.executemany("INSERT INTO events VALUES (?,?,?,?)", (
            (f"bench{i:06d}", event * 3, "title" if event == 0 else "summary" if event == 1 else "speech",
             f"benchmark evidence video {i:06d} event {event}: " + "synthetic transcript alpha gamma delta " * (50 if i == 0 else 4))
            for i in range(size) for event in range(300 if i == 0 else 10)))
        db.executemany("INSERT INTO vectors VALUES (?,?,?,?)", (
            (f"synthetic-{i}", "synthetic-only", 384, bytes(384 * 4)) for i in range(size)))
        counts = {table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("videos", "video_catalog", "events", "vectors")}
    assert counts == {"videos": size, "video_catalog": size, "events": size * 10 + 290, "vectors": size}
    folder = root / "jobs" / "bench000000"
    folder.mkdir(parents=True)
    (folder / "summary.json").write_text(json.dumps({"brief": "Synthetic benchmark summary"}), encoding="utf-8")
    (folder / "transcript.json").write_text(json.dumps({"segments": [
        {"start": i * 3, "text": "synthetic long transcript " * 100} for i in range(300)]}), encoding="utf-8")
    return {**counts, "vector_dimensions": 384, "long_video_events": 300,
            "database_bytes": (root / "library.db").stat().st_size,
            "long_transcript_bytes": (folder / "transcript.json").stat().st_size}


def benchmark(size, samples):
    sys.path.insert(0, str(ROOT))
    from fastapi.testclient import TestClient
    blocked = []
    control_pairs = []
    original_import = builtins.__import__
    original_connect = socket.socket.connect
    socketpair_code = getattr(socket.socketpair, "__code__", None)

    def no_external(*args, **kwargs):
        blocked.append("external_call")
        raise AssertionError("Network/model calls are forbidden in this benchmark")

    def imports(name, *args, **kwargs):
        if name.split(".")[0] in HEAVY:
            blocked.append("model_import")
            raise AssertionError("Model imports are forbidden in this benchmark")
        return original_import(name, *args, **kwargs)

    def connect(sock, address):
        # Windows asyncio implements socketpair through stdlib-only loopback IPC.
        if os.name == "nt" and sys._getframe(1).f_code is socketpair_code and address[0] in {"127.0.0.1", "::1"}:
            control_pairs.append(1)
            return original_connect(sock, address)
        return no_external()

    with tempfile.TemporaryDirectory(prefix="dashboard-benchmark-") as temp, ExitStack() as guards:
        root = Path(temp)
        guards.enter_context(patch.dict(os.environ, {"WEB_TOKEN": "", "COST_TIMEZONE": "UTC", "MCP_ROOT": temp}))
        guards.enter_context(patch("socket.socket.connect", new=connect))
        for target in ("socket.socket.connect_ex", "socket.create_connection", "urllib.request.urlopen"):
            guards.enter_context(patch(target, new=no_external))
        guards.enter_context(patch("builtins.__import__", new=imports))
        import webapp
        import web_services as services
        for name in ("embed_texts", "embed_texts_local", "minimax_chat", "answer_question", "semantic_search"):
            guards.enter_context(patch.object(services.pipeline, name, new=no_external))
        before = memory()
        started = time.perf_counter()
        fixture = seed(root, size, services)
        fixture["build_seconds"] = round(time.perf_counter() - started, 3)
        after_seed = memory()
        paths = {
            "home_html": "/",
            "videos_html": "/videos?page=2&page_size=100",
            "videos_api": "/api/videos?page=3&page_size=100",
            "exact_filter": "/api/videos?category=AI&author=Researcher%200&platform=youtube&page_size=50",
            "local_search": "/api/search?q=benchmark&category=AI&limit=20",
            "evidence_html": "/videos/bench000000?page=6&page_size=50",
            "evidence_api": "/api/videos/bench000000?page=3&page_size=50",
        }
        sizes = {name: 0 for name in paths}
        with TestClient(webapp.create_app(root), base_url="http://127.0.0.1:8080") as client:
            def request(name):
                start = time.perf_counter()
                response = client.get(paths[name])
                elapsed = (time.perf_counter() - start) * 1000
                assert response.status_code == 200, (name, response.status_code, response.text[:200])
                sizes[name] = max(sizes[name], len(response.content))
                if name == "home_html":
                    assert '<section class="overview"' in response.text
                elif name.endswith("html"):
                    marker, count = ('class="video"', 100) if name == "videos_html" else ('class="evidence"', 50)
                    assert response.text.count(marker) == count, name
                else:
                    data = response.json()["data"]
                    if name == "evidence_api":
                        data = data["evidence"]
                        assert data["total"] == 300 and data["page"] == 3
                    if name == "local_search":
                        assert data["mode"] == "fts" and len(data["items"]) == 20
                        assert all(int(item["job"][5:]) % 2 == 0 for item in data["items"])
                    else:
                        assert data["page_size"] <= 100 and len(data["items"]) <= data["page_size"]
                        expected = size // 10 if name == "exact_filter" else 300 if name == "evidence_api" else size
                        assert data["total"] == expected
                        assert len(data["items"]) == min(data["page_size"], expected)
                        if name == "exact_filter":
                            assert all(item["categories"] == ["AI"] and item["author"] == "Researcher 0" for item in data["items"])
                return elapsed

            metrics = {}
            for name in paths:
                request(name)  # One warm-up; measured requests still open their real read-only DB connections.
                metrics[name] = timing([request(name) for _ in range(samples)])
            concurrent, batches = [], []
            with ThreadPoolExecutor(max_workers=8) as pool:
                for _ in range(5):
                    barrier = threading.Barrier(8)
                    def simultaneous(name):
                        barrier.wait(timeout=10)
                        return request(name)
                    start = time.perf_counter()
                    concurrent.extend(pool.map(simultaneous, [*paths, "videos_api"]))
                    batches.append((time.perf_counter() - start) * 1000)
        assert not blocked, blocked
        loaded = sorted(HEAVY.intersection(sys.modules))
        assert not loaded, loaded
        return {"videos": size, "fixture": fixture, "before_fixture": before, "after_fixture": after_seed,
                **memory(), "requests": metrics, "response_bytes_max": sizes,
                "concurrent_8_requests": timing(concurrent), "concurrent_8_batch": timing(batches),
                "network_or_model_attempts": len(blocked), "windows_asyncio_socketpair_ipc": len(control_pairs), "loaded_model_modules": loaded}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Append verification; never replace the saved baseline")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--size", type=int, choices=SIZES, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if not 5 <= args.samples <= 100:
        parser.error("--samples must be between 5 and 100")
    if args.size:
        print(json.dumps(benchmark(args.size, args.samples)))
        return 0
    if args.check:
        if not RECORD.exists() or LIMITS is None:
            parser.error("Record an ungated baseline and calibrate LIMITS before --check")
        report = json.loads(RECORD.read_text(encoding="utf-8"))
    else:
        if RECORD.exists():
            parser.error("Baseline already exists; use --check to preserve it")
        report = {"schema_version": 1, "methodology": {
            "transport": "FastAPI TestClient against the real ASGI app; HTTP status and bounded rows asserted",
            "timing": "perf_counter HTTP round trip; one warm-up per route; nearest-rank p95; five concurrent batches of eight",
            "memory": "OS process lifetime peak RSS in a fresh child per dataset, including imports, fixture build, and HTTP requests",
            "offline": "Socket/urllib transports, provider functions, and heavy model imports are blocked; only Windows stdlib socketpair loopback IPC is allowed; fixtures are TemporaryDirectory-only",
            "limitations": ["Synthetic local data and warm filesystem caches; no production latency claim",
                            "In-process ASGI transport excludes browser rendering, network, TLS, LCP, INP, and physical-device acceptance",
                            "No real model or provider operations; vectors are synthetic 384-dimensional zero blobs"]}, "verifications": []}
    run = {"at": dt.datetime.now(dt.timezone.utc).isoformat(), "machine": machine(), "datasets": []}
    for size in SIZES:
        child = subprocess.run([sys.executable, "-X", "utf8", "-B", str(Path(__file__).resolve()),
                                "--size", str(size), "--samples", str(args.samples)],
                               cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=300)
        if child.returncode:
            print(child.stderr, file=sys.stderr)
            return child.returncode
        data = json.loads(child.stdout)
        run["datasets"].append(data)
        print(f"{size} videos: max route p95={max(m['p95_ms'] for m in data['requests'].values()):.3f} ms; "
              f"concurrent p95={data['concurrent_8_requests']['p95_ms']:.3f} ms; peak RSS={data['peak_rss_mib']:.3f} MiB", flush=True)
    passed = True
    if args.check:
        run["limits"] = LIMITS
        run["passed"] = passed = all(
            max(metric["p95_ms"] for metric in data["requests"].values()) <= LIMITS["route_p95_ms"]
            and data["concurrent_8_requests"]["p95_ms"] <= LIMITS["concurrent_p95_ms"]
            and data["peak_rss_mib"] <= LIMITS["peak_rss_mib"] for data in run["datasets"])
        report["verifications"].append(run)
    else:
        report["baseline"] = run
        report["baseline_sha256"] = hashlib.sha256(json.dumps(run, sort_keys=True).encode()).hexdigest()
    assert report["baseline_sha256"] == hashlib.sha256(json.dumps(report["baseline"], sort_keys=True).encode()).hexdigest()
    RECORD.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(("PASS" if passed else "FAIL") + f": {'verification appended' if args.check else 'ungated baseline recorded'}; {RECORD.relative_to(ROOT)}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
