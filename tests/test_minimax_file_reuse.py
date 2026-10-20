"""MiniMax file_id reuse across reruns of the same video.

Field test c9289f71a482 re-uploaded the same MP4 for each of four reruns while
MiniMax kept answering 529. video_understanding uploads are kept for up to 7 days.
"""
import http.client
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

import pipeline

ANSWER = json.dumps({"events": [{"timestamp": 0, "screen_type": "畫面", "visible_text": [], "visual_summary": "測試畫面",
                                 "important_details": [], "confidence": 0.9}], "overall_summary": "測試"}, ensure_ascii=False)


def overloaded():
    return pipeline.ApiRequestError("API HTTP 529（https://api.minimax.io/v1/chat/completions）：overloaded",
                                    status_code=529, response_body='{"type":"error","error":{"type":"overloaded_error"}}')


class MiniMaxFileReuseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.video = self.root / "source.mp4"
        self.video.write_bytes(b"owned video bytes")
        self.work = self.root / "work"
        environment = mock.patch.dict(os.environ, {"MINIMAX_BASE_URL": "https://api.minimax.io/v1"})
        environment.start()
        self.addCleanup(environment.stop)

    def run_core(self, chat, uploads=("fid-1", "fid-2"), record_dir="work"):
        with mock.patch.object(pipeline, "minimax_upload_video_understanding", side_effect=list(uploads)) as self.upload, \
                mock.patch.object(pipeline, "minimax_chat", side_effect=chat) as self.sent:
            return pipeline._analyze_video_mp4_core(
                self.video, "inert-key", "MiniMax-M3", prompt="p", fps_value=1.0, detail_value="default",
                timeout=60, analysis_method="minimax_video", source_video=self.video,
                upload_record_dir=self.work if record_dir else None)

    def sent_ids(self):
        return [call.args[0][0]["content"][1]["video_url"]["url"] for call in self.sent.call_args_list]

    def record(self):
        return json.loads(next(self.work.glob("minimax-upload-*.json")).read_text(encoding="utf-8"))

    def test_rerun_after_overload_reuses_the_uploaded_file(self):
        with self.assertRaises(pipeline.ApiRequestError):
            self.run_core([overloaded()])
        self.assertEqual(self.upload.call_count, 1)
        observations, _, file_id = self.run_core([ANSWER])
        self.upload.assert_not_called()
        self.assertEqual((file_id, self.sent_ids()), ("fid-1", ["mm_file://fid-1"]))
        self.assertEqual(observations[0]["visual_summary"], "測試畫面")

    def test_overload_on_a_reused_file_fails_fast_without_reupload(self):
        self.run_core([ANSWER])
        with self.assertRaises(pipeline.ApiRequestError):
            self.run_core([overloaded()])
        self.upload.assert_not_called()
        self.assertEqual(self.sent.call_count, 1)
        self.assertEqual(self.record()["file_id"], "fid-1")

    def test_lost_connection_on_a_reused_file_fails_fast_without_reupload(self):
        # urlopen can raise these unwrapped from getresponse() or a body read.
        self.run_core([ANSWER])
        for lost in (http.client.RemoteDisconnected("Remote end closed connection"), ConnectionResetError(10054, "reset"),
                     http.client.IncompleteRead(b"partial")):
            with self.subTest(lost=type(lost).__name__), self.assertRaises(type(lost)):
                self.run_core([lost])
            self.upload.assert_not_called()
            self.assertEqual(self.sent.call_count, 1)

    def test_old_or_changed_records_upload_again(self):
        self.run_core([ANSWER])
        path = next(self.work.glob("minimax-upload-*.json"))
        record = self.record()
        for changed in ({"uploaded_at": time.time() - pipeline.MINIMAX_FILE_REUSE_SECONDS - 1},
                        {"uploaded_at": time.time() + 60},
                        {"base_url": "https://api.minimaxi.com/v1"},
                        {"size_bytes": 1}, {"file_id": ""}, {"sha256": "0" * 64},
                        {"uploaded_at": 10 ** 400}, {"uploaded_at": True}, {"uploaded_at": "recent"}):
            with self.subTest(changed=changed):
                path.write_text(json.dumps({**record, **changed}), encoding="utf-8")
                _, _, file_id = self.run_core([ANSWER], uploads=("fid-new",))
                self.upload.assert_called_once()
                self.assertEqual(file_id, "fid-new")

    def test_reused_file_the_provider_refuses_is_uploaded_once_more(self):
        self.run_core([ANSWER])
        refused = RuntimeError('MiniMax 回應缺少 choices：{"base_resp":{"status_code":2013,"status_msg":"invalid params"}}')
        observations, _, file_id = self.run_core([refused, ANSWER], uploads=("fid-2",))
        self.upload.assert_called_once()
        self.assertEqual((file_id, self.sent_ids()), ("fid-2", ["mm_file://fid-1", "mm_file://fid-2"]))
        self.assertEqual(self.record()["file_id"], "fid-2")
        self.assertEqual(observations[0]["minimax_file_id"], "fid-2")

    def test_failure_on_a_fresh_upload_is_not_retried(self):
        with self.assertRaises(RuntimeError):
            self.run_core([RuntimeError("MiniMax 回應缺少 choices：{}"), ANSWER])
        self.assertEqual((self.upload.call_count, self.sent.call_count), (1, 1))
        self.assertEqual(self.record()["file_id"], "fid-1")

    def test_without_a_record_dir_every_run_uploads(self):
        for expected in ("fid-a", "fid-b"):
            _, _, file_id = self.run_core([ANSWER], uploads=(expected,), record_dir=None)
            self.upload.assert_called_once()
            self.assertEqual(file_id, expected)
        self.assertFalse(self.work.exists())


if __name__ == "__main__":
    unittest.main()
