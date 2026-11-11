"""Audio/video → speaker-labelled subtitle front-end.

Turns a recording into a subtitle file that the notes daemon treats exactly
like a Panopto or Teams export:

    decode (ffmpeg)  →  ASR (faster-whisper)  →  diarization  →  merge
                     →  write .vtt/.srt into the watch directory

The written subtitle carries speaker labels — `<v Speaker 1>` voice spans in
WebVTT, `Speaker 1: text` in SRT — so :mod:`src.srt_daemon.speakers` detects a
labelled transcript and the meeting profile attributes every statement to a
cluster.  Real names can be injected with `--speakers "1=张三,2=李四"`.

**Why a separate command and not a watcher stage**: transcription is slow
(a 1-hour meeting takes ~15-70 minutes on this CPU depending on the model) and
the watcher processes files strictly sequentially — running ASR inside it would
stall every subtitle note.

Usage::

    python -m src.srt_daemon.transcribe --new                    # scan source_dir
    python -m src.srt_daemon.transcribe "videos/meeting.mp3"      # one file
    python -m src.srt_daemon.transcribe --dir "videos" --limit 2 --speakers "1=Alice,2=Bob"
    python -m src.srt_daemon.transcribe --list                   # dry run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from src.srt_daemon import __version__
from src.srt_daemon.config import Config, load_config
from src.srt_daemon.diarize import (
    DiarizationError,
    Diarizer,
    SpeakerTurn,
    get_diarizer,
    label_for,
    speaker_names_from_spec,
)
from src.srt_daemon.logger import get_logger, setup_logging

log = get_logger(__name__)

#: Audio/video extensions handled when scanning a directory.
DEFAULT_EXTENSIONS = (
    ".mp3", ".mp4", ".wav", ".m4a", ".mkv", ".mov",
    ".flac", ".aac", ".opus", ".wma", ".webm",
)


class TranscriptionError(RuntimeError):
    """Raised when decoding, ASR or subtitle writing fails."""


# ---------------------------------------------------------------------------
# Small data holders
# ---------------------------------------------------------------------------


@dataclass
class AsrWord:
    """One word with its time span (only when word timestamps are enabled)."""

    start: float
    end: float
    text: str


@dataclass
class AsrSegment:
    """One ASR segment before speaker assignment."""

    start: float
    end: float
    text: str
    words: list[AsrWord] = field(default_factory=list)


@dataclass
class Cue:
    """A finished subtitle cue with an optional speaker label."""

    start: float
    end: float
    text: str
    speaker: Optional[str] = None

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class TranscriptionResult:
    """Outcome of transcribing one recording."""

    source: Path
    subtitles: list[Path]
    language: str
    duration: float
    cue_count: int
    speaker_count: int
    diarization: str
    elapsed: float


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_timestamp(seconds: float, *, separator: str = ".") -> str:
    """Format *seconds* as `HH:MM:SS.mmm` (VTT) or `HH:MM:SS,mmm` (SRT)."""
    seconds = max(0.0, float(seconds))
    milliseconds = int(round(seconds * 1000))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def escape_vtt(text: str) -> str:
    """Escape `&` and `<` so cue text survives a VTT round-trip."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------


def decode_audio(source: Path, target: Path) -> None:
    """Decode any audio/video file into 16 kHz mono 16-bit PCM WAV.

    ffmpeg is used explicitly (faster-whisper could decode on its own, but
    diarization needs real WAV samples anyway, so the file is decoded once).

    Raises:
        TranscriptionError: If ffmpeg is missing or decoding fails.
    """
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise TranscriptionError(
            "ffmpeg was not found on PATH — install it (or add it to PATH) to "
            "decode audio/video before transcription."
        )

    command = [
        ffmpeg, "-nostdin", "-y", "-v", "error",
        "-i", str(source),
        "-vn", "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
        str(target),
    ]
    log.debug("Decoding: %s", " ".join(command))
    proc = subprocess.run(command, capture_output=True, text=True)
    if proc.returncode != 0 or not target.exists():
        detail = (proc.stderr or "").strip().splitlines()
        raise TranscriptionError(
            f"ffmpeg failed for {source.name}: "
            f"{detail[-1] if detail else 'unknown error'}"
        )


