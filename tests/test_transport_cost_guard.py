"""Regression contracts for the cost guard at provider transport boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
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
            self.assertEqual(
                [(row[0], row[1]) for row in usage],
                [("groq", "succeeded"), ("groq", "blocked_budget")],
            )
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

            with cost_tracker.get_operations_db(root) as connection:
                blocked = connection.execute(
                    "SELECT provider FROM usage_costs WHERE status = 'blocked_budget'"
                ).fetchall()
            self.assertEqual(
                sorted(row[0] for row in blocked),
                sorted(provider for provider, _ in providers),
            )

    def test_provider_for_url_requires_host_boundary(self) -> None:
        self.assertEqual(
            cost_tracker.provider_for_url("https://api.groq.com/openai/v1"), "groq"
        )
        self.assertEqual(
            cost_tracker.provider_for_url("https://notgroq.com/openai/v1"), "external"
        )

    def test_reservation_check_is_atomic_under_concurrency(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DAILY_BUDGET_USD": "0.001", "BUDGET_HARD_STOP": "true"},
        ):
            root = Path(directory)
            barrier = threading.Barrier(4)
            outcomes: list[tuple[bool, str | None, str]] = []

            def reserve() -> None:
                barrier.wait()
                outcomes.append(
                    cost_tracker.check_and_reserve_budget(
                        root, operation_name="test:race", estimated_micro_usd=600
                    )
                )

            threads = [threading.Thread(target=reserve) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            allowed = [outcome for outcome in outcomes if outcome[0]]
            self.assertEqual(len(outcomes), 4)
            self.assertEqual(len(allowed), 1)

    def test_budget_block_does_not_poison_brightdata_submission(self) -> None:
        class Response:
            headers = {}

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"snapshot_id": "sd_abc123"}'

        url = "https://api.brightdata.com/datasets/v3/trigger?dataset_id=ds_1"
        body = b'[{"url": "https://www.instagram.com/p/abc/"}]'
        headers = {"Authorization": "Bearer test-token", "Content-Type": "application/json"}

        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DAILY_BUDGET_USD": "0.000001", "BUDGET_HARD_STOP": "true"},
        ), patch.object(pipeline.urllib.request, "urlopen") as urlopen:
            root = Path(directory)
            folder = root / "job"
            with cost_tracker.cost_tracking(root):
                with self.assertRaises(cost_tracker.BudgetBlockedError):
                    pipeline.submit_brightdata_once(folder, url, headers, body)

            urlopen.assert_not_called()
            self.assertFalse((folder / ".submitted").exists())
            self.assertFalse((folder / "submission.json").exists())

            # A provably unsent submission must be retryable once budget allows.
            urlopen.return_value = Response()
            with patch.dict(os.environ, {"DAILY_BUDGET_USD": "100"}):
                with cost_tracker.cost_tracking(root):
                    self.assertEqual(
                        pipeline.submit_brightdata_once(folder, url, headers, body),
                        "sd_abc123",
                    )

    def test_budget_blocked_send_is_retryable_not_unknown(self) -> None:
        import email_providers
        import notifications

        provider = email_providers.ResendProvider(api_key="re_test_key")
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DAILY_BUDGET_USD": "0.000001", "BUDGET_HARD_STOP": "true"},
        ), patch.object(email_providers.urllib.request, "urlopen") as urlopen:
            result = notifications._send_with_cost_tracking(
                Path(directory),
                provider,
                recipient="user@example.com",
                subject="subject",
                text_body="body",
                html_body=None,
                idempotency_key="idem-1",
            )
            self.assertEqual(result.status, "retryable_not_accepted")
            urlopen.assert_not_called()

    def test_transport_error_redaction_covers_token_and_bearer(self) -> None:
        error = pipeline.urllib.error.URLError(
            "GET https://api.apify.com/v2/x?token=SECRET987 failed; Authorization: Bearer ABC456DEF"
        )
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"DAILY_BUDGET_USD": "100", "BUDGET_HARD_STOP": "true"},
        ), patch.object(
            pipeline.urllib.request, "urlopen", side_effect=error
        ):
            root = Path(directory)
            with self.assertRaises(RuntimeError):
                pipeline.request_json(
                    "https://api.apify.com/v2/actor-runs/1",
                    {},
                    method="GET",
                    timeout=1,
                    attempts=1,
                    cost_root=root,
                )
            with cost_tracker.get_operations_db(root) as connection:
                rows = connection.execute(
                    "SELECT safe_error FROM usage_costs"
                ).fetchall()
            self.assertEqual(len(rows), 1)
            stored = rows[0][0] or ""
            self.assertNotIn("SECRET987", stored)
            self.assertNotIn("ABC456DEF", stored)

    def test_notifications_importable_as_first_module(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-c", "import notifications"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

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
