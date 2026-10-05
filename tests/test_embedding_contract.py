"""Offline actual-index controls; no provider/model invocation or rights claims."""

import contextlib
import copy
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from types import SimpleNamespace

import pipeline
from test_retrieval import seed_video
from embedding_contract_fixture import fictional_embedding_contract


class UnknownEmbeddingContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        seed_video(self.root, "job", timeline=[{"type": "speech", "text": "actual quoted sentence"}])
        pipeline.build_index(self.root)
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            db.execute("INSERT INTO vectors VALUES (?, 'openrouter::unresolved', 2, ?)",
                       (pipeline.text_hash("actual quoted sentence"), pipeline.pack_vector([1., 0.])))
            db.commit()

    def test_unknown_revision_cache_does_not_silently_reuse_or_call_provider(self):
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]) as embed:
            with self.assertRaisesRegex(RuntimeError, "contract|契約"):
                pipeline.build_embeddings(self.root, "unused", "unresolved", "openrouter")
        embed.assert_not_called()

    def test_unknown_revision_query_falls_back_without_provider_or_ddl(self):
        before = (self.root / "library.db").read_bytes()
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]) as embed:
            result = pipeline.hybrid_search(self.root, "actual quoted", 10,
                                            embed_key="unused", embed_model="unresolved", embed_backend="openrouter")
        self.assertEqual(result["retrieval_mode"], "fts")
        self.assertEqual(result["degraded_reason"], "embedding_contract_unknown")
        self.assertTrue(result["hits"])
        embed.assert_not_called()
        self.assertEqual(before, (self.root / "library.db").read_bytes())

    def test_unknown_plan_strict_query_rebuild_and_readiness_are_honest(self):
        before = (self.root / "library.db").read_bytes()
        with mock.patch.object(pipeline, "embed_texts") as embed, contextlib.redirect_stdout(io.StringIO()):
            plan = pipeline.plan_embeddings(self.root, "openrouter", "unresolved")
            self.assertEqual(plan["reusable"], 0)
            self.assertIsNone(plan["est_calls"])
            self.assertEqual(plan["quarantined_vectors"], 1)
            self.assertEqual(plan["blocked_reason"], "embedding_contract_unknown")
            for operation in (
                lambda: pipeline.rebuild_embeddings(self.root, "unused", "unresolved"),
                lambda: pipeline.semantic_search(self.root, "quoted", "unused", "unresolved", 10),
                lambda: pipeline.hybrid_search(self.root, "quoted", 10, embed_key="unused", embed_model="unresolved", embed_backend="openrouter", strict=True),
            ):
                with self.subTest(operation=operation), self.assertRaisesRegex(RuntimeError, "contract|契約"):
                    operation()
            self.assertTrue(any("UNKNOWN" in issue for issue in pipeline.audit_library(self.root)))
        embed.assert_not_called()
        self.assertEqual(before, (self.root / "library.db").read_bytes())


class KnownEmbeddingContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        seed_video(self.root, "one", timeline=[{"type": "speech", "text": "shared quoted text"}])
        seed_video(self.root, "two", timeline=[{"type": "speech", "text": "shared quoted text"}])
        pipeline.build_index(self.root)
        self.contract = fictional_embedding_contract("local", "offline")
        patcher = mock.patch.object(pipeline, "embedding_contract", side_effect=lambda backend, model: copy.deepcopy(self.contract))
        patcher.start(); self.addCleanup(patcher.stop)

    def build(self):
        dim = self.contract["descriptor"]["dimension"]
        with mock.patch.object(pipeline, "embed_texts", side_effect=lambda texts, *args: [[1.] + [0.] * (dim - 1) for _ in texts]) as embed, contextlib.redirect_stdout(io.StringIO()):
            result = pipeline.build_embeddings(self.root, "unused", self.contract["descriptor"]["model"], self.contract["descriptor"]["backend"])
        return result, embed.call_count

    def test_full_contract_canonicalization_and_every_identity_axis(self):
        key = pipeline.vector_namespace("local", "offline", self.contract)
        reordered = {**self.contract, "descriptor": dict(reversed(list(self.contract["descriptor"].items())))}
        self.assertEqual(key, pipeline.vector_namespace("local", "offline", reordered))
        for field, value in (("backend", "openrouter"), ("provider", "https://different.invalid"),
                             ("model", "other"), ("resolved_revision", "a" * 40),
                             ("dimension", 3), ("preprocessing", {"recipe": "different/v2"})):
            changed = copy.deepcopy(self.contract); changed["descriptor"][field] = value
            with self.subTest(field=field):
                self.assertNotEqual(key, pipeline.vector_namespace(changed["descriptor"]["backend"], changed["descriptor"]["model"], changed))

    def test_same_text_contract_reuses_once_and_occurrences_stay_separate(self):
        self.assertEqual(self.build(), ((1, 1), 1))
        self.assertEqual(self.build(), ((0, 1), 0))
        self.assertEqual(len(pipeline.search_index(self.root, "shared quoted", 10)), 2)
        before = (self.root / "library.db").read_bytes()
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]) as embed:
            hits = pipeline.semantic_search(self.root, "quoted", "unused", "offline", 10, "local")
        self.assertEqual(len(hits), 2); embed.assert_called_once()
        self.assertEqual(before, (self.root / "library.db").read_bytes())

    def test_revision_dimension_preprocessing_model_and_provider_changes_do_not_reuse(self):
        self.build()
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            original = db.execute("SELECT * FROM vectors").fetchall()[0]
        for field, value in (("resolved_revision", "b" * 40), ("dimension", 3),
                             ("preprocessing", {"recipe": "offline-new/v2"}),
                             ("model", "renamed"), ("provider", "https://another.invalid")):
            self.contract["descriptor"][field] = value
            with self.subTest(field=field):
                plan = pipeline.plan_embeddings(self.root, "local", self.contract["descriptor"]["model"])
                self.assertEqual(plan["reusable"], 0)
                self.assertEqual(plan["pending"], 1)
                self.assertEqual(self.build(), ((1, 1), 1))
                with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
                    self.assertIn(original, db.execute("SELECT * FROM vectors").fetchall())

    def test_unproven_legacy_rows_preserved_and_no_implicit_rebill(self):
        old = (pipeline.text_hash("shared quoted text"), "local::offline", 2, pipeline.pack_vector([1., 0.]))
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            db.execute("INSERT INTO vectors VALUES (?, ?, ?, ?)", old); db.commit()
        with mock.patch.object(pipeline, "embed_texts") as embed:
            plan = pipeline.plan_embeddings(self.root, "local", "offline")
            self.assertEqual(plan["blocked_reason"], "legacy_embedding_contract_unknown")
            self.assertIsNone(plan["est_calls"])
            with self.assertRaisesRegex(RuntimeError, "legacy embedding contract UNKNOWN"):
                pipeline.build_embeddings(self.root, "unused", "offline", "local")
        embed.assert_not_called()
        with mock.patch.object(pipeline, "embed_texts", return_value=[[1., 0.]]), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(pipeline.rebuild_embeddings(self.root, "unused", "offline", "local")["added"], 1)
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            self.assertIn(old, db.execute("SELECT * FROM vectors").fetchall())

    def test_missing_or_tampered_contract_never_promoted_from_namespace(self):
        self.build()
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            key, descriptor = db.execute("SELECT * FROM vector_contracts").fetchone()
        for mutation in ("DELETE FROM vector_contracts", "UPDATE vector_contracts SET descriptor='{}'"):
            with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
                db.execute(mutation); db.commit()
            before = (self.root / "library.db").read_bytes()
            with mock.patch.object(pipeline, "embed_texts") as embed:
                for operation in (lambda: pipeline.build_embeddings(self.root, "", "offline", "local"),
                                  lambda: pipeline.rebuild_embeddings(self.root, "", "offline", "local"),
                                  lambda: pipeline.semantic_search(self.root, "quoted", "", "offline", 10, "local")):
                    with self.subTest(mutation=mutation), self.assertRaises(RuntimeError):
                        operation()
            embed.assert_not_called()
            self.assertEqual(before, (self.root / "library.db").read_bytes())
            with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
                db.execute("INSERT OR REPLACE INTO vector_contracts VALUES (?, ?)", (key, descriptor)); db.commit()

    def test_query_document_contract_and_dimension_mismatch_before_compare_or_call(self):
        self.build()
        key = pipeline.vector_namespace("local", "offline", self.contract)
        changed = copy.deepcopy(self.contract); changed["descriptor"]["resolved_revision"] = "c" * 40
        with contextlib.closing(pipeline.read_connection(self.root)) as db, mock.patch.object(pipeline, "cosine") as cosine:
            with self.assertRaisesRegex(RuntimeError, "mismatch"):
                pipeline._vector_candidates(db, [1., 0.], key, pipeline.SearchFilter(), 10, contract=changed)
            cosine.assert_not_called()
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            db.execute("UPDATE vectors SET dim=3"); db.commit()
        with mock.patch.object(pipeline, "embed_texts") as embed:
            result = pipeline.hybrid_search(self.root, "quoted", 10, embed_model="offline", embed_backend="local")
            self.assertEqual(result["degraded_reason"], "embedding_contract_invalid")
            self.assertTrue(result["hits"])
            with self.assertRaisesRegex(RuntimeError, "dimension"):
                pipeline.semantic_search(self.root, "quoted", "", "offline", 10, "local")
        embed.assert_not_called()

    def test_contract_drift_during_generation_and_query_is_rejected(self):
        def drift(texts, *args):
            self.contract["descriptor"]["resolved_revision"] = "d" * 40
            return [[1., 0.] for _ in texts]
        with mock.patch.object(pipeline, "embed_texts", side_effect=drift):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                pipeline.build_embeddings(self.root, "", "offline", "local")
        with contextlib.closing(sqlite3.connect(self.root / "library.db")) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM vectors").fetchone()[0], 0)
        self.build()
        with mock.patch.object(pipeline, "embed_texts", side_effect=lambda texts, *args: (self.contract["descriptor"]["preprocessing"].update(recipe="drift/v3") or [[1., 0.]])):
            with self.assertRaisesRegex(RuntimeError, "changed"):
                pipeline.semantic_search(self.root, "quoted", "", "offline", 10, "local")

    def test_malformed_identity_and_preprocessing_fail_before_calls(self):
        for field, value in (("dimension", True), ("dimension", 0), ("resolved_revision", "requested-main"),
                             ("resolved_revision", None), ("preprocessing", {}),
                             ("preprocessing", {"recipe": "bad", "value": float("nan")}),
                             ("preprocessing", {"recipe": "bad", 1: "integer-key"})):
            original = copy.deepcopy(self.contract)
            self.contract["descriptor"][field] = value
            with self.subTest(field=field, value=value), mock.patch.object(pipeline, "embed_texts") as embed:
                with self.assertRaises(RuntimeError):
                    pipeline.build_embeddings(self.root, "unused", "offline", "local")
                embed.assert_not_called()
            self.contract = original


