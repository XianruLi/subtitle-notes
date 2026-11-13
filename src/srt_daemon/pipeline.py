"""Shared "subtitle file -> Markdown note" pipeline.

The notes CLI (--once), the online-video fetcher and the desktop GUI all
end with the same work: parse the subtitle, measure speakers, resolve the
content-type profile, call the AI, write the Markdown file.  Keeping that in
one function stops the three entry points from drifting apart.

Usage::

    from src.srt_daemon.pipeline import generate_notes

    result = generate_notes(config, "lecture.vtt", forced_profile="course")
    print(result.output_path)
"""

from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from src.srt_daemon.config import Config
from src.srt_daemon.logger import get_logger
from src.srt_daemon.profiles import (
    ProfileResolution,
    ProfileRegistry,
    resolve_profile,
)
from src.srt_daemon.speakers import SpeakerInfo, analyse_speakers
from src.srt_daemon.subtitle_parser import ParsedSubtitle, parse_subtitle

log = get_logger(__name__)


#: Sidecar written next to a transcribed subtitle: which video it came from.
SIDECAR_SUFFIX = ".source.json"

#: Containers that actually carry a picture (screenshots need the video).
VIDEO_SUFFIXES = frozenset(
    {".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi", ".m4v", ".ts", ".wmv", ".mpg", ".mpeg"}
)