# ---------------------------------------------------------------------------
# Speaker assignment
# ---------------------------------------------------------------------------


def _best_turn(
    turns: Sequence[SpeakerTurn], start: float, end: float
) -> Optional[SpeakerTurn]:
    """Return the turn overlapping `start`..`end` the most, if any."""
    best: Optional[SpeakerTurn] = None
    best_overlap = 0.0
    for turn in turns:
        overlap = turn.overlap(start, end)
        if overlap > best_overlap:
            best, best_overlap = turn, overlap
    return best


def assign_speakers(
    segments: Sequence[AsrSegment],
    turns: Sequence[SpeakerTurn],
    *,
    label_map: Optional[dict[int, str]] = None,
    split_on_words: bool = True,
) -> list[Cue]:
    """Attach a speaker label to every ASR segment.

    Strategy: pick the diarization turn with the largest time overlap.  When
    word timestamps are available and a segment straddles a speaker change, the
    segment is split at the word boundary so no sentence is attributed to the
    wrong person.  Segments that overlap no turn at all inherit the previous
    speaker, so a short diarization gap does not cause label flapping.

    Args:
        segments: ASR output.
        turns: Diarization output (empty when diarization is disabled).
        label_map: Optional 1-based speaker number to display-name mapping.
        split_on_words: Split straddling segments at word boundaries.

    Returns:
        Cues in chronological order.
    """
    names = dict(label_map or {})

    def display(index: int) -> str:
        return names.get(index + 1, label_for(index))

    if not turns:
        return [Cue(s.start, s.end, s.text.strip()) for s in segments if s.text.strip()]

    cues: list[Cue] = []
    previous: Optional[int] = None

    for segment in segments:
        text = segment.text.strip()
        if not text:
            continue

        if split_on_words and segment.words:
            runs = _word_runs(segment, turns)
            if len(runs) > 1:
                for start, end, index, run_text in runs:
                    previous = index if index is not None else previous
                    cues.append(
                        Cue(
                            start,
                            end,
                            run_text,
                            display(index) if index is not None else None,
                        )
                    )
                continue

        turn = _best_turn(turns, segment.start, segment.end)
        index = turn.speaker if turn is not None else previous
        previous = index
        cues.append(
            Cue(
                segment.start,
                segment.end,
                text,
                display(index) if index is not None else None,
            )
        )

    return cues


def _word_runs(
    segment: AsrSegment, turns: Sequence[SpeakerTurn]
) -> list[tuple[float, float, Optional[int], str]]:
    """Group a segment's words into contiguous same-speaker runs."""
    runs: list[tuple[float, float, Optional[int], str]] = []

    for word in segment.words:
        midpoint = (word.start + word.end) / 2
        turn = _best_turn(turns, word.start, word.end) or _point_turn(turns, midpoint)
        index = turn.speaker if turn is not None else None

        if runs and runs[-1][2] == index:
            start, _, _, text = runs[-1]
            runs[-1] = (start, word.end, index, f"{text} {word.text}".strip())
        else:
            runs.append((word.start, word.end, index, word.text))

    return runs


def _point_turn(turns: Sequence[SpeakerTurn], moment: float) -> Optional[SpeakerTurn]:
    """Turn containing *moment*, else the nearest turn by distance."""
    for turn in turns:
        if turn.start <= moment <= turn.end:
            return turn
    if not turns:
        return None
    return min(turns, key=lambda t: min(abs(t.start - moment), abs(t.end - moment)))


# ---------------------------------------------------------------------------
# Subtitle writers
# ---------------------------------------------------------------------------


def _note_line(source: Path, model: str, diarization: str, language: str) -> str:
    return (
        f"NOTE source: {source.name} | whisper={model} | "
        f"diarization={diarization} | language={language} | "
        f"generated={datetime.now(timezone.utc).isoformat(timespec='seconds')}"
    )


