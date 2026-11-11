"""WebVTT (.vtt) subtitle file parser.

Parses WebVTT cue files into the shared :class:`SubtitleEntry` model so
that the rest of the pipeline (text extraction, merging, AI processing,
Markdown writing) does not care which container format the subtitles came in.

Supported WebVTT constructs:

* the mandatory ``WEBVTT`` signature plus optional header metadata
  (``Kind:``, ``Language:``, ``X-TIMESTAMP-MAP=...``),
* cue identifiers, cue settings (``line:``, ``position:``, ``align:`` …)
  and timestamps with or without an hours component
  (``HH:MM:SS.mmm`` / ``MM:SS.mmm``),
* ``NOTE`` / ``STYLE`` / ``REGION`` blocks (skipped — they carry no
  subtitle text),
* inline markup: ``<v Speaker>`` voice spans (kept as ``Speaker:`` prefixes),
  ``<c.class>`` / ``<i>`` / ``<b>`` / ``<ruby>`` tags and inline
  timestamp tags (removed), and HTML character references (decoded).

Usage::

    from src.srt_daemon.vtt_parser import VttParser

    parser = VttParser()
    entries = parser.parse("path/to/captions.vtt")
    text = parser.extract_text(entries, with_timestamps=True)
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from src.srt_daemon.logger import get_logger
from src.srt_daemon.subtitle_base import SubtitleEntry, SubtitleParserBase

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# File signature: the first non-blank line must start with "WEBVTT".
_HEADER_RE = re.compile(r"^WEBVTT(?:[ \t].*)?$")

# Cue timing line:  [HH:]MM:SS.mmm --> [HH:]MM:SS.mmm [cue settings]
#   * the hours component is optional in WebVTT,
#   * exactly three fraction digits are required by the spec, but 1-3 are
#     accepted (and padded) because some exporters are sloppy,
#   * a comma fraction separator is tolerated for the same reason.
_CUE_TIMING_RE = re.compile(
    r"^\s*"
    r"(?:(\d{1,3}):)?(\d{2}):(\d{2})[.,](\d{1,3})"
    r"\s*-->\s*"
    r"(?:(\d{1,3}):)?(\d{2}):(\d{2})[.,](\d{1,3})"
    r"(?:\s+.*)?$"
)

# Blocks that carry metadata/comment text instead of subtitle cues.
_BLOCK_KEYWORDS = ("NOTE", "STYLE", "REGION")

# Inline timestamp tag inside a cue payload: <00:00:01.000> / <01:00.000>
_INLINE_TS_RE = re.compile(r"<\d{1,3}:\d{2}(?::\d{2})?[.,]\d{1,3}>")

# Voice span opening tag: <v Roger Bingham> / <v.loud Roger>
_VOICE_OPEN_RE = re.compile(r"<v(?:\.[^\s>]+)?\s+([^>]*?)\s*>")

# Any remaining well-formed tag: <i>, </i>, <b>, <c.red>, <ruby>, <lang en>, ...
_TAG_RE = re.compile(r"</?[a-zA-Z][^>]*>")

# Runs of spaces/tabs inside a cue line are collapsed.
_WHITESPACE_RE = re.compile(r"[ \t]+")


# ---------------------------------------------------------------------------
# Parser class
# ---------------------------------------------------------------------------


class VttParser(SubtitleParserBase):
    """Parser for WebVTT subtitle files with encoding auto-detection.

    Encoding detection, text extraction and short-entry merging are
    inherited from :class:`SubtitleParserBase`; only the cue grammar
    below is WebVTT-specific.

    Usage::

        parser = VttParser()
        entries = parser.parse("standup.vtt")
        plain_text = parser.extract_text(entries)
    """

    format_name = "vtt"

    # --- Internal parsing ----------------------------------------------------

    def _parse_content(self, content: str, filename: str) -> list[SubtitleEntry]:
        """Parse raw WebVTT *content* into a list of :class:`SubtitleEntry`.

        The file is walked line by line: the ``WEBVTT`` header and header
        metadata are consumed first, then each cue block
        (optional identifier, timing line, payload lines) is turned into one
        entry.  ``NOTE`` / ``STYLE`` / ``REGION`` blocks are skipped.

        Best-effort recovery:

        * a missing ``WEBVTT`` header is logged but does not abort parsing,
        * cues without a blank separator are accepted as long as the next
          line is a timing line,
        * cues whose payload is empty after markup removal are dropped.

        Args:
            content: Raw text content of the VTT file.
            filename: Source filename (for log context only).

        Returns:
            List of parsed cues.  Empty if no valid cue was found.
        """
        lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        line_count = len(lines)

        i = self._skip_header(lines, filename)

        entries: list[SubtitleEntry] = []
        cue_number = 0
        skipped_blocks = 0

        while i < line_count:
            line = lines[i].strip()

            # --- Blank line: cue separator -----------------------------------
            if not line:
                i += 1
                continue

            # --- NOTE / STYLE / REGION blocks (no subtitle text) -------------
            if line.split()[0].upper() in _BLOCK_KEYWORDS:
                i += 1
                while i < line_count and lines[i].strip():
                    i += 1
                skipped_blocks += 1
                continue

            # --- Optional cue identifier -------------------------------------
            if not _CUE_TIMING_RE.match(line):
                # A cue identifier is only valid when a timing line follows.
                if i + 1 < line_count and _CUE_TIMING_RE.match(lines[i + 1].strip()):
                    i += 1
                    line = lines[i].strip()
                else:
                    log.debug(
                        "%s: line %d is neither a cue timing line nor a cue "
                        "identifier, skipping: %r",
                        filename,
                        i + 1,
                        line[:50],
                    )
                    i += 1
                    continue

            # --- Timing line --------------------------------------------------
            timing = _CUE_TIMING_RE.match(line)
            if timing is None:  # defensive; unreachable by construction
                i += 1
                continue
            start_time = self._normalise_timestamp(timing, 1)
            end_time = self._normalise_timestamp(timing, 5)
            i += 1

            # --- Cue payload ---------------------------------------------------
            payload: list[str] = []
            while i < line_count:
                payload_line = lines[i]
                # A truly empty line ends the cue.  A whitespace-only line is
                # payload (YouTube emits " " placeholders inside cues).
                if payload_line == "":
                    break
                if _CUE_TIMING_RE.match(payload_line.strip()):
                    break  # next cue without a blank separator
                payload.append(payload_line)
                i += 1

            text = self._clean_payload(payload)
            if text:
                cue_number += 1
                entries.append(
                    SubtitleEntry(
                        index=cue_number,
                        start_time=start_time,
                        end_time=end_time,
                        text=text,
                    )
                )

        # --- Post-parse diagnostics ------------------------------------------
        if not entries:
            log.warning(
                "No valid subtitle entries found in %s (check file format)",
                filename,
            )
        else:
            log.debug(
                "%s: parsed %d cue(s), skipped %d NOTE/STYLE/REGION block(s)",
                filename,
                len(entries),
                skipped_blocks,
            )

        return entries

    def _skip_header(self, lines: list[str], filename: str) -> int:
        """Validate the ``WEBVTT`` signature and return the first cue line index.

        Leading blank lines and a UTF-8 BOM are tolerated.  The optional
        header metadata block (everything up to the first blank line) is
        skipped as well.  A file without the signature is not rejected —
        parsing continues best-effort from the current position.
        """
        i = 0
        line_count = len(lines)

        while i < line_count and not lines[i].strip():
            i += 1

        if i >= line_count:
            log.warning("%s: file contains no content", filename)
            return i

        first_line = lines[i].lstrip("\ufeff").strip()
        if not _HEADER_RE.match(first_line):
            log.warning(
                "%s: missing WEBVTT header (first line: %r) — parsing best-effort",
                filename,
                first_line[:50],
            )
            return i

        # Consume the signature line, then the header metadata block.  Stop
        # early if a timing line shows up (malformed header without a blank
        # line) so that the cue is not swallowed.
        i += 1
        while i < line_count and lines[i].strip() and not _CUE_TIMING_RE.match(lines[i].strip()):
            i += 1

        return i

    @staticmethod
    def _normalise_timestamp(match: "re.Match[str]", group: int) -> str:
        """Build an ``HH:MM:SS.mmm`` timestamp from a cue-timing match.

        Args:
            match: Successful match of :data:`_CUE_TIMING_RE`.
            group: Index of the first capture group of the timestamp
                (1 for the start time, 5 for the end time).

        Returns:
            Normalised timestamp, e.g. ``01:02.5`` -> ``00:01:02.500``.
        """
        hours = match.group(group) or "0"
        minutes = match.group(group + 1)
        seconds = match.group(group + 2)
        fraction = match.group(group + 3).ljust(3, "0")
        return f"{int(hours):02d}:{minutes}:{seconds}.{fraction}"

    @staticmethod
    def _clean_payload(payload: list[str]) -> str:
        """Turn raw cue payload lines into plain subtitle text.

        Inline timestamp tags and markup tags are removed, voice spans are
        converted to ``Speaker:`` prefixes, HTML character references are
        decoded, and blank/whitespace-only lines are dropped.
        """
        cleaned: list[str] = []

        for raw_line in payload:
            line = _INLINE_TS_RE.sub("", raw_line)
            line = _VOICE_OPEN_RE.sub(
                lambda m: f"{m.group(1)}: " if m.group(1) else "", line
            )
            line = _TAG_RE.sub("", line)
            # Entities are decoded last so that escaped markup ("&lt;i&gt;")
            # stays literal text instead of being stripped as a tag.
            line = html.unescape(line)
            line = _WHITESPACE_RE.sub(" ", line).strip()
            if line:
                cleaned.append(line)

        return "\n".join(cleaned).strip()


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------


def parse_vtt(
    file_path: str | Path,
    with_timestamps: bool = False,
    merge_short: bool = False,
    max_gap_seconds: float = 0.5,
) -> str:
    """Parse a VTT file and return extracted text in a single call.

    Convenience wrapper that chains :meth:`VttParser.parse`,
    :meth:`VttParser.merge_short_entries` (optional), and
    :meth:`VttParser.extract_text`.

    Args:
        file_path: Path to the ``.vtt`` file.
        with_timestamps: If True, include ``[HH:MM:SS]`` markers in the
            output text.
        merge_short: If True, merge consecutive short cues before
            extracting text.
        max_gap_seconds: Maximum time gap (seconds) for merging.  Only
            used when *merge_short* is True.

    Returns:
        Extracted subtitle text as a single string.
    """
    parser = VttParser()
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
    """Run a quick self-test with a synthetic VTT file.

    Usage::

        python -m src.srt_daemon.vtt_parser
    """
    import logging
    import sys
    import tempfile

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )

    # Covers: header metadata, NOTE/STYLE blocks, cue identifiers, cue
    # settings, missing hours, inline markup, entities, multi-line cues.
    sample_vtt = """\
