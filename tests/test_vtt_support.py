"""Tests for WebVTT support and SRT/VTT format auto-detection.

Covers:

* WebVTT cue parsing (:class:`VttParser`)
* format detection and parser dispatch (:mod:`src.srt_daemon.subtitle_parser`)
* the watcher pipeline for ``.vtt`` files (AI client mocked)

These tests never call the AI API.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.srt_daemon.ai_client import AiClient
from src.srt_daemon.config import Config
from src.srt_daemon.srt_parser import SrtParser
from src.srt_daemon.subtitle_base import SubtitleEntry
from src.srt_daemon.subtitle_parser import (
    FORMAT_SRT,
    FORMAT_VTT,
    UnsupportedSubtitleFormat,
    detect_format,
    parse_subtitle,
    parser_for_format,
)
from src.srt_daemon.vtt_parser import VttParser, parse_vtt
from src.srt_daemon.watcher import FileWatcher

# Mock AI response used by the end-to-end tests.
_MOCK_AI_OUTPUT = (
    "# VTT Notes\n\n"
    "## Overview\n\n"
    "Mock AI output for VTT pipeline testing.\n"
)

_SIMPLE_VTT = (
    "WEBVTT\n"
    "\n"
    "00:00:01.000 --> 00:00:03.000\n"
    "First cue.\n"
    "\n"
    "00:00:04.000 --> 00:00:06.000\n"
    "Second cue.\n"
)


def _write(tmp_path: Path, name: str, body: str, encoding: str = "utf-8") -> Path:
    """Write *body* to *tmp_path/name* and return the path.

    The bytes are written directly so that Windows newline translation
    cannot rewrite the CRLF sequences a test explicitly asked for.
    """
    path = tmp_path / name
    path.write_bytes(body.encode(encoding))
    return path


# =============================================================================
# TestVttParser — WebVTT cue parsing
# =============================================================================


class TestVttParser:
    """Tests for :class:`VttParser`: cue grammar, markup handling,
    timestamp normalisation and encoding support."""

    def test_parse_basic(self, sample_vtt_file: Path) -> None:
        """The sample fixture yields 5 cues with WebVTT-style timestamps."""
        parser = VttParser()
        entries = parser.parse(sample_vtt_file)

        assert len(entries) == 5
        assert entries[0].index == 1
        assert entries[0].start_time == "00:00:01.000"
        assert entries[0].end_time == "00:00:03.500"
        assert "ROS2" in entries[0].text
        # Hours-less end timestamp must be padded, not rejected.
        assert entries[1].end_time == "00:00:07.200"
        # Voice span becomes a speaker prefix, inline tags are stripped.
        assert entries[4].text == "Instructor: Here is some bold text."

    def test_note_and_style_blocks_skipped(self, tmp_path: Path) -> None:
        """NOTE / STYLE / REGION blocks never become subtitle entries."""
        body = (
            "WEBVTT\n"
            "\n"
            "NOTE a comment\n"
            "spanning two lines\n"
            "\n"
            "STYLE\n"
            "::cue { color: yellow; }\n"
            "\n"
            "REGION\n"
            "id:fred\n"
            "\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "Real cue text.\n"
        )
        entries = VttParser().parse(_write(tmp_path, "blocks.vtt", body))

        assert len(entries) == 1
        assert entries[0].text == "Real cue text."
        assert "color" not in entries[0].text

    def test_cue_identifiers_and_settings(self, tmp_path: Path) -> None:
        """Cue identifiers are consumed and cue settings are ignored."""
        body = (
            "WEBVTT\n"
            "\n"
            "intro-cue\n"
            "00:00:01.000 --> 00:00:02.000 line:0 position:50% align:middle\n"
            "Identifier handled.\n"
        )
        entries = VttParser().parse(_write(tmp_path, "identifiers.vtt", body))

        assert len(entries) == 1
        assert entries[0].text == "Identifier handled."
        assert entries[0].start_time == "00:00:01.000"

    def test_hours_less_timestamps(self, tmp_path: Path) -> None:
        """MM:SS.mmm timestamps are normalised to HH:MM:SS.mmm."""
        body = (
            "WEBVTT\n"
            "\n"
            "01:02.500 --> 01:05.000\n"
            "No hours component.\n"
        )
        entries = VttParser().parse(_write(tmp_path, "nohours.vtt", body))

        assert entries[0].start_time == "00:01:02.500"
        assert entries[0].end_time == "00:01:05.000"

    def test_inline_markup_and_entities(self, tmp_path: Path) -> None:
        """Inline tags are removed; HTML entities are decoded afterwards."""
        body = (
            "WEBVTT\n"
            "\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "<i>italic</i> <c.yellow>classed</c> &amp; &lt;tag&gt;\n"
        )
        entries = VttParser().parse(_write(tmp_path, "markup.vtt", body))

        text = entries[0].text
        assert "<i>" not in text and "</i>" not in text
        assert "<c.yellow>" not in text
        assert "italic classed & <tag>" == text

    def test_inline_timestamp_tags_removed(self, tmp_path: Path) -> None:
        """Inline cue timestamps are stripped from the payload."""
        body = (
            "WEBVTT\n"
            "\n"
            "00:00:00.080 --> 00:00:02.879\n"
            "<00:00:00.879>hello<00:00:01.500> world\n"
        )
        entries = VttParser().parse(_write(tmp_path, "inline-ts.vtt", body))

        assert entries[0].text == "hello world"

    def test_multiline_payload_preserved(self, tmp_path: Path) -> None:
        """Multi-line cue payloads keep their line structure."""
        body = (
            "WEBVTT\n"
            "\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "Line one\n"
            "Line two\n"
        )
        entries = VttParser().parse(_write(tmp_path, "multiline.vtt", body))

        assert entries[0].text == "Line one\nLine two"

    def test_whitespace_only_line_inside_cue(self, tmp_path: Path) -> None:
        """A space-only line is payload, not a cue separator (YouTube style)."""
        body = (
            "WEBVTT\n"
            "\n"
            "00:00:00.080 --> 00:00:02.879 align:start position:0%\n"
            " \n"
            "hello and welcome\n"
            "\n"
            "00:00:03.000 --> 00:00:04.000\n"
            "next cue\n"
        )
        entries = VttParser().parse(_write(tmp_path, "space-line.vtt", body))

        assert len(entries) == 2
        assert entries[0].text == "hello and welcome"
        assert entries[1].text == "next cue"

    def test_cues_without_blank_separator(self, tmp_path: Path) -> None:
        """Cues lacking a blank line between them are still separated."""
        body = (
            "WEBVTT\n"
            "\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "First.\n"
            "00:00:03.000 --> 00:00:04.000\n"
            "Second.\n"
        )
        entries = VttParser().parse(_write(tmp_path, "tight.vtt", body))

        assert [e.text for e in entries] == ["First.", "Second."]

    def test_empty_cue_payload_dropped(self, tmp_path: Path) -> None:
        """Cues whose payload is empty after cleanup produce no entry."""
        body = (
            "WEBVTT\n"
            "\n"
            "00:00:01.000 --> 00:00:02.000\n"
            "<i></i>\n"
            "\n"
            "00:00:03.000 --> 00:00:04.000\n"
            "Kept cue.\n"
        )
        entries = VttParser().parse(_write(tmp_path, "empty-cue.vtt", body))

        assert len(entries) == 1
        assert entries[0].text == "Kept cue."

    def test_missing_header_is_tolerated(self, tmp_path: Path) -> None:
        """A file without the WEBVTT signature is parsed best-effort."""
        body = "00:00:01.000 --> 00:00:02.000\nHeaderless cue.\n"
        entries = VttParser().parse(_write(tmp_path, "headerless.vtt", body))

        assert len(entries) == 1
        assert entries[0].text == "Headerless cue."

    def test_utf8_bom_and_crlf(self, tmp_path: Path) -> None:
        """A BOM plus CRLF line endings parse cleanly."""
        body = "\ufeffWEBVTT\r\n\r\n00:00:01.000 --> 00:00:02.000\r\nCRLF cue.\r\n"
        entries = VttParser().parse(_write(tmp_path, "crlf.vtt", body))

        assert len(entries) == 1
        assert entries[0].text == "CRLF cue."

    def test_gbk_encoding(self, tmp_path: Path) -> None:
        """GBK-encoded WebVTT content is decoded and parsed."""
        body = (
            "WEBVTT\n"
            "\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "欢迎来到ROS2教程。\n"
        )
        path = tmp_path / "gbk.vtt"
        path.write_bytes(body.encode("gbk"))

        parser = VttParser()
        assert parser._detect_encoding(path) == "gbk"
        entries = parser.parse(path)
        assert len(entries) == 1
        assert "ROS2" in entries[0].text

    def test_extract_text_with_timestamps(self, sample_vtt_file: Path) -> None:
        """Time markers use HH:MM:SS — the WebVTT period is handled."""
        parser = VttParser()
        entries = parser.parse(sample_vtt_file)

        with_ts = parser.extract_text(entries, with_timestamps=True)
        assert with_ts.startswith("[00:00:01] Hello and welcome")
        assert "[00:00:03]" in with_ts
        assert "00:00:01.000" not in with_ts

        without_ts = parser.extract_text(entries, with_timestamps=False)
        assert "[00:" not in without_ts

    def test_merge_short_entries(self) -> None:
        """Short-entry merging works on WebVTT-style timestamps."""
        parser = VttParser()
        entries = [
            SubtitleEntry(1, "00:00:01.000", "00:00:03.000", "First part of"),
            SubtitleEntry(2, "00:00:03.000", "00:00:05.000", "the sentence."),
            SubtitleEntry(
                3,
                "00:00:30.000",
                "00:00:32.000",
                "Unrelated entry with a much longer text that exceeds the threshold",
            ),
        ]
        merged = parser.merge_short_entries(entries, max_gap_seconds=0.5)

        assert len(merged) == 2
        assert merged[0].text == "First part of the sentence."
        assert merged[0].start_time == "00:00:01.000"
        assert merged[0].end_time == "00:00:05.000"

    def test_time_marker_both_separators(self) -> None:
        """The marker helper strips comma and period fractions alike."""
        assert VttParser._time_marker("00:01:02.500") == "00:01:02"
        assert VttParser._time_marker("00:01:02,500") == "00:01:02"

    def test_file_not_found_raises(self) -> None:
        """Parsing a missing file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            VttParser().parse("nonexistent_captions_xyz.vtt")

    def test_parse_vtt_convenience(self, sample_vtt_file: Path) -> None:
        """parse_vtt() chains parse -> merge -> extract_text."""
        text = parse_vtt(sample_vtt_file, with_timestamps=True, merge_short=True)

        assert "[00:00:01]" in text
        assert "ROS2" in text

    def test_parser_for_format_returns_matching_parser(self) -> None:
        """parser_for_format() maps format ids to parser instances."""
        assert isinstance(parser_for_format(FORMAT_SRT), SrtParser)
        assert isinstance(parser_for_format(FORMAT_VTT), VttParser)
        with pytest.raises(UnsupportedSubtitleFormat):
            parser_for_format("ass")