def write_vtt(
    cues: Sequence[Cue],
    target: Path,
    *,
    source: Path,
    model: str,
    diarization: str,
    language: str,
) -> Path:
    """Write cues as WebVTT with `<v Speaker N>` voice spans."""
    lines = ["WEBVTT", "", _note_line(source, model, diarization, language), ""]
    for cue in cues:
        lines.append(f"{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}")
        body = escape_vtt(cue.text)
        lines.append(f"<v {cue.speaker}>{body}" if cue.speaker else body)
        lines.append("")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")
    return target


def write_srt(
    cues: Sequence[Cue],
    target: Path,
    *,
    source: Path,
    model: str,
    diarization: str,
    language: str,
) -> Path:
    """Write cues as SRT, prefixing labelled cues with `Speaker: `."""
    blocks: list[str] = []
    for index, cue in enumerate(cues, start=1):
        timing = (
            f"{format_timestamp(cue.start, separator=',')} --> "
            f"{format_timestamp(cue.end, separator=',')}"
        )
        text = f"{cue.speaker}: {cue.text}" if cue.speaker else cue.text
        blocks.append(f"{index}\n{timing}\n{text}\n")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(blocks), encoding="utf-8")
    return target


def probe_media_duration(source: Path) -> float:
    """Return the media duration in seconds (0.0 when unknown).

    Uses PyAV, which is already a dependency of the transcription stack, so no
    extra ffprobe process is spawned.
    """
    try:
        import av
    except ImportError:  # pragma: no cover - av ships with faster-whisper extras
        return 0.0
    try:
        with av.open(str(source)) as container:
            if container.duration:
                return float(container.duration / av.time_base)
            for stream in container.streams:
                if stream.duration and stream.time_base:
                    return float(stream.duration * stream.time_base)
    except Exception as exc:  # noqa: BLE001 - duration is best effort
        log.debug("Cannot read the duration of %s: %s", source, exc)
    return 0.0


#: Containers that actually carry a picture (screenshots need the video).
VIDEO_SUFFIXES = frozenset(
    {".mp4", ".mkv", ".mov", ".flv", ".webm", ".avi", ".m4v", ".ts", ".wmv", ".mpg", ".mpeg"}
)


# ---------------------------------------------------------------------------
# State (skip already-transcribed recordings)
# ---------------------------------------------------------------------------


