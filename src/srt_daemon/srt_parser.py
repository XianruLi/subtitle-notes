"""SRT subtitle file parser.

Parses standard SRT format files into :class:`SubtitleEntry` items.  Encoding
detection, text extraction, short-entry merging and timestamp arithmetic are
inherited from :class:`~src.srt_daemon.subtitle_base.SubtitleParserBase`; this
module only implements the SRT block grammar.

Usage::

    from src.srt_daemon.srt_parser import SrtParser

    parser = SrtParser()
    entries = parser.parse("path/to/subtitles.srt")
    text = parser.extract_text(entries, with_timestamps=True)
    merged = parser.merge_short_entries(entries, max_gap_seconds=0.5)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from src.srt_daemon.logger import get_logger
from src.srt_daemon.subtitle_base import SubtitleEntry, SubtitleParserBase

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# SRT timestamp pattern: HH:MM:SS,mmm --> HH:MM:SS,mmm
# Accepts both comma and period as millisecond separator (period is non-standard
# but occurs in some tools).
_TIMESTAMP_RE = re.compile(
    r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})"
)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

#: Subtitle entry model, shared with the WebVTT parser.  The historical
#: `SrtEntry` name is kept as an alias for backwards compatibility.
SrtEntry = SubtitleEntry


# ---------------------------------------------------------------------------
# Parser class
# ---------------------------------------------------------------------------


class SrtParser(SubtitleParserBase):
    """Parser for SRT subtitle files with encoding auto-detection.

    Handles standard SRT format with best-effort recovery for common
    anomalies: extra blank lines, missing or corrupt index numbers,
    non-standard encodings (UTF-8 / UTF-8-BOM / GBK).

    Encoding detection, `extract_text()` and `merge_short_entries()` are inherited
    from :class:`~src.srt_daemon.subtitle_base.SubtitleParserBase`.

    Usage::

        parser = SrtParser()
        entries = parser.parse("lecture.srt")
        plain_text = parser.extract_text(entries)
    """

    format_name = "srt"

    # --- Internal parsing ----------------------------------------------------

    def _parse_content(self, content: str, filename: str) -> list[SrtEntry]:
        """Parse raw SRT text content into a list of :class:`SrtEntry`.

        Uses a state machine:
        - **State 0** — expecting an index number line.
        - **State 1** — expecting a timestamp line.
        - **State 2** — reading text lines until a blank line is encountered.

        Best-effort recovery:
        - Missing index numbers are auto-generated from the last seen index.
        - Extra blank lines between blocks are silently consumed.
        - Blocks with a timestamp but no text are skipped.

        Args:
            content: Raw text content of the SRT file.
            filename: Source filename (for log context only).

        Returns:
            List of parsed entries.  Returns an empty list if no valid
            subtitle blocks are found.
        """
        entries: list[SrtEntry] = []
        lines = content.splitlines()

        # Accumulators for the current subtitle block
        current_index: Optional[int] = None
        current_start: Optional[str] = None
        current_end: Optional[str] = None
        current_text_lines: list[str] = []

        # Parser state
        state = 0  # 0=expect_index, 1=expect_timestamp, 2=reading_text

        # Track the last successfully committed index for gap repair
        last_seen_index = 0

        def _flush_entry() -> Optional[SrtEntry]:
            """Build an SrtEntry from accumulated state, or ``None`` if
            the block is incomplete or has no text."""
            nonlocal last_seen_index
            if current_index is not None and current_start and current_end:
                text = "\n".join(current_text_lines).strip()
                if text:
                    last_seen_index = current_index
                    return SrtEntry(
                        index=current_index,
                        start_time=current_start,
                        end_time=current_end,
                        text=text,
                    )
            return None

        i = 0
        line_count = len(lines)

        while i < line_count:
            line = lines[i].rstrip("\r")

            # Strip BOM if present on the very first line
            if i == 0 and line.startswith("﻿"):
                line = line[1:]

            # --- State 0: Expect index number --------------------------------
            if state == 0:
                if not line:
                    i += 1
                    continue

                # Auto-repair: if the line looks like a timestamp, the index
                # was probably omitted.  Flush any previous entry and create
                # a new one with an auto-incremented index.
                ts_match = _TIMESTAMP_RE.match(line)
                if ts_match:
                    log.debug(
                        "Line %d in %s: missing index, auto-repairing",
                        i + 1,
                        filename,
                    )
                    prev = _flush_entry()
                    if prev:
                        entries.append(prev)
                    current_index = last_seen_index + 1
                    current_start = ts_match.group(1).replace(".", ",")
                    current_end = ts_match.group(2).replace(".", ",")
                    current_text_lines = []
                    state = 2
                    i += 1
                    continue

                # Normal case: parse integer index
                try:
                    current_index = int(line.strip())
                    state = 1
                    i += 1
                    continue
                except ValueError:
                    log.debug(
                        "Line %d in %s: expected index, got '%s', skipping",
                        i + 1,
                        filename,
                        line[:50],
                    )
                    i += 1
                    continue

            # --- State 1: Expect timestamp -----------------------------------
            if state == 1:
                if not line:
                    # Blank line where we expected a timestamp — recoverable
                    i += 1
                    continue

                ts_match = _TIMESTAMP_RE.match(line)
                if ts_match:
                    current_start = ts_match.group(1).replace(".", ",")
                    current_end = ts_match.group(2).replace(".", ",")
                    current_text_lines = []
                    state = 2
                else:
                    # Timestamp line is malformed — discard this block
                    log.debug(
                        "Line %d in %s: expected timestamp, got '%s', resetting",
                        i + 1,
                        filename,
                        line[:50],
                    )
                    current_index = None
                    state = 0
                i += 1
                continue

            # --- State 2: Reading text ---------------------------------------
            if state == 2:
                if not line:
                    # Blank line marks end of the current entry
                    entry = _flush_entry()
                    if entry:
                        entries.append(entry)
                    # Reset accumulators
                    current_index = None
                    current_start = None
                    current_end = None
                    current_text_lines = []
                    state = 0
                else:
                    current_text_lines.append(line)
                i += 1
                continue

        # --- Flush the last entry if the file does not end with a blank line
        entry = _flush_entry()
        if entry:
            entries.append(entry)

        # --- Post-parse diagnostics ------------------------------------------
        if not entries:
            log.warning(
                "No valid subtitle entries found in %s (check file format)",
                filename,
            )
        else:
            # Count index gaps for debug-level awareness
            expected = 1
            gaps = 0
            for e in entries:
                if e.index != expected:
                    gaps += 1
                expected = e.index + 1
            if gaps:
                log.debug(
                    "%s: %d index gaps detected (auto-repaired)", filename, gaps
                )

        return entries


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def parse_srt(
    file_path: str | Path,
    with_timestamps: bool = False,
    merge_short: bool = False,
    max_gap_seconds: float = 0.5,
) -> str:
    """Parse an SRT file and return extracted text in a single call.

    Convenience wrapper that chains :meth:`SrtParser.parse`,
    :meth:`SrtParser.merge_short_entries` (optional), and
    :meth:`SrtParser.extract_text`.

    Args:
        file_path: Path to the ``.srt`` file.
        with_timestamps: If ``True``, include ``[HH:MM:SS]`` markers in
            the output text.
        merge_short: If ``True``, merge consecutive short entries before
            extracting text.
        max_gap_seconds: Maximum time gap (seconds) for merging.  Only
            used when *merge_short* is ``True``.

    Returns:
        Extracted subtitle text as a single string.
    """
    parser = SrtParser()
    entries = parser.parse(file_path)

    if merge_short:
        entries = parser.merge_short_entries(
            entries, max_gap_seconds=max_gap_seconds
        )

    return parser.extract_text(entries, with_timestamps=with_timestamps)


# ===========================================================================
# Self-test
# ===========================================================================

if __name__ == "__main__":
    """Run a quick self-test with a synthetic SRT file.

    Usage::

        python -m src.srt_daemon.srt_parser
    """
    import logging
    import sys
    import tempfile

    # Minimal logging for test visibility
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    # ---- Sample SRT content ------------------------------------------------
    # Covers: basic entries, short entries for merging, code blocks,
    # speaker dialogue markers.
    sample_srt = """\
