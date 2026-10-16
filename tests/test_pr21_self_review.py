"""PR #21 regressions using real local indexing and offline provider doubles."""
import contextlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline
from embedding_contract_fixture import FictionalContractsCase
from test_retrieval import seed_video


class SelfReviewTests(FictionalContractsCase):
    def setUp(self):
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_production_timeline_preserves_nested_original_ids(self):
        timeline = pipeline.merge_timeline({"segments": [{
            "id": 7, "segment_id": "speech-7", "source_ids": ["raw-7"],
            "start": 1., "end": 2., "text": "original speech",
        }]}, [{"timestamp": 3., "segment_id": "frame-3", "visible_text": ["original visual"]}])
        seed_video(self.root, "job", timeline=timeline)
        pipeline.build_index(self.root)
        hits = {hit["kind"]: hit for hit in pipeline.search_index(self.root, "original", 10)}
        self.assertEqual(hits["speech"]["original_segment_ids"], ["raw-7", "speech-7", "7"])
        self.assertEqual(hits["visual"]["original_segment_ids"], ["frame-3"])

    def test_invalid_times_cannot_break_context_or_fabricate_ranges(self):
        for start, end, expected in [
            (1, "00:00:10", (1., None, "point")),
            (1, {}, (1., None, "point")),
            (1, float("inf"), (1., None, "point")),
            (1, 0, (1., None, "point")),
            (float("nan"), 2, (None, None, "unknown")),
            (-1, 2, (None, None, "unknown")),
            (True, 2, (None, None, "unknown")),
        ]:
            with self.subTest(start=start, end=end):
                seed_video(self.root, "job", summary={"short_summary": "topic", "chapters": [
                    {"title": "topic", "start": start, "end": end, "summary": "quoted chapter"},
                ]})
                pipeline.build_index(self.root)
                hit = pipeline.search_index(self.root, "topic", 5, filters=pipeline.SearchFilter(kinds=("chapter",)))[0]
                self.assertEqual((hit["start"], hit["end"], hit["timestamp_kind"]), expected)
                self.assertEqual(len(pipeline.pack_context([hit])["blocks"]), 1)

    def test_ask_cli_applies_explicit_similarity_to_both_modes(self):
        seed_video(self.root, "job", timeline=[{"type": "speech", "text": "topic", "timestamp": 1.}])
        pipeline.build_index(self.root)
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]), contextlib.redirect_stdout(io.StringIO()):
            pipeline.build_embeddings(self.root, "", "offline", "local")
        for mode in ("--semantic", "--hybrid"):
            argv = ["pipeline.py", "--root", str(self.root), "--ask", "unmatched", mode,
                    "--embed-backend", "local", "--embed-model", "offline", "--min-similarity", "2", "--json"]
            output = io.StringIO()
            with self.subTest(mode=mode), mock.patch("sys.argv", argv), mock.patch.object(
                pipeline, "embed_texts", return_value=[[1., 0.]]
            ), mock.patch.object(pipeline, "minimax_subscription_key", return_value="offline"), mock.patch.object(
                pipeline, "minimax_chat", return_value='{"claims":[]}'
            ) as answer, mock.patch.object(pipeline, "load_dotenv"), contextlib.redirect_stdout(output):
                self.assertEqual(pipeline.main(), 0)
                result = json.loads(output.getvalue())
                self.assertEqual(result["evidence"], [])
                answer.assert_not_called()

    def test_degraded_ask_reranks_actual_keyword_candidates_once(self):
        for job in ("aaa", "bbb"):
            seed_video(self.root, job, timeline=[{"type": "speech", "timestamp": 1., "text": "topic " + job}])
        pipeline.build_index(self.root)
        baseline = pipeline.search_index(self.root, "topic", 10)
        requests = []

        def rerank_response(url, headers, body):
            documents = json.loads(body)["documents"]
            requests.append(documents)
            return {"results": [{"index": i, "relevance_score": i} for i in range(len(documents))]}

        with mock.patch.object(pipeline, "question_keywords", return_value=["topic"]), mock.patch.object(
            pipeline, "post_json", side_effect=rerank_response
        ), mock.patch.object(pipeline, "cost_guard", return_value=contextlib.nullcontext()), mock.patch.object(
            pipeline, "minimax_chat", return_value='{"claims":[]}'
        ):
            result = pipeline.answer_question(self.root, "topic", "offline", "offline", 10,
                                             mode="hybrid", embed_backend="local", embed_model="unavailable",
                                             rerank=True, rerank_key="offline")
        self.assertEqual([block["job"] for block in result["evidence"]], [hit["job"] for hit in reversed(baseline)])
        self.assertEqual(requests, [[hit["text"] for hit in baseline]])
        self.assertTrue(result["retrieval"]["rerank"]["applied"])
        self.assertEqual(result["retrieval"]["counts"], {"fts": 2, "vector": 0, "fused": 2})

    def test_hybrid_limit_above_default_candidate_pool(self):
        seed_video(self.root, "job", timeline=[
            {"type": "speech", "timestamp": i, "text": f"topic {i}"} for i in range(100)
        ])
        pipeline.build_index(self.root)
        self.assertEqual(len(pipeline.hybrid_search(self.root, "topic", 100)["hits"]), 100)

    def test_preparatory_progress_does_not_corrupt_json_stdout(self):
        seed_video(self.root, "job", timeline=[{"type": "speech", "text": "topic"}])
        output, errors = io.StringIO(), io.StringIO()
        argv = ["pipeline.py", "--root", str(self.root), "--index", "--embed", "--embed-plan",
                "--embed-backend", "local", "--embed-model", "offline", "--search", "topic", "--json"]
        with mock.patch("sys.argv", argv), mock.patch.object(pipeline, "load_dotenv"), mock.patch.object(
            pipeline, "embed_texts", return_value=[[1., 0.]]
        ), contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            self.assertEqual(pipeline.main(), 0)
        self.assertTrue(json.loads(output.getvalue())["hits"])
        self.assertTrue(errors.getvalue())

    def test_pipeline_persists_first_completion_and_preserves_it_on_rerun(self):
        args = pipeline.parse_args(["--root", str(self.root), "--url", "https://example.test/video", "--no-video"])
        transcript = {"segments": [{"start": 1., "end": 2., "text": "topic"}]}
        with mock.patch.dict(os.environ, {"GROQ_API_KEY": "offline", "MINIMAX_SUBSCRIPTION_KEY": "offline"}), \
             mock.patch.object(pipeline, "load_dotenv"), mock.patch.object(pipeline, "bind_cost_root"), \
             mock.patch.object(pipeline, "fetch_youtube_transcript", return_value=transcript), \
             mock.patch.object(pipeline, "create_summary", return_value={"short_summary": "topic"}), \
             mock.patch.object(pipeline, "minimax_endpoint"), \
             mock.patch("notifications.record_pipeline_completed_event"), \
             mock.patch.object(pipeline, "rss_now", return_value="2026-10-17T01:00:00Z") as now:
            path = pipeline.run_pipeline(args)
            manifest = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["first_completed_at"], now.return_value)
            now.return_value = "2026-10-18T01:00:00Z"
            pipeline.run_pipeline(args)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["first_completed_at"], manifest["first_completed_at"])
            hits = pipeline.search_index(self.root, "topic", 10, filters=pipeline.SearchFilter(
                date_field="first_completed_at", since="2026-10-17", until="2026-10-17"))
            self.assertTrue(hits)
            # A completed legacy manifest supplies no evidence of its first date.
            manifest.pop("first_completed_at")
            pipeline.write_json(path, manifest)
            pipeline.run_pipeline(args)
            self.assertNotIn("first_completed_at", json.loads(path.read_text(encoding="utf-8")))

    def test_artifact_hash_and_parsed_evidence_share_one_snapshot(self):
        for artifact in ("timeline.json", "summary.json"):
            with self.subTest(artifact=artifact):
                job = seed_video(self.root, "job", timeline=[{"type": "speech", "text": "topic alpha"}],
                                 summary={"short_summary": "topic alpha"})
                path = job / artifact
                replacement = ([{"type": "speech", "text": "topic beta"}] if artifact == "timeline.json"
                               else {"short_summary": "topic beta"})
                original_open = Path.open
                swapped = False

                @contextlib.contextmanager
                def replace_after_read(target, mode="r", *args, **kwargs):
                    nonlocal swapped
                    with original_open(target, mode, *args, **kwargs) as handle:
                        yield handle
                    if target == path and mode == "rb" and not swapped:
                        swapped = True
                        with original_open(target, "w", encoding="utf-8") as handle:
                            json.dump(replacement, handle)

                with mock.patch.object(Path, "open", replace_after_read):
                    pipeline.build_index(self.root)
                self.assertTrue(swapped)
                kind = "speech" if artifact == "timeline.json" else "summary"
                hit = pipeline.search_index(self.root, "topic", 10, filters=pipeline.SearchFilter(kinds=(kind,)))[0]
                self.assertEqual(hit["text"], "topic alpha")
                before = hit["analysis_revision"]
                pipeline.build_index(self.root)
                hit = pipeline.search_index(self.root, "topic", 10, filters=pipeline.SearchFilter(kinds=(kind,)))[0]
                self.assertEqual(hit["text"], "topic beta")
                self.assertNotEqual(hit["analysis_revision"], before)

    def test_nested_ranges_merge_before_enforcing_video_cap(self):
        hits = [dict(job="job", kind="speech", start=start, end=end, text=str(start),
                     occurrence_id=str(start)) for start, end in [(0., 100.), (5., 6.), (50., 51.)]]
        packed = pipeline.pack_context(hits, per_job_cap=1)
        self.assertEqual(len(packed["blocks"][0]["occurrence_ids"]), 3)
        self.assertEqual(packed["omitted"], 0)

    def test_malformed_rerank_payload_is_not_success(self):
        for payload in ({}, {"results": None}, {"results": ""}, {"results": {}}, {"data": False}):
            with self.subTest(payload=payload), mock.patch.object(pipeline, "post_json", return_value=payload), \
                 mock.patch.object(pipeline, "cost_guard", return_value=contextlib.nullcontext()):
                with self.assertRaises(RuntimeError):
                    pipeline.rerank_hits("topic", [{"text": "topic"}], "offline")

    def test_occurrence_schema_migration_retains_lookup_indexes(self):
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            db.execute("CREATE TABLE occurrences (occurrence_id TEXT PRIMARY KEY, job TEXT, "
                       "seq INTEGER, kind TEXT, start REAL, end REAL, timestamp_kind TEXT, "
                       "text TEXT, content_hash TEXT)")
            db.commit()
        with contextlib.closing(pipeline.index_connection(self.root)) as db:
            indexes = {row[1] for row in db.execute("PRAGMA index_list(occurrences)")}
            self.assertTrue({"occurrences_job_idx", "occurrences_hash_idx"} <= indexes)


if __name__ == "__main__":
    unittest.main()
