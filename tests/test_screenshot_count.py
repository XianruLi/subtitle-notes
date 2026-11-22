"""Dynamic screenshot count: how many frames a video of a given length gets."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.srt_daemon.config import Config
from src.srt_daemon.pipeline import generate_notes
from src.srt_daemon.screenshots import probe_duration

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)


def _video(path: Path, seconds: int = 6) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-nostdin", "-y", "-v", "error",
            "-f", "lavfi", "-i", f"testsrc=size=320x240:rate=5:duration={seconds}",
            "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


class TestTargetMath:
    def test_twenty_minutes_gives_ten(self, temp_config: Config) -> None:
        assert temp_config.screenshot_target(20 * 60) == 10

    def test_short_clip_keeps_the_minimum(self, temp_config: Config) -> None:
        assert temp_config.screenshot_target(30) == 3

    def test_long_video_is_capped(self, temp_config: Config) -> None:
        assert temp_config.screenshot_target(300 * 60) == temp_config.screenshots_max_count

    def test_unknown_duration_returns_none(self, temp_config: Config) -> None:
        assert temp_config.screenshot_target(None) is None

    def test_disabled_returns_none(self, temp_config: Config) -> None:
        temp_config._data["screenshots"] = {"enabled": True, "dynamic_count": False}

        assert temp_config.screenshot_target(20 * 60) is None

    def test_per_minute_is_configurable(self, temp_config: Config) -> None:
        temp_config._data["screenshots"] = {
            "enabled": True,
            "dynamic_count": True,
            "per_minute": 1.0,
            "min_count": 3,
            "max_count": 60,
        }

        assert temp_config.screenshot_target(30 * 60) == 30


@needs_ffmpeg
class TestProbe:
    def test_reads_the_duration(self, tmp_path: Path) -> None:
        video = _video(tmp_path / "clip.mp4", seconds=6)

        duration = probe_duration(shutil.which("ffmpeg"), video)

        assert duration is not None
        assert 5.0 <= duration <= 7.5


@needs_ffmpeg
class TestPipelinePassesTheTarget:
    def _run(self, temp_config: Config, tmp_path: Path):
        project = tmp_path / "proj"
        project.mkdir()
        subtitle = project / "talk.srt"
        subtitle.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nhello\n", encoding="utf-8"
        )
        _video(project / "talk.mp4", seconds=5)
        temp_config._data["screenshots"] = {
            "enabled": True,
            "dynamic_count": True,
            "per_minute": 0.5,
            "max_count": 40,
        }
        with patch(
            "src.srt_daemon.screenshots.probe_duration", return_value=20 * 60
        ), patch(
            "src.srt_daemon.ai_client.AiClient.process", return_value="body"
        ) as process, patch(
            "src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None
        ):
            generate_notes(temp_config, subtitle)
        return process

    def test_model_is_told_the_range(self, temp_config: Config, tmp_path: Path) -> None:
        process = self._run(temp_config, tmp_path)

        assert process.call_args.kwargs["screenshot_target"] == 10

    def test_hint_contains_the_range(self, temp_config: Config) -> None:
        from src.srt_daemon.ai_client import AiClient

        client = AiClient(temp_config)
        block = client._build_context_block(
            "a.vtt", None, None, "SRT", has_video=True, screenshot_target=10
        )

        assert "6–10" in block

    def test_default_range_without_target(self, temp_config: Config) -> None:
        from src.srt_daemon.ai_client import AiClient

        client = AiClient(temp_config)
        block = client._build_context_block(
            "a.vtt", None, None, "SRT", has_video=True
        )

        assert "至少 3 个、最多 8 个" in block
