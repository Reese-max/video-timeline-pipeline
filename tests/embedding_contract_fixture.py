"""Explicit fictional metadata for existing offline encoders, never vendor facts.

The 1024-D encoder is test_retrieval.fake_embed (SHA/ngram recipe); the 2-D
controls intentionally emit two coordinates. A dimensional drift remains an
error against this descriptor. No real provider contract is activated.
"""
import hashlib
import inspect
import json
import unittest
from unittest import mock

import pipeline


def fictional_two_coordinates(texts):
    return [[1., 0.] for _ in texts]


def fictional_embedding_contract(backend, model, *, dimension=None):
    from test_retrieval import fake_embed
    dimension = dimension if dimension is not None else (2 if model in {
        "offline", "review-model", "memory-model", *pipeline.DEFAULT_EMBED_MODEL.values()
    } else 1024)
    encoder = fictional_two_coordinates if dimension == 2 else fake_embed
    return {"status": "KNOWN", "reason": None, "descriptor": {
        "schema": "embedding-contract/v1", "backend": backend,
        "provider": "https://fictional-embedding.invalid/" + backend, "model": model,
        "resolved_revision": hashlib.sha256(inspect.getsource(encoder).encode()).hexdigest(),
        "dimension": dimension, "preprocessing": {
            "recipe": "offline-two-coordinate/v1" if dimension == 2 else "offline-sha-ngram/v1",
            "normalize_embeddings": dimension != 2, "fixture_only": True,
        },
    }}


def seed_contract(db, backend, model, *, dimension=None):
    contract = fictional_embedding_contract(backend, model, dimension=dimension)
    key = pipeline.vector_namespace(backend, model, contract)
    descriptor = json.dumps(contract["descriptor"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    db.execute("CREATE TABLE IF NOT EXISTS vector_contracts (namespace TEXT PRIMARY KEY, descriptor TEXT NOT NULL)")
    db.execute("INSERT OR IGNORE INTO vector_contracts VALUES (?, ?)", (key, descriptor))
    return key


class FictionalContractsCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(pipeline, "embedding_contract", side_effect=fictional_embedding_contract)
        patcher.start()
        self.addCleanup(patcher.stop)