# =============================================================================
# TestFormatDetection — SRT vs WebVTT routing
# =============================================================================


class TestFormatDetection:
    """Tests for content sniffing and extension fallback."""

    def test_srt_file_detected(self, sample_srt_file: Path) -> None:
        detection = detect_format(sample_srt_file)

        assert detection.format == FORMAT_SRT
        assert detection.source == "content"
        assert not detection.mismatched_extension

    def test_vtt_file_detected(self, sample_vtt_file: Path) -> None:
        detection = detect_format(sample_vtt_file)

        assert detection.format == FORMAT_VTT
        assert detection.source == "content"

    def test_srt_content_in_vtt_file(self, tmp_path: Path) -> None:
        """A mislabelled ``.vtt`` holding SRT content is parsed as SRT."""
        body = "1\n00:00:01,000 --> 00:00:03,000\nSRT body in a vtt file.\n"
        path = _write(tmp_path, "mislabelled.vtt", body)

        detection = detect_format(path)
        assert detection.format == FORMAT_SRT
        assert detection.mismatched_extension

        parsed = parse_subtitle(path)
        assert parsed.format == FORMAT_SRT
        assert parsed.entries[0].text == "SRT body in a vtt file."
        assert parsed.entries[0].start_time == "00:00:01,000"

    def test_vtt_content_in_srt_file(self, tmp_path: Path) -> None:
        """A mislabelled ``.srt`` holding WebVTT content is parsed as VTT."""
        path = _write(tmp_path, "mislabelled.srt", _SIMPLE_VTT)

        detection = detect_format(path)
        assert detection.format == FORMAT_VTT
        assert detection.mismatched_extension

        parsed = parse_subtitle(path)
        assert parsed.format == FORMAT_VTT
        assert parsed.entries[0].start_time == "00:00:01.000"

    def test_srt_with_period_separators(self, tmp_path: Path) -> None:
        """Numbered blocks with period timestamps stay SRT."""
        body = "1\n00:00:01.000 --> 00:00:03.000\nPeriod-style SRT block.\n"
        detection = detect_format(_write(tmp_path, "period.srt", body))

        assert detection.format == FORMAT_SRT

    def test_extension_fallback_when_content_inconclusive(self, tmp_path: Path) -> None:
        """With no timing lines, the extension decides."""
        path = _write(tmp_path, "plain.srt", "just prose without timings\n")
        detection = detect_format(path)

        assert detection.format == FORMAT_SRT
        assert detection.source == "extension"

        vtt_path = _write(tmp_path, "header-only.vtt", "WEBVTT\n")
        assert detect_format(vtt_path).format == FORMAT_VTT

    def test_unknown_format_raises(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "notes.txt", "plain prose, no subtitle data\n")

        with pytest.raises(UnsupportedSubtitleFormat):
            detect_format(path)

    def test_parse_subtitle_returns_text_and_metadata(self, sample_vtt_file: Path) -> None:
        parsed = parse_subtitle(sample_vtt_file, with_timestamps=True)

        assert parsed.format == FORMAT_VTT
        assert parsed.format_label == "VTT"
        assert parsed.entry_count == 5
        assert parsed.text_length == len(parsed.text)
        assert parsed.text.startswith("[00:00:01] Hello and welcome")
        assert parsed.source_path.is_absolute()

    def test_parse_subtitle_without_timestamps(self, sample_vtt_file: Path) -> None:
        parsed = parse_subtitle(sample_vtt_file, with_timestamps=False)

        assert "[00:" not in parsed.text
        assert "ROS2" in parsed.text

    def test_parse_subtitle_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            parse_subtitle(tmp_path / "ghost.vtt")


