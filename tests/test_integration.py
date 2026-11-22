"""Integration tests for the SRT Daemon project.

Covers all major modules:
* SRT parsing (SrtParser)
* Markdown output writing (MarkdownWriter)
* File watching state management (FileWatcher)
* AI client (AiClient) with mocked API calls
* End-to-end pipeline integration

All tests are independent and do **not** require a real DeepSeek API key.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# openai exceptions used for retry-logic tests
from openai import (
    APIConnectionError,
    APIStatusError,
)

from src.srt_daemon import __version__
from src.srt_daemon.ai_client import AiClient, AiClientError
from src.srt_daemon.config import Config
from src.srt_daemon.main import parse_args
from src.srt_daemon.srt_parser import SrtEntry, SrtParser, parse_srt
from src.srt_daemon.watcher import FileWatcher
from src.srt_daemon.writer import MarkdownWriter, _sanitise_filename

# Re-exported from conftest for clarity
from tests.conftest import _MOCK_AI_OUTPUT, create_mock_chat_response


# =============================================================================
# TestSrtParser — SRT parsing module
# =============================================================================


class TestSrtParser:
    """Tests for :class:`SrtParser` covering parsing, encoding, merging,
    text extraction, and anomaly recovery."""

    def test_parse_basic(self, sample_srt_file: Path) -> None:
        """Parse a standard UTF-8 SRT file and verify entry count/content."""
        parser = SrtParser()
        entries = parser.parse(sample_srt_file)

        assert len(entries) == 5, f"Expected 5 entries, got {len(entries)}"
        assert entries[0].index == 1
        assert entries[0].start_time == "00:00:01,000"
        assert entries[0].end_time == "00:00:03,500"
        assert "ROS2" in entries[0].text

    def test_encoding_detection_utf8(self, tmp_path: Path) -> None:
        """Verify UTF-8 encoding is detected for UTF-8 files without BOM."""
        content = (
            "1\n"
            "00:00:01,000 --> 00:00:03,000\n"
            "Test content in UTF-8.\n"
        )
        srt_file = tmp_path / "utf8_test.srt"
        srt_file.write_text(content, encoding="utf-8")

        parser = SrtParser()
        encoding = parser._detect_encoding(srt_file)
        assert encoding == "utf-8", f"Expected utf-8, got {encoding}"

        entries = parser.parse(srt_file)
        assert len(entries) == 1
        assert "Test content" in entries[0].text

    def test_encoding_detection_gbk(self, tmp_path: Path) -> None:
        """Verify GBK encoding is detected for Chinese-language GBK files."""
        gbk_content = (
            "1\r\n"
            "00:00:01,000 --> 00:00:03,000\r\n"
            "欢迎来到ROS2教程。\r\n"
            "\r\n"
            "2\r\n"
            "00:00:03,500 --> 00:00:06,000\r\n"
            "今天我们将学习C++编程。\r\n"
        )
        srt_file = tmp_path / "gbk_test.srt"
        srt_file.write_bytes(gbk_content.encode("gbk"))

        parser = SrtParser()
        encoding = parser._detect_encoding(srt_file)
        assert encoding == "gbk", f"Expected gbk, got {encoding}"

        entries = parser.parse(srt_file)
        assert len(entries) == 2, f"Expected 2 entries, got {len(entries)}"
        assert "ROS2" in entries[0].text

    def test_merge_short_entries(self) -> None:
        """Two entries with short text and tight gap (0s) should be merged."""
        entries = [
            SrtEntry(1, "00:00:01,000", "00:00:03,000", "First part of"),
            SrtEntry(2, "00:00:03,000", "00:00:05,000", "the sentence."),
            SrtEntry(3, "00:00:10,000", "00:00:12,000", "Unrelated entry with a much longer text that exceeds the threshold"),
        ]

        parser = SrtParser()
        merged = parser.merge_short_entries(entries, max_gap_seconds=0.5)

        # Entries 1+2 should merge (gap=0s, both short), entry 3 stays separate
        assert len(merged) == 2, f"Expected 2 entries after merge, got {len(merged)}"
        assert "First part of the sentence." in merged[0].text
        assert merged[0].start_time == "00:00:01,000"
        assert merged[0].end_time == "00:00:05,000"

    def test_merge_speaker_break_not_merged(self) -> None:
        """Entries with different speaker prefixes (``- Speaker:``) should
        NOT be merged even with a tight gap and short text."""
        entries = [
            SrtEntry(1, "00:00:01,000", "00:00:02,000", "- Alice: Hello"),
            SrtEntry(2, "00:00:02,000", "00:00:03,000", "- Bob: Hi there"),
        ]

        parser = SrtParser()
        merged = parser.merge_short_entries(entries, max_gap_seconds=0.5, max_text_length=80)

        # Both are short with gap=0s, but different speakers — no merge
        assert len(merged) == 2

    def test_extract_text_with_timestamps(self, sample_srt_file: Path) -> None:
        """Extracted text with timestamps enabled should contain ``[HH:MM:SS]`` markers."""
        parser = SrtParser()
        entries = parser.parse(sample_srt_file)
        text = parser.extract_text(entries, with_timestamps=True)

        assert "[00:00:01]" in text
        assert "[00:00:15]" in text
        # Verify no millisecond portion in the marker
        assert "[00:00:01,000]" not in text

    def test_extract_text_without_timestamps(self, sample_srt_file: Path) -> None:
        """Extracted text without timestamps should NOT contain ``[HH:MM:SS]`` markers."""
        parser = SrtParser()
        entries = parser.parse(sample_srt_file)
        text = parser.extract_text(entries, with_timestamps=False)

        assert "[00:" not in text
        assert "ROS2" in text

    def test_format_anomaly_recovery_missing_index(self, tmp_path: Path) -> None:
        """SRT file with a missing index number on one entry should still parse
        (best-effort auto-repair)."""
        # Entry 2 is missing the index number — the timestamp follows directly.
        malformed = (
            "1\n"
            "00:00:01,000 --> 00:00:03,000\n"
            "First entry.\n"
            "\n"
            "00:00:04,000 --> 00:00:06,000\n"  # missing index!
            "Second entry — auto-repaired.\n"
        )
        srt_file = tmp_path / "missing_index.srt"
        srt_file.write_text(malformed, encoding="utf-8")

        parser = SrtParser()
        entries = parser.parse(srt_file)
        assert len(entries) == 2, f"Expected 2 entries, got {len(entries)}"
        assert entries[1].index == 2  # auto-incremented from previous
        assert "auto-repaired" in entries[1].text

    def test_format_anomaly_empty_entry_skipped(self, tmp_path: Path) -> None:
        """An SRT entry with a timestamp but no text should be skipped."""
        empty_entry_content = (
            "1\n"
            "00:00:01,000 --> 00:00:03,000\n"
            "\n"  # no text — should be skipped
            "\n"
            "2\n"
            "00:00:04,000 --> 00:00:06,000\n"
            "Valid entry here.\n"
        )
        srt_file = tmp_path / "empty_entry.srt"
        srt_file.write_text(empty_entry_content, encoding="utf-8")

        parser = SrtParser()
        entries = parser.parse(srt_file)
        assert len(entries) == 1
        assert entries[0].text == "Valid entry here."

    def test_parse_srt_convenience(self, sample_srt_file: Path) -> None:
        """The :func:`parse_srt` convenience function should chain parse +
        merge + extract correctly."""
        result = parse_srt(
            sample_srt_file,
            with_timestamps=True,
            merge_short=True,
            max_gap_seconds=0.3,
        )
        assert "ROS2" in result
        assert "[00:00:01]" in result

    def test_file_not_found_raises(self) -> None:
        """Parsing a nonexistent file raises :class:`FileNotFoundError`."""
        parser = SrtParser()
        with pytest.raises(FileNotFoundError):
            parser.parse("nonexistent_file_xyz_12345.srt")


# =============================================================================
# TestMarkdownWriter — Markdown output module
# =============================================================================


class TestMarkdownWriter:
    """Tests for :class:`MarkdownWriter` covering file creation, naming
    template resolution, YAML front matter, and filename sanitization."""

    def test_write_creates_file(self, temp_config: Config) -> None:
        """Writing Markdown content creates a file at the expected path
        with the content present."""
        writer = MarkdownWriter(temp_config)
        output_path = writer.write(
            content="# Hello\n\nWorld.",
            source_filename="lecture01.srt",
            metadata={"entries": 42, "text_length": 1000},
        )

        assert output_path.exists()
        content = output_path.read_text(encoding="utf-8")
        assert "# Hello" in content
        assert "World." in content
        assert "entries: 42" in content

    def test_output_naming_template(self, temp_config: Config) -> None:
        """The output filename should expand ``{basename}``, ``{date}``,
        and ``{time}`` placeholders."""
        writer = MarkdownWriter(temp_config)
        output_path = writer.write(
            content="# Test",
            source_filename="my_lecture.srt",
        )

        filename = output_path.name
        # Template: "{basename}_notes_{date}.md"
        assert filename.startswith("my_lecture_notes_")
        assert filename.endswith(".md")
        # The date portion should look like YYYY-MM-DD
        date_part = filename[len("my_lecture_notes_"):-len(".md")]
        assert len(date_part) == 10  # YYYY-MM-DD

    def test_yaml_front_matter(self, temp_config: Config) -> None:
        """The output file must contain a YAML front-matter header with the
        source / processed / model / format / profile / speakers / entries /
        text_length fields, closed before the body starts."""
        writer = MarkdownWriter(temp_config)
        output_path = writer.write(
            content="# Body",
            source_filename="test.srt",
            metadata={
                "format": "SRT",
                "profile": "course",
                "speakers": "inferred",
                "entries": 10,
                "text_length": 500,
            },
        )

        content = output_path.read_text(encoding="utf-8")
        lines = content.splitlines()

        # First line should be the YAML delimiter
        assert lines[0] == "---"
        # The block must be closed before the body starts
        closing = lines.index("---", 1)
        assert closing > 1
        assert lines[closing + 1] == "# Body"

        assert 'source: "test.srt"' in content
        assert "model:" in content
        assert "format: SRT" in content
        assert "profile: course" in content
        assert 'speakers: "inferred"' in content
        assert "entries: 10" in content
        assert "text_length: 500" in content

    def test_filename_sanitization_forbidden_chars(self) -> None:
        """Sanitise replaces ``< > : \" / \\ | ? *`` and control chars."""
        # All Windows-forbidden characters
        result = _sanitise_filename('test<file>:name?with|chars*')
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert '"' not in result
        assert "\\" not in result
        assert "|" not in result
        assert "?" not in result
        assert "*" not in result
        # Should still retain the meaningful parts
        assert "test" in result
        assert "file" in result
        assert "name" in result

    def test_filename_sanitization_empty_fallback(self) -> None:
        """An empty or whitespace-only name falls back to ``"untitled"``."""
        assert _sanitise_filename("") == "untitled"
        assert _sanitise_filename("   ") == "untitled"
        assert _sanitise_filename("...") == "untitled"  # dots stripped

    def test_filename_sanitization_strips_dots_and_spaces(self) -> None:
        """Leading/trailing dots and spaces are stripped."""
        assert _sanitise_filename("...test...") == "test"
        assert _sanitise_filename("  hello  ") == "hello"


