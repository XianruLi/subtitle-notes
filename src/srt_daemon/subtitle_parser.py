"""Subtitle format detection and parser dispatch (SRT vs WebVTT).

This module is the step that runs *before* parsing: given a file dropped into
the watch directory it decides whether the content is SRT or WebVTT and hands
it to the matching parser, so that callers never need to know which format
they are dealing with.

Detection order:

1. **Content sniff** — the first few KB are inspected for the ``WEBVTT``
   signature and for SRT/WebVTT timing patterns.
2. **Extension** — ``.srt`` / ``.vtt`` is used when the content is
   inconclusive.

Content wins over the extension (mislabelled downloads are common) and any
mismatch is logged as a warning.  Files whose extension is unknown *and*
whose content is inconclusive raise :class:`UnsupportedSubtitleFormat`.

Usage::

    from src.srt_daemon.subtitle_parser import parse_subtitle

    parsed = parse_subtitle("videos/lecture.vtt")
    print(parsed.format, parsed.entry_count)
    ai_input = parsed.text          # "[HH:MM:SS] text" blocks
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from src.srt_daemon.logger import get_logger
from src.srt_daemon.subtitle_base import (
    SubtitleEntry,
    SubtitleParserBase,
    read_text_sample,
)
from src.srt_daemon.srt_parser import SrtParser
from src.srt_daemon.vtt_parser import VttParser

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Format identifiers used throughout the dispatch layer.
FORMAT_SRT = "srt"
FORMAT_VTT = "vtt"

#: Formats the daemon can parse.
SUPPORTED_FORMATS: tuple[str, ...] = (FORMAT_SRT, FORMAT_VTT)

#: File extension -> format mapping (lower-case, with the leading dot).
EXTENSION_FORMATS: dict[str, str] = {
    ".srt": FORMAT_SRT,
    ".vtt": FORMAT_VTT,
}

#: Default number of characters sampled when sniffing file content.
_SNIFF_SAMPLE_CHARS = 8192

#: WebVTT signature: the first non-blank line starts with "WEBVTT".
_WEBVTT_HEADER_RE = re.compile(r"^WEBVTT(?:[ \t].*)?$")

#: SRT timing line (comma fraction separator) — the strongest SRT signal.
_SRT_TIMING_RE = re.compile(
    r"\d{2}:\d{2}:\d{2},\d{3}\s*-->\s*\d{2}:\d{2}:\d{2},\d{3}"
)

#: Any subtitle timing line (SRT or WebVTT, hours optional).
_ANY_TIMING_RE = re.compile(
    r"(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{1,3}\s*-->\s*"
    r"(?:\d{1,3}:)?\d{2}:\d{2}[.,]\d{1,3}"
)

#: A bare integer line, i.e. an SRT sequence number.
_INDEX_LINE_RE = re.compile(r"^\s*\d+\s*$")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class UnsupportedSubtitleFormat(ValueError):
    """Raised when a file is neither recognisable SRT nor recognisable WebVTT."""


# ---------------------------------------------------------------------------
# Detection results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormatDetection:
    """Outcome of :func:`detect_format`.

    Attributes:
        format: Detected format, :data:`FORMAT_SRT` or :data:`FORMAT_VTT`.
        source: ``content`` when the file body decided the format,
            ``extension`` when the file name was the only signal.
        extension_format: Format implied by the extension, if it is a
            supported one (kept for logging/diagnostics).
    """

    format: str
    source: str
    extension_format: Optional[str] = None

    @property
    def mismatched_extension(self) -> bool:
        """True when the file name disagrees with the detected content."""
        return (
            self.extension_format is not None
            and self.extension_format != self.format
        )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def sniff_format(
    file_path: str | Path, sample_chars: int = _SNIFF_SAMPLE_CHARS
) -> Optional[str]:
    """Guess the subtitle format from the file *content* alone.

    Args:
        file_path: Path to the subtitle file.
        sample_chars: Number of characters to inspect from the start.

    Returns:
        :data:`FORMAT_VTT`, :data:`FORMAT_SRT`, or nothing (None)
        when the sample is inconclusive.
    """
    sample = read_text_sample(file_path, sample_chars)
    if not sample.strip():
        return None

    # 1. WebVTT signature — the definitive marker.
    head_lines = [
        line.lstrip("\ufeff").strip()
        for line in sample.splitlines()[:5]
    ]
    if any(_WEBVTT_HEADER_RE.match(line) for line in head_lines):
        return FORMAT_VTT

    # 2. SRT grammar: comma-separated milliseconds.
    if _SRT_TIMING_RE.search(sample):
        return FORMAT_SRT

    # 3. Period-separated timings without a WEBVTT header: WebVTT-style,
    #    unless the block is preceded by an SRT sequence number.
    timing_match = _ANY_TIMING_RE.search(sample)
    if timing_match is not None:
        preceding = sample[: timing_match.start()].splitlines()
        if preceding and _INDEX_LINE_RE.match(preceding[-1]):
            return FORMAT_SRT
        return FORMAT_VTT

    return None


def detect_format(file_path: str | Path) -> FormatDetection:
    """Detect the subtitle format of *file_path*.

    Content sniffing takes precedence over the file extension; a mismatch is
    logged as a warning.

    Args:
        file_path: Path to the subtitle file.

    Returns:
        A :class:`FormatDetection` describing the decision.

    Raises:
        UnsupportedSubtitleFormat: If neither the content nor the extension
            identifies a supported format.
    """
    path = Path(file_path)
    extension_format = EXTENSION_FORMATS.get(path.suffix.lower())
    content_format = sniff_format(path)

    if content_format is not None:
        detection = FormatDetection(
            format=content_format,
            source="content",
            extension_format=extension_format,
        )
        if detection.mismatched_extension:
            log.warning(
                "Format mismatch for %s: extension implies %s but content "
                "looks like %s — trusting the content",
                path.name,
                extension_format.upper() if extension_format else "?",
                content_format.upper(),
            )
        return detection

    if extension_format is not None:
        return FormatDetection(
            format=extension_format,
            source="extension",
            extension_format=extension_format,
        )

    raise UnsupportedSubtitleFormat(
        f"Cannot determine subtitle format of {path.name!r}: "
        f"expected one of {', '.join(sorted(EXTENSION_FORMATS))} "
        f"or readable SRT/WebVTT content"
    )


# ---------------------------------------------------------------------------
# Parser dispatch
# ---------------------------------------------------------------------------


def parser_for_format(fmt: str) -> SubtitleParserBase:
    """Return a fresh parser instance for *fmt*.

    Args:
        fmt: :data:`FORMAT_SRT` or :data:`FORMAT_VTT`.

    Returns:
        The matching parser.

    Raises:
        UnsupportedSubtitleFormat: If *fmt* is not a supported format.
    """
    if fmt == FORMAT_SRT:
        return SrtParser()
    if fmt == FORMAT_VTT:
        return VttParser()
    raise UnsupportedSubtitleFormat(f"Unsupported subtitle format: {fmt!r}")


def get_parser(file_path: str | Path) -> SubtitleParserBase:
    """Detect the format of *file_path* and return the parser for it."""
    return parser_for_format(detect_format(file_path).format)


# ---------------------------------------------------------------------------
# Parsed result
# ---------------------------------------------------------------------------


@dataclass
class ParsedSubtitle:
    """A subtitle file that has been detected and parsed.

    Attributes:
        entries: Parsed subtitle entries.
        text: Extracted text, ready to be sent to the AI (timestamp markers
            included when *with_timestamps* was requested).
        format: Detected format (:data:`FORMAT_SRT` / :data:`FORMAT_VTT`).
        detection: Full :class:`FormatDetection` result, including how the
            format was determined.
        parser: Parser instance that produced *entries*.
        source_path: Absolute path of the source file.
    """

    entries: list[SubtitleEntry]
    text: str
    format: str
    detection: FormatDetection
    parser: SubtitleParserBase
    source_path: Path

    @property
    def entry_count(self) -> int:
        """Number of parsed subtitle entries."""
        return len(self.entries)

    @property
    def text_length(self) -> int:
        """Length (in characters) of the extracted text."""
        return len(self.text)

    @property
    def format_label(self) -> str:
        """Upper-case format label for logs/metadata (``SRT`` / ``VTT``)."""
        return self.format.upper()


def parse_subtitle(
    file_path: str | Path,
    with_timestamps: bool = True,
) -> ParsedSubtitle:
    """Detect the format of *file_path*, parse it, and extract its text.

    This is the single entry point used by the watcher and the CLI: it hides
    the SRT/WebVTT distinction from the rest of the pipeline.

    Args:
        file_path: Path to a ``.srt`` or ``.vtt`` file.
        with_timestamps: Include ``[HH:MM:SS]`` markers in the extracted
            text (useful for time-point navigation in the generated notes).

    Returns:
        A :class:`ParsedSubtitle` with entries, text and detection info.

    Raises:
        FileNotFoundError: If *file_path* does not exist.
        UnsupportedSubtitleFormat: If the format cannot be determined.
    """
    path = Path(file_path)
    detection = detect_format(path)
    parser = parser_for_format(detection.format)

    entries = parser.parse(path)
    text = parser.extract_text(entries, with_timestamps=with_timestamps)

    log.info(
        "Detected %s format for %s (via %s): %d entries, %d chars",
        detection.format.upper(),
        path.name,
        detection.source,
        len(entries),
        len(text),
    )

    return ParsedSubtitle(
        entries=entries,
        text=text,
        format=detection.format,
        detection=detection,
        parser=parser,
        source_path=path.resolve(),
    )


# ===========================================================================
# Self-test
# ===========================================================================

if __name__ == "__main__":
    """Run a quick self-test of detection and dispatch.

    Usage::

        python -m src.srt_daemon.subtitle_parser
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

    srt_body = (
        "1\n"
        "00:00:01,000 --> 00:00:03,500\n"
        "Hello from an SRT file.\n"
    )
    vtt_body = (
        "WEBVTT\n\n"
        "00:00:01.000 --> 00:00:03.500\n"
        "Hello from a WebVTT file.\n"
    )

    tmp_dir = Path(tempfile.mkdtemp(prefix="subtitle_format_test_"))
    cases = [
        ("plain.srt", srt_body, FORMAT_SRT),
        ("plain.vtt", vtt_body, FORMAT_VTT),
        ("mislabelled.vtt", srt_body, FORMAT_SRT),   # content wins
        ("mislabelled.srt", vtt_body, FORMAT_VTT),   # content wins
        ("noextension", vtt_body, FORMAT_VTT),       # content only
    ]

    print("=" * 60)
    print("SUBTITLE FORMAT DETECTION — SELF TEST")
    print("=" * 60)

    try:
        for name, body, expected in cases:
            case_path = tmp_dir / name
            case_path.write_text(body, encoding="utf-8")
            detection = detect_format(case_path)
            parsed = parse_subtitle(case_path)
            print(
                f"  {name:20s} -> {detection.format:3s} "
                f"(via {detection.source}, {parsed.entry_count} entry/entries)"
            )
            assert detection.format == expected, (
                f"{name}: expected {expected}, got {detection.format}"
            )
            assert parsed.entry_count == 1

        print("\n[Unsupported format]")
        unknown = tmp_dir / "notes.txt"
        unknown.write_text("just some prose, no timings at all", encoding="utf-8")
        try:
            detect_format(unknown)
            raise AssertionError("Should have raised UnsupportedSubtitleFormat")
        except UnsupportedSubtitleFormat:
            print("  Correctly rejected non-subtitle content")
            print("  PASSED")

        print("\n" + "=" * 60)
        print("ALL DETECTION TESTS PASSED")
        print("=" * 60)
    finally:
        for child in tmp_dir.iterdir():
            child.unlink(missing_ok=True)
        tmp_dir.rmdir()