def write_source_sidecar(
    subtitle_path: str | Path, video_path: str | Path, duration: float = 0.0
) -> Path:
    """Record the source video next to a transcribed subtitle.

    Local videos have no URL, so without this the notes pipeline cannot find
    the picture and [[screenshot:...]] markers would be dropped.
    """
    subtitle = Path(subtitle_path)
    sidecar = subtitle.with_name(subtitle.stem + SIDECAR_SUFFIX)
    payload = {
        "video": str(Path(video_path).resolve()),
        "duration": float(duration or 0.0),
        "recorded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        sidecar.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:  # noqa: BLE001 - sidecar is best effort
        log.warning("Cannot write the source sidecar for %s: %s", subtitle.name, exc)
    return sidecar


def find_source_video(subtitle_path: str | Path) -> Optional[Path]:
    """Return the video recorded beside *subtitle_path*, when it still exists."""
    subtitle = Path(subtitle_path)
    sidecar = subtitle.with_name(subtitle.stem + SIDECAR_SUFFIX)
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    candidate = Path(str(payload.get("video") or ""))
    if not candidate.is_file():
        return None
    return candidate


def stems_match(subtitle_stem: str, video_stem: str) -> bool:
    """True when a subtitle stem belongs to a video stem.

    "lecture.vtt" matches "lecture.mp4", and so does "lecture.en.vtt" or
    "lecture.zh-Hans.vtt" — that is how browser CC-export extensions name the
    file next to the downloaded video.
    """
    if subtitle_stem == video_stem:
        return True
    return subtitle_stem.startswith(video_stem + ".") or video_stem.startswith(
        subtitle_stem + "."
    )


def find_sibling_video(subtitle_path: str | Path) -> Optional[Path]:
    """Return a video sitting next to *subtitle_path* for the same recording.

    Downloading a video and its subtitle (e.g. a YouTube CC export) into one
    folder is a common workflow; such a subtitle needs no transcription, but the
    picture is still useful for screenshots.  An exact stem wins, then a
    language-suffixed one ("lecture.en.vtt" next to "lecture.mp4").
    """
    subtitle = Path(subtitle_path)
    exact: list[Path] = []
    loose: list[Path] = []
    try:
        entries = list(subtitle.parent.iterdir())
    except OSError:
        return None
    for candidate in entries:
        if not candidate.is_file() or candidate == subtitle:
            continue
        if candidate.suffix.lower() not in VIDEO_SUFFIXES:
            continue
        if candidate.stem == subtitle.stem:
            exact.append(candidate)
        elif stems_match(subtitle.stem, candidate.stem):
            loose.append(candidate)
    if exact:
        return sorted(exact)[0]
    if loose:
        return sorted(loose)[0]
    return None


def write_note(
    config: Config,
    markdown: str,
    subtitle_path: Path,
    metadata: dict[str, Any],
    *,
    video_path: Optional[str | Path] = None,
    screenshots: Optional[bool] = None,
) -> tuple[Path, int]:
    """Embed screenshots (optional) and write the Markdown file.

    Shared by :func:generate_notes and the folder watcher so both paths
    behave identically.

    Returns:
        (note path, number of embedded images)
    """
    from src.srt_daemon.writer import MarkdownWriter

    want_screenshots = config.screenshots_enabled if screenshots is None else screenshots
    images: list[Path] = []
    staging: Optional[Path] = None

    if want_screenshots:
        from src.srt_daemon.screenshots import process_markdown

        staging = Path(tempfile.mkdtemp(prefix="shots_"))
        processed = process_markdown(
            markdown,
            Path(video_path) if video_path else None,
            staging,
            config=config,
        )
        markdown = processed.markdown
        images = processed.images
        if not video_path and processed.found:
            log.info(
                "Dropped %d screenshot marker(s): no video available for this source",
                processed.found,
            )

    metadata = dict(metadata)
    metadata["images"] = len(images)

    writer = MarkdownWriter(config)
    output_path = writer.write(
        content=markdown,
        source_filename=subtitle_path.name,
        metadata=metadata,
    )

    if images:
        target_dir = output_path.parent / "images"
        target_dir.mkdir(parents=True, exist_ok=True)
        for image in images:
            shutil.move(str(image), str(target_dir / image.name))
        # Re-generating a note can pick different timestamps; any shot left over
        # from the previous run would be an orphan nobody references.
        keep = {image.name for image in images}
        for stale in target_dir.glob("shot-*.jpg"):
            if stale.name not in keep:
                try:
                    stale.unlink()
                    log.info("Removed stale screenshot %s", stale.name)
                except OSError:
                    pass
        log.info("Embedded %d screenshot(s) into %s", len(images), output_path.parent.name)

    if staging is not None:
        shutil.rmtree(staging, ignore_errors=True)

    return output_path, len(images)


class NotesPipelineError(RuntimeError):
    """Raised when a subtitle cannot be turned into notes."""


@dataclass
class NotesResult:
    """Everything an entry point needs after a successful run."""

    output_path: Path
    parsed: ParsedSubtitle
    profile: ProfileResolution
    speaker: SpeakerInfo
    model: str

    @property
    def entry_count(self) -> int:
        """Number of parsed subtitle entries."""
        return len(self.parsed.entries)

    @property
    def text_length(self) -> int:
        """Length of the text handed to the AI."""
        return len(self.parsed.text)


def make_classifier(config: Config) -> Callable[[str, Sequence[Any]], Optional[str]]:
    """Return a lazy content-type classifier for :func:resolve_profile.

    The AI client is only constructed when the classifier is actually called,
    so files with a file-name hint never pay for it.
    """
    from src.srt_daemon.ai_client import AiClient

    def classify(sample: str, candidates: Sequence[Any]) -> Optional[str]:
        return AiClient(config).classify_profile(candidates, sample)

    return classify


def generate_notes(
    config: Config,
    subtitle_path: str | Path,
    *,
    forced_profile: Optional[str] = None,
    extra_metadata: Optional[dict[str, Any]] = None,
    with_timestamps: bool = True,
    classifier: Optional[Callable[[str, Sequence[Any]], Optional[str]]] = None,
    video_path: Optional[str | Path] = None,
    screenshots: Optional[bool] = None,
) -> NotesResult:
    """Turn one subtitle file into a Markdown note.

    Args:
        config: Resolved daemon configuration.
        subtitle_path: .srt or .vtt file to process.
        forced_profile: Profile id that overrides auto-detection (CLI flag).
        extra_metadata: Additional front-matter fields (for example
            source_url and platform when the subtitle came from a URL).
        with_timestamps: Keep [HH:MM:SS] markers in the AI input.
        classifier: Optional classifier override (tests pass a stub).
        video_path: Source video for [[screenshot:...]] markers.  When absent the
            markers are stripped and no images are written.
        screenshots: Override screenshots.enabled for this call.

    Returns:
        A :class:NotesResult describing what was produced.

    Raises:
        NotesPipelineError: When the subtitle has no usable entries.
        FileNotFoundError: When *subtitle_path* does not exist.
        UnsupportedSubtitleFormat: When the format cannot be detected.
    """
    from src.srt_daemon.ai_client import AiClient
    from src.srt_daemon.writer import MarkdownWriter

    path = Path(subtitle_path)
    parsed = parse_subtitle(path, with_timestamps=with_timestamps)
    if not parsed.entries:
        raise NotesPipelineError(f"No subtitle entries found in {path.name}")

    registry = ProfileRegistry(config)
    speaker = analyse_speakers(parsed.entries)
    resolution = resolve_profile(
        config,
        path.name,
        parsed.text,
        registry=registry,
        forced=forced_profile,
        classifier=classifier or make_classifier(config),
    )
    profile = resolution.profile
    log.info(
        "Profile for %s: %s (via %s)  speakers=%s  entries=%d",
        path.name,
        profile.id,
        resolution.source,
        speaker.mode,
        len(parsed.entries),
    )

    # Picture for [[screenshot:...]] markers, in order of preference:
    #   1. an explicitly supplied video (URL fetch / GUI),
    #   2. the sidecar written when the subtitle was transcribed from a video,
    #   3. a video next to the subtitle with the same name.
    # Resolve this *before* the API call: telling the model that a video exists
    # and will really be rendered is what makes it emit markers at all.
    if video_path is None:
        video_path = find_source_video(path)
    if video_path is None:
        video_path = find_sibling_video(path)
        if video_path is not None:
            log.info("Using the sibling video for screenshots: %s", video_path.name)
    has_video = video_path is not None and config.screenshots_enabled
    log.info(
        "Screenshots: enabled=%s  video=%s  asking model for markers=%s",
        config.screenshots_enabled,
        video_path.name if video_path is not None else "(none)",
        has_video,
    )

    client = AiClient(config, profile=profile)
    markdown = client.process(
        parsed.text,
        path.name,
        profile=profile,
        speaker=speaker,
        source_format=parsed.format_label,
        has_video=has_video,
    )

    metadata: dict[str, Any] = {
        "format": parsed.format_label,
        "profile": profile.id,
        "speakers": speaker.describe(),
        "entries": len(parsed.entries),
        "text_length": len(parsed.text),
        "model": config.ai_model,
    }
    if extra_metadata:
        metadata.update({key: value for key, value in extra_metadata.items() if value})

    output_path, _images = write_note(
        config,
        markdown,
        path,
        metadata,
        video_path=video_path,
        screenshots=screenshots,
    )
    log.info("Note written: %s", output_path)

    return NotesResult(
        output_path=output_path,
        parsed=parsed,
        profile=resolution,
        speaker=speaker,
        model=config.ai_model,
    )