# =============================================================================
# TestFileWatcher — file watching (state management, no AI calls)
# =============================================================================


class TestFileWatcher:
    """Tests for :class:`FileWatcher` state management and file-tracking
    logic.  These tests do **not** make AI API calls — they validate the
    internal bookkeeping that happens before/after processing."""

    def test_state_initialization(self, temp_config: Config) -> None:
        """A newly created FileWatcher starts with an empty state
        (no processed files, no in-progress markers)."""
        watcher = FileWatcher(temp_config)
        assert watcher._state == {"files": {}, "in_progress": {}}
        assert temp_config.state_file.exists()

    def test_file_hash_computation(self, temp_config: Config, tmp_path: Path) -> None:
        """SHA-256 hash changes when file content changes."""
        watcher = FileWatcher(temp_config)
        test_file = tmp_path / "hash_test.srt"
        test_file.write_text("Content A", encoding="utf-8")

        hash_a = watcher._compute_file_hash(test_file)
        assert len(hash_a) == 64  # SHA-256 hex digest

        test_file.write_text("Content B — different", encoding="utf-8")
        hash_b = watcher._compute_file_hash(test_file)

        assert hash_a != hash_b, "Hash should change when content changes"

    def test_processed_tracking(self, temp_config: Config) -> None:
        """After marking a file as processed, ``_is_processed`` returns True."""
        watcher = FileWatcher(temp_config)
        test_file = temp_config.watch_dir / "processed_test.srt"
        test_file.write_text("Some content", encoding="utf-8")

        file_hash = watcher._compute_file_hash(test_file)
        assert not watcher._is_processed(test_file)

        watcher._mark_processed(
            test_file, file_hash, temp_config.output_dir / "output.md"
        )
        assert watcher._is_processed(test_file)

    def test_in_progress_marking(self, temp_config: Config) -> None:
        """Marking and clearing in-progress works correctly."""
        watcher = FileWatcher(temp_config)
        test_file = temp_config.watch_dir / "inprog_test.srt"
        test_file.write_text("Processing me", encoding="utf-8")
        file_key = str(test_file.resolve())

        # Initially not in-progress
        assert file_key not in watcher._state["in_progress"]

        # Mark in-progress
        watcher._mark_processing(test_file)
        assert file_key in watcher._state["in_progress"]

        # Clear in-progress
        watcher._clear_processing(test_file)
        assert file_key not in watcher._state["in_progress"]

    def test_deduplication_by_hash(self, temp_config: Config) -> None:
        """A file with identical content hash should be skipped."""
        watcher = FileWatcher(temp_config)
        test_file = temp_config.watch_dir / "dedup_test.srt"
        test_file.write_text("Unique content for dedup", encoding="utf-8")

        file_hash = watcher._compute_file_hash(test_file)
        file_key = str(test_file.resolve())

        # Mark as processed
        watcher._mark_processed(
            test_file, file_hash, temp_config.output_dir / "dedup_output.md"
        )

        # Same content — should skip
        assert watcher._is_processed(test_file)
        assert watcher._state["files"][file_key]["hash"] == file_hash

        current_hash = watcher._compute_file_hash(test_file)
        skip = (
            watcher._is_processed(test_file)
            and watcher._state["files"][file_key].get("hash") == current_hash
        )
        assert skip, "Same hash should trigger deduplication skip"

    def test_changed_content_triggers_reprocessing(self, temp_config: Config) -> None:
        """A file with changed content (different hash) should NOT be skipped."""
        watcher = FileWatcher(temp_config)
        test_file = temp_config.watch_dir / "changed_test.srt"
        test_file.write_text("Original content", encoding="utf-8")

        file_hash = watcher._compute_file_hash(test_file)
        file_key = str(test_file.resolve())

        # Mark as processed with original hash
        watcher._mark_processed(
            test_file, file_hash, temp_config.output_dir / "changed_output.md"
        )

        # Change the content
        test_file.write_text("Modified content — should trigger reprocess", encoding="utf-8")
        new_hash = watcher._compute_file_hash(test_file)

        skip = (
            watcher._is_processed(test_file)
            and watcher._state["files"][file_key].get("hash") == new_hash
        )
        assert not skip, "Different hash should NOT trigger dedup skip"

    def test_scan_existing_skips_processed(self, temp_config: Config) -> None:
        """``_scan_existing`` should skip already-processed files and enqueue
        unprocessed ones."""
        watcher = FileWatcher(temp_config)

        # Create a processed file
        processed_file = temp_config.watch_dir / "already_done.srt"
        processed_file.write_text("Already processed", encoding="utf-8")
        p_hash = watcher._compute_file_hash(processed_file)
        watcher._mark_processed(
            processed_file, p_hash, temp_config.output_dir / "done_output.md"
        )

        # Create an unprocessed file
        fresh_file = temp_config.watch_dir / "fresh_file.srt"
        fresh_file.write_text("New and unprocessed", encoding="utf-8")

        # Run scan
        watcher._scan_existing()

        # The fresh file should be in the queue; processed should not
        found_fresh = False
        found_processed = False
        while not watcher._queue.empty():
            queued = watcher._queue.get_nowait()
            if queued.resolve() == fresh_file.resolve():
                found_fresh = True
            if queued.resolve() == processed_file.resolve():
                found_processed = True

        assert found_fresh, "Unprocessed file should be enqueued"
        assert not found_processed, "Processed file should NOT be enqueued"

    def test_stale_in_progress_cleared_on_startup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Stale in-progress entries (older than 1 hour) are cleared when
        a new FileWatcher is created (simulating restart after crash)."""
        # Build a config pointing at tmp_path
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        watch_dir = tmp_path / "watch"
        watch_dir.mkdir()
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("## System Prompt\nYou are a test assistant.\n", encoding="utf-8")

        config_data = {
            "watch": {
                "directory": str(watch_dir),
                "extensions": [".srt"],
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
                "file": str(tmp_path / "logs" / "test.log"),
                "level": "DEBUG",
                "format": "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
                "date_format": "%Y-%m-%d %H:%M:%S",
                "rotation": "midnight",
                "backup_count": 30,
            },
            "state_file": str(tmp_path / "output" / ".processed.json"),
        }
        config = Config(config_data, tmp_path)

        # Create a watcher, inject a stale in-progress entry directly
        watcher1 = FileWatcher(config)
        stale_key = str((watch_dir / "stale_file.srt").resolve())
        watcher1._state["in_progress"][stale_key] = "2020-01-01T00:00:00"
        watcher1._save_state()

        # Create a second watcher — should clear the stale entry
        watcher2 = FileWatcher(config)
        assert stale_key not in watcher2._state["in_progress"]

    def test_file_disappeared_before_processing(self, temp_config: Config) -> None:
        """If a file is deleted before processing, ``_process_file`` returns
        without error."""
        watcher = FileWatcher(temp_config)
        ghost_file = temp_config.watch_dir / "ghost.srt"
        ghost_file.write_text("I will disappear", encoding="utf-8")
        ghost_file.unlink()  # delete it

        # Should not raise
        watcher._process_file(ghost_file)


# =============================================================================
# TestAiClient — AI client (all tests mock the API)
# =============================================================================


class TestAiClient:
    """Tests for :class:`AiClient` covering input validation, subtitle
    counting, text chunking, response merging, backoff math, and retry
    behavior.  All tests that would trigger API calls are mocked."""

    def test_process_rejects_empty_text(self, temp_config: Config) -> None:
        """``process()`` with empty or whitespace-only text raises
        :class:`ValueError`."""
        client = AiClient(temp_config)
        with pytest.raises(ValueError, match="empty"):
            client.process("", "test.srt")
        with pytest.raises(ValueError, match="empty"):
            client.process("   \n  ", "test.srt")

    def test_count_subtitles(self, temp_config: Config) -> None:
        """``_count_subtitles`` returns the correct number of SRT entries."""
        client = AiClient(temp_config)

        # 5-entry SRT
        srt_text = (
            "1\n00:00:01,000 --> 00:00:02,000\nLine one.\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\nLine two.\n\n"
            "3\n00:00:03,000 --> 00:00:04,000\nLine three.\n\n"
            "4\n00:00:04,000 --> 00:00:05,000\nLine four.\n\n"
            "5\n00:00:05,000 --> 00:00:06,000\nLine five.\n"
        )
        count = client._count_subtitles(srt_text)
        assert count == 5

    def test_count_subtitles_plain_text_returns_1(self, temp_config: Config) -> None:
        """Non-SRT plain text returns ``1`` (treated as single logical unit)."""
        client = AiClient(temp_config)
        count = client._count_subtitles("This is just plain text, no SRT structure.")
        assert count == 1

    def test_chunk_text_splitting(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Text with 6 entries and chunk_threshold=2 produces 3 chunks."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("## System Prompt\nTest prompt.\n", encoding="utf-8")

        # Build config with chunk_threshold=2
        config_data = {
            "watch": {
                "directory": str(tmp_path / "watch"),
                "extensions": [".srt"],
                "poll_interval_ms": 1000,
                "debounce_seconds": 0.1,
            },
            "output": {
                "directory": str(tmp_path / "output"),
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
                "chunk_threshold": 2,  # low threshold forces chunking
                "system_prompt_file": str(prompt_file),
            },
            "logging": {
                "file": str(tmp_path / "logs" / "test.log"),
                "level": "DEBUG",
                "format": "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
                "date_format": "%Y-%m-%d %H:%M:%S",
                "rotation": "midnight",
                "backup_count": 30,
            },
            "state_file": str(tmp_path / "state.json"),
        }
        (tmp_path / "watch").mkdir()
        (tmp_path / "output").mkdir()
        (tmp_path / "logs").mkdir()
        config = Config(config_data, tmp_path)

        client = AiClient(config)

        # 6 entries → should produce 3 chunks of 2 entries each
        srt_text = (
            "1\n00:00:01,000 --> 00:00:02,000\nEntry one.\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\nEntry two.\n\n"
            "3\n00:00:03,000 --> 00:00:04,000\nEntry three.\n\n"
            "4\n00:00:04,000 --> 00:00:05,000\nEntry four.\n\n"
            "5\n00:00:05,000 --> 00:00:06,000\nEntry five.\n\n"
            "6\n00:00:06,000 --> 00:00:07,000\nEntry six.\n"
        )
        chunks = client._chunk_text(srt_text)
        assert len(chunks) == 3, f"Expected 3 chunks, got {len(chunks)}"

        # Each chunk should contain "Entry" text
        for chunk in chunks:
            assert "Entry" in chunk

    def test_merge_responses_single(self, temp_config: Config) -> None:
        """A single response in ``_merge_responses`` is returned unchanged."""
        client = AiClient(temp_config)
        single = "# My Notes\n\nContent here.\n"
        result = client._merge_responses([single], "test.srt")
        assert result == single

    def test_backoff_delay_calculation(self, temp_config: Config) -> None:
        """Exponential backoff formula: ``base * 2^(attempt-1)``, capped at 300s."""
        client = AiClient(temp_config)

        # base=0.1, attempt=1 → 0.1 * 2^0 = 0.1
        assert client._backoff_delay(0.1, 1) == pytest.approx(0.1)

        # base=0.1, attempt=3 → 0.1 * 2^2 = 0.4
        assert client._backoff_delay(0.1, 3) == pytest.approx(0.4)

        # base=2.0, attempt=5 → 2.0 * 2^4 = 32.0
        assert client._backoff_delay(2.0, 5) == pytest.approx(32.0)

        # Cap at 300s
        assert client._backoff_delay(100.0, 10) == pytest.approx(300.0)

    def test_retry_on_connection_error(self, temp_config: Config) -> None:
        """On ``APIConnectionError``, the client retries up to max_retries
        and then raises :class:`AiClientError`."""
        client = AiClient(temp_config)

        # APIConnectionError(message: str = ..., *, request: httpx.Request)
        # Both message and request are keyword-only; request has no default
        # in openai 2.x, so we pass a minimal mock.
        conn_err = APIConnectionError(
            message="Connection refused", request=MagicMock()
        )

        with patch.object(client, "_call_api", side_effect=conn_err) as mock_call:
            with patch("time.sleep"):  # no actual waiting
                with pytest.raises(AiClientError, match="API call failed"):
                    client.process("Test subtitle text", "test.srt")

        # Should have been called (max_retries=2) + 1 = 3 times
        assert mock_call.call_count == 3

    def test_no_retry_on_4xx_client_error(self, temp_config: Config) -> None:
        """A 4xx ``APIStatusError`` (non-429) is NOT retried — it raises
        :class:`AiClientError` immediately after one attempt."""
        client = AiClient(temp_config)

        mock_response = MagicMock()
        mock_response.status_code = 400

        # APIStatusError(message: str, *, response: httpx.Response, body: object | None)
        status_err = APIStatusError(
            "Bad request", response=mock_response, body=None
        )

        with patch.object(client, "_call_api", side_effect=status_err) as mock_call:
            with pytest.raises(AiClientError):
                client.process("Test subtitle text", "test.srt")

        # Only one attempt — no retries for 4xx
        assert mock_call.call_count == 1

    def test_process_single_chunk_invokes_api(self, temp_config: Config) -> None:
        """For a small subtitle set (below chunk threshold),
        ``process()`` makes exactly one API call and returns the response."""
        client = AiClient(temp_config)

        srt_text = (
            "1\n00:00:01,000 --> 00:00:02,000\nHello world.\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\nThis is a test.\n"
        )

        with patch.object(
            client,
            "_call_api",
            return_value=_MOCK_AI_OUTPUT,
        ) as mock_call:
            result = client.process(srt_text, "test.srt")

        assert result == _MOCK_AI_OUTPUT
        mock_call.assert_called_once()

    def test_split_entries_complex(self, temp_config: Config) -> None:
        """``_split_entries`` correctly parses multi-line subtitle text
        that spans multiple lines within a single entry."""
        client = AiClient(temp_config)

        srt_text = (
            "1\n"
            "00:00:01,000 --> 00:00:05,000\n"
            "This is a multi-line\n"
            "subtitle entry that spans\n"
            "several lines of text.\n\n"
            "2\n"
            "00:00:06,000 --> 00:00:10,000\n"
            "Single line.\n"
        )
        entries = client._split_entries(srt_text)
        assert len(entries) == 2
        assert "multi-line" in entries[0]
        assert "Single line" in entries[1]


# =============================================================================
# TestEndToEnd — full pipeline integration (mock AI)
# =============================================================================


class TestEndToEnd:
    """End-to-end pipeline tests that exercise parse -> AI (mocked) -> write
    as well as CLI integration."""

    def test_full_pipeline_parse_to_write(
        self, temp_config: Config, sample_srt_file: Path
    ) -> None:
        """Full pipeline: parse an SRT file, run through mock AI, and write
        Markdown output.  Verifies the output file is correctly created."""
        from src.srt_daemon.srt_parser import SrtParser
        from src.srt_daemon.writer import MarkdownWriter

        # Stage 1: Parse
        parser = SrtParser()
        entries = parser.parse(sample_srt_file)
        assert len(entries) == 5
        text = parser.extract_text(entries, with_timestamps=True)

        # Stage 2: AI processing (mocked)
        with patch.object(AiClient, "process", return_value=_MOCK_AI_OUTPUT):
            client = AiClient(temp_config)
            markdown = client.process(text, "lecture01.srt")

        assert markdown == _MOCK_AI_OUTPUT
        assert "Test Notes" in markdown

        # Stage 3: Write output
        writer = MarkdownWriter(temp_config)
        output_path = writer.write(
            content=markdown,
            source_filename="lecture01.srt",
            metadata={"entries": len(entries), "text_length": len(text)},
        )

        # Verify output
        assert output_path.exists()
        output_content = output_path.read_text(encoding="utf-8")
        assert "Test Notes" in output_content
        assert "Mock AI output" in output_content
        # YAML header should be present
        assert 'source: "lecture01.srt"' in output_content
        assert output_content.startswith("---\n")

    def test_watcher_startup_scan_enqueues_files(
        self, temp_config: Config
    ) -> None:
        """``_scan_existing`` should find unprocessed SRT files in the
        watch directory and enqueue them."""
        # Create an unprocessed file
        srt_file = temp_config.watch_dir / "new_lecture.srt"
        srt_file.write_text(
            "1\n00:00:01,000 --> 00:00:03,000\nNew lecture content.\n",
            encoding="utf-8",
        )

        watcher = FileWatcher(temp_config)
        watcher._scan_existing()

        # The file should be in the queue
        found = False
        while not watcher._queue.empty():
            queued = watcher._queue.get_nowait()
            if queued.resolve() == srt_file.resolve():
                found = True

        assert found, "New SRT file should be enqueued by startup scan"

    def test_cli_version_flag(self, capsys: pytest.CaptureFixture) -> None:
        """``--version`` prints the version string and exits cleanly."""
        test_args = ["srt-daemon", "--version"]
        with patch.object(sys, "argv", test_args):
            from src.srt_daemon.main import main
            main()

        captured = capsys.readouterr()
        assert f"SRT Daemon v{__version__}" in captured.out

    def test_cli_parse_args_version(self) -> None:
        """``parse_args`` correctly parses the ``--version`` flag."""
        args = parse_args(["--version"])
        assert args.version is True

    def test_cli_parse_args_once(self) -> None:
        """``parse_args`` correctly parses the ``--once`` flag with a filename."""
        args = parse_args(["--once", "my_file.srt"])
        assert args.once == "my_file.srt"
        assert args.scan is False

    def test_cli_parse_args_scan(self) -> None:
        """``parse_args`` correctly parses the ``--scan`` flag."""
        args = parse_args(["--scan"])
        assert args.scan is True

    def test_cli_parse_args_defaults(self) -> None:
        """``parse_args`` with no arguments returns the expected defaults."""
        args = parse_args([])
        assert args.once is None
        assert args.scan is False
        assert args.version is False
        assert args.config is None
