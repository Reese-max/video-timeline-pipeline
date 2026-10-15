"""Whole-video MiniMax analysis must not run into the small server-default output cap.

Job 714b23bfc9c0 (552s) came back finish_reason=length because the request
carried no output limit; the same call with max_completion_tokens finished
with stop and 17 events.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pipeline


def _spread_response(duration: float) -> str:
    return json.dumps(
        {
            "events": [
                {
                    "timestamp": stamp,
                    "screen_type": "畫面",
                    "visible_text": [],
                    "visual_summary": f"摘要 {stamp}",
                    "important_details": [],
                    "confidence": 0.9,
                }
                for stamp in (0.0, duration * 0.5, duration - 5.0)
            ],
            "overall_summary": "整片摘要",
        },
        ensure_ascii=False,
    )


class MinimaxChatPayloadTests(unittest.TestCase):
    def _payload(self, **kwargs) -> dict:
        response = {"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}]}
        with mock.patch.object(pipeline, "post_json", return_value=response) as post, \
             mock.patch.object(pipeline, "minimax_endpoint", return_value="https://example.invalid/v1/chat/completions"):
            pipeline.minimax_chat([{"role": "user", "content": "t"}], "key", "MiniMax-M3", **kwargs)
        return json.loads(post.call_args.args[2].decode("utf-8"))

    def test_max_completion_tokens_is_sent(self) -> None:
        payload = self._payload(max_completion_tokens=524288)
        self.assertEqual(payload["max_completion_tokens"], 524288)
        self.assertNotIn("max_tokens", payload)

    def test_default_chat_sends_no_output_limit(self) -> None:
        payload = self._payload()
        self.assertNotIn("max_completion_tokens", payload)
        self.assertNotIn("max_tokens", payload)


class VideoMp4OutputCeilingTests(unittest.TestCase):
    def _run(self, duration: float, env: dict[str, str] | None = None):
        root = Path(tempfile.mkdtemp(prefix="video-ceiling-"))
        video = root / "clip.mp4"
        video.write_bytes(b"fake-mp4-bytes")
        with mock.patch.dict(os.environ, env or {}, clear=False), \
             mock.patch.object(pipeline, "minimax_upload_video_understanding", return_value="fid"), \
             mock.patch.object(pipeline, "minimax_chat", return_value=_spread_response(duration)) as chat, \
             mock.patch.object(pipeline, "analyze_video_mp4_frames_fallback") as frames:
            observations = pipeline.analyze_video_mp4(
                video, "key", "MiniMax-M3", duration=duration, work_dir=root / "work"
            )
        return observations, chat, frames

    def test_whole_video_call_requests_model_maximum_output(self) -> None:
        _, chat, frames = self._run(552.0)
        kwargs = chat.call_args.kwargs
        self.assertEqual(kwargs["max_completion_tokens"], pipeline.MINIMAX_VIDEO_MAX_COMPLETION_TOKENS)
        self.assertGreaterEqual(kwargs["timeout"], 3600)
        frames.assert_not_called()

    def test_prompt_no_longer_caps_event_count(self) -> None:
        _, chat, _ = self._run(552.0)
        prompt = chat.call_args.args[0][0]["content"][0]["text"]
        self.assertNotIn("20 筆", prompt)
        self.assertIn("不設筆數上限", prompt)

    def test_env_can_lower_ceiling_but_not_exceed_model_limit(self) -> None:
        _, chat, _ = self._run(60.0, {"ANALYZE_VIDEO_MAX_TOKENS": "999999999"})
        self.assertEqual(chat.call_args.kwargs["max_completion_tokens"], 524288)
        _, chat, _ = self._run(60.0, {"ANALYZE_VIDEO_MAX_TOKENS": "65536"})
        self.assertEqual(chat.call_args.kwargs["max_completion_tokens"], 65536)

    def test_short_video_keeps_requested_fps(self) -> None:
        _, chat, _ = self._run(552.0)
        video_part = chat.call_args.args[0][0]["content"][1]["video_url"]
        self.assertEqual(video_part["fps"], 1.0)

    def test_long_video_fps_drops_to_fit_context(self) -> None:
        _, chat, _ = self._run(7200.0)
        fps = chat.call_args.args[0][0]["content"][1]["video_url"]["fps"]
        self.assertLess(fps, 1.0)
        self.assertLessEqual(
            fps * 7200.0 * pipeline.MINIMAX_VIDEO_TOKENS_PER_FRAME,
            pipeline.MINIMAX_VIDEO_CONTEXT_TOKEN_BUDGET,
        )

    def test_fps_floor(self) -> None:
        self.assertEqual(pipeline.video_mp4_fps_for_duration(1.0, 10 ** 6), 0.2)
        self.assertEqual(pipeline.video_mp4_fps_for_duration(1.0, None), 1.0)


if __name__ == "__main__":
    unittest.main()
