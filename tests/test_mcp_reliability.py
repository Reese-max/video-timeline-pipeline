import asyncio
import copy
import csv
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import drive_storage as drive
import mcp_server as server


class TranscriptGapQualityTests(unittest.TestCase):
    def setUp(self):
        self.result = {"status": "complete", "audio_status": "complete", "visual_status": "complete",
                       "transcript": {"segments": [{"text": "hello"}],
                                      "gap_check": {"max_gap": 5.0, "remaining": []}, "backfill_errors": []},
                       "errors": [], "timeline": [{"type": "visual", "data": {
                           "visual_summary": "A visible sign", "confidence": 0.9}}]}

    def test_remaining_gap_requires_review_with_time_range(self):
        self.result["transcript"]["gap_check"]["remaining"] = [{"start": 118.0, "end": 150.0}]
        quality = server.analysis_quality(self.result)
        self.assertEqual(quality["status"], "evidence_available")
        self.assertEqual(quality["reasons"], [])
        self.assertEqual(quality["warnings"], [
            "逐字稿有超過 5 秒的空白（1:58–2:30），可能是無人說話或音樂，必要時核對原片。"])

    def test_remaining_gaps_show_only_three_ranges(self):
        self.result["transcript"]["gap_check"]["remaining"] = [
            {"start": start, "end": start + 10} for start in (60, 120, 180, 240, 300)]
        quality = server.analysis_quality(self.result)
        self.assertEqual(quality["status"], "evidence_available")
        self.assertEqual(quality["reasons"], [])
        reason = quality["warnings"][0]
        for interval in ("1:00–1:10", "2:00–2:10", "3:00–3:10"):
            self.assertIn(interval, reason)
        self.assertNotIn("4:00", reason)
        self.assertNotIn("5:00", reason)
        self.assertEqual(reason.count("–"), 3)
        self.assertIn("等 5 段", reason)

    def test_backfill_errors_without_remaining_gaps_require_review(self):
        self.result["transcript"]["backfill_errors"] = [{"error": "Groq 503"}]
        quality = server.analysis_quality(self.result)
        self.assertEqual(quality["status"], "needs_review")
        self.assertEqual(quality["reasons"], ["逐字稿補轉失敗，可能漏字。"])
        self.assertEqual(quality["warnings"], [])

    def test_remaining_gap_and_backfill_error_share_one_reason(self):
        self.result["transcript"]["gap_check"]["remaining"] = [{"start": 118, "end": 150}]
        self.result["transcript"]["backfill_errors"] = [{"error": "Groq 503"}]
        quality = server.analysis_quality(self.result)
        self.assertEqual(quality["status"], "needs_review")
        self.assertEqual(len(quality["reasons"]), 1)
        self.assertIn("1:58–2:30", quality["reasons"][0])
        self.assertIn("補轉失敗，可能漏字", quality["reasons"][0])
        self.assertEqual(quality["warnings"], [])

    def test_empty_gap_metadata_adds_no_reason(self):
        quality = server.analysis_quality(self.result)
        self.assertEqual(quality["status"], "evidence_available")
        self.assertEqual(quality["reasons"], [])
        self.assertEqual(quality["warnings"], [])

    def test_old_transcript_without_gap_metadata_adds_no_reason(self):
        del self.result["transcript"]["gap_check"]
        del self.result["transcript"]["backfill_errors"]
        quality = server.analysis_quality(self.result)
        self.assertEqual(quality["status"], "evidence_available")
        self.assertEqual(quality["reasons"], [])
        self.assertEqual(quality["warnings"], [])

    def test_invalid_gap_times_are_skipped_without_raising(self):
        for value in (None, float("nan"), float("inf"), float("-inf"), "118", True):
            with self.subTest(value=value):
                self.result["transcript"]["gap_check"]["remaining"] = [
                    {"start": value, "end": 150}, {"start": 118, "end": value},
                    {"start": 118, "end": 150}]
                quality = server.analysis_quality(self.result)
                self.assertEqual(quality["status"], "evidence_available")
                self.assertEqual(quality["reasons"], [])
                self.assertEqual(quality["warnings"][0].count("–"), 1)
                self.assertIn("1:58–2:30", quality["warnings"][0])
                self.assertNotIn("等", quality["warnings"][0])
        self.result["transcript"]["gap_check"]["remaining"] = [{"start": None, "end": float("nan")}]
        quality = server.analysis_quality(self.result)
        self.assertEqual(quality["status"], "evidence_available")
        self.assertEqual(quality["reasons"], [])
        self.assertNotIn("–", quality["warnings"][0])


