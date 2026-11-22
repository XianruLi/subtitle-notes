"""Pytest fixtures and helpers for SRT Daemon integration tests.

Provides shared fixtures for creating temporary config objects, sample SRT
content, and mock AI clients.  All fixtures are function-scoped to ensure
test independence.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure the project's src/ directory is on the Python path so that
# ``from src.srt_daemon.xxx import ...`` works regardless of how pytest
# is invoked.
_project_root = Path(__file__).resolve().parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from src.srt_daemon.config import Config


# ---------------------------------------------------------------------------
# Logging — configure once per session so test failures show captured output
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session", autouse=True)
def _setup_logging():
    """Suppress 'no handler' warnings and let pytest capture log output."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


# ---------------------------------------------------------------------------
# Sample SRT content fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_srt_content() -> str:
    """Return standard SRT subtitle content with 5 entries."""
    return (
        "1\n"
        "00:00:01,000 --> 00:00:03,500\n"
        "Hello and welcome to this ROS2 tutorial.\n"
        "\n"
        "2\n"
        "00:00:03,800 --> 00:00:07,200\n"
        "Today we will learn how to create\n"
        "\n"
        "3\n"
        "00:00:07,200 --> 00:00:10,000\n"
        "a simple publisher and subscriber in C++.\n"
        "\n"
        "4\n"
        "00:00:12,000 --> 00:00:15,500\n"
        "First, let's talk about ROS2 nodes.\n"
        "\n"
        "5\n"
        "00:00:15,800 --> 00:00:20,000\n"
        "A node is the basic unit of computation in ROS2.\n"
    )


@pytest.fixture
def sample_srt_file(tmp_path: Path, sample_srt_content: str) -> Path:
    """Write *sample_srt_content* to a temporary ``.srt`` file."""
    srt_path = tmp_path / "lecture01.srt"
    srt_path.write_text(sample_srt_content, encoding="utf-8")
    return srt_path


# ---------------------------------------------------------------------------
# Sample WebVTT content fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_vtt_content() -> str:
    """Return standard WebVTT content with 5 cues.

    Includes header metadata, a NOTE block, an hours-less timestamp and a
    voice span so that the parser exercises its full feature set.
    """
    return (
        "WEBVTT\n"
        "Kind: captions\n"
        "Language: en\n"
        "\n"
        "NOTE exported by a test fixture\n"
        "\n"
        "00:00:01.000 --> 00:00:03.500 align:start position:0%\n"
        "Hello and welcome to this ROS2 tutorial.\n"
        "\n"
        "00:00:03.800 --> 00:07.200\n"
        "Today we will learn how to create\n"
        "\n"
        "00:00:07.200 --> 00:00:10.000\n"
        "a simple publisher and subscriber in C++.\n"
        "\n"
        "00:00:12.000 --> 00:00:15.500\n"
        "First, let's talk about ROS2 nodes.\n"
        "\n"
        "00:00:15.800 --> 00:00:20.000\n"
        "<v Instructor>Here is some <b>bold</b> text.\n"
    )


@pytest.fixture
def sample_vtt_file(tmp_path: Path, sample_vtt_content: str) -> Path:
    """Write *sample_vtt_content* to a temporary ``.vtt`` file."""
    vtt_path = tmp_path / "lecture01.vtt"
    vtt_path.write_text(sample_vtt_content, encoding="utf-8")
    return vtt_path


# ---------------------------------------------------------------------------
# Config fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def temp_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Create a fully-resolved :class:`Config` with temporary filesystem paths.

    Sets ``DEEPSEEK_API_KEY`` to a dummy value so that :class:`AiClient`
    construction does not fail.  All directories (watch, output, logs) and
    the system-prompt file are created inside *tmp_path*.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key-integration")

    # Directory layout inside tmp_path
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()

    # Minimal system prompt file (must contain "## System Prompt" heading
    # for AiClient._load_system_prompt() to locate the prompt body).
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text(
        "## System Prompt\n"
        "You are a test assistant. Output exactly: 'MOCK RESPONSE'.\n",
        encoding="utf-8",
    )

    config_data = {
        "watch": {
            "directory": str(watch_dir),
            "extensions": [".srt", ".vtt"],
            "poll_interval_ms": 1000,
            "debounce_seconds": 0.1,
        },
        "output": {
            "directory": str(output_dir),
            "filename_template": "{basename}_notes_{date}.md",
        },
        "ai": {
            "model": "deepseek-chat",
            "max_tokens": 100,
            "temperature": 0.0,
            "connection_timeout_sec": 5,
            "read_timeout_sec": 10,
            "max_retries": 2,
            "retry_backoff_base_sec": 0.1,
            "chunk_threshold": 100,
            "system_prompt_file": str(prompt_file),
        },
        "logging": {
            "file": str(logs_dir / "test.log"),
            "level": "DEBUG",
            "format": "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            "date_format": "%Y-%m-%d %H:%M:%S",
            "rotation": "midnight",
            "backup_count": 30,
        },
        "state_file": str(tmp_path / "output" / ".processed.json"),
    }

    return Config(config_data, tmp_path)


# ---------------------------------------------------------------------------
# Mock AI helpers
# ---------------------------------------------------------------------------

_MOCK_AI_OUTPUT = (
    "# Test Notes\n\n"
    "## Overview\n\n"
    "Mock AI output for integration testing.\n\n"
    "## Key Concepts\n\n"
    "- ROS2 nodes\n"
    "- Publisher and subscriber\n\n"
    "## Summary\n\n"
    "Test summary content.\n"
)


def create_mock_chat_response(content: str) -> MagicMock:
    """Build a MagicMock that simulates an OpenAI chat completion response.

    The returned mock has ``choices[0].message.content == content``, which is
    what :meth:`AiClient._call_api` reads after calling the API.
    """
    response = MagicMock()
    response.choices[0].message.content = content
    return response
