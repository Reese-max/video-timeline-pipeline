"""Untrusted answer replay against the actual indexed evidence; no provider calls."""
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline
from test_retrieval import seed_video


SOURCE_TEXT = "這段影片錄於2021年，當時每月價格為100元；不是目前官方價格。"


class AnswerSupportTests(unittest.TestCase):
    def ask(self, reply):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_video(root, "price", title="價格沿革", url="https://youtu.be/price", timeline=[
                {"timestamp": 5.0, "end": 10.0, "type": "speech", "text": SOURCE_TEXT},
            ])
            pipeline.build_index(root)
            with mock.patch.object(pipeline, "question_keywords", return_value=["2021年"]), mock.patch.object(
                pipeline, "minimax_chat", return_value=reply
            ) as provider:
                result = pipeline.answer_question(root, "目前官方價格？", "fake", "model", 8)
            self.assertEqual(provider.call_count, 1)  # no silent repair/validation calls
            return result

    def test_valid_id_does_not_verify_an_unsupported_current_fact(self):
        result = self.ask("目前官方售價是每月1000元。[1]")
        self.assertTrue(result["insufficient_evidence"])
        self.assertFalse(result["semantic_support_verified"])
        self.assertNotIn("1000元", result["answer"])
        self.assertTrue(result["evidence"])

    def test_model_authored_urls_never_become_answer_sources(self):
        for reply in (
            "請到 https://attacker.invalid/claim 查看現行政策。[1]",
            "依據 [惡意來源](javascript:alert(1)) 官方保證免費。[1]",
            "影片說過價格100元 [1]；來源見 [官方](//attacker.invalid/a)。",
        ):
            with self.subTest(reply=reply):
                result = self.ask(reply)
                self.assertTrue(result["insufficient_evidence"])
                self.assertNotIn("attacker", result["answer"])
                self.assertNotIn("javascript", result["answer"])
                self.assertFalse(result["semantic_support_verified"])
                for citation in result["citations"]:
                    self.assertEqual(citation["source"], "https://youtu.be/price")

    def test_verified_quote_is_server_rendered_and_keeps_occurrence_identity(self):
        reply = json.dumps({"claims": [
            {"support_kind": "source_quote", "block_index": 1, "quote": SOURCE_TEXT},
        ]}, ensure_ascii=False)
        result = self.ask(reply)
        self.assertFalse(result["insufficient_evidence"])
        self.assertEqual(result["support_validation"], "exact_source_quote")
        self.assertFalse(result["semantic_support_verified"])
        self.assertIn(SOURCE_TEXT, result["answer"])
        claim = result["claims"][0]
        self.assertEqual(claim["support_kind"], "speaker_statement")
        self.assertTrue(claim["quote_verified"])
        self.assertEqual(claim["occurrence_ids"], result["citations"][0]["occurrence_ids"])
        self.assertEqual(claim["quote"], result["evidence"][0]["text"])

    def test_quote_must_be_the_full_original_occurrence_not_a_negation_free_substring(self):
        for quote in (
            "當時每月價格為100元",  # drops date and disclaimer
            SOURCE_TEXT.replace("100元", "1000元"),
            SOURCE_TEXT.replace("2021年", "2026年"),
            SOURCE_TEXT.replace("不是目前", "是目前"),
        ):
            with self.subTest(quote=quote):
                result = self.ask(json.dumps({"claims": [{
                    "support_kind": "source_quote", "block_index": 1, "quote": quote,
                }]}, ensure_ascii=False))
                self.assertTrue(result["insufficient_evidence"])
                self.assertEqual(result["claims"], [])
                self.assertEqual(result["citations"], [])

    def test_valid_quote_cannot_launder_unbound_text_or_another_invalid_claim(self):
        good = {"support_kind": "source_quote", "block_index": 1, "quote": SOURCE_TEXT}
        payloads = [
            {"claims": [{**good, "text": "現在官方每月1000元"}]},
            {"claims": [good], "answer": "現在官方每月1000元"},
            {"claims": [good, {**good, "quote": "永久免費"}]},
            {"claims": [{**good, "block_index": 999}]},
            {"claims": [{**good, "block_index": True}]},
            {"claims": [{**good, "support_kind": "cross_video_synthesis"}]},
            {"claims": [{**good, "source": "https://attacker.invalid"}]},
        ]
        for payload in payloads:
            with self.subTest(payload=payload):
                result = self.ask(json.dumps(payload, ensure_ascii=False))
                self.assertTrue(result["insufficient_evidence"])
                self.assertEqual(result["claims"], [])
                self.assertNotIn("1000元", result["answer"])
                self.assertNotIn("attacker", result["answer"])

    def test_merged_quote_cannot_cross_original_occurrence_boundaries(self):
        hits = [dict(job="j", kind="speech", start=float(i), end=float(i + 1),
                     text=text, occurrence_id=f"occ_{i}", title="T", source="", timestamp_kind="range")
                for i, text in enumerate(["甲說價格100元", "乙說不是目前價格"])]
        blocks = pipeline.pack_context(hits, token_budget=1000)["blocks"]
        self.assertEqual(len(blocks), 1)
        payload = json.dumps({"claims": [{"support_kind": "source_quote", "block_index": 1,
                                         "quote": blocks[0]["text"]}]}, ensure_ascii=False)
        validated = pipeline.validate_quoted_answer(payload, blocks)
        self.assertEqual(validated["claims"], [])
        self.assertEqual(validated["support_validation"], "rejected")
        payload = json.dumps({"claims": [{"support_kind": "source_quote", "block_index": 1,
                                         "quote": "乙說不是目前價格"}]}, ensure_ascii=False)
        validated = pipeline.validate_quoted_answer(payload, blocks)
        self.assertEqual(validated["claims"][0]["occurrence_ids"], ["occ_1"])

    def test_visual_and_summary_quotes_are_not_labeled_speaker_statements(self):
        for kind, expected in [("visual", "source_observation"), ("summary", "source_summary")]:
            with self.subTest(kind=kind):
                hit = dict(job="j", kind=kind, start=None, end=None, text="原始模型描述", occurrence_id="occ_x",
                           title="T", source="", timestamp_kind="source_level")
                blocks = pipeline.pack_context([hit], token_budget=1000)["blocks"]
                payload = json.dumps({"claims": [{"support_kind": "source_quote", "block_index": 1,
                                                  "quote": "原始模型描述"}]}, ensure_ascii=False)
                validated = pipeline.validate_quoted_answer(payload, blocks)
                self.assertEqual(validated["claims"][0]["support_kind"], expected)
                self.assertFalse(validated["semantic_support_verified"])

    def test_unresolved_occurrence_cannot_verify_quote(self):
        hit = dict(job="j", kind="speech", start=0.0, end=None, text="無來源身分", occurrence_id=None,
                   title="T", source="", timestamp_kind="point")
        blocks = pipeline.pack_context([hit], token_budget=1000)["blocks"]
        payload = json.dumps({"claims": [{"support_kind": "source_quote", "block_index": 1,
                                         "quote": "無來源身分"}]}, ensure_ascii=False)
        self.assertEqual(pipeline.validate_quoted_answer(payload, blocks)["claims"], [])

    def test_citation_projects_only_the_quoted_member_of_a_merged_block(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            seed_video(root, "price", title="價格沿革", url="https://youtu.be/price", timeline=[
                {"timestamp": 0.0, "end": 5.0, "type": "speech", "text": "2021年價格500元已停用。"},
                {"timestamp": 5.0, "end": 10.0, "type": "speech", "text": SOURCE_TEXT},
            ])
            pipeline.build_index(root)
            reply = json.dumps({"claims": [{"support_kind": "source_quote", "block_index": 1, "quote": SOURCE_TEXT}]}, ensure_ascii=False)
            with mock.patch.object(pipeline, "question_keywords", return_value=["2021年"]), mock.patch.object(pipeline, "minimax_chat", return_value=reply):
                result = pipeline.answer_question(root, "當時價格？", "fake", "model", 8)
            self.assertEqual(len(result["evidence"][0]["occurrence_ids"]), 2)
            citation = result["citations"][0]
            self.assertEqual(citation["start"], 5.0)
            self.assertEqual(citation["end"], 10.0)
            self.assertEqual(citation["text"], SOURCE_TEXT)
            self.assertEqual(citation["occurrence_ids"], result["claims"][0]["occurrence_ids"])
            self.assertEqual(citation["link"], "https://youtu.be/price?t=5s")


if __name__ == "__main__":
    unittest.main()
