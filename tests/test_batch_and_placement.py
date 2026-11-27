"""Folder batch (--once <dir>) and where transcribed subtitles land."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _subtitle(path: Path, text: str = "1\n00:00:01,000 --> 00:00:02,000\nhello\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class TestOnceFolderBatch:
    def test_every_subtitle_is_processed(self, temp_config, tmp_path, monkeypatch) -> None:
        from src.srt_daemon import main as main_mod

        folder = tmp_path / "course"
        first = _subtitle(folder / "01 intro.srt")
        second = _subtitle(folder / "week 2" / "02 deep dive.vtt")
        seen: list[Path] = []

        def fake_notes(config, path, *, forced_profile=None, **kwargs):
            seen.append(Path(path))
            return SimpleNamespace(output_path=Path(path).with_suffix(".md"))

        monkeypatch.setattr("src.srt_daemon.pipeline.generate_notes", fake_notes)

        main_mod.run_once(temp_config, str(folder), profile="course")

        assert [p.name for p in seen] == ["01 intro.srt", "02 deep dive.vtt"]
        assert first.exists() and second.exists()

    def test_a_failure_fails_the_batch(self, temp_config, tmp_path, monkeypatch) -> None:
        from src.srt_daemon import main as main_mod
        from src.srt_daemon.pipeline import NotesPipelineError

        folder = tmp_path / "course"
        _subtitle(folder / "a.srt")
        _subtitle(folder / "b.srt")

        def fake_notes(config, path, *, forced_profile=None, **kwargs):
            if Path(path).name == "b.srt":
                raise NotesPipelineError("boom")
            return SimpleNamespace(output_path=Path(path).with_suffix(".md"))

        monkeypatch.setattr("src.srt_daemon.pipeline.generate_notes", fake_notes)

        with pytest.raises(SystemExit) as excinfo:
            main_mod.run_once(temp_config, str(folder))

        assert excinfo.value.code == 1

    def test_empty_folder_is_rejected(self, temp_config, tmp_path) -> None:
        from src.srt_daemon import main as main_mod

        folder = tmp_path / "empty"
        folder.mkdir()

        with pytest.raises(SystemExit) as excinfo:
            main_mod.run_once(temp_config, str(folder))

        assert excinfo.value.code == 1


class TestTranscribedSubtitleLocation:

    def _transcriber_with(self, temp_config, tmp_path, *, beside=True, explicit=None):
        temp_config._data["transcribe"] = {
            "output_dir": str(tmp_path / "watch"),
            "source_dir": str(tmp_path / "inbox"),
            "output_beside_source": beside,
        }
        from src.srt_daemon.transcribe import Transcriber

        transcriber = Transcriber.__new__(Transcriber)
        transcriber._config = temp_config
        transcriber._output_dir = Path(explicit or (tmp_path / "watch")).resolve()
        transcriber._output_dir_explicit = explicit is not None
        return transcriber

    def test_picked_folder_keeps_it_beside_the_media(self, temp_config, tmp_path) -> None:
        transcriber = self._transcriber_with(temp_config, tmp_path)
        source = tmp_path / "course" / "lecture.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"")

        assert transcriber._subtitle_dir_for(source) == source.parent

    def test_inbox_still_goes_to_the_watch_directory(self, temp_config, tmp_path) -> None:
        transcriber = self._transcriber_with(temp_config, tmp_path)
        watch = tmp_path / "watch"
        watch.mkdir(parents=True, exist_ok=True)
        source = watch / "dropped.mp4"
        source.write_bytes(b"")

        assert transcriber._subtitle_dir_for(source) == watch.resolve()

    def test_explicit_output_dir_wins(self, temp_config, tmp_path) -> None:
        destination = tmp_path / "elsewhere"
        transcriber = self._transcriber_with(
            temp_config, tmp_path, explicit=destination
        )
        source = tmp_path / "course" / "lecture.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"")

        assert transcriber._subtitle_dir_for(source) == destination.resolve()

    def test_option_off_restores_the_old_behaviour(self, temp_config, tmp_path) -> None:
        transcriber = self._transcriber_with(temp_config, tmp_path, beside=False)
        source = tmp_path / "course" / "lecture.mp4"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"")

        assert transcriber._subtitle_dir_for(source) == (tmp_path / "watch").resolve()
