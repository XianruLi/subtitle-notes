"""Shared foundations for the daemon's subtitle parsers (SRT + WebVTT).

The daemon accepts two subtitle container formats:

* **SRT** (SubRip) — blocks of ``index / HH:MM:SS,mmm --> HH:MM:SS,mmm / text``.
* **WebVTT** — a ``WEBVTT`` header followed by cues carrying
  ``HH:MM:SS.mmm --> HH:MM:SS.mmm`` timings and optional cue settings.

Both are parsed into the same :class:`SubtitleEntry` model, so every stage
downstream of parsing (text extraction, short-entry merging, AI processing,
Markdown writing) is format-agnostic.  This module holds what the two parsers
share:

* the :class:`SubtitleEntry` data model,
* encoding auto-detection (BOM / UTF-8 / GBK) and tolerant file reading,
* text extraction with optional ``[HH:MM:SS]`` time markers,
* short-entry merging with speaker-break protection,
* timestamp arithmetic (accepts both ``,`` and ``.`` fractional separators).

Usage::

    from src.srt_daemon.subtitle_base import SubtitleEntry, detect_encoding

    encoding = detect_encoding("lecture.vtt")
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.srt_daemon.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# BOM byte sequences for encoding detection
_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"

# Maximum character length considered a "short" entry for merging
_DEFAULT_SHORT_TEXT_LENGTH = 80

# Bytes sampled when sniffing the encoding of a file.
_ENCODING_SAMPLE_BYTES = 4096


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------


@dataclass
class SubtitleEntry:
    """A single subtitle entry (an SRT block or a WebVTT cue).

    Attributes:
        index: 1-based sequence number within the file.
        start_time: Start timestamp.  SRT entries use ``"HH:MM:SS,mmm"``,
            WebVTT entries use ``"HH:MM:SS.mmm"``.
        end_time: End timestamp, same convention as *start_time*.
        text: Subtitle text content (may contain embedded newlines for
            multi-line subtitles/cues).
    """

    index: int
    start_time: str
    end_time: str
    text: str


# ---------------------------------------------------------------------------
# Encoding detection & tolerant reading
# ---------------------------------------------------------------------------


def detect_encoding(file_path: str | Path) -> str:
    """Detect the character encoding of *file_path*.

    Detection priority:
    1. BOM inspection (UTF-8, UTF-16 LE, UTF-16 BE)
    2. UTF-8 decode attempt on the first 4 KB of the file
    3. GBK decode attempt (common for Chinese-language Windows)
    4. Fallback to UTF-8 with ``errors='replace'``

    Args:
        file_path: Path to the file to inspect.

    Returns:
        Encoding name string suitable for ``open(..., encoding=...)``.
    """
    path = Path(file_path)
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_ENCODING_SAMPLE_BYTES)
    except OSError as exc:
        log.error("Cannot read file for encoding detection: %s", exc)
        return "utf-8"

    if not raw:
        return "utf-8"

    # Step 1: BOM detection
    if raw.startswith(_BOM_UTF8):
        log.debug("Detected UTF-8 BOM -> utf-8-sig")
        return "utf-8-sig"
    if raw.startswith(_BOM_UTF16_LE):
        log.debug("Detected UTF-16 LE BOM -> utf-16-le")
        return "utf-16-le"
    if raw.startswith(_BOM_UTF16_BE):
        log.debug("Detected UTF-16 BE BOM -> utf-16-be")
        return "utf-16-be"

    # Step 2: Try UTF-8 — trim trailing bytes that may form an
    # incomplete multi-byte character (the 4 KB sample may have cut
    # mid-sequence).  If the data is not UTF-8 at all we fall through.
    try:
        clean_len = truncate_to_valid_utf8_boundary(raw)
        raw[:clean_len].decode("utf-8")
        log.debug("UTF-8 decode successful")
        return "utf-8"
    except UnicodeDecodeError:
        pass

    # Step 3: Try GBK (common for Chinese Windows locales)
    try:
        raw.decode("gbk")
        log.debug("GBK decode successful")
        return "gbk"
    except UnicodeDecodeError:
        pass

    # Step 4: Last resort — UTF-8 with replacement characters
    log.warning(
        "Encoding detection failed for %s, using utf-8 with errors='replace'",
        path.name,
    )
    return "utf-8"


def truncate_to_valid_utf8_boundary(data: bytes) -> int:
    """Return the length of *data* truncated to a valid UTF-8 boundary.

    If the last few bytes form an incomplete multi-byte UTF-8 sequence,
    they are excluded so that ``data[:result].decode('utf-8')`` succeeds
    (assuming the rest of the data is valid UTF-8).

    If *data* is not valid UTF-8 at all (e.g. it is GBK-encoded), the
    full length is returned — the caller's subsequent ``.decode()`` will
    then raise and be handled upstream.
    """
    length = len(data)
    # UTF-8 multi-byte sequences are at most 4 bytes; check last 3 bytes.
    for trim in range(4):
        if trim > length:
            break
        try:
            data[: length - trim].decode("utf-8")
            return length - trim
        except UnicodeDecodeError as exc:
            # "unexpected end of data" means we cut mid-sequence — keep
            # trimming.  Any other decode error means the data is not
            # UTF-8 at all; return full length and let the caller fail.
            if "unexpected end" not in str(exc).lower():
                return length
    return length


def read_text_file(file_path: str | Path, encoding: str) -> str:
    """Read *file_path* with the given *encoding*.

    Falls back to ``errors='replace'`` if the initial decode fails
    (handles files where the encoding detection was only partially
    correct, e.g. a mostly-UTF-8 file with a few invalid bytes).

    Returns an empty string when the file cannot be read at all.
    """
    path = Path(file_path)
    try:
        with open(path, "r", encoding=encoding) as fh:
            return fh.read()
    except UnicodeDecodeError:
        log.warning(
            "Decode failed with %s for %s, retrying with errors='replace'",
            encoding,
            path.name,
        )
        with open(path, "r", encoding=encoding, errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        log.error("Failed to read subtitle file %s: %s", path.name, exc)
        return ""


def read_text_sample(file_path: str | Path, max_chars: int = 8192) -> str:
    """Read at most *max_chars* characters of *file_path* for format sniffing.

    The encoding is auto-detected and decoding errors are replaced rather
    than raised, so a truncated multi-byte character at the sample boundary
    can never break detection.
    """
    path = Path(file_path)
    encoding = detect_encoding(path)
    try:
        with open(path, "r", encoding=encoding, errors="replace") as fh:
            return fh.read(max_chars)
    except OSError as exc:
        log.error("Failed to read %s for format detection: %s", path.name, exc)
        return ""


# ---------------------------------------------------------------------------
# Parser base class
# ---------------------------------------------------------------------------


class SubtitleParserBase:
    """Behaviour shared by the SRT and WebVTT parsers.

    Subclasses declare :attr:`format_name` and implement
    :meth:`_parse_content` (the block grammar of their format).  Everything
    else — reading the file with the right encoding, extracting text with
    time markers, merging short entries, timestamp arithmetic — is provided
    here so that both formats behave identically downstream.
    """

    #: Short format identifier ("srt" / "vtt"), set by subclasses.
    format_name: str = "subtitle"

    # --- Public API -----------------------------------------------------------

    def parse(self, file_path: str | Path) -> list[SubtitleEntry]:
        """Parse a subtitle file and return a list of :class:`SubtitleEntry`.

        Encoding is automatically detected via BOM inspection and fallback
        decoding attempts (UTF-8 -> GBK -> UTF-8 with replacement chars).

        Args:
            file_path: Path to the subtitle file (absolute or relative).

        Returns:
            Parsed subtitle entries in file order.  A malformed file that
            cannot be read at all returns an empty list rather than raising.

        Raises:
            FileNotFoundError: If *file_path* does not exist.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(
                f"{self.format_name.upper()} file not found: {path}"
            )

        encoding = self._detect_encoding(path)
        log.info(
            "Parsing %s file: %s (encoding=%s)",
            self.format_name.upper(),
            path.name,
            encoding,
        )

        content = self._read_file(path, encoding)
        if not content:
            log.warning(
                "%s file is empty or unreadable: %s",
                self.format_name.upper(),
                path.name,
            )
            return []

        entries = self._parse_content(content, path.name)
        log.info("Parsed %d subtitle entries from %s", len(entries), path.name)
        return entries

    def extract_text(
        self,
        entries: list[SubtitleEntry],
        with_timestamps: bool = False,
    ) -> str:
        """Extract plain text from parsed subtitle entries.

        Args:
            entries: List of :class:`SubtitleEntry` as returned by :meth:`parse`.
            with_timestamps: If True, prepend each entry's start time as a
                ``[HH:MM:SS]`` marker (fractional seconds stripped).  Useful
                for the AI to build a time-point navigation index.

        Returns:
            Concatenated subtitle text, entries separated by a blank line.
        """
        lines: list[str] = []
        for entry in entries:
            text = entry.text.strip()
            if not text:
                continue
            if with_timestamps:
                time_marker = self._time_marker(entry.start_time)
                lines.append(f"[{time_marker}] {text}")
            else:
                lines.append(text)
        return "\n\n".join(lines)

    def merge_short_entries(
        self,
        entries: list[SubtitleEntry],
        max_gap_seconds: float = 0.5,
        max_text_length: int = _DEFAULT_SHORT_TEXT_LENGTH,
    ) -> list[SubtitleEntry]:
        """Merge consecutive short subtitle entries into larger paragraphs.

        Two consecutive entries are candidates for merging when **all** of
        the following conditions are met:

        1. The time gap between *entry_n.end_time* and
           *entry_{n+1}.start_time* is <= *max_gap_seconds*.
        2. At least one of the two entries has text shorter than
           *max_text_length* characters.
        3. The entries are not detected as belonging to different speakers
           (see :meth:`_is_speaker_break`).

        Merging is iterative: once two entries are merged, the combined
        entry may be merged with the next entry, and so on.

        Args:
            entries: List of :class:`SubtitleEntry` as returned by :meth:`parse`.
            max_gap_seconds: Maximum allowed time gap (in seconds) between
                consecutive entries for them to be considered mergeable.
                Default 0.5 seconds.
            max_text_length: Entries with text shorter than this (in
                characters) are candidates for merging.  Default 80.

        Returns:
            A new list of :class:`SubtitleEntry` with short consecutive
            entries merged.  The merged entry inherits the first entry's
            index and start time, and the last entry's end time.
        """
        if not entries:
            return []

        merged: list[SubtitleEntry] = []
        current = entries[0]

        for next_entry in entries[1:]:
            gap = self._time_gap_seconds(current.end_time, next_entry.start_time)
            current_short = len(current.text) < max_text_length
            next_short = len(next_entry.text) < max_text_length

            can_merge = (
                gap <= max_gap_seconds
                and (current_short or next_short)
                and not self._is_speaker_break(current, next_entry)
            )

            if can_merge:
                merged_text = f"{current.text.strip()} {next_entry.text.strip()}"
                current = SubtitleEntry(
                    index=current.index,
                    start_time=current.start_time,
                    end_time=next_entry.end_time,
                    text=merged_text,
                )
            else:
                merged.append(current)
                current = next_entry

        merged.append(current)

        log.debug(
            "Merged %d entries -> %d (gap<=%.2fs, len<%d)",
            len(entries),
            len(merged),
            max_gap_seconds,
            max_text_length,
        )
        return merged

    # --- Subclass hook --------------------------------------------------------

    def _parse_content(self, content: str, filename: str) -> list[SubtitleEntry]:
        """Parse raw file *content* into entries (format-specific).

        Args:
            content: Full decoded text of the subtitle file.
            filename: Source filename, for log context only.

        Returns:
            Parsed entries; an empty list when no valid block was found.
        """
        raise NotImplementedError(
            f"{type(self).__name__} must implement _parse_content()"
        )

    # --- Internal helpers -----------------------------------------------------

    def _detect_encoding(self, file_path: Path) -> str:
        """Detect the encoding of *file_path* (see :func:`detect_encoding`)."""
        return detect_encoding(file_path)

    @staticmethod
    def _truncate_to_valid_utf8_boundary(data: bytes) -> int:
        """Trim *data* to a valid UTF-8 boundary (see module function)."""
        return truncate_to_valid_utf8_boundary(data)

    def _read_file(self, file_path: Path, encoding: str) -> str:
        """Read *file_path* with *encoding* (see :func:`read_text_file`)."""
        return read_text_file(file_path, encoding)

    # --- Time utilities ------------------------------------------------------

    @staticmethod
    def _time_marker(ts: str) -> str:
        """Return the ``HH:MM:SS`` part of an SRT or WebVTT timestamp.

        Both the SRT comma (``00:00:01,000``) and the WebVTT period
        (``00:00:01.000``) fractional separators are handled.
        """
        for separator in (",", "."):
            if separator in ts:
                return ts.split(separator)[0]
        return ts

    @staticmethod
    def _time_to_seconds(ts: str) -> float:
        """Convert ``"HH:MM:SS,mmm"`` / ``"HH:MM:SS.mmm"`` to seconds.

        Args:
            ts: Timestamp string (comma or period as millisecond separator).

        Returns:
            Total seconds as a float, e.g. ``"00:01:30,500"`` -> 90.5.
        """
        ts = ts.replace(",", ".")
        h_m_s, frac = ts.split(".")
        h, m, s = h_m_s.split(":")
        return int(h) * 3600 + int(m) * 60 + int(s) + int(frac) / 1000.0

    @staticmethod
    def _time_gap_seconds(end_ts: str, start_ts: str) -> float:
        """Calculate the gap in seconds between two timestamps.

        Args:
            end_ts: End timestamp of the earlier entry.
            start_ts: Start timestamp of the later entry.

        Returns:
            Gap in seconds.  Negative if the entries overlap in time.
        """
        return (
            SubtitleParserBase._time_to_seconds(start_ts)
            - SubtitleParserBase._time_to_seconds(end_ts)
        )

    @staticmethod
    def _is_speaker_break(
        current: SubtitleEntry, next_entry: SubtitleEntry
    ) -> bool:
        """Heuristic to detect if two consecutive entries belong to
        different speakers.

        Detects the common pattern where dialogue lines are prefixed with
        ``- SpeakerName:`` and two consecutive entries have different
        speaker names.  This prevents merging lines spoken by different
        people.

        Only triggers when **both** entries start with a dash ``-``
        followed by different first-word tokens.

        Args:
            current: Current subtitle entry.
            next_entry: Next subtitle entry.

        Returns:
            True if the entries are likely from different speakers and
            should **not** be merged.
        """
        cur_text = current.text.strip()
        nxt_text = next_entry.text.strip()

        # Both must start with a dash
        if not (cur_text.startswith("-") and nxt_text.startswith("-")):
            return False

        # Extract the first word after the dash
        cur_first = cur_text[1:].strip().split()[0] if len(cur_text) > 1 else ""
        nxt_first = nxt_text[1:].strip().split()[0] if len(nxt_text) > 1 else ""

        return bool(cur_first and nxt_first and cur_first != nxt_first)
