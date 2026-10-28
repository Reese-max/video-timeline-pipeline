import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pipeline


class YtdlpResolutionTests(unittest.TestCase):
    def test_resolution_options(self):
        for value, expected in [(None, []), ("", []), ("  ", []), ("720", ["-S", "res:720"]),
                                (" 1080 ", ["-S", "res:1080"]), ("1", ["-S", "res:1"]),
                                ("4320", ["-S", "res:4320"])]:
            with self.subTest(value=value), patch.dict(os.environ, {}, clear=True):
                if value is not None:
                    os.environ["YTDLP_MAX_HEIGHT"] = value
                self.assertEqual(pipeline.ytdlp_resolution_options(), expected)

    def test_invalid_resolution(self):
        for value in ["0", "-1", "720p", "1.5", "abc", "+720", "7_20", "７２０",
                      "4321", "9999", "10000", "9" * 5000]:
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory, \
                    patch.dict(os.environ, {"YTDLP_MAX_HEIGHT": value}), \
                    patch.object(pipeline, "run_external") as run:
                with self.assertRaisesRegex(ValueError, "YTDLP_MAX_HEIGHT 必須是正整數"):
                    pipeline.download_video("https://youtu.be/example", Path(directory))
                run.assert_not_called()

    def test_download_command(self):
        for height in ["", "720"]:
            for url, dispatch in [("https://youtu.be/example", "run_external"),
                                  ("https://vimeo.com/123", "run_external"),
                                  ("https://www.facebook.com/reel/123", "download_facebook"),
                                  ("https://www.instagram.com/reel/123", "download_instagram")]:
                with self.subTest(height=height, url=url), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    (root / "source.mp4").write_bytes(b"video")
                    with patch.dict(os.environ, {"YTDLP_MAX_HEIGHT": height}), \
                            patch.object(pipeline, "yt_dlp_command", return_value=["yt-dlp"]), \
                            patch.object(pipeline, dispatch) as run:
                        pipeline.download_video(url, root)
                    command = run.call_args.args[0]
                    self.assertEqual(command[-1], url)
                    if height:
                        self.assertEqual(command[command.index("-S") + 1], "res:720")
                    else:
                        self.assertNotIn("-S", command)

    def test_social_download_keeps_resolution_in_extractor_command(self):
        for url in ["https://www.facebook.com/reel/123", "https://www.instagram.com/reel/123"]:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "source.mp4").write_bytes(b"video")
                with patch.dict(os.environ, {"YTDLP_MAX_HEIGHT": "720"}, clear=True), \
                        patch.object(pipeline, "yt_dlp_command", return_value=["yt-dlp"]), \
                        patch.object(pipeline, "instagram_download_diagnostics", return_value={"chrome_impersonation": True}), \
                        patch.object(pipeline, "run_external") as run:
                    pipeline.download_video(url, root)
                command = run.call_args.args[0]
                self.assertEqual(command[command.index("-S") + 1], "res:720")
                self.assertEqual(command[-1], url)


if __name__ == "__main__":
    unittest.main()
