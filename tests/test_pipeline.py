import unittest
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pipeline


class PipelineTests(unittest.TestCase):
    def test_showinfo_timestamps_are_original_pts(self):
        log = "\n".join(
            [
                "[Parsed_showinfo_0 @ 0x1] n:0 pts:147000 pts_time:4.9",
                "[Parsed_showinfo_0 @ 0x1] n:1 pts:447000 pts_time:14.9",
                "[Parsed_showinfo_0 @ 0x1] n:2 pts:747000 pts_time:24.9",
            ]
        )
        self.assertEqual(pipeline.parse_showinfo_timestamps(log), [4.9, 14.9, 24.9])

    def test_chunk_defaults_use_requested_window(self):
        specs = pipeline.normalize_chunk_specs(["a.mp3", "b.mp3"], 300)
        self.assertEqual((specs[0]["start"], specs[0]["end"]), (0.0, 300.0))
        self.assertEqual((specs[1]["start"], specs[1]["end"]), (300.0, 600.0))

    def test_audio_resume_reextracts_only_missing_chunk(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            calls = []
            original_run = pipeline.run_external
            original_require = pipeline.require_tool

            def fake_run(command, *, cwd=None, stream=False):
                calls.append(command[-1])
                Path(command[-1]).write_bytes(b"audio")

            pipeline.run_external = fake_run
            pipeline.require_tool = lambda name: name
            try:
                probe = {"format": {"duration": "11"}, "streams": [{"codec_type": "audio"}]}
                chunks = pipeline.extract_audio(
                    root / "video.mp4",
                    root / "job",
                    5,
                    1,
                    "source",
                    11,
                    True,
                    probe,
                )
                calls.clear()
                Path(chunks[1]["path"]).unlink()
                resumed = pipeline.extract_audio(
                    root / "video.mp4",
                    root / "job",
                    5,
                    1,
                    "source",
                    11,
                    False,
                    probe,
                )
            finally:
                pipeline.run_external = original_run
                pipeline.require_tool = original_require
            self.assertEqual(len(resumed), len(chunks))
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0].endswith("audio_0001.mp3"))

    def test_caption_selection_prefers_original_manual_track(self):
        info = {
            "language": "zh-Hant",
            "subtitles": {"en": [{}], "zh-Hant": [{}]},
            "automatic_captions": {"en": [{}], "ja": [{}]},
        }
        tracks = pipeline.choose_caption_tracks(info, "auto")
        self.assertEqual(tracks[0]["language"], "zh-Hant")
        self.assertEqual(tracks[0]["kind"], "manual")

    def test_stage_cache_requires_matching_signature_and_artifact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            artifact = root / "artifact.json"
            pipeline.write_json(artifact, {"value": 1})
            manifest = {}
            signature = pipeline.stage_signature("test", "source", {"v": 1})
            pipeline.set_stage_record(manifest, "test", signature, artifact)
            self.assertEqual(
                pipeline.stage_cache_value(manifest["stages"], "test", artifact, signature),
                {"value": 1},
            )
            other_signature = pipeline.stage_signature("test", "source", {"v": 2})
            self.assertIsNone(
                pipeline.stage_cache_value(manifest["stages"], "test", artifact, other_signature)
            )

    def test_summary_chunks_keep_the_tail(self):
        timeline = [
            {"timestamp": float(index), "type": "speech", "text": "x" * 30}
            for index in range(8)
        ]
        chunks = pipeline.timeline_chunks(timeline, 100, overlap_events=1)
        self.assertGreater(len(chunks), 1)
        self.assertEqual(chunks[-1][-1]["timestamp"], 7.0)

    def test_summary_source_timestamps_must_match_timeline(self):
        timeline = [{"timestamp": 10, "end": 12, "type": "speech", "text": "evidence"}]
        summary = {
            "short_summary": "摘要",
            "chapters": [],
            "steps": [],
            "tools_and_terms": [],
            "fact_check": [],
            "source_timestamps": [11],
        }
        self.assertEqual(pipeline.validate_summary_sources(summary, timeline), [])
        summary["source_timestamps"] = [40]
        self.assertTrue(pipeline.validate_summary_sources(summary, timeline))

    def test_failed_index_cleanup_preserves_shared_vector(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = pipeline.index_connection(root)
            failed_job = root / "jobs" / "failed123456"
            good_job = root / "jobs" / "good1234567"
            failed_job.mkdir(parents=True)
            good_job.mkdir(parents=True)
            pipeline.write_json(failed_job / "manifest.json", {"status": "failed"})
            pipeline.write_json(
                good_job / "manifest.json",
                {
                    "status": "completed",
                    "source": {"title": "good", "url": "https://example.com/good"},
                    "video": {"duration_seconds": 1},
                },
            )
            connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?)",
                (failed_job.name, 1, "speech", "shared"),
            )
            connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?)",
                (failed_job.name, 2, "speech", "unique"),
            )
            connection.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?)",
                (good_job.name, 1, "speech", "shared"),
            )
            connection.execute(
                "INSERT INTO vectors VALUES (?, ?, ?, ?)",
                (pipeline.text_hash("shared"), "model", 1, b"0"),
            )
            connection.execute(
                "INSERT INTO vectors VALUES (?, ?, ?, ?)",
                (pipeline.text_hash("unique"), "model", 1, b"0"),
            )
            pipeline.index_job(connection, failed_job)
            connection.commit()
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM events WHERE job = ?", (failed_job.name,)
                ).fetchone()[0],
                0,
            )
            self.assertIsNotNone(
                connection.execute(
                    "SELECT 1 FROM vectors WHERE text_hash = ?", (pipeline.text_hash("shared"),)
                ).fetchone()
            )
            self.assertIsNone(
                connection.execute(
                    "SELECT 1 FROM vectors WHERE text_hash = ?", (pipeline.text_hash("unique"),)
                ).fetchone()
            )
            connection.close()

    def test_answer_context_includes_adjacent_original_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            job = root / "jobs" / "context123456"
            job.mkdir(parents=True)
            pipeline.write_json(
                job / "manifest.json",
                {
                    "status": "completed",
                    "source": {"title": "context", "url": "https://example.com/v"},
                    "video": {"duration_seconds": 60},
                },
            )
            pipeline.write_json(
                job / "timeline.json",
                [
                    {"timestamp": 10, "type": "speech", "text": "原始語音證據"},
                    {
                        "timestamp": 12,
                        "type": "visual",
                        "data": {"visual_summary": "同時段畫面證據"},
                    },
                ],
            )
            pipeline.build_index(root)
            hits = pipeline.search_index(root, "原始語音", 5)
            expanded = pipeline.expand_hit_context(root, hits)
            self.assertIn("同時段畫面證據", expanded[0]["context_text"])


if __name__ == "__main__":
    unittest.main()
