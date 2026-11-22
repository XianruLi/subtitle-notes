"""Speaker-label analysis for parsed subtitle entries.

Meeting and interview notes are only as good as their speaker attribution, so
before the AI runs we determine which of two situations applies:

* **labeled** — the transcript itself says who spoke.  Teams/Zoom exports use
  ``Alice Chen: ...`` lines, WebVTT voice spans are normalised to the same
  form by the VTT parser, and SRT dialogue often uses ``- Speaker A: ...``.
* **inferred** — nothing marks the speaker (YouTube auto-captions and most
  course subtitles).  The AI may only cluster turns from conversational
  evidence and must say so.

Detection is deliberately conservative: a label counts as a speaker only when
it repeats at least twice and covers a meaningful share of the transcript, so
a stray ``注意:`` line in a course subtitle never becomes a "speaker".

Usage::

    from src.srt_daemon.speakers import analyse_speakers

    info = analyse_speakers(parsed.entries)
    if info.labeled:
        print("speakers:", info.names)
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Optional, Sequence

from src.srt_daemon.subtitle_base import SubtitleEntry

# A leading label, optionally behind a dialogue dash:
#   "Alice Chen: let us start"   /   "- Sprecher A: hallo"
_LABEL_RE = re.compile(r"^\s*-?\s*(?P<name>[^:：]{1,24})[:：]\s*(?P<body>\S.*)$")

# Characters that disqualify a candidate from being a speaker name.
_INVALID_NAME_CHARS = "。！？，、；：,.!?;\"'（）()【】《》"

# A candidate must appear in at least this many entries to count as a speaker.
_MIN_NAME_OCCURRENCES = 2

# ...and labeled entries must cover at least this share of the transcript.
_LABELED_ENTRY_RATIO = 0.25

# Short transcripts (a two-line interview clip) may never repeat a label, so a
# second, denser rule applies: nearly every entry is labeled and there are only
# a handful of distinct labels.
_DENSE_LABEL_RATIO = 0.6
_MAX_DENSE_SPEAKERS = 6

# Upper bound on reported speakers (guards against noisy transcripts).
_MAX_SPEAKERS = 12

# Longest plausible speaker name.
_NAME_MAX_LENGTH = 24


@dataclass(frozen=True)
class SpeakerInfo:
    """Speaker situation of one transcript.

    Attributes:
        labeled: True when the transcript carries usable speaker labels.
        names: Speaker names in descending order of speaking turns (empty when
            *labeled* is False).
        labeled_entries: Number of entries that carried a speaker label.
        total_entries: Total number of subtitle entries analysed.
    """

    labeled: bool
    names: tuple[str, ...]
    labeled_entries: int
    total_entries: int

    @property
    def mode(self) -> str:
        """``labeled`` or ``inferred`` — the value recorded in the front matter."""
        return "labeled" if self.labeled else "inferred"

    @property
    def speaker_count(self) -> int:
        """Number of distinct named speakers (0 when inferred)."""
        return len(self.names)

    def describe(self) -> str:
        """One-line summary for logs and the Markdown front matter."""
        if self.labeled:
            return ", ".join(self.names) if self.names else "labeled"
        return "inferred" if self.total_entries else "none"


#: Returned for empty transcripts.
EMPTY_SPEAKER_INFO = SpeakerInfo(False, (), 0, 0)


def _is_plausible_name(name: str) -> bool:
    """Heuristic filter that keeps prose and punctuation out of the name set."""
    if not name or len(name) > _NAME_MAX_LENGTH:
        return False
    if name.isdigit():
        return False
    if any(ch in _INVALID_NAME_CHARS for ch in name):
        return False
    # "this is a long sentence: ..." is prose, not a speaker name.
    if len(name.split()) > 4:
        return False
    return True


def _label_of(text: str) -> Optional[str]:
    """Return the speaker label at the start of *text*, if any."""
    stripped = text.strip()
    if not stripped:
        return None
    first_line = stripped.splitlines()[0]
    match = _LABEL_RE.match(first_line)
    if match is None:
        return None
    name = match.group("name").strip()
    return name if _is_plausible_name(name) else None


def analyse_speakers(entries: Sequence[SubtitleEntry]) -> SpeakerInfo:
    """Analyse *entries* and report how speakers are represented.

    Args:
        entries: Parsed subtitle entries (SRT blocks or WebVTT cues).

    Returns:
        A :class:`SpeakerInfo` describing the speaker situation.
    """
    total = len(entries)
    if total == 0:
        return EMPTY_SPEAKER_INFO

    counts: Counter[str] = Counter()
    for entry in entries:
        name = _label_of(entry.text)
        if name is not None:
            counts[name] += 1

    # Every plausible label, most frequent first.
    distinct = sorted(counts, key=lambda name: (-counts[name], name))
    labeled_entries = sum(counts[name] for name in distinct)
    ratio = labeled_entries / total

    repeated = [name for name in distinct if counts[name] >= _MIN_NAME_OCCURRENCES]
    labeled = bool(repeated) and ratio >= _LABELED_ENTRY_RATIO

    if not labeled:
        # Dense labeling: nearly every line carries a label and there are only
        # a few of them.  Catches short clips where nothing repeats, while a
        # single stray "注意: ..." in a long course still stays "inferred".
        labeled = (
            ratio >= _DENSE_LABEL_RATIO
            and len(distinct) <= _MAX_DENSE_SPEAKERS
        )

    if not labeled:
        return SpeakerInfo(False, (), 0, total)

    # The transcript is labeled, so a participant who spoke only once still
    # exists and must not be silently re-attributed to somebody else.  Every
    # label is kept (the instruction tells the AI to attribute statements to
    # the label that actually precedes them).
    names = tuple(distinct[:_MAX_SPEAKERS])

    return SpeakerInfo(
        labeled=True,
        names=names,
        labeled_entries=sum(counts[name] for name in names),
        total_entries=total,
    )


def build_speaker_instruction(info: SpeakerInfo, profile_id: str = "") -> str:
    """Build the speaker-handling instruction injected into the AI request.

    Labeled transcripts get a "use these exact names" instruction; unlabeled
    meeting/interview transcripts get an explicit "inference only, mark it as
    uncertain, never invent names" instruction.  Course transcripts without
    labels need no instruction at all (an empty string is returned).

    Args:
        info: Speaker analysis of the current transcript.
        profile_id: Id of the resolved content-type profile.

    Returns:
        Instruction text, or an empty string when none is needed.
    """
    if info.labeled:
        names = ", ".join(info.names) if info.names else "(unnamed)"
        return (
            "SPEAKER DATA: this transcript is speaker-labeled. "
            f"Labels found in the text: {names}. "
            "Attribute every statement to the label that actually precedes it, "
            "keep one spelling per person throughout the document, and never "
            "invent, translate or rename a speaker."
        )

    if profile_id in ("meeting", "interview"):
        return (
            "SPEAKER DATA: this transcript has NO speaker labels. "
            "Only separate speakers when the text itself gives evidence "
            "(direct address, question/answer turn-taking, role references). "
            "Use neutral placeholders (Speaker A, Speaker B, ...) and state "
            "explicitly in the document that the speaker split is inferred and "
            "may be inaccurate. Never invent real names."
        )

    return ""
