"""Offline SSE contract: real chat/HTTP/guard code, fake HTTP responses only."""
import contextlib
import http.client
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import traceback
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error

import cost_tracker
import execution_control
import pipeline


KEY = "offline-stream-secret"
URL = "https://api.minimax.io/v1/chat/completions"


def event(value):
    return b"data: " + json.dumps(value, ensure_ascii=False).encode() + b"\r\n\r\n"


def completion(content="答案", reason="stop", done=True):
    wire = event({"choices": [{"delta": {"reasoning_content": "private reasoning"}}]})
    wire += event({"choices": [{"delta": {"content": content}}]})
    if reason is not None:
        wire += event({"choices": [{"delta": {}, "finish_reason": reason}]})
    return wire + (b"data: [DONE]\n\n" if done else b"")


class Response:
    def __init__(self, wire, chunk_size=19, on_read=None):
        self.buffer = io.BytesIO(wire)
        self.chunk_size = chunk_size
        self.on_read = on_read
        self.socket = Mock()
        self.fp = SimpleNamespace(raw=SimpleNamespace(_sock=self.socket))
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def read1(self, size):
        if self.on_read:
            self.on_read()
        return self.buffer.read(min(size, self.chunk_size))


class MinimaxStreamingTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(execution_control.reset_execution_cancellation)
        execution_control.reset_execution_cancellation()
        # Keep every test artifact inside the worktree.
        directory = self.stack.enter_context(tempfile.TemporaryDirectory(dir=Path.cwd()))
        self.root = Path(directory)
        self.stack.enter_context(patch.dict(os.environ, {
            "MINIMAX_BASE_URL": "https://api.minimax.io/v1",
            "BUDGET_HARD_STOP": "false", "DAILY_BUDGET_USD": "100",
        }, clear=True))
        self.stack.enter_context(cost_tracker.cost_root_scope(self.root))
        self.sender = self.stack.enter_context(patch.object(
            pipeline.urllib.request, "urlopen", side_effect=AssertionError("Unexpected HTTP request")))
        self.sleep = self.stack.enter_context(patch.object(pipeline.OperationBudget, "sleep"))

    def chat(self, **kwargs):
        return pipeline.minimax_chat([{"role": "user", "content": "test"}], KEY,
                                     "MiniMax-M3", stream=True, **kwargs)

    def respond(self, wire, **kwargs):
        response = Response(wire, **kwargs)
        self.sender.side_effect = None
        self.sender.return_value = response
        return response

    def rows(self, table="usage_costs"):
        with contextlib.closing(sqlite3.connect(self.root / "operations.db")) as db:
            db.row_factory = sqlite3.Row
            return [dict(row) for row in db.execute("SELECT * FROM " + table + " ORDER BY rowid")]

    def test_split_utf8_reasoning_and_content_match_nonstream_result(self):
        self.respond(completion("<think>hidden</think>答案"), chunk_size=1)
        self.assertEqual(self.chat(), "答案")
        payload = json.loads(self.sender.call_args.args[0].data)
        self.assertIs(payload["stream"], True)
        self.assertIs(payload["reasoning_split"], True)
        self.assertEqual([r["status"] for r in self.rows()], ["succeeded"])

    def test_multiple_content_events_usage_and_heartbeat(self):
        wire = b": heartbeat\r\n\r\n" + event({"choices": [{"delta": {"content": "答"}}]})
        wire += event({"choices": [{"delta": {"content": "案"}, "finish_reason": "stop"}]})
        wire += event({"choices": [], "usage": {"completion_tokens": 2}}) + b"data: [DONE]\n\n"
        self.respond(wire)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(self.chat(), "答案")
        self.assertIn('"completion_tokens": 2', output.getvalue())

    def test_both_video_paths_send_stream_true(self):
        video = self.root / "offline.mp4"
        video.write_bytes(b"fake-video")
        answer = json.dumps({"events": [{"timestamp": 0, "screen_type": "video",
            "visible_text": [], "visual_summary": "畫面", "important_details": [], "confidence": 0.9}]})
        self.respond(completion(answer))
        with patch.object(pipeline, "minimax_reusable_upload", return_value=("file-1", False)):
            pipeline._analyze_video_mp4_core(video, KEY, "MiniMax-M3", prompt="test", fps_value=1,
                detail_value="default", timeout=180, analysis_method="minimax_video", source_video=video,
                duration_hint=5)
        self.assertIs(json.loads(self.sender.call_args.args[0].data).get("stream"), True)
        self.sender.reset_mock()
        self.respond(completion(answer))
        pipeline._fill_video_event_gaps([], file_id="file-1", api_key=KEY, model="MiniMax-M3",
            fps_value=1, detail_value="default", timeout=180, duration=60, max_completion_tokens=None)
        self.sender.assert_called_once()
        self.assertIs(json.loads(self.sender.call_args.args[0].data).get("stream"), True)

    def test_nonvideo_default_remains_json(self):
        self.respond(json.dumps({"choices": [{"finish_reason": "stop",
            "message": {"content": "plain"}}]}).encode())
        self.assertEqual(pipeline.minimax_chat([], KEY, "MiniMax-M3"), "plain")
        self.assertNotIn("stream", json.loads(self.sender.call_args.args[0].data))

    def test_incomplete_streams_fail_closed(self):
        for reason, done in [("length", True), ("length", False), (None, True), (None, False),
                             ("content_filter", True), ("tool_calls", True)]:
            with self.subTest(reason=reason, done=done):
                self.respond(completion(reason=reason, done=done))
                with self.assertRaises(pipeline.MiniMaxIncompleteError) as caught:
                    self.chat()
                self.assertEqual(caught.exception.finish_reason, reason)

    def test_stop_without_done_is_complete(self):
        self.respond(completion(done=False))
        self.assertEqual(self.chat(), "答案")

    def test_done_without_finish_reason_is_not_success(self):
        self.respond(b"data: [DONE]\n\n")
        with self.assertRaises(pipeline.MiniMaxIncompleteError):
            self.chat()

    def test_sse_quota_error_redacted_classified_and_not_retried(self):
        self.respond(event({"error": {"status": 429, "code": "insufficient_quota", "message": KEY}}))
        with self.assertRaises(pipeline.ApiRequestError) as caught:
            self.chat()
        self.assertNotIn(KEY, str(caught.exception))
        self.assertNotIn(KEY, caught.exception.response_body)
        self.sender.assert_called_once()
        self.assertEqual([r["safe_error"] for r in self.rows()], ["quota_exhausted"])
        self.assertEqual([r["status"] for r in self.rows()], ["failed"])

    def test_http_error_redacted_classified_and_not_retried(self):
        self.sender.side_effect = urllib.error.HTTPError(URL, 403, "denied", {},
            io.BytesIO(json.dumps({"error": {"code": "insufficient_scope", "message": KEY}}).encode()))
        with self.assertRaises(pipeline.ApiRequestError) as caught:
            self.chat()
        self.assertEqual(caught.exception.status_code, 403)
        self.assertNotIn(KEY, caught.exception.response_body)
        self.sender.assert_called_once()
        self.assertEqual([r["safe_error"] for r in self.rows()], ["permission"])

    def test_sse_rate_limit_retries_with_separate_cost_attempts(self):
        self.sender.side_effect = [Response(event({"error": {"status": 429, "code": "rate_limit"}})),
                                   Response(completion())]
        self.assertEqual(self.chat(), "答案")
        self.assertEqual(self.sender.call_count, 2)
        self.sleep.assert_called_once_with(2.0)
        rows = self.rows()
        self.assertEqual([r["safe_error"] for r in rows], ["rate_limit", None])
        self.assertEqual([r["attempt_no"] for r in rows], [1, 2])
        self.assertEqual(len({r["logical_operation_id"] for r in rows}), 1)

    def test_http_retry_after_is_preserved(self):
        self.sender.side_effect = [urllib.error.HTTPError(URL, 429, "busy", {"Retry-After": "7"},
            io.BytesIO(b'{"error":{"code":"rate_limit"}}')), Response(completion())]
        self.assertEqual(self.chat(), "答案")
        self.sleep.assert_called_once_with(7.0)
        self.assertEqual([r["attempt_no"] for r in self.rows()], [1, 2])

    def test_error_after_partial_content_is_not_silently_accepted(self):
        wire = completion("partial", done=False) + event({"error": {"message": KEY}})
        self.respond(wire)
        with self.assertRaises(pipeline.ApiRequestError) as caught:
            self.chat()
        self.assertNotIn(KEY, str(caught.exception))

    def test_minimax_business_error_is_not_swallowed(self):
        self.respond(event({"base_resp": {"status_code": 1004, "status_msg": "invalid_api_key " + KEY}}))
        with self.assertRaises(pipeline.ApiRequestError) as caught:
            self.chat()
        self.assertNotIn(KEY, caught.exception.response_body)
        self.assertEqual([r["safe_error"] for r in self.rows()], ["auth"])

    def test_malformed_event_is_redacted(self):
        self.respond(b"data: " + KEY.encode() + b"\n\n")
        with self.assertRaises(pipeline.ApiRequestError) as caught:
            self.chat()
        self.assertNotIn(KEY, str(caught.exception))
        self.assertTrue(caught.exception.provider_outcome_unknown)
        self.sender.assert_called_once()
        self.assertEqual([r["status"] for r in self.rows()], ["unknown"])

    def test_durable_stream_timeout_is_never_replayed(self):
        self.sender.side_effect = TimeoutError("idle")
        fence = Mock(return_value=True)
        with pipeline.durable_transport_scope(fence):
            with self.assertRaises(pipeline.ApiRequestError) as caught:
                self.chat()
        self.assertTrue(caught.exception.provider_outcome_unknown)
        self.sender.assert_called_once()
        fence.assert_called_once()
        self.assertEqual([r["status"] for r in self.rows()], ["unknown"])
        self.assertEqual([r["status"] for r in self.rows("budget_reservations")], ["reserved"])

    def test_durable_fence_cancellation_prevents_dispatch(self):
        with pipeline.durable_transport_scope(lambda: False):
            with self.assertRaises(execution_control.ExecutionCancelled):
                self.chat()
        self.sender.assert_not_called()

    def test_cost_guard_blocks_before_dispatch(self):
        os.environ["BUDGET_HARD_STOP"] = "true"
        with self.assertRaises(cost_tracker.BudgetBlockedError):
            self.chat()
        self.sender.assert_not_called()

    def test_stream_reads_check_cancellation(self):
        response = self.respond(completion(), on_read=execution_control.cancel_execution)
        with self.assertRaises(execution_control.ExecutionCancelled):
            self.chat()
        self.sender.assert_called_once()
        self.assertTrue(response.closed)
        self.sleep.assert_not_called()

    def test_stream_total_deadline_and_read_timeout_are_bounded(self):
        clock = [0.0]
        def tick():
            clock[0] += 1
        os.environ["VIDEO_API_TIMEOUT_SECONDS"] = "2.5"
        response = self.respond(completion(), chunk_size=1, on_read=tick)
        with patch("execution_control.time.monotonic", side_effect=lambda: clock[0]):
            with self.assertRaises(execution_control.DeadlineExceeded):
                self.chat(timeout=180)
        self.assertEqual(self.sender.call_args.kwargs["timeout"], 2.5)
        self.assertEqual([c.args[0] for c in response.socket.settimeout.call_args_list], [2.5, 1.5, 0.5])
        self.assertTrue(response.closed)
        self.sender.assert_called_once()

    def test_idle_timeout_errors_and_retries_do_not_join_partial_answers(self):
        calls = [0]
        def idle():
            calls[0] += 1
            if calls[0] == 2:
                raise TimeoutError("idle read")
        response = Response(event({"choices": [{"delta": {"content": "discard"}}]}),
                            chunk_size=65536, on_read=idle)
        self.sender.side_effect = [response, Response(completion("fresh"))]
        self.assertEqual(self.chat(timeout=7), "fresh")
        self.assertTrue(response.closed)
        self.assertTrue(all(c.args[0] == 7 for c in response.socket.settimeout.call_args_list))
        self.assertEqual([r["status"] for r in self.rows()], ["unknown", "succeeded"])

    def test_idle_timeout_exhausts_finite_attempts_and_keeps_risk_reservations(self):
        def idle():
            raise TimeoutError("idle read")
        self.sender.side_effect = lambda *a, **kw: Response(b"", on_read=idle)
        with self.assertRaises(pipeline.ApiRequestError):
            self.chat(timeout=7)
        self.assertEqual(self.sender.call_count, 4)
        self.assertEqual([r["status"] for r in self.rows()], ["unknown"] * 4)
        self.assertEqual([r["status"] for r in self.rows("budget_reservations")], ["reserved"] * 4)

    def test_durable_sse_server_error_is_not_replayed(self):
        self.respond(event({"error": {"status_code": 503, "message": "busy"}}))
        with pipeline.durable_transport_scope(lambda: True):
            with self.assertRaises(pipeline.ApiRequestError) as caught:
                self.chat()
        self.assertTrue(caught.exception.provider_outcome_unknown)
        self.sender.assert_called_once()
        self.assertEqual([r["status"] for r in self.rows()], ["unknown"])

    def test_sse_error_type_without_http_status_uses_existing_classifier(self):
        self.sender.side_effect = [Response(b"event: error\n" + event({"type": "rate_limit_error"})),
                                   Response(completion())]
        self.assertEqual(self.chat(), "答案")
        self.assertEqual([r["safe_error"] for r in self.rows()], ["rate_limit", None])

    def test_transport_failures_redact_even_formatted_tracebacks(self):
        self.sender.side_effect = urllib.error.URLError("connection refused " + KEY)
        with pipeline.durable_transport_scope(lambda: True):
            try:
                self.chat()
            except pipeline.ApiRequestError:
                self.assertNotIn(KEY, traceback.format_exc())
            else:
                self.fail("transport error was swallowed")
        self.sender.assert_called_once()

    def test_durable_broken_stream_is_unknown_and_not_replayed(self):
        for failure in (ConnectionResetError(KEY), http.client.IncompleteRead(KEY.encode(), 100)):
            with self.subTest(error=type(failure).__name__):
                def broken():
                    raise failure
                response = self.respond(completion(), on_read=broken)
                self.sender.reset_mock()
                with pipeline.durable_transport_scope(lambda: True):
                    with self.assertRaises(pipeline.ApiRequestError) as caught:
                        self.chat()
                self.assertTrue(caught.exception.provider_outcome_unknown)
                self.assertNotIn(KEY, str(caught.exception))
                self.assertTrue(response.closed)
                self.sender.assert_called_once()

    def test_multiline_data_and_multiple_events_in_one_read(self):
        wire = (b'data: {"choices":\n'
                b'data: [{"delta": {"content": "joined"}, "finish_reason": "stop"}]}\n\n'
                b'data: [DONE]\n\n')
        self.respond(wire, chunk_size=65536)
        self.assertEqual(self.chat(), "joined")


if __name__ == "__main__":
    unittest.main()
