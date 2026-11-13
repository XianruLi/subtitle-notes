"""Local recordings: source sidecars, watch-dir media and the fetch switch."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.srt_daemon.config import Config
from src.srt_daemon.pipeline import (
    find_sibling_video,
    find_source_video,
    generate_notes,
    write_source_sidecar,
)
from src.srt_daemon.watcher import FileWatcher

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)


def make_video(path: Path, seconds: int = 6) -> Path:
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


def enable_media(config: Config) -> Config:
    """Give the config a transcribe block that accepts recordings."""
    config._data["transcribe"] = {
        "extensions": [".mp4", ".mkv", ".mp3"],
        "auto_transcribe_media": True,
        "output_format": "vtt",
        "output_dir": str(config.watch_dir),
    }
    return config


# =============================================================================
# Source sidecar
# =============================================================================


class TestSourceSidecar:
    def test_roundtrip(self, tmp_path: Path) -> None:
        video = tmp_path / "talk.mp4"
        video.write_bytes(b"video")
        subtitle = tmp_path / "talk.vtt"
        subtitle.write_text("WEBVTT\n", encoding="utf-8")

        sidecar = write_source_sidecar(subtitle, video, 12.5)

        assert sidecar.name == "talk.source.json"
        assert find_source_video(subtitle) == video.resolve()

    def test_missing_sidecar(self, tmp_path: Path) -> None:
        assert find_source_video(tmp_path / "nothing.vtt") is None

    def test_missing_video_is_ignored(self, tmp_path: Path) -> None:
        subtitle = tmp_path / "a.vtt"
        write_source_sidecar(subtitle, tmp_path / "deleted.mp4")

        assert find_source_video(subtitle) is None

    def test_broken_sidecar_is_ignored(self, tmp_path: Path) -> None:
        subtitle = tmp_path / "a.vtt"
        (tmp_path / "a.source.json").write_text("{not json", encoding="utf-8")

        assert find_source_video(subtitle) is None


# =============================================================================
# Notes pipeline uses the sidecar for screenshots
# =============================================================================


class TestPipelineUsesSidecar:
    @needs_ffmpeg
    def test_screenshots_from_sidecar_video(
        self, temp_config: Config, tmp_path: Path
    ) -> None:
        temp_config._data["screenshots"] = {
            "enabled": True,
            "max_count": 3,
            "width": 320,
            "quality": 5,
        }
        subtitle = temp_config.watch_dir / "lecture.vtt"
        subtitle.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello\n", encoding="utf-8"
        )
        video = make_video(tmp_path / "lecture.mp4", 6)
        write_source_sidecar(subtitle, video, 6)

        with patch(
            "src.srt_daemon.ai_client.AiClient.process",
            return_value="body\n\n[[screenshot:00:00:02]]\n",
        ), patch(
            "src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None
        ):
            result = generate_notes(temp_config, subtitle)

        text = result.output_path.read_text(encoding="utf-8")
        assert "![](images/" in text
        assert (result.output_path.parent / "images").is_dir()


# =============================================================================
# Watch directory accepts recordings
# =============================================================================


class TestWatchDirectoryMedia:
    def test_extensions_include_media(self, temp_config: Config) -> None:
        enable_media(temp_config)

        watcher = FileWatcher(temp_config)

        assert ".mp4" in watcher._allowed_extensions
        assert ".srt" in watcher._allowed_extensions

    def test_media_can_be_disabled(self, temp_config: Config) -> None:
        enable_media(temp_config)
        temp_config._data["transcribe"]["auto_transcribe_media"] = False

        watcher = FileWatcher(temp_config)

        assert ".mp4" not in watcher._allowed_extensions
        assert ".srt" in watcher._allowed_extensions

    def test_transcribe_media_returns_the_subtitle(self, temp_config: Config) -> None:
        enable_media(temp_config)
        media = temp_config.watch_dir / "lecture.mp4"
        media.write_bytes(b"fake")
        subtitle = temp_config.watch_dir / "lecture.vtt"
        subtitle.write_text("WEBVTT\n", encoding="utf-8")
        result = MagicMock()
        result.subtitles = [subtitle]

        watcher = FileWatcher(temp_config)
        with patch("src.srt_daemon.transcribe.Transcriber") as transcriber:
            transcriber.return_value.transcribe.return_value = result
            produced = watcher._transcribe_media(media)

        assert produced == subtitle

    def test_transcribe_media_reuses_existing_subtitle(self, temp_config: Config) -> None:
        enable_media(temp_config)
        media = temp_config.watch_dir / "lecture.mp4"
        media.write_bytes(b"fake")
        subtitle = temp_config.watch_dir / "lecture.vtt"
        subtitle.write_text("WEBVTT\n", encoding="utf-8")

        watcher = FileWatcher(temp_config)
        with patch("src.srt_daemon.transcribe.Transcriber") as transcriber:
            transcriber.return_value.transcribe.return_value = None
            produced = watcher._transcribe_media(media)

        assert produced == subtitle

    def test_transcription_failure_is_swallowed(self, temp_config: Config) -> None:
        enable_media(temp_config)
        media = temp_config.watch_dir / "bad.mp4"
        media.write_bytes(b"fake")

        watcher = FileWatcher(temp_config)
        with patch("src.srt_daemon.transcribe.Transcriber") as transcriber:
            transcriber.return_value.transcribe.side_effect = RuntimeError("boom")
            assert watcher._transcribe_media(media) is None


# =============================================================================
# Online-video switch
# =============================================================================


class TestFetchSwitch:
    def test_enabled_by_default(self, temp_config: Config) -> None:
        assert temp_config.fetch_enabled is True

    def test_can_be_disabled(self, temp_config: Config) -> None:
        temp_config._data["fetch"] = {"enabled": False}

        assert temp_config.fetch_enabled is False

    def test_cli_refuses_when_disabled(
        self, temp_config: Config, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from src.srt_daemon import fetch as fetch_module

        temp_config._data["fetch"] = {"enabled": False}
        monkeypatch.setattr(fetch_module, "load_config", lambda *a, **k: temp_config)
        monkeypatch.setattr(sys, "argv", ["prog", "https://youtu.be/x"])

        with pytest.raises(SystemExit) as excinfo:
            fetch_module.main()

        assert excinfo.value.code == 1



class TestSiblingVideo:
    """A downloaded video next to its subtitle supplies the screenshots."""

    def test_finds_the_same_stem_video(self, tmp_path: Path) -> None:
        subtitle = tmp_path / "lecture.vtt"
        subtitle.write_text("WEBVTT\n", encoding="utf-8")
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"x")

        assert find_sibling_video(subtitle) == video

    def test_ignores_audio_and_other_names(self, tmp_path: Path) -> None:
        subtitle = tmp_path / "lecture.vtt"
        subtitle.write_text("WEBVTT\n", encoding="utf-8")
        (tmp_path / "lecture.mp3").write_bytes(b"x")
        (tmp_path / "other.mp4").write_bytes(b"x")

        assert find_sibling_video(subtitle) is None

    def test_none_when_folder_is_empty(self, tmp_path: Path) -> None:
        subtitle = tmp_path / "a.vtt"
        subtitle.write_text("WEBVTT\n", encoding="utf-8")

        assert find_sibling_video(subtitle) is None

    @needs_ffmpeg
    def test_generate_notes_uses_the_sibling_video(
        self, temp_config: Config, tmp_path: Path
    ) -> None:
        temp_config._data["screenshots"] = {
            "enabled": True,
            "max_count": 2,
            "width": 320,
            "quality": 5,
        }
        subtitle = temp_config.watch_dir / "lecture.vtt"
        subtitle.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello\n", encoding="utf-8"
        )
        make_video(temp_config.watch_dir / "lecture.mp4", 6)

        with patch(
            "src.srt_daemon.ai_client.AiClient.process",
            return_value="body\n\n[[screenshot:00:00:02]]\n",
        ), patch(
            "src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None
        ):
            result = generate_notes(temp_config, subtitle)

        text = result.output_path.read_text(encoding="utf-8")
        assert "![](images/" in text
        assert (result.output_path.parent / "images").is_dir()


class TestWatchPrefersExistingSubtitle:
    def test_existing_subtitle_skips_transcription(self, temp_config: Config) -> None:
        enable_media(temp_config)
        media = temp_config.watch_dir / "lecture.mp4"
        media.write_bytes(b"fake")
        subtitle = temp_config.watch_dir / "lecture.vtt"
        subtitle.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello world\n", encoding="utf-8"
        )

        watcher = FileWatcher(temp_config)

        with patch(
            "src.srt_daemon.ai_client.AiClient.process", return_value="# Notes"
        ), patch(
            "src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None
        ), patch("src.srt_daemon.transcribe.Transcriber") as transcriber:
            watcher._process_file(media)

        transcriber.assert_not_called()
        assert list(temp_config.output_dir.rglob("*.md"))

    def test_subtitle_in_the_media_folder_is_found(self, temp_config: Config) -> None:
        enable_media(temp_config)
        other = temp_config.watch_dir / "elsewhere"
        other.mkdir()
        media = other / "talk.mp4"
        media.write_bytes(b"fake")
        subtitle = other / "talk.srt"
        subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")

        watcher = FileWatcher(temp_config)

        assert watcher._find_subtitle_for(media) == subtitle

class TestLanguageSuffixedNames:
    """CC export extensions usually append a language: lecture.en.vtt."""

    def test_sibling_video_matches_a_language_suffix(self, tmp_path: Path) -> None:
        subtitle = tmp_path / "lecture.en.vtt"
        subtitle.write_text("WEBVTT\n", encoding="utf-8")
        video = tmp_path / "lecture.mp4"
        video.write_bytes(b"x")

        assert find_sibling_video(subtitle) == video

    def test_watcher_finds_a_language_suffixed_subtitle(self, temp_config: Config) -> None:
        enable_media(temp_config)
        media = temp_config.watch_dir / "lecture.mp4"
        media.write_bytes(b"x")
        subtitle = temp_config.watch_dir / "lecture.zh-Hans.vtt"
        subtitle.write_text("WEBVTT\n", encoding="utf-8")

        watcher = FileWatcher(temp_config)

        assert watcher._find_subtitle_for(media) == subtitle

    def test_unrelated_names_do_not_match(self, tmp_path: Path) -> None:
        subtitle = tmp_path / "lecture.vtt"
        subtitle.write_text("WEBVTT\n", encoding="utf-8")
        (tmp_path / "lecture2.mp4").write_bytes(b"x")

        assert find_sibling_video(subtitle) is None

class TestScreenshotHint:
    """The model is told a video exists, which is what makes it emit markers."""

    def _run(self, temp_config, subtitle, *, screenshots: bool):
        temp_config._data["screenshots"] = {"enabled": screenshots, "max_count": 2}
        subtitle.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello\n", encoding="utf-8"
        )
        with patch(
            "src.srt_daemon.ai_client.AiClient.process", return_value="body"
        ) as process, patch(
            "src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None
        ):
            generate_notes(temp_config, subtitle)
        return process.call_args.kwargs.get("has_video")

    def test_true_when_a_sibling_video_exists(self, temp_config) -> None:
        subtitle = temp_config.watch_dir / "talk.vtt"
        (temp_config.watch_dir / "talk.mp4").write_bytes(b"x")

        assert self._run(temp_config, subtitle, screenshots=True) is True

    def test_false_without_a_video(self, temp_config) -> None:
        subtitle = temp_config.watch_dir / "talk.vtt"

        assert self._run(temp_config, subtitle, screenshots=True) is False

    def test_false_when_screenshots_are_off(self, temp_config) -> None:
        subtitle = temp_config.watch_dir / "talk.vtt"
        (temp_config.watch_dir / "talk.mp4").write_bytes(b"x")

        assert self._run(temp_config, subtitle, screenshots=False) is False

class TestWatchIgnoresRecordingsWhenOff:
    """With auto_transcribe_media off, the watch dir only handles subtitles."""

    def test_allowed_extensions_exclude_media(self, temp_config: Config) -> None:
        enable_media(temp_config)
        temp_config._data["transcribe"]["auto_transcribe_media"] = False

        watcher = FileWatcher(temp_config)

        assert watcher._allowed_extensions == {".srt", ".vtt"}

    def test_event_for_a_recording_is_ignored(self, temp_config: Config) -> None:
        enable_media(temp_config)
        temp_config._data["transcribe"]["auto_transcribe_media"] = False
        media = temp_config.watch_dir / "meeting.mp3"
        media.write_bytes(b"fake")
        watcher = FileWatcher(temp_config)

        with patch.object(watcher, "_enqueue_file") as enqueue:
            watcher._on_file_event(media)

        enqueue.assert_not_called()

    def test_subtitles_still_work(self, temp_config: Config) -> None:
        enable_media(temp_config)
        temp_config._data["transcribe"]["auto_transcribe_media"] = False
        subtitle = temp_config.watch_dir / "lecture.vtt"
        subtitle.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello\n", encoding="utf-8"
        )
        watcher = FileWatcher(temp_config)

        with patch(
            "src.srt_daemon.ai_client.AiClient.process", return_value="# Notes"
        ), patch(
            "src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None
        ):
            watcher._process_file(subtitle)

        assert list(temp_config.output_dir.rglob("*.md"))

class TestSubtitleOutsideTheWatchDir:
    @needs_ffmpeg
    def test_sibling_video_next_to_a_project_subtitle(
        self, temp_config: Config, tmp_path: Path
    ) -> None:
        """A bilibili video+subtitle pair in a project folder still gets shots."""
        temp_config._data["screenshots"] = {
            "enabled": True,
            "max_count": 2,
            "width": 0,
            "quality": 5,
            "best_frame_window": 0,
        }
        prompts = Path(__file__).resolve().parent.parent / "doc" / "prompts"
        temp_config._data.setdefault("ai", {})["profiles"] = {
            name: {"prompt_file": str(prompts / f"{name}.md")}
            for name in ("course", "meeting", "interview")
        }
        project = tmp_path / "projects" / "收纳"
        project.mkdir(parents=True)
        subtitle = project / "talk.srt"
        subtitle.write_text(
            "1\n00:00:01,000 --> 00:00:03,000\nhello there\n", encoding="utf-8"
        )
        make_video(project / "talk.mp4", 6)

        with patch(
            "src.srt_daemon.ai_client.AiClient.process",
            return_value="body\n\n[[screenshot:00:00:02]]\n",
        ), patch(
            "src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None
        ):
            result = generate_notes(temp_config, subtitle, forced_profile="interview")

        text = result.output_path.read_text(encoding="utf-8")
        assert "![](images/" in text
        assert "profile: interview" in text
