"""Progress files: the contract between a long job and the GUI."""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

from src.srt_daemon import progress as pr
from src.srt_daemon.config import Config


def test_roundtrip(tmp_path: Path) -> None:
    pr.write_progress(
        tmp_path, pr.NOTES, stage=pr.STAGE_AI, done=1, total=3, detail="第 2/3 块", file="a.vtt"
    )

    data = pr.read_progress(tmp_path, pr.NOTES)

    assert data is not None
    assert data["stage"] == "ai"
    assert data["done"] == 1
    assert data["total"] == 3
    assert data["detail"] == "第 2/3 块"
    assert data["file"] == "a.vtt"
    assert data["updated_at"] > 0


def test_missing_file_reads_as_none(tmp_path: Path) -> None:
    assert pr.read_progress(tmp_path, pr.TRANSCRIBE) is None


def test_clear_removes_the_document(tmp_path: Path) -> None:
    pr.write_progress(tmp_path, pr.TRANSCRIBE, stage=pr.STAGE_ASR)
    pr.clear_progress(tmp_path, pr.TRANSCRIBE)

    assert pr.read_progress(tmp_path, pr.TRANSCRIBE) is None


def test_staleness(tmp_path: Path) -> None:
    pr.write_progress(tmp_path, pr.NOTES, stage=pr.STAGE_PARSE)

    assert pr.is_stale(pr.read_progress(tmp_path, pr.NOTES), max_age_seconds=30) is False
    assert pr.is_stale(None) is True

    old = time.time() - 3600
    pr.write_progress(tmp_path, pr.NOTES, stage=pr.STAGE_PARSE)
    path = pr.progress_path(tmp_path, pr.NOTES)
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["updated_at"] = old
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert pr.is_stale(pr.read_progress(tmp_path, pr.NOTES), max_age_seconds=30) is True


class TestJobsPublishProgress:
    def test_pipeline_publishes_stages_and_clears(
        self, temp_config: Config, tmp_path: Path
    ) -> None:
        from src.srt_daemon.pipeline import generate_notes

        subtitle = temp_config.watch_dir / "talk.vtt"
        subtitle.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello\n", encoding="utf-8"
        )

        stages: list[str] = []

        def capture(*_args, **_kwargs) -> None:
            stages.append(_kwargs.get("stage", ""))

        def fake_process(text, name, *, progress=None, **kwargs):
            if progress is not None:
                progress(1, 1, "调用模型")
            return "# Notes"

        with patch(
            "src.srt_daemon.progress.write_progress", side_effect=capture
        ), patch(
            "src.srt_daemon.ai_client.AiClient.process", side_effect=fake_process
        ), patch(
            "src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None
        ), patch(
            "src.srt_daemon.progress.clear_progress"
        ) as clear:
            generate_notes(temp_config, subtitle)

        assert stages[0] == "parse"
        assert "ai" in stages
        assert stages[-1] == "write"
        clear.assert_called()

    def test_transcribe_publishes_asr_progress(
        self, temp_config: Config, tmp_path: Path, monkeypatch
    ) -> None:
        from types import SimpleNamespace

        from src.srt_daemon import transcribe as tr

        subject = tr.Transcriber.__new__(tr.Transcriber)
        subject._config = temp_config
        subject._beam_size = 5
        subject._language = "en"
        subject._word_timestamps = False
        subject._current_file = "a.wav"
        subject._started_at = time.time()

        recorded: list[dict] = []

        def fake_transcribe(path, **options):
            segment = SimpleNamespace(start=0.0, end=1.0, text="hi", words=[])
            info = SimpleNamespace(language="en", duration=10.0, language_probability=0.9)
            return iter([segment]), info

        monkeypatch.setattr(
            tr.Transcriber,
            "_ensure_asr",
            lambda self: SimpleNamespace(transcribe=fake_transcribe),
        )
        monkeypatch.setattr(
            "src.srt_daemon.progress.write_progress",
            lambda *a, **kw: recorded.append(kw),
        )

        subject._run_asr(tmp_path / "a.wav")

        assert recorded
        assert recorded[0]["stage"] == "asr"
        assert recorded[0]["total"] == 10.0
        assert recorded[0]["file"] == "a.wav"