class TranscriptionState:
    """Remembers which recordings were already transcribed.

    A recording counts as done when its path, size and mtime match the record,
    so re-recording or editing a file triggers a fresh transcription.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, Any] = {"files": {}}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            self._data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Transcription state unreadable (%s) — starting fresh", exc)
            self._data = {"files": {}}
        self._data.setdefault("files", {})

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(self._data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(self._path)

    @staticmethod
    def _key(source: Path) -> str:
        return str(source.resolve())

    def is_done(self, source: Path) -> bool:
        """True when *source* was transcribed before and has not changed."""
        record = self._data["files"].get(self._key(source))
        if not record:
            return False
        try:
            stat = source.stat()
        except OSError:
            return False
        return (
            record.get("size") == stat.st_size
            and int(record.get("mtime", -1)) == int(stat.st_mtime)
        )

    def mark(
        self, source: Path, subtitles: Sequence[Path], result: TranscriptionResult
    ) -> None:
        """Record a successful transcription."""
        stat = source.stat()
        self._data["files"][self._key(source)] = {
            "size": stat.st_size,
            "mtime": int(stat.st_mtime),
            "subtitles": [str(p) for p in subtitles],
            "language": result.language,
            "speakers": result.speaker_count,
            "cues": result.cue_count,
            "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self._save()


# ---------------------------------------------------------------------------
# Transcriber
# ---------------------------------------------------------------------------


class Transcriber:
    """Runs the decode → ASR → diarize → write pipeline for one config."""

    def __init__(
        self,
        config: Config,
        *,
        model: Optional[str] = None,
        device: Optional[str] = None,
        compute_type: Optional[str] = None,
        cpu_threads: Optional[int] = None,
        language: Optional[str] = None,
        beam_size: Optional[int] = None,
        diarizer: Optional[Diarizer] = None,
        output_dir: Optional[Path] = None,
        output_format: Optional[str] = None,
        word_timestamps: Optional[bool] = None,
        state: Optional["TranscriptionState"] = None,
    ) -> None:
        self._config = config
        self._model_explicit = model is not None
        self._model_name = model or config.transcribe_model
        self._device = device or config.transcribe_device
        self._compute_type = compute_type or config.transcribe_compute_type
        self._cpu_threads = (
            cpu_threads if cpu_threads is not None else config.transcribe_cpu_threads
        )
        raw_language = language if language is not None else config.transcribe_language
        self._language = (
            None if str(raw_language).lower() in ("", "none", "auto") else raw_language
        )
        self._beam_size = (
            beam_size if beam_size is not None else config.transcribe_beam_size
        )
        self._word_timestamps = (
            config.transcribe_word_timestamps
            if word_timestamps is None
            else word_timestamps
        )
        self._output_dir = (output_dir or config.transcribe_output_dir).resolve()
        self._output_format = (
            output_format or config.transcribe_output_format
        ).lower()
        self._diarizer = diarizer if diarizer is not None else get_diarizer(config)
        self._asr: Any = None
        self.state = state or TranscriptionState(config.transcribe_state_file)

    # -- lazy ASR -------------------------------------------------------------

    def _ensure_asr(self) -> Any:
        if self._asr is not None:
            return self._asr
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise TranscriptionError(
                "faster-whisper is not installed. Run: pip install faster-whisper"
            ) from exc

        last_error: Optional[Exception] = None
        for device, compute_type in self._device_attempts():
            log.info(
                "Loading whisper model %s (device=%s compute=%s threads=%s)",
                self._model_name, device, compute_type, self._cpu_threads,
            )
            started = time.time()
            try:
                model = WhisperModel(
                    self._model_name,
                    device=device,
                    compute_type=compute_type,
                    cpu_threads=self._cpu_threads,
                )
            except Exception as exc:  # noqa: BLE001 - try the next device
                last_error = exc
                log.warning(
                    "Cannot load %s on %s/%s: %s", self._model_name, device, compute_type, exc
                )
                continue
            self._asr = model
            self._device = device
            self._compute_type = compute_type
            log.info(
                "Model ready in %.1fs (device=%s compute=%s)",
                time.time() - started, device, compute_type,
            )
            return self._asr

        raise TranscriptionError(
            "Could not load whisper model '{model}': {error}. On a GPU machine install "
            "CUDA 12 + cuDNN 9 (doc/OPERATIONS.md), or set transcribe.device to cpu.".format(
                model=self._model_name, error=last_error
            )
        )

    def _device_attempts(self) -> list[tuple[str, str]]:
        """(device, compute_type) pairs to try, best first."""
        device = (self._device or "auto").lower()
        compute = (self._compute_type or "auto").lower()

        def resolve(dev: str) -> str:
            if compute not in ("", "auto"):
                return compute
            return "float16" if dev == "cuda" else "int8"

        if device in ("", "auto"):
            return [("cuda", resolve("cuda")), ("cpu", resolve("cpu"))]

        attempts = [(device, resolve(device))]
        if device != "cpu":
            attempts.append(("cpu", resolve("cpu")))
        return attempts

    def _apply_long_media_policy(self, source: Path) -> None:
        """Switch to a faster model when the recording is very long.

        A 2-hour lecture with large-v3 takes ~5 hours on CPU; the same file with
        medium takes ~2.2 hours.  An explicitly requested model always wins.
        """
        if self._model_explicit:
            return
        if not self._config.transcribe_long_media_enabled:
            return

        duration = probe_media_duration(source)
        if duration <= 0:
            return
        threshold = self._config.transcribe_long_media_threshold_minutes * 60.0
        if duration < threshold:
            return

        fallback = self._config.transcribe_long_media_model
        if fallback == self._model_name:
            return
        log.info(
            "Long recording (%.1f min >= %.0f min): switching model %s -> %s",
            duration / 60.0,
            threshold / 60.0,
            self._model_name,
            fallback,
        )
        self._model_name = fallback
        self._asr = None

    # -- ASR ------------------------------------------------------------------

    def _run_asr(self, wav_path: Path) -> tuple[list[AsrSegment], str, float]:
        """Run faster-whisper over *wav_path* and collect its segments."""
        model = self._ensure_asr()
        segments, info = model.transcribe(
            str(wav_path),
            beam_size=self._beam_size,
            language=self._language,
            vad_filter=self._config.transcribe_vad_filter,
            vad_parameters=dict(
                min_silence_duration_ms=self._config.transcribe_min_silence_ms
            ),
            word_timestamps=self._word_timestamps,
        )

        collected: list[AsrSegment] = []
        for segment in segments:
            words = [
                AsrWord(
                    start=float(w.start), end=float(w.end), text=str(w.word).strip()
                )
                for w in (getattr(segment, "words", None) or [])
            ]
            collected.append(
                AsrSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=str(segment.text).strip(),
                    words=words,
                )
            )
            if len(collected) % 25 == 0:
                log.info(
                    "  ... %d segments, at %s",
                    len(collected),
                    format_timestamp(collected[-1].end),
                )

        language = getattr(info, "language", self._language or "?")
        duration = float(getattr(info, "duration", 0.0) or 0.0)
        probability = float(getattr(info, "language_probability", 0.0) or 0.0)
        log.info(
            "ASR done: %d segment(s), language=%s (p=%.2f), audio=%.1fs",
            len(collected), language, probability, duration,
        )
        return collected, str(language), duration

    # -- full pipeline ---------------------------------------------------------

    def transcribe(
        self,
        source: Path,
        *,
        num_speakers: Optional[int] = None,
        speaker_names: Optional[dict[int, str]] = None,
        force: bool = False,
    ) -> Optional[TranscriptionResult]:
        """Transcribe one recording and write its subtitle.

        Args:
            source: Audio/video file.
            num_speakers: Explicit cluster count (overrides auto clustering).
            speaker_names: 1-based speaker number to real name mapping.
            force: Transcribe even when the state says it is already done.

        Returns:
            The result, or nothing when the file was skipped as already done.
        """
        source = source.resolve()
        if not source.exists():
            raise TranscriptionError(f"File not found: {source}")
        if not force and self.state.is_done(source):
            log.info("Skipping (already transcribed): %s", source.name)
            return None

        self._apply_long_media_policy(source)

        started = time.time()
        log.info("=" * 60)
        log.info("Transcribing: %s (%.1f MB)", source.name, source.stat().st_size / 1e6)

        with tempfile.TemporaryDirectory(prefix="transcribe_") as tmp:
            wav_path = Path(tmp) / "audio16k.wav"
            decode_audio(source, wav_path)

            segments, language, duration = self._run_asr(wav_path)

            turns: list[SpeakerTurn] = []
            try:
                turns = self._diarizer.diarize(wav_path, num_speakers=num_speakers)
            except DiarizationError as exc:
                log.error(
                    "Diarization failed (%s) — writing an unlabelled subtitle", exc
                )
                turns = []

        cues = assign_speakers(
            segments,
            turns,
            label_map=speaker_names,
            split_on_words=self._word_timestamps,
        )
        speaker_count = len({cue.speaker for cue in cues if cue.speaker})

        subtitles = self._write_subtitles(
            cues,
            source,
            model=self._model_name,
            diarization=self._diarizer.name,
            language=language,
        )

        # Remember where the picture is: the notes pipeline needs the video for
        # [[screenshot:...]] markers.
        if source.suffix.lower() in VIDEO_SUFFIXES:
            from src.srt_daemon.pipeline import write_source_sidecar

            for subtitle in subtitles:
                write_source_sidecar(subtitle, source, duration)

        elapsed = time.time() - started
        result = TranscriptionResult(
            source=source,
            subtitles=subtitles,
            language=language,
            duration=duration,
            cue_count=len(cues),
            speaker_count=speaker_count,
            diarization=self._diarizer.name,
            elapsed=elapsed,
        )
        self.state.mark(source, subtitles, result)

        log.info(
            "Done: %s -> %s | %d cue(s), %d speaker(s), %.1fs audio in %.1fs",
            source.name,
            ", ".join(p.name for p in subtitles),
            len(cues),
            speaker_count,
            duration,
            elapsed,
        )
        return result

    def _write_subtitles(
        self,
        cues: Sequence[Cue],
        source: Path,
        *,
        model: str,
        diarization: str,
        language: str,
    ) -> list[Path]:
        """Write the requested subtitle format(s) next to the watch directory."""
        written: list[Path] = []
        wanted = self._output_format
        if wanted in ("vtt", "both", ""):
            written.append(
                write_vtt(
                    cues,
                    self._output_dir / f"{source.stem}.vtt",
                    source=source,
                    model=model,
                    diarization=diarization,
                    language=language,
                )
            )
        if wanted in ("srt", "both"):
            written.append(
                write_srt(
                    cues,
                    self._output_dir / f"{source.stem}.srt",
                    source=source,
                    model=model,
                    diarization=diarization,
                    language=language,
                )
            )
        return written


# ---------------------------------------------------------------------------
# Job discovery
# ---------------------------------------------------------------------------


def discover_sources(
    sources: Sequence[str],
    directory: Optional[str],
    extensions: Sequence[str],
    *,
    recursive: bool = False,
) -> list[Path]:
    """Resolve CLI arguments into a sorted list of media files.

    Args:
        sources: File or directory arguments.
        directory: Extra directory to scan (may be absent).
        extensions: Media extensions to accept.
        recursive: Also walk sub-directories of scanned directories.
    """
    found: list[Path] = []

    for raw in sources:
        path = Path(raw).expanduser()
        if path.is_dir():
            found.extend(_scan_directory(path, extensions, recursive=recursive))
        elif path.exists():
            found.append(path)
        else:
            raise TranscriptionError(f"Input not found: {path}")

    if directory:
        found.extend(
            _scan_directory(
                Path(directory).expanduser(), extensions, recursive=recursive
            )
        )

    unique: dict[str, Path] = {}
    for path in found:
        unique[str(path.resolve())] = path.resolve()
    return sorted(unique.values(), key=lambda p: p.name.lower())


def _scan_directory(
    directory: Path, extensions: Sequence[str], *, recursive: bool = False
) -> list[Path]:
    """Collect media files in *directory* (optionally walking sub-directories)."""
    if not directory.is_dir():
        raise TranscriptionError(f"Not a directory: {directory}")
    lowered = {ext.lower() for ext in extensions}
    candidates = directory.rglob("*") if recursive else directory.iterdir()
    return [
        entry
        for entry in candidates
        if entry.is_file() and entry.suffix.lower() in lowered
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        prog="srt-daemon-transcribe",
        description=(
            "Transcribe audio/video into speaker-labelled subtitles that the "
            "SRT Daemon then turns into Markdown notes.  Runs as a separate "
            "command because ASR is far slower than the notes pipeline."
        ),
    )
    parser.add_argument("sources", nargs="*", help="Audio/video files or directories.")
    parser.add_argument("--config", default=None, metavar="PATH", help="Path to config.yaml.")
    parser.add_argument(
        "--dir", default=None, metavar="PATH",
        help="Also scan this directory (default: transcribe.source_dir).",
    )
    parser.add_argument(
        "--new", action="store_true",
        help="Only transcribe files that were not transcribed before.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Re-transcribe even when the state says it is done.",
    )
    parser.add_argument(
        "--recursive", action="store_true",
        help="Also scan sub-directories of the given directories.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Only list what would be transcribed, then exit.",
    )
    parser.add_argument("--limit", type=int, default=None, metavar="N",
                        help="Stop after N recordings.")
    parser.add_argument("--model", default=None, help="Whisper model (default: config).")
    parser.add_argument("--device", default=None, help="cpu | cuda (default: config).")
    parser.add_argument("--compute-type", default=None, help="int8 | float16 | ...")
    parser.add_argument("--cpu-threads", type=int, default=None)
    parser.add_argument("--language", default=None,
                        help="Force a language (e.g. en, zh); default auto-detect.")
    parser.add_argument("--beam-size", type=int, default=None)
    parser.add_argument(
        "--word-timestamps", dest="word_timestamps", action="store_true", default=None,
        help="Enable word-level timestamps (exact speaker splits, slower ASR).",
    )
    parser.add_argument(
        "--no-word-timestamps", dest="word_timestamps", action="store_false",
        help="Disable word-level timestamps (segment-level speaker assignment).",
    )
    parser.add_argument("--diarizer", default=None, choices=["sherpa", "pyannote", "none"],
                        help="Override transcribe.diarization.backend.")
    parser.add_argument("--num-speakers", type=int, default=None,
                        help="Exact number of speakers (skips auto clustering) — "
                             "the most reliable way to avoid over-splitting.")
    parser.add_argument("--cluster-threshold", type=float, default=None,
                        help="Clustering distance threshold (higher = fewer "
                             "speakers; default from config, 0.8).")
    parser.add_argument("--speakers", default=None, metavar="MAP",
                        help='Map cluster numbers to names, e.g. "1=Alice,2=Bob".')
    parser.add_argument("--format", default=None, choices=["vtt", "srt", "both"],
                        help="Subtitle format to write (default: config).")
    parser.add_argument("--output-dir", default=None, metavar="PATH",
                        help="Where to write subtitles (default: the watch directory).")
    parser.add_argument("--version", action="store_true", help="Print version and exit.")
    return parser.parse_args(argv)


def main() -> None:
    """CLI entry point."""
    args = parse_args()

    if args.version:
        print(f"SRT Daemon transcribe v{__version__}")
        return

    try:
        config = load_config(args.config)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    setup_logging(
        log_file=config.log_file,
        level=config.log_level,
        fmt=config.log_format,
        date_format=config.log_date_format,
        rotation=config.log_rotation,
        backup_count=config.log_backup_count,
    )

    log.info("=" * 60)
    log.info("SRT Daemon transcribe v%s  pid=%d", __version__, os.getpid())
    log.info("=" * 60)

    # --- Discover jobs --------------------------------------------------------
    scan_dir = args.dir or (str(config.transcribe_source_dir) if not args.sources else None)
    try:
        sources = discover_sources(
            args.sources,
            scan_dir,
            config.transcribe_extensions,
            recursive=args.recursive,
        )
    except TranscriptionError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not sources:
        print("Nothing to transcribe.", file=sys.stderr)
        return

    # --- Build the transcriber ------------------------------------------------
    diarizer_overrides: dict[str, Any] = {}
    if args.diarizer:
        diarizer_overrides["backend"] = args.diarizer
    if args.cluster_threshold is not None:
        diarizer_overrides["cluster_threshold"] = args.cluster_threshold

    transcriber = Transcriber(
        config,
        model=args.model,
        device=args.device,
        compute_type=args.compute_type,
        cpu_threads=args.cpu_threads,
        language=args.language,
        beam_size=args.beam_size,
        diarizer=get_diarizer(config, diarizer_overrides) if diarizer_overrides else None,
        output_dir=Path(args.output_dir) if args.output_dir else None,
        output_format=args.format,
        word_timestamps=args.word_timestamps,
    )

    if args.list:
        for source in sources:
            mark = "done" if transcriber.state.is_done(source) else "TODO"
            print(f"[{mark}] {source}")
        return

    # --- Run ------------------------------------------------------------------
    done = skipped = failed = 0
    for source in sources:
        if args.limit is not None and done >= args.limit:
            log.info("Reached --limit %d — stopping", args.limit)
            break
        try:
            result = transcriber.transcribe(
                source,
                num_speakers=args.num_speakers,
                speaker_names=speaker_names_from_spec(args.speakers),
                force=args.force,
            )
        except (TranscriptionError, DiarizationError) as exc:
            log.error("Failed: %s — %s", source.name, exc)
            print(f"FAILED {source.name}: {exc}", file=sys.stderr)
            failed += 1
            continue

        if result is None:
            skipped += 1
            continue
        done += 1
        for path in result.subtitles:
            print(str(path))

    log.info(
        "Transcription run finished: %d transcribed, %d skipped, %d failed",
        done, skipped, failed,
    )
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