WEBVTT
Kind: captions
Language: en

NOTE This is a comment block
that spans two lines.

STYLE
::cue {
  color: yellow;
}

intro
00:00:01.000 --> 00:00:03.500 align:start position:0%
Hello and welcome to this ROS2 tutorial.

00:00:03.800 --> 00:07.200
Today we will learn how to create
a simple publisher and subscriber.

00:00:08.000 --> 00:00:10.000
<v Roger Bingham>Here is some <b>bold</b> text &amp; an entity.

00:00:11.000 --> 00:00:12.000
<00:00:11.500>Inline timestamp tag removed.
"""

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".vtt", encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(sample_vtt)
        tmp_path = Path(tmp.name)

    try:
        print("=" * 60)
        print("VTT PARSER — SELF TEST")
        print("=" * 60)

        parser = VttParser()

        print("\n[Test 1] Basic cue parsing (expected: 4 cues)...")
        entries = parser.parse(tmp_path)
        for e in entries:
            print(f"  [{e.index}] {e.start_time} --> {e.end_time}: {e.text[:60]}")
        assert len(entries) == 4, f"Expected 4 cues, got {len(entries)}"
        print("  PASSED")

        print("\n[Test 2] Hours-less timestamp normalisation...")
        assert entries[1].start_time == "00:00:03.800"
        assert entries[1].end_time == "00:00:07.200"
        print(f"  00:07.200 -> {entries[1].end_time}")
        print("  PASSED")

        print("\n[Test 3] Multi-line payload preserved...")
        assert "\n" in entries[1].text
        print(f"  {entries[1].text!r}")
        print("  PASSED")

        print("\n[Test 4] Voice span + inline markup + entities...")
        assert entries[2].text.startswith("Roger Bingham: ")
        assert "<b>" not in entries[2].text
        assert "&" in entries[2].text and "&amp;" not in entries[2].text
        print(f"  {entries[2].text!r}")
        print("  PASSED")

        print("\n[Test 5] Inline timestamp tag removed...")
        assert entries[3].text == "Inline timestamp tag removed."
        print("  PASSED")

        print("\n[Test 6] extract_text() with timestamps...")
        text = parser.extract_text(entries, with_timestamps=True)
        assert "[00:00:01]" in text
        print(f"  First line: {text.splitlines()[0]!r}")
        print("  PASSED")

        print("\n[Test 7] WEBVTT header missing — best-effort parse...")
        loose = tmp_path.with_suffix(".loose.vtt")
        loose.write_text(
            "00:00:01.000 --> 00:00:02.000\nNo header here.\n",
            encoding="utf-8",
        )
        try:
            loose_entries = parser.parse(loose)
            assert len(loose_entries) == 1
            print(f"  Parsed {len(loose_entries)} cue without header")
            print("  PASSED")
        finally:
            loose.unlink(missing_ok=True)

        print("\n[Test 8] parse_vtt() convenience function...")
        full_text = parse_vtt(tmp_path, with_timestamps=True, merge_short=True)
        assert "ROS2" in full_text and "[00:00:01]" in full_text
        print(f"  Output: {len(full_text)} chars")
        print("  PASSED")

        print("\n[Test 9] FileNotFoundError handling...")
        try:
            parser.parse("nonexistent_file_xyz.vtt")
            raise AssertionError("Should have raised FileNotFoundError")
        except FileNotFoundError:
            print("  Correctly raised FileNotFoundError")
            print("  PASSED")

        print("\n" + "=" * 60)
        print("ALL 9 TESTS PASSED")
        print("=" * 60)
    finally:
        tmp_path.unlink(missing_ok=True)
