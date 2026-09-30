"""Regression contracts for the cost guard at provider transport boundaries."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cost_tracker
import pipeline


class TransportCostGuardTests(unittest.TestCase):
    def test_allowed_transport_is_recorded_before_next_budget_decision(self) -> None:
        class Response:
            headers = {}

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok": true}'

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DAILY_BUDGET_USD": "0.001", "BUDGET_HARD_STOP": "true"},
        ), patch.object(pipeline.urllib.request, "urlopen", return_value=Response()) as urlopen:
            root = Path(directory)
            self.assertEqual(
                pipeline.post_json(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    {},
                    b"{}",
                    timeout=1,
                    cost_root=root,
                ),
                {"ok": True},
            )
            with self.assertRaises(cost_tracker.BudgetBlockedError):
                pipeline.post_json(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    {},
                    b"{}",
                    timeout=1,
                    cost_root=root,
                )

            with cost_tracker.get_operations_db(root) as connection:
                usage = connection.execute(
                    "SELECT provider, status FROM usage_costs WHERE provider = 'groq'"
                ).fetchall()
                reservations = connection.execute(
                    "SELECT status FROM budget_reservations"
                ).fetchall()
            self.assertEqual([(row[0], row[1]) for row in usage], [("groq", "succeeded")])
            self.assertEqual([row[0] for row in reservations], ["settled"])
            urlopen.assert_called_once()

    def test_hard_budget_blocks_every_paid_json_provider_before_transport(self) -> None:
        providers = (
            ("groq", "https://api.groq.com/openai/v1/audio/transcriptions"),
            ("minimax", "https://api.minimax.io/v1/chat/completions"),
            ("openrouter", "https://openrouter.ai/api/v1/embeddings"),
            ("apify", "https://api.apify.com/v2/actors/user~actor/runs"),
            ("brightdata", "https://api.brightdata.com/datasets/v3/trigger"),
        )

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DAILY_BUDGET_USD": "0.000001", "BUDGET_HARD_STOP": "true"},
        ), patch.object(pipeline.urllib.request, "urlopen") as urlopen:
            root = Path(directory)
            for provider, url in providers:
                with self.subTest(provider=provider):
                    with self.assertRaises(cost_tracker.BudgetBlockedError):
                        pipeline.post_json(
                            url,
                            {"Content-Type": "application/json"},
                            b"{}",
                            timeout=1,
                            cost_root=root,
                        )

            urlopen.assert_not_called()

    def test_timeout_keeps_sent_reservation_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DAILY_BUDGET_USD": "0.001", "BUDGET_HARD_STOP": "true"},
        ), patch.object(pipeline.urllib.request, "urlopen", side_effect=TimeoutError("late response")):
            root = Path(directory)
            with self.assertRaisesRegex(RuntimeError, "API 網路錯誤"):
                pipeline.request_json(
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    {},
                    method="POST",
                    body=b"{}",
                    timeout=1,
                    attempts=1,
                    cost_root=root,
                )

            with cost_tracker.get_operations_db(root) as connection:
                usage = connection.execute(
                    "SELECT provider, status FROM usage_costs WHERE provider = 'groq'"
                ).fetchall()
                reservations = connection.execute(
                    "SELECT status FROM budget_reservations"
                ).fetchall()
            self.assertEqual([(row[0], row[1]) for row in usage], [("groq", "unknown")])
            self.assertEqual([row[0] for row in reservations], ["unknown"])


if __name__ == "__main__":
    unittest.main()