# =============================================================================
# TestWatcherVttPipeline — watcher integration (AI mocked)
# =============================================================================


class TestWatcherVttPipeline:
    """Tests that the watcher detects and processes ``.vtt`` files."""

    def test_watchdog_handler_accepts_vtt(self, temp_config: Config) -> None:
        """The filesystem-event path schedules debounce for `.vtt` files.

        This is the flow that matters in production: the user drops a file
        into the watch directory while the daemon is running.
        """
        from src.srt_daemon.watcher import (
            _SrtEventHandler,
            _SubtitleEventHandler,
        )

        # The historical handler name is kept as an alias.
        assert _SrtEventHandler is _SubtitleEventHandler

        watcher = FileWatcher(temp_config)
        handler = _SubtitleEventHandler(watcher)

        vtt_path = temp_config.watch_dir / "dropped.vtt"
        vtt_path.write_text(_SIMPLE_VTT, encoding="utf-8")
        handler._handle(str(vtt_path), False)
        assert vtt_path in watcher._pending, "VTT file should get a debounce timer"

        txt_path = temp_config.watch_dir / "ignored.txt"
        txt_path.write_text("not a subtitle", encoding="utf-8")
        handler._handle(str(txt_path), False)
        assert txt_path not in watcher._pending

        watcher._cancel_all_pending()

    def test_scan_existing_enqueues_vtt(self, temp_config: Config) -> None:
        vtt_file = temp_config.watch_dir / "new_lecture.vtt"
        vtt_file.write_text(_SIMPLE_VTT, encoding="utf-8")

        watcher = FileWatcher(temp_config)
        watcher._scan_existing()

        queued = []
        while not watcher._queue.empty():
            queued.append(watcher._queue.get_nowait().resolve())

        assert vtt_file.resolve() in queued

    def test_scan_existing_enqueues_both_formats(self, temp_config: Config) -> None:
        srt_file = temp_config.watch_dir / "talk.srt"
        srt_file.write_text(
            "1\n00:00:01,000 --> 00:00:03,000\nSRT content.\n", encoding="utf-8"
        )
        vtt_file = temp_config.watch_dir / "talk.vtt"
        vtt_file.write_text(_SIMPLE_VTT, encoding="utf-8")
        # Not a watched extension.
        (temp_config.watch_dir / "readme.txt").write_text("ignore me", encoding="utf-8")

        watcher = FileWatcher(temp_config)
        watcher._scan_existing()

        queued = set()
        while not watcher._queue.empty():
            queued.add(watcher._queue.get_nowait().resolve())

        assert queued == {srt_file.resolve(), vtt_file.resolve()}

    def test_process_vtt_file_end_to_end(self, temp_config: Config) -> None:
        """A ``.vtt`` file is auto-detected, parsed, sent to the AI and
        written to the output directory with VTT metadata."""
        vtt_file = temp_config.watch_dir / "standup.vtt"
        vtt_file.write_text(
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "Welcome to the standup.\n\n"
            "00:00:04.000 --> 00:00:06.000\n"
            "Today we discuss the ROS2 bridge.\n",
            encoding="utf-8",
        )

        watcher = FileWatcher(temp_config)
        with patch.object(AiClient, "process", return_value=_MOCK_AI_OUTPUT) as mock_ai:
            watcher._process_file(vtt_file)

        assert mock_ai.called, "AI client should have been invoked for the VTT file"
        sent_text = mock_ai.call_args[0][0]
        assert sent_text.startswith("[00:00:01] Welcome to the standup.")

        state = watcher._state["files"][str(vtt_file.resolve())]
        output_path = Path(state["output"])
        assert output_path.exists()

        content = output_path.read_text(encoding="utf-8")
        assert "format: VTT" in content
        assert 'source: "standup.vtt"' in content
        assert "Mock AI output" in content
        assert str(vtt_file.resolve()) not in watcher._state["in_progress"]

    def test_process_srt_file_still_works(self, temp_config: Config) -> None:
        """SRT handling is unchanged by the added VTT support."""
        srt_file = temp_config.watch_dir / "classic.srt"
        srt_file.write_text(
            "1\n00:00:01,000 --> 00:00:03,000\nClassic SRT content.\n",
            encoding="utf-8",
        )

        watcher = FileWatcher(temp_config)
        with patch.object(AiClient, "process", return_value=_MOCK_AI_OUTPUT) as mock_ai:
            watcher._process_file(srt_file)

        sent_text = mock_ai.call_args[0][0]
        assert sent_text.startswith("[00:00:01] Classic SRT content.")

        state = watcher._state["files"][str(srt_file.resolve())]
        content = Path(state["output"]).read_text(encoding="utf-8")
        assert "format: SRT" in content

    def test_process_mislabelled_vtt_holding_srt(self, temp_config: Config) -> None:
        """A ``.vtt`` file containing SRT text is routed to the SRT parser."""
        weird = temp_config.watch_dir / "actually_srt.vtt"
        weird.write_text(
            "1\n00:00:01,000 --> 00:00:03,000\nSRT text behind a vtt name.\n",
            encoding="utf-8",
        )

        watcher = FileWatcher(temp_config)
        with patch.object(AiClient, "process", return_value=_MOCK_AI_OUTPUT):
            watcher._process_file(weird)

        state = watcher._state["files"][str(weird.resolve())]
        content = Path(state["output"]).read_text(encoding="utf-8")
        assert "format: SRT" in content

    def test_unparsable_file_leaves_state_clean(self, temp_config: Config) -> None:
        """A watched-extension file with no cues is skipped without state."""
        broken = temp_config.watch_dir / "broken.vtt"
        broken.write_text("WEBVTT\n\nNOTE nothing else here\n", encoding="utf-8")

        watcher = FileWatcher(temp_config)
        with patch.object(AiClient, "process", return_value=_MOCK_AI_OUTPUT) as mock_ai:
            watcher._process_file(broken)

        assert not mock_ai.called
        assert str(broken.resolve()) not in watcher._state["files"]
        assert str(broken.resolve()) not in watcher._state["in_progress"]
