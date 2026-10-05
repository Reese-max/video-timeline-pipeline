"""Retrieval-specific transports must stop at the shared cost fence."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cost_tracker
import pipeline
from embedding_contract_fixture import FictionalContractsCase
from test_retrieval import seed_library, seed_vectors


STRICT = {"BUDGET_HARD_STOP": "true", "DAILY_BUDGET_USD": "1"}
OPEN = {"BUDGET_HARD_STOP": "false", "DAILY_BUDGET_USD": "100"}


class RetrievalCostGuardTests(FictionalContractsCase):
    def test_rerank_guard_rejects_before_transport(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, STRICT), cost_tracker.cost_root_scope(Path(temporary)):
            with mock.patch.object(pipeline, "post_json", return_value={"results": [{"index": 0}]}) as transport:
                with self.assertRaises(cost_tracker.BudgetBlockedError):
                    pipeline.rerank_hits("Q", [{"text": "證據"}], "fake", "test-reranker")
                transport.assert_not_called()

    def test_keyword_budget_rejection_is_visible_and_cannot_become_local_fallback(self):
        with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(os.environ, STRICT), cost_tracker.cost_root_scope(Path(temporary)):
            with mock.patch.object(pipeline, "post_json") as transport:
                with self.assertRaises(cost_tracker.BudgetBlockedError):
                    pipeline.question_keywords("Q", "fake", "test-model")
                transport.assert_not_called()

    def test_query_embedding_budget_rejection_cannot_trigger_another_provider(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            seed_vectors(root, pipeline.vector_namespace("openrouter", "test-embed"))
            with mock.patch.dict(os.environ, STRICT), cost_tracker.cost_root_scope(root):
                with mock.patch.object(pipeline, "post_json") as transport, mock.patch.object(pipeline, "question_keywords") as keywords:
                    with self.assertRaises(cost_tracker.BudgetBlockedError):
                        pipeline.answer_question(root, "時間軸", "fake", "model", 8, mode="hybrid",
                                                 embed_key="fake", embed_model="test-embed", embed_backend="openrouter")
                    transport.assert_not_called()
                    keywords.assert_not_called()

    def test_optional_rerank_budget_rejection_cannot_be_swallowed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            with mock.patch.dict(os.environ, STRICT), cost_tracker.cost_root_scope(root), mock.patch.object(pipeline, "post_json") as transport:
                with self.assertRaises(cost_tracker.BudgetBlockedError):
                    pipeline.hybrid_search(root, "時間軸", 8, rerank=True, rerank_key="fake")
                transport.assert_not_called()

    def test_answer_guard_rejects_before_transport_after_local_retrieval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_library(root)
            with mock.patch.dict(os.environ, STRICT), cost_tracker.cost_root_scope(root):
                with mock.patch.object(pipeline, "question_keywords", return_value=["時間軸"]), mock.patch.object(pipeline, "post_json") as transport:
                    with self.assertRaises(cost_tracker.BudgetBlockedError):
                        pipeline.answer_question(root, "Q", "fake", "model", 8)
                    transport.assert_not_called()

    def test_rerank_attempt_is_recorded_once_and_unknown_timeout_is_preserved(self):
        for response in ({"results": [{"index": 0, "relevance_score": 1.0}]}, TimeoutError("unknown result")):
            with self.subTest(response=type(response).__name__), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                with mock.patch.dict(os.environ, OPEN), cost_tracker.cost_root_scope(root):
                    with mock.patch.object(pipeline, "post_json", side_effect=response if isinstance(response, Exception) else None,
                                           return_value=response) as transport:
                        if isinstance(response, Exception):
                            with self.assertRaises(TimeoutError):
                                pipeline.rerank_hits("Q", [{"text": "證據"}], "fake", "test-reranker")
                        else:
                            self.assertEqual(len(pipeline.rerank_hits("Q", [{"text": "證據"}], "fake", "test-reranker")), 1)
                        self.assertEqual(transport.call_count, 1)
                with cost_tracker.closing(cost_tracker.get_operations_db(root)) as db:
                    rows = db.execute("SELECT provider, operation, status FROM usage_costs").fetchall()
                    reservations = db.execute("SELECT status FROM budget_reservations").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(tuple(rows[0]), ("openrouter", "rerank", "unknown" if isinstance(response, Exception) else "succeeded"))
                self.assertEqual(len(reservations), 1)
                self.assertEqual(reservations[0][0], "reserved" if isinstance(response, Exception) else "settled")


class RerankResponseContractTests(unittest.TestCase):
    def test_invalid_provider_ranking_is_not_applied(self):
        hits = [{"text": "第一來源"}, {"text": "第二來源"}]
        invalid = [
            [],
            {"results": "not an array"},
            {"results": [{"index": True, "relevance_score": 1.0}]},
            {"results": [{"index": 0, "relevance_score": float("nan")}]},
            {"results": [{"index": 0, "relevance_score": float("inf")}]},
            {"results": [{"index": 0, "relevance_score": 10 ** 500}]},
            {"results": [{"index": 0, "relevance_score": True}]},
            {"results": [{"index": 0, "relevance_score": "high"}]},
        ]
        with mock.patch.dict(os.environ, OPEN), cost_tracker.cost_root_scope(None):
            for payload in invalid:
                with self.subTest(payload=payload), mock.patch.object(pipeline, "post_json", return_value=payload):
                    with self.assertRaises(RuntimeError):
                        pipeline.rerank_hits("Q", hits, "fake", "test-reranker")


if __name__ == "__main__":
    unittest.main()
