#!/usr/bin/env python3
"""Reproducible, local-only W10 dashboard scale measurements.

Builds a synthetic library, starts the dashboard on loopback in a separate
process, and measures paginated library and local FTS HTTP GET requests. No
provider credentials or external services are used. RSS is sampled from the
dashboard process through Linux /proc so the load-generator's memory is not
included.

Example:
    python scripts/benchmark_dashboard_scale.py --output docs/dashboard-w10-results.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pipeline  # noqa: E402


PAGE_SIZE = 100
SEARCH_HIT_TERM = "needleterm"
SEARCH_MISS_TERM = "absenttoken"
RSS_SAMPLE_INTERVAL_SECONDS = 0.01
MAX_CONCURRENCY = 32
ACCEPTANCE_NOTE = (
    "Issue #6 W10 defines no numeric performance threshold. These values are "
    "baseline observations only; no pass/fail claim is made."
)


def _write_manifest(root: Path, index: int, job: str, source_url: str) -> None:
    job_dir = root / "jobs" / job
    job_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "status": "completed",
        "eligible_at": "2026-01-01T00:00:00Z",
        "source": {
            "url": source_url,
            "title": f"Synthetic benchmark video {index:06d}",
            "author": "Synthetic Benchmark Channel",
            "published_at": "2025-12-31T00:00:00Z",
        },
        "video": {"duration_seconds": 180.0},
        "categories": ["synthetic", "benchmark"],
    }
    (job_dir / "manifest.json").write_text(
        json.dumps(manifest, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )


def build_synthetic_library(root: Path, count: int) -> dict[str, int]:
    """Create indexed videos, FTS rows, and list-page manifests in batches."""
    if count < 1:
        raise ValueError("count must be positive")
    jobs_root = root / "jobs"
    jobs_root.mkdir(parents=True, exist_ok=True)

    conn = pipeline.index_connection(root)
    try:
        conn.execute("BEGIN")
        for start in range(0, count, 250):
            videos: list[tuple[Any, ...]] = []
            events: list[tuple[Any, ...]] = []
            states: list[tuple[Any, ...]] = []
            for index in range(start, min(start + 250, count)):
                job = f"bench-{index:06d}"
                url = f"https://www.youtube.com/watch?v=synthetic{index:06d}"
                title = f"Synthetic benchmark video {index:06d}"
                videos.append((job, title, url, "completed", 180.0,
                               "2026-01-01T00:00:00Z", '["synthetic","benchmark"]'))
                needle = f" {SEARCH_HIT_TERM}" if index % 10 == 0 else ""
                events.extend((
                    (job, "00:00", "summary",
                     f"Synthetic dashboard summary record {index:06d}{needle}"),
                    (job, "00:30", "transcript",
                     f"Synthetic transcript record {index:06d} contains local searchable content."),
                ))
                states.append((job, f"synthetic-{index:06d}", 2))
                _write_manifest(root, index, job, url)

            conn.executemany(
                "INSERT INTO videos (job,title,source,status,duration,indexed_at,categories) "
                "VALUES (?,?,?,?,?,?,?)",
                videos,
            )
            conn.executemany(
                "INSERT INTO events (job,timestamp,kind,text) VALUES (?,?,?,?)",
                events,
            )
            conn.executemany(
                "INSERT INTO index_state (job,signature,row_count) VALUES (?,?,?)",
                states,
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()

    return {"videos": count, "events": count * 2, "search_hit_videos": math.ceil(count / 10)}


def current_process_rss_bytes(pid: int) -> int:
    """Read current RSS for a Linux process without third-party packages."""
    statm = Path(f"/proc/{pid}/statm")
    try:
        fields = statm.read_text(encoding="ascii").split()
        resident_pages = int(fields[1])
    except (OSError, IndexError, ValueError) as exc:
        raise RuntimeError(
            "RSS sampling requires Linux /proc/<pid>/statm; run this benchmark on Linux"
        ) from exc
    return resident_pages * os.sysconf("SC_PAGE_SIZE")


class ProcessRssSampler:
    def __init__(self, pid: int, interval_seconds: float = RSS_SAMPLE_INTERVAL_SECONDS):
        self.pid = pid
        self.interval_seconds = interval_seconds
        self.baseline_bytes = current_process_rss_bytes(pid)
        self.peak_bytes = self.baseline_bytes
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._sample, name="dashboard-rss-sampler", daemon=True)

    def _sample(self) -> None:
        while not self._stop.is_set():
            try:
                self.peak_bytes = max(self.peak_bytes, current_process_rss_bytes(self.pid))
            except RuntimeError:
                return
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> dict[str, float]:
        self._stop.set()
        self._thread.join()
        final_bytes = current_process_rss_bytes(self.pid)
        self.peak_bytes = max(self.peak_bytes, final_bytes)
        mib = 1024 * 1024
        return {
            "baseline_mib": round(self.baseline_bytes / mib, 3),
            "peak_mib": round(self.peak_bytes / mib, 3),
            "peak_delta_mib": round((self.peak_bytes - self.baseline_bytes) / mib, 3),
            "final_mib": round(final_bytes / mib, 3),
            "sampling_interval_ms": round(self.interval_seconds * 1000, 1),
            "process": "dashboard server only; Linux /proc resident pages",
        }


def _serve_worker(root: Path, ready_file: Path) -> int:
    import webapp

    server = webapp.build_server(root, host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    ready_file.write_text(json.dumps({
        "pid": os.getpid(),
        "host": "127.0.0.1",
        "port": server.server_address[1],
    }), encoding="utf-8")
    try:
        # The parent writes one line to stdin to request graceful shutdown.
        sys.stdin.readline()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0


def start_server(root: Path) -> tuple[subprocess.Popen[str], str, int]:
    ready_file = root / ".benchmark-server-ready.json"
    worker = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--serve-worker-root", str(root),
         "--ready-file", str(ready_file)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if ready_file.is_file():
            try:
                ready = json.loads(ready_file.read_text(encoding="utf-8"))
                return worker, str(ready["host"]), int(ready["port"])
            except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
                pass
        if worker.poll() is not None:
            _stdout, stderr = worker.communicate(timeout=1)
            raise RuntimeError(f"dashboard benchmark server exited early: {stderr[-2000:]}")
        time.sleep(0.05)
    worker.terminate()
    _stdout, stderr = worker.communicate(timeout=5)
    raise RuntimeError(f"dashboard benchmark server did not start: {stderr[-2000:]}")


def stop_server(worker: subprocess.Popen[str]) -> None:
    if worker.poll() is None:
        assert worker.stdin is not None
        worker.stdin.write("stop\n")
        worker.stdin.flush()
        worker.stdin.close()
        try:
            worker.wait(timeout=10)
        except subprocess.TimeoutExpired:
            worker.terminate()
            worker.wait(timeout=5)
    if worker.returncode not in (0, None):
        stderr = worker.stderr.read() if worker.stderr else ""
        raise RuntimeError(f"dashboard benchmark server failed: {stderr[-2000:]}")


def _get_json(base_url: str, path: str) -> tuple[dict[str, Any], float]:
    request = urllib.request.Request(
        base_url + path,
        headers={"Accept": "application/json", "Connection": "close"},
        method="GET",
    )
    started = time.perf_counter_ns()
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = response.status
            payload = json.loads(response.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"GET {path} failed: {exc}") from exc
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    if status != 200:
        raise RuntimeError(f"GET {path} returned HTTP {status}")
    return payload, elapsed_ms


def _page_url(page: int) -> str:
    query = urllib.parse.urlencode({"page": page, "page_size": PAGE_SIZE, "sort": "indexed_at", "order": "asc"})
    return f"/api/videos?{query}"


def _search_url(query: str) -> str:
    return "/api/search?" + urllib.parse.urlencode({"q": query, "limit": 20})


def _check_page(base_url: str, page: int, total: int) -> tuple[dict[str, Any], float]:
    result, elapsed = _get_json(base_url, _page_url(page))
    if result.get("total") != total or result.get("page") != page:
        raise RuntimeError(f"page response mismatch: expected total={total} page={page}")
    expected_items = min(PAGE_SIZE, max(0, total - (page - 1) * PAGE_SIZE))
    if len(result.get("items", [])) != expected_items:
        raise RuntimeError(f"page {page} returned an unexpected item count")
    return result, elapsed


def _check_search(base_url: str, query: str, expected_min: int | None) -> tuple[dict[str, Any], float]:
    result, elapsed = _get_json(base_url, _search_url(query))
    if result.get("external_calls") != 0 or result.get("mode") != "local":
        raise RuntimeError("GET search did not identify itself as local with external_calls=0")
    count = len(result.get("results", []))
    if expected_min is not None and count != expected_min:
        raise RuntimeError(f"search {query!r} returned {count}; expected {expected_min}")
    return result, elapsed


def _percentiles(samples_ms: list[float]) -> dict[str, float | int]:
    if not samples_ms:
        raise ValueError("cannot summarize an empty sample set")
    ordered = sorted(samples_ms)
    # Nearest-rank percentile: rank = ceil(p * n), one-based.
    p95_index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return {
        "samples": len(ordered),
        "p50_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
    }


def _spread_pages(sample_count: int, page_count: int) -> list[int]:
    if sample_count == 1 or page_count == 1:
        return [1] * sample_count
    return [1 + (index * (page_count - 1) // (sample_count - 1)) for index in range(sample_count)]


def benchmark_dataset(
    root: Path,
    *,
    video_count: int,
    samples: int,
    warmups: int,
    concurrency: int,
    requests_per_worker: int,
) -> dict[str, Any]:
    worker: subprocess.Popen[str] | None = None
    sampler: ProcessRssSampler | None = None
    try:
        worker, host, port = start_server(root)
        pid = worker.pid
        base_url = f"http://{host}:{port}"
        sampler = ProcessRssSampler(pid)
        sampler.start()

        page_count = math.ceil(video_count / PAGE_SIZE)
        page_samples = _spread_pages(samples, page_count)
        hit_count = min(20, math.ceil(video_count / 10))

        # Warm OS/filesystem/SQLite caches, but exclude warmups from latency percentiles.
        for index in range(warmups):
            _check_page(base_url, page_samples[index % len(page_samples)], video_count)
            _check_search(base_url, SEARCH_HIT_TERM, hit_count)
            _check_search(base_url, SEARCH_MISS_TERM, 0)

        serial_page: list[float] = []
        serial_hit: list[float] = []
        serial_miss: list[float] = []
        for page in page_samples:
            _page, elapsed = _check_page(base_url, page, video_count)
            serial_page.append(elapsed)
            _search, elapsed = _check_search(base_url, SEARCH_HIT_TERM, hit_count)
            serial_hit.append(elapsed)
            _search, elapsed = _check_search(base_url, SEARCH_MISS_TERM, 0)
            serial_miss.append(elapsed)

        concurrent_page: list[float] = []
        concurrent_hit: list[float] = []
        total_pairs = concurrency * requests_per_worker

        def concurrent_reader(reader_index: int) -> tuple[list[float], list[float]]:
            pages: list[float] = []
            searches: list[float] = []
            for iteration in range(requests_per_worker):
                sample_index = reader_index * requests_per_worker + iteration
                page = page_samples[sample_index % len(page_samples)]
                _page, elapsed = _check_page(base_url, page, video_count)
                pages.append(elapsed)
                _search, elapsed = _check_search(base_url, SEARCH_HIT_TERM, hit_count)
                searches.append(elapsed)
            return pages, searches

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="w10-reader") as pool:
            futures = [pool.submit(concurrent_reader, index) for index in range(concurrency)]
            for future in futures:
                pages, searches = future.result()
                concurrent_page.extend(pages)
                concurrent_hit.extend(searches)

        rss = sampler.stop()
        sampler = None
        return {
            "video_count": video_count,
            "synthetic_index": {
                "videos": video_count,
                "fts_events": video_count * 2,
                "needleterm_matching_videos": math.ceil(video_count / 10),
            },
            "query_parameters": {
                "page_size": PAGE_SIZE,
                "search_hit_query": SEARCH_HIT_TERM,
                "search_miss_query": SEARCH_MISS_TERM,
                "search_limit": 20,
                "serial_samples_per_operation": samples,
                "warmups_per_operation": warmups,
            },
            "serial_round_trip_latency": {
                "library_page": _percentiles(serial_page),
                "search_hit": _percentiles(serial_hit),
                "search_miss": _percentiles(serial_miss),
            },
            "bounded_concurrent_readers": {
                "workers": concurrency,
                "page_search_pairs_per_worker": requests_per_worker,
                "pairs_total": total_pairs,
                "requests_per_endpoint": total_pairs,
                "library_page": _percentiles(concurrent_page),
                "search_hit": _percentiles(concurrent_hit),
            },
            "dashboard_process_rss": rss,
        }
    finally:
        if sampler is not None:
            try:
                sampler.stop()
            except RuntimeError:
                pass
        if worker is not None:
            stop_server(worker)


def machine_info() -> dict[str, Any]:
    processor = platform.processor()
    if not processor and Path("/proc/cpuinfo").is_file():
        match = re.search(r"^model name\s*:\s*(.+)$", Path("/proc/cpuinfo").read_text(errors="replace"), re.M)
        processor = match.group(1).strip() if match else "unknown"
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": processor or "unknown",
        "logical_cpu_count": os.cpu_count(),
        "python": platform.python_version(),
    }


def git_revision() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def run_benchmarks(args: argparse.Namespace) -> dict[str, Any]:
    if platform.system() != "Linux" or not Path("/proc/self/statm").is_file():
        raise RuntimeError("dashboard process RSS sampling currently requires Linux /proc")
    results = []
    for count in args.sizes:
        with tempfile.TemporaryDirectory(prefix=f"dashboard-w10-{count}-") as temp:
            root = Path(temp)
            dataset = build_synthetic_library(root, count)
            measured = benchmark_dataset(
                root,
                video_count=count,
                samples=args.samples,
                warmups=args.warmups,
                concurrency=args.concurrency,
                requests_per_worker=args.requests_per_worker,
            )
            assert dataset["videos"] == measured["video_count"]
            results.append(measured)

    return {
        "benchmark": "dashboard-w10-synthetic-scale",
        "measured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "revision": git_revision(),
        "machine": machine_info(),
        "method": {
            "transport": "HTTP GET to a separate loopback dashboard server process",
            "rss": "server process current resident pages sampled through /proc every 10ms; load generator excluded",
            "latency": "client-observed round-trip including HTTP and JSON decoding; p95 uses nearest rank",
            "dataset_setup_excluded_from_timing": True,
            "providers_or_external_calls": 0,
            "acceptance_thresholds": None,
            "threshold_note": ACCEPTANCE_NOTE,
        },
        "configuration": {
            "sizes": args.sizes,
            "page_size": PAGE_SIZE,
            "serial_samples": args.samples,
            "warmups": args.warmups,
            "concurrent_readers": args.concurrency,
            "page_search_pairs_per_reader": args.requests_per_worker,
            "max_concurrency_supported": MAX_CONCURRENCY,
        },
        "results": results,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sizes", nargs="+", type=int, default=[1000, 10000])
    parser.add_argument("--samples", type=int, default=30, help="serial samples per query type")
    parser.add_argument("--warmups", type=int, default=3, help="warmup requests per query type")
    parser.add_argument("--concurrency", type=int, default=8, help=f"bounded reader threads (1..{MAX_CONCURRENCY})")
    parser.add_argument("--requests-per-worker", type=int, default=5)
    parser.add_argument("--output", type=Path, help="also write the complete JSON report to this path")
    parser.add_argument("--serve-worker-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--ready-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.serve_worker_root is not None:
        if args.ready_file is None:
            parser.error("--ready-file is required with --serve-worker-root")
        return args
    if any(size < 1 for size in args.sizes):
        parser.error("all sizes must be positive")
    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        parser.error(f"--concurrency must be between 1 and {MAX_CONCURRENCY}")
    if args.samples < 1 or args.warmups < 0 or args.requests_per_worker < 1:
        parser.error("samples and requests-per-worker must be positive; warmups must be nonnegative")
    return args


def main() -> int:
    args = parse_args()
    if args.serve_worker_root is not None:
        return _serve_worker(args.serve_worker_root, args.ready_file)
    report = run_benchmarks(args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
