"""Where notes are written: central output/ vs beside the source file."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from src.srt_daemon.config import Config
from src.srt_daemon.pipeline import generate_notes, resolve_output_root


def _write_subtitle(path: Path) -> Path:
    path.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nhello there\n", encoding="utf-8"
    )
    return path


def _fake_ai(text=None):
    return (
        patch("src.srt_daemon.ai_client.AiClient.process", return_value=text or "# Notes\n\nbody"),
        patch("src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None),
    )


class TestOutputMode:
    def test_central_is_the_default(self, temp_config: Config) -> None:
        assert temp_config.output_mode == "central"

    def test_source_mode_writes_beside_the_subtitle(
        self, temp_config: Config, tmp_path: Path
    ) -> None:
        temp_config._data["output"]["mode"] = "source"
        project = tmp_path / "projects" / "收纳"
        project.mkdir(parents=True)
        subtitle = _write_subtitle(project / "talk.srt")

        process_patch, classify_patch = _fake_ai()
        with process_patch, classify_patch:
            result = generate_notes(temp_config, subtitle)

        # <project>/<basename>/<note>.md
        assert result.output_path.parent.parent == project
        assert result.output_path.exists()

    def test_central_mode_still_uses_output_dir(
        self, temp_config: Config, tmp_path: Path
    ) -> None:
        project = tmp_path / "projects" / "课程"
        project.mkdir(parents=True)
        subtitle = _write_subtitle(project / "lesson.srt")

        process_patch, classify_patch = _fake_ai()
        with process_patch, classify_patch:
            result = generate_notes(temp_config, subtitle)

        assert result.output_path.is_relative_to(temp_config.output_dir)

    def test_explicit_root_wins_over_source_mode(
        self, temp_config: Config, tmp_path: Path
    ) -> None:
        """URL fetches pin the central directory even in source mode."""
        temp_config._data["output"]["mode"] = "source"
        subtitle = _write_subtitle(tmp_path / "download.srt")

        assert resolve_output_root(temp_config, subtitle, explicit=temp_config.output_dir) == (
            temp_config.output_dir
        )

    def test_source_mode_root_is_the_folder(self, temp_config: Config, tmp_path: Path) -> None:
        temp_config._data["output"]["mode"] = "source"
        subtitle = _write_subtitle(tmp_path / "x.srt")

        assert resolve_output_root(temp_config, subtitle) == tmp_path.resolve()
