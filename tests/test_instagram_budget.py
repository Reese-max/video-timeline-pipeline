"""Issue #8: paid Instagram discovery must fail closed without a finite budget.

A paid Instagram/Bright Data discovery run cannot start with an
unbounded (None) result budget, and --allow-batch-cloud alone is
insufficient. Invalid, missing, zero, or negative budgets fail before any
Bright Data request; retries of a known snapshot reuse it instead of
creating a second billable snapshot.
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


def _ready(*, snapshot_id="sd_budget"):
    def fake_get(url, headers):
        if "/progress/" in url:
            return {"snapshot_id": snapshot_id, "status": "ready"}
        return []

    return fake_get


class InstagramBudgetTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(mock.patch.dict(pipeline.os.environ, {
            "BRIGHTDATA_CREDIT_LEDGER": str(Path(directory) / "credits.sqlite3"),
            "BRIGHTDATA_MONTHLY_CREDIT_LIMIT": "4500", "BRIGHTDATA_POST_DAILY_LIMIT": "20",
        }))
        self.enterContext(mock.patch.object(pipeline, "load_dotenv"))

    def test_missing_budget_fails_before_any_provider_call(self) -> None:
        environment = {
            "BRIGHTDATA_API_TOKEN": "test-brightdata-token",
            "BRIGHTDATA_RUN_TIMEOUT_SECONDS": "60",
        }
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            pipeline, "request_json", return_value={"snapshot_id": "sd_budget"}
        ) as submit, mock.patch.object(
            pipeline, "get_json", side_effect=_ready()
        ) as poll, mock.patch.dict(
            pipeline.os.environ, environment
        ):
            with self.assertRaisesRegex(ValueError, "--instagram-max-items"):
                pipeline.fetch_instagram_author(
                    "https://www.instagram.com/example/",
                    "365d",
                    allow_paid=True,
                    root=Path(root),
                )
            submit.assert_not_called()
            poll.assert_not_called()

    def test_invalid_budgets_fail_before_any_provider_call(self) -> None:
        environment = {
            "BRIGHTDATA_API_TOKEN": "test-brightdata-token",
            "BRIGHTDATA_RUN_TIMEOUT_SECONDS": "60",
        }
        for bad in (0, -5, True, "20", 2.5):
            with self.subTest(bad=bad), tempfile.TemporaryDirectory() as root, mock.patch.object(
                pipeline, "request_json", return_value={"snapshot_id": "sd_budget"}
            ) as submit, mock.patch.object(
                pipeline, "get_json", side_effect=_ready()
            ) as poll, mock.patch.dict(
                pipeline.os.environ, environment
            ):
                with self.assertRaises(ValueError):
                    pipeline.fetch_instagram_author(
                        "https://www.instagram.com/example/",
                        "365d",
                        max_items=bad,
                        allow_paid=True,
                        root=Path(root),
                    )
                submit.assert_not_called()
                poll.assert_not_called()

    def test_opt_in_alone_is_insufficient_at_cli(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            pipeline, "request_json", return_value={"snapshot_id": "sd_budget"}
        ) as submit, mock.patch.object(
            pipeline, "get_json", side_effect=_ready()
        ) as poll, mock.patch.dict(
            pipeline.os.environ, {"BRIGHTDATA_API_TOKEN": "test-brightdata-token"}
        ), mock.patch.object(
            pipeline.sys,
            "argv",
            [
                "pipeline.py",
                "--instagram-author",
                "testauthor",
                "--instagram-discover-only",
                "--allow-batch-cloud",
                "--root",
                directory,
            ],
        ):
            args = pipeline.parse_args()
            with self.assertRaisesRegex(ValueError, "--instagram-max-items"):
                pipeline.run_rss_command(args)
            submit.assert_not_called()
            poll.assert_not_called()

    def test_bound_is_sent_and_printed_without_token(self) -> None:
        import io

        environment = {
            "BRIGHTDATA_API_TOKEN": "test-brightdata-token",
            "BRIGHTDATA_RUN_TIMEOUT_SECONDS": "60",
        }
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            pipeline, "request_json", return_value={"snapshot_id": "sd_budget"}
        ) as submit, mock.patch.object(
            pipeline, "get_json", side_effect=_ready()
        ), mock.patch.dict(pipeline.os.environ, environment):
            feed = pipeline.fetch_instagram_author(
                "https://www.instagram.com/example/",
                "365d",
                max_items=7,
                allow_paid=True,
                root=Path(root),
            )
        request_input = pipeline.json.loads(submit.call_args.kwargs["body"])["input"][0]
        self.assertEqual(request_input["num_of_posts"], 7)
        self.assertEqual(feed["instagram"]["max_items"], 7)

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            pipeline, "request_json", return_value={"snapshot_id": "sd_budget"}
        ), mock.patch.object(
            pipeline, "get_json", side_effect=_ready()
        ), mock.patch.dict(pipeline.os.environ, environment), mock.patch.object(
            pipeline.sys,
            "argv",
            [
                "pipeline.py",
                "--instagram-author",
                "testauthor",
                "--instagram-max-items",
                "7",
                "--instagram-discover-only",
                "--allow-batch-cloud",
                "--root",
                directory,
            ],
        ), mock.patch.object(pipeline.sys, "stdout", io.StringIO()) as stdout:
            args = pipeline.parse_args()
            self.assertEqual(pipeline.run_rss_command(args), 0)
            printed = stdout.getvalue()
        self.assertIn("num_of_posts=7", printed)
        self.assertNotIn("test-brightdata-token", printed)

    def test_retry_reuses_snapshot_without_second_billable_post(self) -> None:
        environment = {
            "BRIGHTDATA_API_TOKEN": "test-brightdata-token",
            "BRIGHTDATA_RUN_TIMEOUT_SECONDS": "60",
        }
        with tempfile.TemporaryDirectory() as root, mock.patch.object(
            pipeline, "request_json", return_value={"snapshot_id": "sd_budget"}
        ) as submit, mock.patch.object(
            pipeline, "get_json",
            side_effect=[RuntimeError("GET timeout"), {"status": "ready"}, []],
        ):
            with mock.patch.dict(pipeline.os.environ, environment):
                with self.assertRaisesRegex(RuntimeError, "GET timeout"):
                    pipeline.fetch_instagram_author(
                        "testauthor", max_items=5, allow_paid=True, root=Path(root)
                    )
                result = pipeline.fetch_instagram_author(
                    "testauthor", max_items=5, allow_paid=True, root=Path(root)
                )
            self.assertEqual(result["instagram"]["snapshot_id"], "sd_budget")
            submit.assert_called_once()

    def test_legacy_unbounded_subscription_refresh_fails_closed(self) -> None:
        environment = {
            "BRIGHTDATA_API_TOKEN": "test-brightdata-token",
            "BRIGHTDATA_RUN_TIMEOUT_SECONDS": "60",
        }
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            pipeline, "request_json", return_value={"snapshot_id": "sd_budget"}
        ) as submit, mock.patch.object(
            pipeline, "get_json", side_effect=_ready()
        ) as poll, mock.patch.dict(pipeline.os.environ, environment):
            root = Path(directory)
            state = pipeline.load_rss_state(root)
            state["subscriptions"].append(
                {
                    "id": "legacy-ig",
                    "kind": "instagram-author",
                    "url": "https://www.instagram.com/legacy/",
                    "instagram_username": "legacy",
                    "instagram_since": "365d",
                    "instagram_max_items": None,
                    "name": "legacy",
                    "enabled": True,
                    "latest_only": False,
                    "keywords": [],
                    "tags": [],
                }
            )
            pipeline.save_rss_state(root, state)
            totals = pipeline.refresh_rss(root, allow_paid=True)
            self.assertEqual(totals["failed"], 1)
            submit.assert_not_called()
            poll.assert_not_called()
            subscription = pipeline.load_rss_state(root)["subscriptions"][0]
            self.assertIn("--instagram-max-items", subscription["last_error"])

    def test_update_command_can_set_legacy_budget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = pipeline.load_rss_state(root)
            state["subscriptions"].append(
                {
                    "id": "legacy-ig",
                    "kind": "instagram-author",
                    "url": "https://www.instagram.com/legacy/",
                    "instagram_username": "legacy",
                    "instagram_since": "365d",
                    "instagram_max_items": None,
                    "name": "legacy",
                    "enabled": True,
                    "latest_only": False,
                    "keywords": [],
                    "tags": [],
                }
            )
            pipeline.save_rss_state(root, state)
            updated = pipeline.update_rss_subscription(
                root, "legacy-ig", instagram_max_items=25
            )
            self.assertEqual(updated["instagram_max_items"], 25)
            with self.assertRaises(ValueError):
                pipeline.update_rss_subscription(root, "legacy-ig", instagram_max_items=0)

    def test_rss_update_cli_sets_legacy_budget_without_author_flag(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            pipeline, "request_json", return_value={"snapshot_id": "sd_budget"}
        ) as submit, mock.patch.object(
            pipeline.sys,
            "argv",
            [
                "pipeline.py",
                "--rss-update",
                "legacy-ig",
                "--instagram-max-items",
                "20",
                "--root",
                directory,
            ],
        ):
            root = Path(directory)
            state = pipeline.load_rss_state(root)
            state["subscriptions"].append(
                {
                    "id": "legacy-ig",
                    "kind": "instagram-author",
                    "url": "https://www.instagram.com/legacy/",
                    "instagram_username": "legacy",
                    "instagram_since": "365d",
                    "instagram_max_items": None,
                    "name": "legacy",
                    "enabled": True,
                    "latest_only": False,
                    "keywords": [],
                    "tags": [],
                }
            )
            pipeline.save_rss_state(root, state)
            args = pipeline.parse_args()
            self.assertEqual(pipeline.run_rss_command(args), 0)
            submit.assert_not_called()
            self.assertEqual(
                pipeline.load_rss_state(root)["subscriptions"][0]["instagram_max_items"],
                20,
            )

    def test_rss_update_cli_rejects_bad_budget_and_non_budget_flags(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            pipeline, "request_json"
        ) as submit:
            root = Path(directory)
            state = pipeline.load_rss_state(root)
            state["subscriptions"].append(
                {
                    "id": "legacy-ig",
                    "kind": "instagram-author",
                    "url": "https://www.instagram.com/legacy/",
                    "instagram_username": "legacy",
                    "instagram_since": "365d",
                    "instagram_max_items": None,
                    "name": "legacy",
                    "enabled": True,
                    "latest_only": False,
                    "keywords": [],
                    "tags": [],
                }
            )
            pipeline.save_rss_state(root, state)
            for extra in (["--instagram-max-items", "0"], ["--instagram-since", "365d"]):
                with self.subTest(extra=extra), mock.patch.object(
                    pipeline.sys,
                    "argv",
                    ["pipeline.py", "--rss-update", "legacy-ig", *extra, "--root", directory],
                ):
                    args = pipeline.parse_args()
                    with self.assertRaises(ValueError):
                        pipeline.run_rss_command(args)
                    submit.assert_not_called()
            self.assertIsNone(
                pipeline.load_rss_state(root)["subscriptions"][0]["instagram_max_items"]
            )


if __name__ == "__main__":
    unittest.main()
