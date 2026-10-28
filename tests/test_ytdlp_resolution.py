import os
from unittest.mock import patch

import pytest
import pipeline


@pytest.mark.parametrize("value,expected", [(None, []), ("", []), ("  ", []), ("720", ["-S", "res:720"]),
                                           (" 1080 ", ["-S", "res:1080"])])
def test_resolution_options(value, expected):
    with patch.dict(os.environ, {}, clear=True):
        if value is not None:
            os.environ["YTDLP_MAX_HEIGHT"] = value
        assert pipeline.ytdlp_resolution_options() == expected


@pytest.mark.parametrize("value", ["0", "-1", "720p", "1.5", "abc", "+720", "7_20", "７２０", "9" * 5000])
def test_invalid_resolution(value, tmp_path):
    with patch.dict(os.environ, {"YTDLP_MAX_HEIGHT": value}), patch.object(pipeline, "run_external") as run:
        with pytest.raises(ValueError, match="YTDLP_MAX_HEIGHT 必須是正整數"):
            pipeline.download_video("https://youtu.be/example", tmp_path)
        run.assert_not_called()


@pytest.mark.parametrize("url,dispatch", [("https://youtu.be/example", "run_external"),
                                        ("https://vimeo.com/123", "run_external"),
                                        ("https://www.facebook.com/reel/123", "download_facebook"),
                                        ("https://www.instagram.com/reel/123", "download_instagram")])
@pytest.mark.parametrize("height", ["", "720"])
def test_download_command(height, url, dispatch, tmp_path):
    (tmp_path / "source.mp4").write_bytes(b"video")
    with patch.dict(os.environ, {"YTDLP_MAX_HEIGHT": height}), \
            patch.object(pipeline, "yt_dlp_command", return_value=["yt-dlp"]), \
            patch.object(pipeline, dispatch) as run:
        pipeline.download_video(url, tmp_path)
    command = run.call_args.args[0]
    assert command[-1] == url
    if height:
        assert command[command.index("-S") + 1] == "res:720"
    else:
        assert "-S" not in command
