"""Embed video frames into notes.

The AI is asked to emit a marker on its own line when a visual moment matters::

    [[screenshot:00:03:21]]

This module finds those markers, pulls the matching frame out of the video with
ffmpeg, and rewrites the marker into a Markdown image reference::

    ![](images/shot-000321.jpg)

Nothing here needs a GPU or an extra dependency: ffmpeg is already required by
the transcription pipeline.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from src.srt_daemon.config import Config
from src.srt_daemon.logger import get_logger

log = get_logger(__name__)

#: Marker emitted by the AI: [[screenshot:HH:MM:SS]].
SCREENSHOT_MARKER = re.compile(
    r"\[\[screenshot:(\d{1,2}):(\d{2}):(\d{2})\]\]",
    re.IGNORECASE,
)

#: Sub-directory inside the note folder that receives the images.
IMAGE_DIR_NAME = "images"


class ScreenshotError(RuntimeError):
    """Raised when frames cannot be extracted at all."""


@dataclass(frozen=True)
class Marker:
    """One [[screenshot:...]] occurrence."""

    raw: str
    seconds: int

    @property
    def filename(self) -> str:
        """Image file name for this timestamp (shot-HHMMSS.jpg)."""
        hours, rest = divmod(self.seconds, 3600)
        minutes, secs = divmod(rest, 60)
        return f"shot-{hours:02d}{minutes:02d}{secs:02d}.jpg"


@dataclass
class ScreenshotResult:
    """Outcome of processing one note."""

    markdown: str
    images: list[Path] = field(default_factory=list)
    found: int = 0
    skipped: int = 0


def parse_markers(markdown: str) -> list[Marker]:
    """Return every screenshot marker in *markdown*, in document order."""
    markers: list[Marker] = []
    for match in SCREENSHOT_MARKER.finditer(markdown or ""):
        hours, minutes, seconds = (int(part) for part in match.groups())
        markers.append(
            Marker(
                raw=match.group(0),
                seconds=hours * 3600 + minutes * 60 + seconds,
            )
        )
    return markers


def find_ffmpeg() -> Optional[str]:
    """Locate the ffmpeg binary (same requirement as transcription)."""
    return shutil.which("ffmpeg")


def extract_frame(
    ffmpeg: str,
    video: Path,
    seconds: int,
    target: Path,
    *,
    width: int = 1280,
    quality: int = 3,
) -> bool:
    """Write one frame of *video* at *seconds* to *target* (JPEG).

    Args:
        ffmpeg: Path to the ffmpeg executable.
        video: Video file to read.
        seconds: Timestamp of the frame.
        target: Destination image path.
        width: Output width in pixels (height keeps the aspect ratio).
        quality: ffmpeg -q:v value (2 = best, 31 = worst).

    Returns:
        True on success, False when ffmpeg could not produce the frame.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-v",
        "error",
        "-ss",
        str(seconds),
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:-2",
        "-q:v",
        str(quality),
        str(target),
    ]
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover - env
        log.warning("ffmpeg could not extract a frame at %ss: %s", seconds, exc)
        return False
    if result.returncode != 0 or not target.exists():
        log.warning(
            "ffmpeg failed at %ss (%s)", seconds, (result.stderr or "").strip()[:200]
        )
        return False
    return True


def _image_reference(marker: Marker, prefix: str = IMAGE_DIR_NAME) -> str:
    return f"![]({prefix}/{marker.filename})"


def process_markdown(
    markdown: str,
    video: Optional[Path],
    image_dir: Path,
    *,
    config: Config,
    ffmpeg: Optional[str] = None,
) -> ScreenshotResult:
    """Replace screenshot markers with real images.

    Markers beyond screenshots.max_count, duplicated timestamps and frames
    that cannot be extracted are dropped so the note never contains a dangling
    placeholder.

    Args:
        markdown: AI output containing zero or more markers.
        video: Source video (None disables extraction — markers are removed).
        image_dir: Directory that receives the JPEG files.
        config: Resolved configuration (max count, width, quality).
        ffmpeg: Optional explicit ffmpeg path.

    Returns:
        A :class:ScreenshotResult with the rewritten Markdown.
    """
    markers = parse_markers(markdown)
    if not markers:
        return ScreenshotResult(markdown=markdown, found=0)

    max_count = max(0, config.screenshots_max_count)
    ffmpeg_path = ffmpeg or find_ffmpeg()

    unique: list[Marker] = []
    seen: set[int] = set()
    for marker in markers:
        if marker.seconds in seen:
            continue
        if len(unique) >= max_count:
            break
        seen.add(marker.seconds)
        unique.append(marker)

    extracted: dict[str, Marker] = {}
    if video is not None and video.exists() and ffmpeg_path:
        for marker in unique:
            target = image_dir / marker.filename
            if extract_frame(
                ffmpeg_path,
                video,
                marker.seconds,
                target,
                width=config.screenshots_width,
                quality=config.screenshots_quality,
            ):
                extracted[marker.raw] = marker

    text = markdown
    for marker in markers:
        if marker.raw in extracted:
            text = text.replace(marker.raw, _image_reference(extracted[marker.raw]))
        else:
            text = text.replace(marker.raw, "")

    # Drop lines that became empty placeholders.
    cleaned = [line for line in text.splitlines() if line.strip() or line == ""]
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()

    images = [image_dir / marker.filename for marker in extracted.values()]
    result = ScreenshotResult(
        markdown="\n".join(cleaned),
        images=images,
        found=len(markers),
        skipped=len(markers) - len(images),
    )
    log.info(
        "Screenshots: %d marker(s), %d image(s), %d skipped",
        result.found,
        len(images),
        result.skipped,
    )
    return result