class LoadedLocalMetadataTests(unittest.TestCase):
    def encoder(self):
        class Transformer:
            def get_config_dict(self):
                return {"max_seq_length": 128, "do_lower_case": False}
        Transformer.__module__ = "sentence_transformers.models.Transformer"
        transformer = Transformer()
        transformer.backend = "torch"
        revision = hashlib.sha256(b"explicit fictional local snapshot").hexdigest()
        transformer.auto_model = SimpleNamespace(config=SimpleNamespace(_commit_hash=revision), dtype="torch.float32")
        tokenizer_state = {"model": {"type": "WordLevel", "vocab": {"fictional": 0}}, "added_tokens": []}
        transformer.tokenizer = SimpleNamespace(init_kwargs={"_commit_hash": revision},
            is_fast=True, truncation_side="right", padding_side="right", special_tokens_map={},
            split_special_tokens=False, model_input_names=["input_ids", "attention_mask"],
            backend_tokenizer=SimpleNamespace(to_str=lambda: json.dumps(tokenizer_state)),
            fictional_state=tokenizer_state)
        return SimpleNamespace(_modules={"0": transformer}, get_sentence_embedding_dimension=lambda: 2,
                               max_seq_length=128, prompts={}, default_prompt_name=None,
                               truncate_dim=None, device="cpu",
                               encode=lambda texts, **kwargs: [SimpleNamespace(tolist=lambda: [1., 0.]) for _ in texts])

    def observe(self, encoder):
        with mock.patch.dict(pipeline._LOCAL_EMBEDDERS, {"requested-alias": encoder}, clear=True), mock.patch.object(pipeline.importlib.metadata, "version", return_value="explicit-fictional-runtime-1"):
            return pipeline.embedding_contract("local", "requested-alias")

    def test_loaded_resolved_revision_and_actual_dimension_preprocessing(self):
        encoder = self.encoder(); contract = self.observe(encoder)
        self.assertEqual(contract["status"], "KNOWN")
        descriptor = contract["descriptor"]
        self.assertEqual(descriptor["resolved_revision"], encoder._modules["0"].auto_model.config._commit_hash)
        self.assertNotEqual(descriptor["resolved_revision"], "requested-alias")
        self.assertEqual(descriptor["dimension"], 2)
        self.assertEqual(descriptor["preprocessing"]["max_seq_length"], 128)
        self.assertFalse(descriptor["preprocessing"]["normalize_embeddings"])
        previous = pipeline.vector_namespace("local", "requested-alias", contract)
        encoder.prompts = {"query": "prefix: "}; encoder.default_prompt_name = "query"
        self.assertNotEqual(previous, pipeline.vector_namespace("local", "requested-alias", self.observe(encoder)))

    def test_incomplete_metadata_revision_and_unknown_weighted_modules_refuse(self):
        cases = ("missing_revision", "tokenizer_drift", "dimension_bool", "missing_module_config", "unknown_weighted_module", "unknown_prompt", "nonfinite_config")
        for case in cases:
            encoder = self.encoder(); transformer = encoder._modules["0"]
            if case == "missing_revision": transformer.auto_model.config._commit_hash = None
            elif case == "tokenizer_drift": transformer.tokenizer.init_kwargs["_commit_hash"] = "f" * 40
            elif case == "dimension_bool": encoder.get_sentence_embedding_dimension = lambda: True
            elif case == "missing_module_config": transformer.get_config_dict = lambda: None
            elif case == "unknown_weighted_module": encoder._modules["1"] = SimpleNamespace()
            elif case == "unknown_prompt": encoder.default_prompt_name = "unknown"
            else: transformer.get_config_dict = lambda: {"max_seq_length": float("nan")}
            with self.subTest(case=case):
                self.assertEqual(self.observe(encoder)["status"], "UNKNOWN")

    def test_configuration_only_pooling_and_parameterless_normalize_are_bound(self):
        class Pooling:
            def get_config_dict(self): return {"pooling_mode_mean_tokens": True, "word_embedding_dimension": 2}
        class Normalize: pass
        Pooling.__module__ = "sentence_transformers.models.Pooling"
        Normalize.__module__ = "sentence_transformers.models.Normalize"
        encoder = self.encoder(); encoder._modules.update({"1": Pooling(), "2": Normalize()})
        contract = self.observe(encoder)
        self.assertEqual(contract["status"], "KNOWN")
        self.assertEqual(contract["descriptor"]["preprocessing"]["modules"][-1]["config"], {})

    def test_cold_local_and_cloud_never_load_encode_or_probe(self):
        with mock.patch.dict(pipeline._LOCAL_EMBEDDERS, {}, clear=True), mock.patch.object(pipeline, "embed_texts_local") as local, mock.patch.object(pipeline, "post_json") as post:
            self.assertEqual(pipeline.embedding_contract("local", "not-loaded")["status"], "UNKNOWN")
            self.assertEqual(pipeline.embedding_contract("openrouter", "requested-pin")["status"], "UNKNOWN")
        local.assert_not_called(); post.assert_not_called()

    def test_adapter_weights_and_alternate_backends_cannot_reuse_base_revision(self):
        for mode in ("peft", "loaded_flag", "active_adapter", "active_adapters", "onnx", "openvino", "missing_backend"):
            encoder = self.encoder(); transformer = encoder._modules["0"]
            if mode == "peft":
                transformer.auto_model.peft_config = {"default": SimpleNamespace(revision="b" * 40)}
            elif mode == "loaded_flag": transformer.auto_model._hf_peft_config_loaded = True
            elif mode == "active_adapter": transformer.auto_model.active_adapter = "default"
            elif mode == "active_adapters": transformer.auto_model.active_adapters = ["default"]
            elif mode == "missing_backend": del transformer.backend
            else: transformer.backend = mode
            with self.subTest(mode=mode):
                self.assertEqual(self.observe(encoder)["status"], "UNKNOWN")
                with tempfile.TemporaryDirectory() as temp, mock.patch.dict(pipeline._LOCAL_EMBEDDERS, {"requested-alias": encoder}, clear=True), mock.patch.object(pipeline.importlib.metadata, "version", return_value="explicit-fictional-runtime-1"), mock.patch.object(pipeline, "embed_texts") as embed:
                    root = Path(temp); seed_video(root, "one", timeline=[{"type": "speech", "text": "adapter quote"}]); pipeline.build_index(root)
                    with self.assertRaisesRegex(RuntimeError, "contract UNKNOWN"):
                        pipeline.build_embeddings(root, "", "requested-alias", "local")
                    embed.assert_not_called()
                    with contextlib.closing(sqlite3.connect(root / "library.db")) as db:
                        self.assertEqual(db.execute("SELECT count(*) FROM vectors").fetchone()[0], 0)

    def test_actual_tokenizer_mutations_do_not_reuse_unchanged_commit_vectors(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); seed_video(root, "one", timeline=[{"type": "speech", "text": "tokenizer quote"}]); pipeline.build_index(root)
            encoder = self.encoder(); tokenizer = encoder._modules["0"].tokenizer
            mutations = (
                lambda: setattr(tokenizer, "truncation_side", "left"),
                lambda: setattr(tokenizer, "padding_side", "left"),
                lambda: tokenizer.fictional_state["added_tokens"].append({"id": 1, "content": "extra", "special": True}),
                lambda: tokenizer.fictional_state.update(normalizer={"type": "Lowercase"}),
                lambda: tokenizer.fictional_state.update(pre_tokenizer={"type": "Whitespace"}),
                lambda: tokenizer.fictional_state.update(post_processor={"type": "ByteLevel"}),
                lambda: tokenizer.special_tokens_map.update(pad_token="[fictional-pad]"),
                lambda: setattr(tokenizer, "split_special_tokens", True),
                lambda: tokenizer.model_input_names.append("token_type_ids"),
            )
            with mock.patch.dict(pipeline._LOCAL_EMBEDDERS, {"requested-alias": encoder}, clear=True), mock.patch.object(pipeline.importlib.metadata, "version", return_value="explicit-fictional-runtime-1"), mock.patch.object(pipeline, "post_json", side_effect=AssertionError("provider forbidden")) as post, contextlib.redirect_stdout(io.StringIO()):
                previous = pipeline.vector_namespace("local", "requested-alias")
                self.assertEqual(pipeline.build_embeddings(root, "", "requested-alias", "local"), (1, 1))
                for mutate in mutations:
                    mutate()
                    current = pipeline.vector_namespace("local", "requested-alias")
                    self.assertNotEqual(previous, current)
                    self.assertEqual(pipeline.plan_embeddings(root, "local", "requested-alias")["reusable"], 0)
                    self.assertEqual(pipeline.build_embeddings(root, "", "requested-alias", "local"), (1, 1))
                    previous = current
                tokenizer.fictional_state.update(padding={"length": 17}, truncation={"max_length": 19})
                self.assertEqual(previous, pipeline.vector_namespace("local", "requested-alias"))
                self.assertEqual(pipeline.build_embeddings(root, "", "requested-alias", "local"), (0, 1))
            post.assert_not_called()

    def test_incomplete_actual_tokenizer_state_refuses_before_embedding(self):
        for mode in ("slow", "missing_side", "invalid_side", "missing_backend", "malformed_json", "missing_model", "missing_added_tokens", "missing_split_flag", "nonbool_split_flag", "missing_input_names"):
            encoder = self.encoder(); tokenizer = encoder._modules["0"].tokenizer
            if mode == "slow": tokenizer.is_fast = False
            elif mode == "missing_side": del tokenizer.truncation_side
            elif mode == "invalid_side": tokenizer.padding_side = "unknown"
            elif mode == "missing_backend": del tokenizer.backend_tokenizer
            elif mode == "malformed_json": tokenizer.backend_tokenizer.to_str = lambda: "not json"
            elif mode == "missing_model": tokenizer.fictional_state.pop("model")
            elif mode == "missing_added_tokens": tokenizer.fictional_state.pop("added_tokens")
            elif mode == "missing_split_flag": del tokenizer.split_special_tokens
            elif mode == "nonbool_split_flag": tokenizer.split_special_tokens = "false"
            else: del tokenizer.model_input_names
            with self.subTest(mode=mode), mock.patch.dict(pipeline._LOCAL_EMBEDDERS, {"requested-alias": encoder}, clear=True), mock.patch.object(pipeline.importlib.metadata, "version", return_value="explicit-fictional-runtime-1"), mock.patch.object(pipeline, "embed_texts") as embed:
                self.assertEqual(self.observe(encoder)["status"], "UNKNOWN")
                with tempfile.TemporaryDirectory() as temp:
                    root = Path(temp); seed_video(root, "one", timeline=[{"type": "speech", "text": "unknown tokenizer"}]); pipeline.build_index(root)
                    with self.assertRaisesRegex(RuntimeError, "contract UNKNOWN"):
                        pipeline.build_embeddings(root, "", "requested-alias", "local")
                embed.assert_not_called()

    def test_actual_loaded_metadata_assembler_and_reuse_without_provider(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            seed_video(root, "one", timeline=[{"type": "speech", "text": "actual local quote"}])
            pipeline.build_index(root)
            encoder = self.encoder()
            with mock.patch.dict(pipeline._LOCAL_EMBEDDERS, {"requested-alias": encoder}, clear=True), mock.patch.object(pipeline.importlib.metadata, "version", return_value="explicit-fictional-runtime-1"), mock.patch.object(pipeline, "post_json", side_effect=AssertionError("provider forbidden")) as post, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(pipeline.build_embeddings(root, "", "requested-alias", "local"), (1, 1))
                self.assertEqual(pipeline.build_embeddings(root, "", "requested-alias", "local"), (0, 1))
                self.assertEqual(len(pipeline.semantic_search(root, "quote", "", "requested-alias", 10, "local")), 1)
                self.assertEqual(pipeline.plan_embeddings(root, "local", "requested-alias")["reusable"], 1)
                # Losing the observed resolved revision blocks this same cache.
                encoder._modules["0"].auto_model.config._commit_hash = None
                self.assertEqual(pipeline.plan_embeddings(root, "local", "requested-alias")["reusable"], 0)
                result = pipeline.hybrid_search(root, "local quote", 10, embed_model="requested-alias", embed_backend="local")
                self.assertEqual(result["degraded_reason"], "embedding_contract_unknown")
            post.assert_not_called()


if __name__ == "__main__":
    unittest.main()
