"""Tests for the long-recording model policy and GPU device selection."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.srt_daemon.config import Config
from src.srt_daemon.transcribe import Transcriber, probe_media_duration

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)


def transcriber(config: Config, **kwargs: object) -> Transcriber:
    """Build a Transcriber without touching models or diarizers."""
    return Transcriber(config, **kwargs)


def with_transcribe(config: Config, **values: object) -> Config:
    """Merge values into the transcribe block of a test config."""
    block = dict(config._data.get("transcribe") or {})
    block.update(values)
    config._data["transcribe"] = block
    return config


class TestProbeDuration:
    @needs_ffmpeg
    def test_reads_media_duration(self, tmp_path: Path) -> None:
        wav = tmp_path / "clip.wav"
        subprocess.run(
            [
                "ffmpeg", "-nostdin", "-y", "-v", "error",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                str(wav),
            ],
            check=True,
            capture_output=True,
        )

        duration = probe_media_duration(wav)

        assert 2.5 < duration < 3.5

    def test_missing_file_returns_zero(self, tmp_path: Path) -> None:
        assert probe_media_duration(tmp_path / "nope.wav") == 0.0


class TestDeviceAttempts:
    def test_auto_tries_gpu_then_cpu(self, temp_config: Config) -> None:
        with_transcribe(temp_config, device="auto", compute_type="auto")

        attempts = transcriber(temp_config)._device_attempts()

        assert attempts == [("cuda", "float16"), ("cpu", "int8")]

    def test_cpu_only(self, temp_config: Config) -> None:
        with_transcribe(temp_config, device="cpu", compute_type="int8")

        assert transcriber(temp_config)._device_attempts() == [("cpu", "int8")]

    def test_cuda_falls_back_to_cpu(self, temp_config: Config) -> None:
        with_transcribe(temp_config, device="cuda", compute_type="auto")

        attempts = transcriber(temp_config)._device_attempts()

        assert attempts[0] == ("cuda", "float16")
        assert attempts[-1] == ("cpu", "int8")

    def test_explicit_compute_type_is_kept(self, temp_config: Config) -> None:
        with_transcribe(temp_config, device="cuda", compute_type="int8_float16")

        assert transcriber(temp_config)._device_attempts()[0] == ("cuda", "int8_float16")


class TestLongRecordingPolicy:
    def test_switches_to_medium_when_long(self, temp_config: Config) -> None:
        with_transcribe(
            temp_config,
            model="large-v3",
            long_media={"enabled": True, "threshold_minutes": 10, "model": "medium"},
        )
        subject = transcriber(temp_config)
        assert subject._model_name == "large-v3"

        with patch("src.srt_daemon.transcribe.probe_media_duration", return_value=3600.0):
            subject._apply_long_media_policy(Path("lecture.mp4"))

        assert subject._model_name == "medium"

    def test_explicit_model_wins(self, temp_config: Config) -> None:
        with_transcribe(
            temp_config,
            long_media={"enabled": True, "threshold_minutes": 10, "model": "medium"},
        )
        subject = transcriber(temp_config, model="large-v3")

        with patch("src.srt_daemon.transcribe.probe_media_duration", return_value=7200.0):
            subject._apply_long_media_policy(Path("lecture.mp4"))

        assert subject._model_name == "large-v3"

    def test_short_recording_keeps_the_model(self, temp_config: Config) -> None:
        with_transcribe(
            temp_config,
            model="large-v3",
            long_media={"enabled": True, "threshold_minutes": 30, "model": "medium"},
        )
        subject = transcriber(temp_config)

        with patch("src.srt_daemon.transcribe.probe_media_duration", return_value=600.0):
            subject._apply_long_media_policy(Path("clip.mp4"))

        assert subject._model_name == "large-v3"

    def test_policy_can_be_disabled(self, temp_config: Config) -> None:
        with_transcribe(
            temp_config,
            model="large-v3",
            long_media={"enabled": False, "threshold_minutes": 1, "model": "medium"},
        )
        subject = transcriber(temp_config)

        with patch("src.srt_daemon.transcribe.probe_media_duration", return_value=7200.0):
            subject._apply_long_media_policy(Path("long.mp4"))

        assert subject._model_name == "large-v3"


class TestModelLoadingFallback:
    def test_cuda_failure_falls_back_to_cpu(self, temp_config: Config) -> None:
        with_transcribe(temp_config, device="auto", compute_type="auto")
        subject = transcriber(temp_config)
        calls: list[str] = []

        class FakeModel:
            def __init__(self, name: str, device: str, compute_type: str, cpu_threads: int):
                calls.append(device)
                if device == "cuda":
                    raise RuntimeError("no CUDA driver")

        with patch("faster_whisper.WhisperModel", FakeModel):
            model = subject._ensure_asr()

        assert isinstance(model, FakeModel)
        assert calls == ["cuda", "cpu"]
        assert subject._device == "cpu"
        assert subject._compute_type == "int8"