class ReliabilityTests(unittest.TestCase):
    def test_unreliable_visual_timestamps_warn_and_count_only_visual(self):
        good = {"status": "complete", "audio_status": "skipped", "visual_status": "complete",
                "transcript": {"segments": []}, "timeline": [
                    {"type": "visual", "data": {"visual_summary": "測試畫面", "confidence": 0.9,
                                                "timestamp_reliable": False}},
                    {"type": "visual", "data": {"visual_summary": "舊畫面", "confidence": 0.9}},
                    {"type": "visual", "data": {"visual_summary": "測試畫面", "confidence": 0.9,
                                                "timestamp_reliable": False}},
                    {"type": "speech", "data": {"timestamp_reliable": False}}]}
        quality = server.analysis_quality(good)
        self.assertEqual(quality["status"], "evidence_available")
        self.assertEqual(quality["reasons"], [])
        self.assertEqual(quality["warnings"], ["2 筆畫面時間碼不可靠，未提供跳轉。"])
        for row in good["timeline"][:3]:
            row["data"].pop("timestamp_reliable", None)
        self.assertEqual(server.analysis_quality(good)["status"], "evidence_available")
        self.assertEqual(server.analysis_quality(good)["reasons"], [])
        self.assertEqual(server.analysis_quality(good)["warnings"], [])

    def test_unreliable_timestamps_count_only_usable_visuals(self):
        for include_usable in (False, True):
            with self.subTest(include_usable=include_usable):
                rows = [{"visual_summary": "未完成", "completion_status": "incomplete"},
                        {"visual_summary": "原始回應", "raw": "unparsed"},
                        {"visual_summary": "尚未執行 MiniMax 視覺理解。"}, {"visual_summary": ""}]
                if include_usable:
                    rows.append({"visual_summary": "可用畫面", "confidence": 0.9})
                result = {"status": "complete", "audio_status": "skipped", "visual_status": "complete",
                          "timeline": [{"type": "visual", "data": {**row, "timestamp_reliable": False}}
                                       for row in rows]}
                quality = server.analysis_quality(result)
                self.assertEqual(quality["status"], "needs_review")
                self.assertEqual(quality["reasons"], ["缺少可用畫面觀察，或部分畫面回應不完整。"])
                self.assertEqual(quality["warnings"], ["1 筆畫面時間碼不可靠，未提供跳轉。"]
                                 if include_usable else [])

    def test_warnings_survive_tool_schema_and_allow_summary(self):
        result = {"status": "complete", "audio_status": "complete", "visual_status": "complete",
                  "transcript": {"segments": [{"text": "hello"}], "backfill_errors": [],
                                 "gap_check": {"max_gap": 5, "remaining": [{"start": 118, "end": 150}]}},
                  "timeline": [{"type": "visual", "data": {"visual_summary": "可用畫面", "confidence": 0.9,
                                                            "analysis_method": "minimax_video",
                                                            "timestamp_reliable": False}}]}
        response = server.analysis_tool_result(result)
        quality = response.structuredContent["quality"]
        self.assertEqual(quality["status"], "evidence_available")
        self.assertEqual(quality["reasons"], [])
        self.assertEqual(len(quality["warnings"]), 2)
        text = response.content[0].text
        self.assertIn("注意：" + "；".join(quality["warnings"]), text)
        self.assertNotIn("停止摘要", text)
        self.assertIn("請根據 transcript／timeline 整理", text)
        model = server.PreparedVideoAnalysisResult.model_validate(response.structuredContent)
        self.assertEqual(model.model_dump()["quality"]["warnings"], quality["warnings"])
        tool = next(item for item in asyncio.run(server.mcp.list_tools()) if item.name == "analyze_prepared_video")
        schema = tool.outputSchema["$defs"]["AnalysisQuality"]
        self.assertEqual(schema["properties"]["warnings"]["items"], {"type": "string"})
        self.assertNotIn("warnings", schema["required"])
        legacy = {key: value for key, value in quality.items() if key != "warnings"}
        first = server.AnalysisQuality.model_validate(legacy)
        first.warnings.append("test")
        self.assertEqual(server.AnalysisQuality.model_validate(legacy).warnings, [])
        result["transcript"]["backfill_errors"] = [{"error": "Groq 503"}]
        response = server.analysis_tool_result(result)
        self.assertEqual(response.structuredContent["quality"]["status"], "needs_review")
        self.assertEqual(response.structuredContent["quality"]["warnings"], ["1 筆畫面時間碼不可靠，未提供跳轉。"])
        self.assertIn("停止摘要", response.content[0].text)
        self.assertIn("需覆核：", response.content[0].text)
        self.assertIn("注意：", response.content[0].text)

    def test_analysis_quality_requires_usable_evidence_not_just_api_success(self):
        good = {"status": "complete", "audio_status": "complete", "visual_status": "complete",
                "transcript": {"segments": [{"text": "hello"}]}, "errors": [],
                "timeline": [{"timestamp": 0, "type": "visual", "data": {
                    "visual_summary": "A visible sign", "confidence": 0.9}}]}
        quality = server.analysis_quality(good)
        self.assertEqual(quality["status"], "evidence_available")
        self.assertEqual(quality["visual_frames"], 1)
        self.assertIn("不保證", quality["limitation"])
        for case in ["empty_speech", "empty_visual", "incomplete", "low_confidence", "missing_confidence",
                     "invalid_confidence", "raw_response", "placeholder", "audio_error", "partial"]:
            with self.subTest(case=case):
                bad = copy.deepcopy(good)
                if case == "empty_speech": bad["transcript"]["segments"] = []
                if case == "empty_visual": bad["timeline"] = []
                if case == "incomplete": bad["timeline"][0]["data"]["completion_status"] = "incomplete"
                if case == "low_confidence": bad["timeline"][0]["data"]["confidence"] = 0.2
                if case == "missing_confidence": bad["timeline"][0]["data"].pop("confidence")
                if case == "invalid_confidence": bad["timeline"][0]["data"]["confidence"] = True
                if case == "raw_response": bad["timeline"][0]["data"]["raw"] = "unparsed provider response"
                if case == "placeholder": bad["timeline"][0]["data"]["visual_summary"] = "尚未執行 MiniMax 視覺理解。"
                if case == "audio_error": bad["audio_status"] = "error"
                if case == "partial": bad["status"] = "partial"
                self.assertEqual(server.analysis_quality(bad)["status"], "needs_review")
                self.assertTrue(server.analysis_quality(bad)["reasons"])
                response = server.analysis_tool_result(bad)
                self.assertEqual(server.PreparedVideoAnalysisResult.model_validate(response.structuredContent).quality.status, "needs_review")
                self.assertIn("停止摘要", response.content[0].text)
        good["audio_status"] = "skipped"
        good["transcript"]["segments"] = []
        self.assertEqual(server.analysis_quality(good)["status"], "evidence_available", "Silent video is not failed ASR")

    def test_cached_analysis_is_rechecked_for_quality(self):
        cached = {"status": "complete", "source_fingerprint": "same", "cache_signature": "signature", "audio_status": "complete",
                  "visual_status": "complete", "timeline": [], "transcript": {"segments": []}}
        with (patch.object(server, "prepared_job_dir", return_value=Path("unused")),
              patch.object(server, "read_manifest", return_value={"status": "ready"}),
              patch.object(server.pipeline, "locate_source_video", return_value=Path("unused.mp4")),
              patch.object(server.pipeline, "file_fingerprint", return_value="same"),
              patch.object(server, "analysis_cache_signature", return_value="signature"),
              patch.object(server.pipeline, "reuse_artifact", return_value=cached)):
            result = server.analyze_prepared_sync("0123456789ab")
        self.assertEqual(result["quality"]["status"], "needs_review")

    def test_analysis_busy_does_not_start_provider_work(self):
        with patch.object(server, "analyze_prepared_sync") as analyze, server.JOB_LOCK:
            result = asyncio.run(server.mcp._tool_manager.call_tool(
                "analyze_prepared_video", {"job_id": "0123456789ab"}, convert_result=True))
        analyze.assert_not_called()
        self.assertFalse(result.isError)
        self.assertEqual(server.PreparedVideoAnalysisResult.model_validate(result.structuredContent).status, "busy")

    def test_server_limit_applies_when_argument_is_omitted(self):
        with patch.object(server, "MCP_MAX_SEGMENTS", 1):
            with self.assertRaisesRegex(ValueError, "max_segments"):
                server.segment_bounds(301, 120, None)

    def test_media_accepts_four_digit_indices_without_loosening_paths(self):
        with tempfile.TemporaryDirectory() as raw, patch.object(server, "MCP_JOBS_ROOT", Path(raw)):
            folder = Path(raw) / "0123456789ab" / "segments"
            folder.mkdir(parents=True)
            target = folder / "segment_1000.mp4"
            target.write_bytes(b"mp4")
            self.assertEqual(server.safe_media_path(folder.parent.name, "segments/segment_1000.mp4"), target.resolve())
            self.assertIsNone(server.safe_media_path(folder.parent.name, "segments/../segment_1000.mp4"))

    def test_old_nominal_timeline_cache_is_not_reused(self):
        with patch.object(server.pipeline, "file_fingerprint", return_value="same"):
            manifest = {"version": 1, "segment_seconds": 120, "max_segments": None,
                        "source": {"fingerprint": "same"}, "segments": [{"filename": "segment_000.mp4"}]}
            with tempfile.TemporaryDirectory() as raw:
                source = Path(raw) / "source.mp4"
                (Path(raw) / "segments").mkdir()
                (Path(raw) / "segments" / "segment_000.mp4").write_bytes(b"mp4")
                self.assertFalse(server.manifest_is_ready(manifest, source, 120, None))

    def test_prepare_publishes_observed_times_and_invalidates_old_ready_first(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            job_dir = root / "0123456789ab"
            (job_dir / "segments").mkdir(parents=True)
            source = job_dir / "source.mp4"
            source.write_bytes(b"source")
            parts = [(job_dir / "segments" / f"segment_{i:03}.mp4", start, end)
                     for i, (start, end) in enumerate([(0, 15.143), (15.143, 30.286), (30.286, 31.143)])]
            for path, _, _ in parts:
                path.write_bytes(b"video")
            server.pipeline.write_json(job_dir / "manifest.json", {"version": 1, "status": "ready"})
            def split(*_args):
                self.assertIsNone(server.read_manifest(job_dir))
                return parts
            with (patch.object(server, "MCP_JOBS_ROOT", root),
                  patch.object(server, "validate_source_url", side_effect=lambda url: url),
                  patch.object(server.pipeline, "job_key", return_value=job_dir.name),
                  patch.object(server.pipeline, "probe_video", return_value={"format": {"duration": "31.143"}, "streams": [{"codec_type": "video"}]}),
                  patch.object(server, "split_video", side_effect=split),
                  patch.object(server, "extract_preview", return_value=False),
                  patch.object(server, "require_disk_space")):
                result = server.prepare_video_sync("https://example.test/video", 15, None)
            self.assertEqual(result["version"], 2)
            self.assertEqual([(r["start_seconds"], r["end_seconds"]) for r in result["segments"]],
                             [(start, end) for _, start, end in parts])

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_real_segments_use_muxer_timestamps_not_requested_boundaries(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / "source.mp4"
            server.pipeline.run_external([
                server.pipeline.require_tool("ffmpeg"), "-v", "error", "-f", "lavfi", "-i",
                "testsrc2=size=64x64:rate=7/2:duration=31", "-f", "lavfi", "-i",
                "sine=frequency=880:duration=31", "-c:v", "libx264", "-c:a", "aac", str(source)])
            # 邊界取自 muxer 的實際關鍵畫面，需要重新編碼模式（copy 不強制關鍵畫面）。
            with patch.dict(os.environ, {"MCP_SEGMENT_MODE": "transcode"}):
                parts = server.split_video(source, root / "segments", 15)
            rows = list(csv.reader((root / "segments" / "segments.csv").read_text().splitlines()))
            self.assertEqual(len(parts), 3)
            self.assertNotEqual(float(rows[1][1]), 15.0)
            for (path, start, end), row in zip(parts, rows, strict=True):
                self.assertEqual(path.name, row[0])
                self.assertEqual((start, end), (float(row[1]), float(row[2])))
                self.assertTrue(path.is_file())
                server.pipeline.run_external([server.pipeline.require_tool("ffmpeg"), "-v", "error",
                                              "-i", str(path), "-f", "null", "-"])
            self.assertAlmostEqual(parts[-1][2], 218 / 7, places=3)
            self.assertAlmostEqual(parts[1][1], 106 / 7, places=3)

    def test_busy_and_low_disk_fail_without_starting_work(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        operation = Mock()
        with server.JOB_LOCK:
            with self.assertRaisesRegex(RuntimeError, "請稍後重試"):
                server.run_job(operation)
        operation.assert_not_called()
        with self.assertRaisesRegex(ValueError, "failed"):
            server.run_job(lambda: (_ for _ in ()).throw(ValueError("failed")))
        self.assertFalse(server.JOB_LOCK.locked())
        with tempfile.TemporaryDirectory() as raw, patch.object(server, "MCP_JOBS_ROOT", Path(raw)):
            with patch.object(server.shutil, "disk_usage", return_value=SimpleNamespace(free=1)):
                with self.assertRaisesRegex(RuntimeError, "磁碟空間不足"):
                    server.require_disk_space(10)

    def test_drive_busy_returns_retryable_state_without_starting_upload(self):
        with patch.object(server, "upload_prepared_drive_sync") as upload:
            with server.JOB_LOCK:
                result = asyncio.run(server.mcp._tool_manager.call_tool(
                    "upload_prepared_video_to_drive", {"job_id": "0123456789ab"}, convert_result=True))
            upload.assert_not_called()
        self.assertFalse(result.isError)
        state = server.DriveUploadResult.model_validate(result.structuredContent)
        self.assertEqual(state.status, "busy")
        self.assertEqual(state.error_code, "MCP_BUSY")
        self.assertEqual(state.retry_after_seconds, 5)
        self.assertEqual(state.files, [])
        self.assertIn("尚未分析或上傳", result.content[0].text)
        denied = server.drive_tool_error_result(RuntimeError("HTTP 403"))
        self.assertTrue(denied.isError)
        self.assertEqual(denied.structuredContent["status"], "error")
        self.assertFalse(server.JOB_LOCK.locked())

    def test_database_handles_close_even_when_schema_setup_fails(self):
        from unittest.mock import MagicMock
        import cost_tracker
        import notifications
        connection = MagicMock()
        with (patch.object(notifications.sqlite3, "connect", return_value=connection),
              patch.object(notifications, "_init_operations_schema", side_effect=RuntimeError("schema"))):
            with self.assertRaisesRegex(RuntimeError, "schema"):
                notifications.get_operations_db(Path("unused"))
        connection.close.assert_called_once()
        connection.reset_mock()
        with (patch.object(cost_tracker, "get_operations_db", return_value=connection),
              patch.object(cost_tracker, "_init_cost_schema", side_effect=RuntimeError("schema"))):
            with self.assertRaisesRegex(RuntimeError, "schema"):
                cost_tracker.get_cost_report(Path("unused"))
        connection.close.assert_called_once()

    def test_drive_partial_and_lost_responses_reuse_ids_and_verify_cache(self):
        # Exercise a real on-disk journal, including a restart after Drive committed the bytes.
        for committed in (False, True):
            with self.subTest(committed=committed), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                (root / "segments").mkdir()
                for i in range(2):
                    (root / "segments" / f"segment_{i:03}.mp4").write_bytes(f"video-{i}".encode())
                rows = [{"filename": f"segment_{i:03}.mp4"} for i in range(2)]
                remote, uploaded, folders = {}, [], []
                next_id, fail = 0, True

                def reserve(_token):
                    nonlocal next_id
                    next_id += 1
                    return f"id_{next_id}"

                def folder(_token, name, parent, file_id=None):
                    folders.append(file_id)
                    remote[file_id] = {"id": file_id, "mimeType": drive.FOLDER_MIME, "parents": [],
                                       "webViewLink": f"https://drive.google.com/drive/folders/{file_id}"}
                    return remote[file_id]

                def upload(_token, path, name, parent, *, file_id):
                    nonlocal fail
                    uploaded.append(file_id)
                    should_fail = name == "segment_001.mp4" and fail
                    if not should_fail or committed:
                        remote[file_id] = {"id": file_id, "mimeType": "video/mp4", "parents": [parent],
                            "size": str(path.stat().st_size), "sha256Checksum": drive._sha256(path),
                            "webViewLink": f"https://drive.google.com/file/d/{file_id}/view"}
                    if should_fail:
                        fail = False
                        raise RuntimeError("lost response")
                    return remote[file_id]

                with (patch.object(drive, "configured", return_value=True),
                      patch.object(drive, "_access_token", return_value="test"),
                      patch.object(drive, "_env", return_value=""),
                      patch.object(drive, "_new_id", side_effect=reserve),
                      patch.object(drive, "_create_folder", side_effect=folder),
                      patch.object(drive, "_upload_file", side_effect=upload),
                      patch.object(drive, "_get_file", side_effect=lambda _, fid: remote.get(fid))):
                    args = (root, "0123456789ab", "demo", "source", rows)
                    with self.assertRaisesRegex(RuntimeError, "lost response"):
                        drive.upload_prepared_segments(*args)
                    journal = json.loads((root / "drive-manifest.json").read_text())
                    self.assertEqual(journal["status"], "uploading")
                    second = drive.upload_prepared_segments(*args)
                    self.assertEqual(second["status"], "drive_ready")
                    self.assertEqual(folders, ["id_1"])
                    self.assertEqual(uploaded, ["id_2", "id_3"] if committed else ["id_2", "id_3", "id_3"])
                    self.assertEqual(drive.upload_prepared_segments(*args), second)
                    # Deleted or edited files must not be returned as ready or overwritten.
                    del remote["id_2"]
                    repaired = drive.upload_prepared_segments(*args)
                    self.assertEqual(repaired["files"][0]["file_id"], "id_4")
                    self.assertEqual(repaired["files"][1]["file_id"], "id_3")
                    remote["id_3"]["sha256Checksum"] = "edited-by-user"
                    repaired = drive.upload_prepared_segments(*args)
                    self.assertEqual(repaired["files"][1]["file_id"], "id_5")
                    self.assertEqual(remote["id_3"]["sha256Checksum"], "edited-by-user")
                    before = list(uploaded)
                    with patch.object(drive, "_get_file", side_effect=drive.DriveAPIError(403)):
                        with self.assertRaisesRegex(RuntimeError, "403"):
                            drive.upload_prepared_segments(*args)
                    self.assertEqual(uploaded, before)
                    self.assertEqual(json.loads((root / "drive-manifest.json").read_text())["status"], "uploading")

    def test_drive_conflict_reads_back_reserved_id_and_get_has_no_body(self):
        from unittest.mock import MagicMock
        metadata = {"id": "reserved", "webViewLink": "https://drive.google.com/file/d/reserved/view"}
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "video.mp4"
            path.write_bytes(b"video")
            with (patch.object(drive.urllib.request, "urlopen", side_effect=urllib.error.HTTPError("https://example.test", 409, "conflict", {}, None)),
                  patch.object(drive, "_get_file", return_value=metadata) as readback):
                self.assertEqual(drive._upload_file("test", path, path.name, "folder", file_id="reserved"), metadata)
                readback.assert_called_once_with("test", "reserved")
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(metadata).encode()
        with patch.object(drive.urllib.request, "urlopen", return_value=response) as request:
            drive._get_file("test", "reserved")
            self.assertIsNone(request.call_args.args[0].data)
            self.assertEqual(request.call_args.args[0].method, "GET")


if __name__ == "__main__":
    unittest.main()