1
00:00:01,000 --> 00:00:03,500
Hello and welcome to this ROS2 tutorial.

2
00:00:03,800 --> 00:00:07,200
Today we will learn how to create

3
00:00:07,200 --> 00:00:10,000
a simple publisher and subscriber in C++.

4
00:00:12,000 --> 00:00:15,500
First, let's talk about ROS2 nodes.

5
00:00:15,800 --> 00:00:20,000
A node is the basic unit of computation in ROS2.

6
00:00:22,000 --> 00:00:25,000
Now let's look at some code:

7
00:00:25,500 --> 00:00:30,000
#include <rclcpp/rclcpp.hpp>
#include <std_msgs/msg/string.hpp>

8
00:00:30,500 --> 00:00:35,000
class Talker : public rclcpp::Node {
public:
  Talker() : Node("talker") {}
};

9
00:00:38,000 --> 00:00:42,000
- Speaker A: This is an interview.
- Speaker B: Yes, let's discuss ROS2.
"""

    # ---- Write sample to a temporary file ----------------------------------
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".srt",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        tmp.write(sample_srt)
        tmp_path = Path(tmp.name)

    try:
        print("=" * 60)
        print("SRT PARSER — SELF TEST")
        print("=" * 60)

        parser = SrtParser()

        # -- Test 1: Basic parsing -------------------------------------------
        print("\n[Test 1] Basic parsing (expected: 9 entries)...")
        entries = parser.parse(tmp_path)
        print(f"  Parsed {len(entries)} entries")
        for e in entries[:3]:
            print(
                f"  [{e.index}] {e.start_time} --> {e.end_time}: "
                f"{e.text[:50]}..."
            )
        assert len(entries) == 9, f"Expected 9 entries, got {len(entries)}"
        # Verify first entry content
        assert "ROS2" in entries[0].text
        assert entries[0].start_time == "00:00:01,000"
        print("  PASSED")

        # -- Test 2: Extract text without timestamps -------------------------
        print("\n[Test 2] extract_text() — without timestamps...")
        text = parser.extract_text(entries, with_timestamps=False)
        assert "ROS2" in text
        assert "[00:" not in text
        print(f"  Output: {len(text)} chars, first 80: {text[:80]!r}")
        print("  PASSED")

        # -- Test 3: Extract text with timestamps ----------------------------
        print("\n[Test 3] extract_text() — with timestamps...")
        text_ts = parser.extract_text(entries, with_timestamps=True)
        assert "[00:00:01]" in text_ts
        print(f"  First line: {repr(text_ts.split(chr(10))[0])}")
        print("  PASSED")

        # -- Test 4: Merge short entries (tight gap) -------------------------
        print("\n[Test 4] merge_short_entries() — max_gap=0.5s...")
        merged = parser.merge_short_entries(entries, max_gap_seconds=0.5)
        print(f"  Original: {len(entries)} entries -> Merged: {len(merged)} entries")
        assert len(merged) < len(entries), "Merge should reduce entry count"
        # Entries 2+3 (gap=0s) should definitely be merged
        merged_texts = [e.text for e in merged]
        found_merged_23 = any(
            "learn how to create" in t and "simple publisher" in t
            for t in merged_texts
        )
        assert found_merged_23, "Entries 2+3 should be merged (gap=0s)"
        print("  PASSED")

        # -- Test 5: Speaker break detection ---------------------------------
        print("\n[Test 5] _is_speaker_break() heuristic...")
        # Entry 8 (code) and Entry 9 (speaker dialogue) — should NOT trigger
        # because entry 8 does not start with "-".
        e8 = entries[7]
        e9 = entries[8]
        is_break = parser._is_speaker_break(e8, e9)
        print(f"  e8 starts: {e8.text[:40]!r}")
        print(f"  e9 starts: {e9.text[:40]!r}")
        print(f"  Speaker break: {is_break} (expected: False — only e9 starts with '-')")
        assert not is_break, "Should not flag speaker break when only one side has '-'"
        print("  PASSED")

        # -- Test 6: Time utility --------------------------------------------
        print("\n[Test 6] _time_to_seconds() conversion...")
        secs = SrtParser._time_to_seconds("01:30:15,500")
        expected = 5415.5
        assert abs(secs - expected) < 0.001, (
            f"Expected {expected}, got {secs}"
        )
        print(f"  01:30:15,500 -> {secs} seconds")
        print("  PASSED")

        # -- Test 7: Encoding detection (UTF-8 without BOM) ------------------
        print("\n[Test 7] _detect_encoding()...")
        enc = parser._detect_encoding(tmp_path)
        print(f"  Detected: {enc}")
        assert enc in ("utf-8", "utf-8-sig"), f"Unexpected encoding: {enc}"
        print("  PASSED")

        # -- Test 8: Parse GBK-encoded file ----------------------------------
        print("\n[Test 8] GBK encoding detection...")
        gbk_content = (
            "1\r\n"
            "00:00:01,000 --> 00:00:03,000\r\n"
            "欢迎来到ROS2教程。\r\n"
            "\r\n"
            "2\r\n"
            "00:00:03,500 --> 00:00:06,000\r\n"
            "今天我们将学习C++编程。\r\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".srt", delete=False
        ) as gbk_tmp:
            gbk_tmp.write(gbk_content.encode("gbk"))
            gbk_path = Path(gbk_tmp.name)

        try:
            gbk_enc = parser._detect_encoding(gbk_path)
            print(f"  Detected: {gbk_enc}")
            assert gbk_enc == "gbk", f"Expected gbk, got {gbk_enc}"
            gbk_entries = parser.parse(gbk_path)
            assert len(gbk_entries) == 2
            assert "ROS2" in gbk_entries[0].text
            print(f"  Parsed {len(gbk_entries)} entries from GBK file")
            print("  PASSED")
        finally:
            gbk_path.unlink(missing_ok=True)

        # -- Test 9: parse_srt() convenience function ------------------------
        print("\n[Test 9] parse_srt() convenience function...")
        full_text = parse_srt(
            tmp_path, with_timestamps=True, merge_short=True, max_gap_seconds=0.3
        )
        assert "ROS2" in full_text
        assert "[00:00:01]" in full_text
        print(f"  Output: {len(full_text)} chars")
        print("  PASSED")

        # -- Test 10: File not found -----------------------------------------
        print("\n[Test 10] FileNotFoundError handling...")
        try:
            parser.parse("nonexistent_file_xyz.srt")
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError:
            print("  Correctly raised FileNotFoundError")
            print("  PASSED")

        # -- Test 11: Empty / edge-case entries ------------------------------
        print("\n[Test 11] Edge case — entry with empty text...")
        empty_content = (
            "1\n"
            "00:00:01,000 --> 00:00:03,000\n"
            "\n"  # text line is empty
            "\n"
            "2\n"
            "00:00:04,000 --> 00:00:06,000\n"
            "Valid text here.\n"
        )
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".srt", encoding="utf-8", delete=False
        ) as ec_tmp:
            ec_tmp.write(empty_content)
            ec_path = Path(ec_tmp.name)

        try:
            ec_entries = parser.parse(ec_path)
            assert len(ec_entries) == 1, (
                f"Expected 1 entry (skipped empty), got {len(ec_entries)}"
            )
            assert ec_entries[0].text == "Valid text here."
            print(f"  Skipped empty entry, kept: {ec_entries[0]}")
            print("  PASSED")
        finally:
            ec_path.unlink(missing_ok=True)

        print("\n" + "=" * 60)
        print("ALL 11 TESTS PASSED")
        print("=" * 60)

    finally:
        tmp_path.unlink(missing_ok=True)
