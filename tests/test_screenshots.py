"""Tests for frame extraction and marker replacement.

The frame-extraction tests build a tiny video with ffmpeg, so they exercise the
real subprocess path; they are skipped when ffmpeg is not installed.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from src.srt_daemon.config import Config
from src.srt_daemon.pipeline import generate_notes
from src.srt_daemon.screenshots import (
    Marker,
    _detail_score,
    extract_best_frame,
    extract_frame,
    parse_markers,
    process_markdown,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS = PROJECT_ROOT / "doc" / "prompts"

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)


@pytest.fixture
def shot_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Config with screenshots enabled and temporary directories."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key-shots")

    watch = tmp_path / "watch"
    output = tmp_path / "output"
    logs = tmp_path / "logs"
    for directory in (watch, output, logs):
        directory.mkdir()

    data = {
        "watch": {"directory": str(watch), "extensions": [".srt", ".vtt"]},
        "output": {"directory": str(output), "filename_template": "{basename}_notes_{date}.md"},
        "ai": {
            "model": "deepseek-chat",
            "system_prompt_file": str(PROMPTS / "course.md"),
            "profiles": {
                "course": {"prompt_file": str(PROMPTS / "course.md")},
                "meeting": {"prompt_file": str(PROMPTS / "meeting.md")},
                "interview": {"prompt_file": str(PROMPTS / "interview.md")},
            },
        },
        "logging": {"file": str(logs / "test.log"), "level": "DEBUG"},
        "state_file": str(output / ".processed.json"),
        "screenshots": {"enabled": True, "max_count": 3, "width": 320, "quality": 5},
    }
    return Config(data, PROJECT_ROOT)


def make_video(path: Path, seconds: int = 10) -> Path:
    """Render a tiny test video with ffmpeg."""
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


# =============================================================================
# Marker parsing
# =============================================================================


class TestMarkers:
    def test_single_marker(self) -> None:
        markers = parse_markers("text\n[[screenshot:00:03:21]]\nmore")

        assert len(markers) == 1
        assert markers[0].seconds == 201
        assert markers[0].raw == "[[screenshot:00:03:21]]"

    def test_hours_are_supported(self) -> None:
        markers = parse_markers("[[screenshot:01:23:45]]")

        assert markers[0].seconds == 5025

    def test_multiple_and_case_insensitive(self) -> None:
        text = "[[screenshot:00:00:05]] and [[SCREENSHOT:00:00:09]]"

        assert [m.seconds for m in parse_markers(text)] == [5, 9]

    def test_no_markers(self) -> None:
        assert parse_markers("just text") == []

    def test_malformed_ignored(self) -> None:
        assert parse_markers("[[screenshot:3:21]]") == []
        assert parse_markers("[[screenshot:00-03-21]]") == []

    def test_filename(self) -> None:
        assert Marker(raw="", seconds=3723).filename == "shot-010203.jpg"
        assert Marker(raw="", seconds=5).filename == "shot-000005.jpg"


# =============================================================================
# Marker replacement
# =============================================================================


class TestProcessMarkdown:
    @needs_ffmpeg
    def test_extracts_and_rewrites(self, shot_config: Config, tmp_path: Path) -> None:
        video = make_video(tmp_path / "v.mp4", seconds=10)
        markdown = "## A\n\n[[screenshot:00:00:02]]\n\n## B\n\n[[screenshot:00:00:07]]\n"

        result = process_markdown(markdown, video, tmp_path / "img", config=shot_config)

        assert len(result.images) == 2
        assert result.skipped == 0
        assert "![](images/shot-000002.jpg)" in result.markdown
        assert "![](images/shot-000007.jpg)" in result.markdown
        assert "[[screenshot" not in result.markdown
        for image in result.images:
            assert image.exists() and image.stat().st_size > 0

    def test_no_video_drops_markers(self, shot_config: Config, tmp_path: Path) -> None:
        markdown = "text\n\n[[screenshot:00:00:05]]\n\nmore"

        result = process_markdown(markdown, None, tmp_path / "img", config=shot_config)

        assert result.images == []
        assert result.found == 1
        assert result.skipped == 1
        assert "[[screenshot" not in result.markdown
        assert "text" in result.markdown and "more" in result.markdown

    @needs_ffmpeg
    def test_max_count_cap(self, shot_config: Config, tmp_path: Path) -> None:
        video = make_video(tmp_path / "v.mp4", seconds=8)
        markdown = "\n".join(
            f"[[screenshot:00:00:0{n}]]" for n in range(1, 6)
        )

        result = process_markdown(markdown, video, tmp_path / "img", config=shot_config)

        assert len(result.images) == 3  # max_count in the fixture
        assert result.skipped == 2

    @needs_ffmpeg
    def test_duplicate_timestamps_collapse(self, shot_config: Config, tmp_path: Path) -> None:
        video = make_video(tmp_path / "v.mp4", seconds=6)
        markdown = "[[screenshot:00:00:02]]\n[[screenshot:00:00:02]]"

        result = process_markdown(markdown, video, tmp_path / "img", config=shot_config)

        assert len(result.images) == 1

    def test_no_markers_is_a_noop(self, shot_config: Config, tmp_path: Path) -> None:
        result = process_markdown("plain note", None, tmp_path / "img", config=shot_config)

        assert result.markdown == "plain note"
        assert result.found == 0


# =============================================================================
# Pipeline integration
# =============================================================================


class TestPipelineScreenshots:
    @needs_ffmpeg
    def test_note_contains_images(self, shot_config: Config, tmp_path: Path) -> None:
        subtitle = tmp_path / "talk.vtt"
        subtitle.write_text(
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:04.000\n这一页幻灯片展示了电流波形\n\n"
            "00:00:05.000 --> 00:00:08.000\n接下来是结论\n",
            encoding="utf-8",
        )
        video = make_video(tmp_path / "v.mp4", seconds=10)
        ai_output = "## 波形\n\n[[screenshot:00:00:02]]\n\n## 结论\n\ndone"

        with patch("src.srt_daemon.ai_client.AiClient.process", return_value=ai_output), patch(
            "src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None
        ):
            result = generate_notes(
                shot_config,
                subtitle,
                forced_profile="course",
                video_path=video,
                screenshots=True,
            )

        text = result.output_path.read_text(encoding="utf-8")
        assert "![](images/" in text
        assert "images: 1" in text
        images_dir = result.output_path.parent / "images"
        assert images_dir.is_dir()
        assert len(list(images_dir.glob("*.jpg"))) == 1

    def test_markers_dropped_without_video(self, shot_config: Config, tmp_path: Path) -> None:
        subtitle = tmp_path / "talk.vtt"
        subtitle.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:04.000\nhello\n", encoding="utf-8"
        )

        with patch(
            "src.srt_daemon.ai_client.AiClient.process",
            return_value="text\n\n[[screenshot:00:00:02]]\n",
        ), patch("src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None):
            result = generate_notes(
                shot_config, subtitle, forced_profile="course", screenshots=True
            )

        text = result.output_path.read_text(encoding="utf-8")
        assert "[[screenshot" not in text
        assert not (result.output_path.parent / "images").exists()

class TestNativeResolution:
    @needs_ffmpeg
    def test_width_zero_keeps_the_source_size(self, tmp_path: Path) -> None:
        from PIL import Image

        video = make_video(tmp_path / "clip.mp4", seconds=4)
        target = tmp_path / "shot.jpg"

        ok = extract_frame(
            shutil.which("ffmpeg"), video, 1, target, width=0, quality=5
        )

        assert ok
        with Image.open(target) as image:
            assert image.width == 320  # the size make_video renders

    @needs_ffmpeg
    def test_width_scales_when_asked(self, tmp_path: Path) -> None:
        from PIL import Image

        video = make_video(tmp_path / "clip.mp4", seconds=4)
        target = tmp_path / "shot.jpg"

        assert extract_frame(
            shutil.which("ffmpeg"), video, 1, target, width=160, quality=5
        )
        with Image.open(target) as image:
            assert image.width == 160


class TestBestFrame:
    @needs_ffmpeg
    def test_moves_past_a_blank_leader(self, tmp_path: Path) -> None:
        """A marker on a blank first seconds should land on real content."""
        video = tmp_path / "clip.mp4"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-v", "error",
                "-f", "lavfi", "-i", "color=c=black:s=320x240:d=3",
                "-f", "lavfi", "-i", "testsrc=size=320x240:rate=5:duration=5",
                "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
                "-pix_fmt", "yuv420p", str(video),
            ],
            check=True,
            capture_output=True,
        )
        target = tmp_path / "shot.jpg"

        ok, offset = extract_best_frame(
            shutil.which("ffmpeg"), video, 0, target, width=0, quality=5, window=6
        )

        assert ok
        assert offset >= 2, f"expected to skip the black leader, used offset {offset}"
        assert _detail_score(target) > 5

    @needs_ffmpeg
    def test_window_zero_uses_the_exact_timestamp(self, tmp_path: Path) -> None:
        video = make_video(tmp_path / "clip.mp4", seconds=6)
        target = tmp_path / "shot.jpg"

        ok, offset = extract_best_frame(
            shutil.which("ffmpeg"), video, 2, target, window=0
        )

        assert ok and offset == 0

    def test_default_window_is_six_seconds(self, shot_config: Config) -> None:
        assert shot_config.screenshots_best_frame_window == 6
