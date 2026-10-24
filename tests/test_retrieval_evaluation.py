"""Replay 25 fixed synthetic questions against the admitted baseline and new routes.

Fake embeddings exercise routing/ranking, not real-model semantic accuracy.
Run with unittest to print the reproducible metrics (no provider or network I/O).
"""
import contextlib
import importlib.util
import io
import json
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

import pipeline


BASE_SHA = "42d225936ba704cf70b93cac7bdf84da85ac86b8"
FIXTURE = Path(__file__).parent / "fixtures" / "retrieval.json"


def fixture_embedding(texts, *_args, **_kwargs):
    groups = (("AI", "密碼", "資安"), ("大象", "鼻", "長鼻子"), ("終端機",), ("紅色", "外套"),
              ("價格",), ("忽略", "命令"), ("print(", "Python", "程式"), ("知識",))
    result = []
    for text in texts:
        vector = [float(any(term in text for term in group)) for group in groups]
        vector.append(-1.0 if text in {"量子火箭", "%", '" OR *'} else float(not any(vector)))
        result.append(vector)
    return result


def peak_rss_bytes():
    if sys.platform != "win32":
        import resource
        value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return value if sys.platform == "darwin" else value * 1024
    import ctypes
    from ctypes import wintypes
    class MemoryCounters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD),
                    *[(name, ctypes.c_size_t) for name in ("peak_rss", "rss", "peak_pool", "pool", "peak_nonpaged",
                                                         "nonpaged", "pagefile", "peak_pagefile")]]
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(MemoryCounters), wintypes.DWORD]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return counters.peak_rss


class RetrievalEvaluationTests(unittest.TestCase):
    def test_replay_25_questions_baseline_and_hybrid(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(fixture["cases"]), 25)
        source = subprocess.run(["git", "show", BASE_SHA + ":pipeline.py"], check=True,
                                capture_output=True, encoding="utf-8").stdout
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            module_file = root / "baseline.py"
            module_file.write_text(source, encoding="utf-8")
            spec = importlib.util.spec_from_file_location("issue5_baseline", module_file)
            baseline = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(baseline)
            for item in fixture["jobs"]:
                job = root / "jobs" / item["job"]
                job.mkdir(parents=True)
                pipeline.write_json(job / "manifest.json", {
                    "status": "completed", "categories": [item["category"]],
                    "source": {"title": item["title"], "url": "https://example.test/" + item["job"],
                               "platform": "fixture", "author": item["author"], "published_at": item["published_at"]},
                    "first_completed_at": "2026-09-02T12:00:00Z",
                })
                pipeline.write_json(job / "timeline.json", item["events"])
                pipeline.write_json(job / "summary.json", {"short_summary": item["summary"]})
            baseline.build_index(root)
            with mock.patch.object(baseline, "embed_texts", side_effect=fixture_embedding), contextlib.redirect_stdout(io.StringIO()):
                baseline.build_embeddings(root, "", "fixture", "local")
            metrics = {}

            def measure(label, retrieve):
                recalls, reciprocal_ranks, empty_checks, latencies, citation_checks = [], [], [], [], []
                tracemalloc.start()
                for repetition in range(2):
                    for case in fixture["cases"]:
                        start = time.perf_counter()
                        hits = retrieve(case)
                        latencies.append((time.perf_counter() - start) * 1000)
                        actual = [(h["job"], h["kind"], None if h["kind"] in {"title", "summary"} else h["timestamp"])
                                  for h in hits]
                        gold = {tuple(value) for value in case["gold"]}
                        if gold:
                            recalls.append(len(set(actual) & gold) / len(gold))
                            reciprocal_ranks.append(next((1 / rank for rank, value in enumerate(actual, 1) if value in gold), 0))
                        else:
                            empty_checks.append(not actual)
                        if label == "fts" and case["id"] != "synonym":
                            with self.subTest(case=case["id"]):
                                self.assertEqual(set(actual), gold)
                        if label in {"semantic", "hybrid"}:
                            with self.subTest(case=case["id"], route=label):
                                self.assertTrue(gold.issubset(set(actual)))
                                if not gold:
                                    self.assertFalse(actual)
                        if not label.startswith("baseline_"):
                            citation_checks.extend(pipeline._evidence_current(root, hit) for hit in hits)
                            self.assertTrue(all(citation_checks), case["id"])
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                metrics[label] = {"recall_at_5": round(statistics.mean(recalls), 4),
                                  "mrr": round(statistics.mean(reciprocal_ranks), 4),
                                  "no_answer_accuracy": round(statistics.mean(empty_checks), 4),
                                  "median_ms": round(statistics.median(latencies), 3),
                                  "first_pass_median_ms": round(statistics.median(latencies[:25]), 3),
                                  "warm_pass_median_ms": round(statistics.median(latencies[25:]), 3),
                                  "peak_python_bytes": peak, "process_peak_rss_bytes": peak_rss_bytes(),
                                  "citation_validity": statistics.mean(citation_checks) if citation_checks else None,
                                  "query_samples": len(latencies), "paid_calls": 0, "provider_tokens": 0}

            with mock.patch.object(baseline, "embed_texts", side_effect=fixture_embedding):
                measure("baseline_fts", lambda case: baseline.search_index(root, case["query"], 5,
                                                                           case["filters"].get("category", "")))
                measure("baseline_semantic", lambda case: baseline.semantic_search(root, case["query"], "", "fixture", 5, "local"))
            pipeline.build_index(root)
            adopted = pipeline.adopt_legacy_embeddings(root, pipeline.EmbeddingContract("local", "fixture"))
            self.assertGreater(adopted, 0)
            with mock.patch.object(pipeline, "embed_texts", side_effect=AssertionError("no re-embedding on migration")):
                self.assertEqual(pipeline.build_embeddings(root, "", "fixture", "local")[0], 0)
            with mock.patch.object(pipeline, "embed_texts", side_effect=fixture_embedding):
                for mode in ("fts", "semantic", "hybrid"):
                    measure(mode, lambda case, mode=mode: pipeline.retrieve_evidence(
                        root, case["query"], 5, filters=pipeline.SearchFilter(**case["filters"]), mode=mode,
                        embed_backend="local", embed_model="fixture")["evidence"])
            self.assertGreaterEqual(metrics["hybrid"]["recall_at_5"], metrics["fts"]["recall_at_5"])
            with contextlib.closing(pipeline.library_connection(root)) as db:
                index_size = {"videos": db.execute("SELECT count(*) FROM videos").fetchone()[0],
                              "occurrences": db.execute("SELECT count(*) FROM events").fetchone()[0],
                              "unique_texts": db.execute("SELECT count(DISTINCT text) FROM events").fetchone()[0]}
            print("RETRIEVAL_EVAL " + json.dumps({"base_sha": BASE_SHA, "questions": 25,
                "machine": platform.platform(), "python": platform.python_version(), "model": "deterministic-fake-9d",
                "processor": platform.processor(), "index": index_size,
                "label_provenance": fixture["label_provenance"], "human_semantic_support_rate": None,
                "rss_scope": "process lifetime, not isolated per-route", "metrics": metrics}, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
