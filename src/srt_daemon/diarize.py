"""Speaker diarization backends — who spoke when.

Diarization does **not** run inside the notes daemon.  It belongs to the
transcription front-end (:mod:`src.srt_daemon.transcribe`), which turns an
audio/video file into a **speaker-labelled subtitle**.  The watcher then
processes that subtitle exactly like a Panopto/Zoom export: the VTT/SRT parser
turns the labels into text, :mod:`src.srt_daemon.speakers` detects them, and the
meeting profile attributes every statement to its cluster.

Backends, selected by `transcribe.diarization.backend`:

* `sherpa`   — sherpa-onnx offline diarization (pyannote segmentation 3.0 +
  a speaker-embedding model).  No account, no token, runs on CPU; ~40 MB of
  ONNX models.
* `pyannote` — the pyannote.audio pipeline.  Needs `pip install pyannote.audio`
  plus a **free** HuggingFace account and an access token (the diarization
  models are gated; inference itself is local and costs nothing).
* `none`     — no diarization: every cue gets one label.

Usage::

    from src.srt_daemon.diarize import get_diarizer

    diarizer = get_diarizer(config)
    turns = diarizer.diarize(Path("16k-mono.wav"))
"""

from __future__ import annotations

import os
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from src.srt_daemon.config import Config
from src.srt_daemon.logger import get_logger

log = get_logger(__name__)

#: Sample rate every diarization backend expects.
REQUIRED_SAMPLE_RATE = 16000


class DiarizationError(RuntimeError):
    """Raised when a diarization backend is unavailable or fails."""


@dataclass(frozen=True)
class SpeakerTurn:
    """One contiguous stretch of speech assigned to a single speaker.

    Attributes:
        start: Start time in seconds.
        end: End time in seconds.
        speaker: Speaker index (0-based) as produced by the backend.
    """

    start: float
    end: float
    speaker: int

    @property
    def duration(self) -> float:
        """Length of the turn in seconds."""
        return max(0.0, self.end - self.start)

    def overlap(self, start: float, end: float) -> float:
        """Seconds of overlap with the interval *start*..*end*."""
        return max(0.0, min(self.end, end) - max(self.start, start))


def label_for(speaker_index: int) -> str:
    """Human-readable label for a backend speaker index (1-based)."""
    return f"Speaker {speaker_index + 1}"


def read_wav_mono16k(path: str | Path) -> Any:
    """Read a 16 kHz mono PCM WAV file into a float32 numpy array.

    The transcription front-end always decodes its input to this exact format
    before diarizing, so no resampling or channel mixing is needed here.

    Raises:
        DiarizationError: If the file is not 16 kHz mono 16-bit PCM.
    """
    import numpy as np

    wav_path = Path(path)
    with wave.open(str(wav_path), "rb") as fh:
        channels = fh.getnchannels()
        width = fh.getsampwidth()
        rate = fh.getframerate()
        frames = fh.readframes(fh.getnframes())

    if rate != REQUIRED_SAMPLE_RATE or channels != 1 or width != 2:
        raise DiarizationError(
            f"{wav_path.name}: expected 16 kHz mono 16-bit PCM, got "
            f"{rate} Hz / {channels} channel(s) / {width * 8}-bit"
        )

    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


class Diarizer:
    """Base class for diarization backends."""

    #: Short backend name, used in logs.
    name = "base"

    def diarize(
        self, wav_path: str | Path, num_speakers: Optional[int] = None
    ) -> list[SpeakerTurn]:
        """Return speaker turns for a 16 kHz mono WAV file."""
        raise NotImplementedError

    def describe(self) -> str:
        """One-line description for logs."""
        return self.name


class NoDiarizer(Diarizer):
    """Diarization disabled: the caller falls back to a single speaker."""

    name = "none"

    def diarize(
        self, wav_path: str | Path, num_speakers: Optional[int] = None
    ) -> list[SpeakerTurn]:
        return []

    def describe(self) -> str:
        return "none (single speaker)"


class SherpaDiarizer(Diarizer):
    """sherpa-onnx offline diarization (pyannote segmentation + embedding).

    Requires the `sherpa-onnx` package and two ONNX models: a pyannote
    segmentation model and a speaker-embedding model (3D-Speaker / WeSpeaker /
    NeMo).  Everything runs locally on CPU — no account, token or quota.
    """

    name = "sherpa"

    def __init__(self, settings: dict, project_root: Path) -> None:
        self._settings = settings
        self._root = project_root
        self._instance: Any = None

    # -- helpers --------------------------------------------------------------

    def _resolve(self, key: str, default: str) -> Path:
        raw = str(self._settings.get(key) or default)
        path = Path(raw)
        return path if path.is_absolute() else (self._root / path).resolve()

    def _build(self) -> Any:
        """Create (once) the sherpa-onnx diarization pipeline."""
        if self._instance is not None:
            return self._instance

        try:
            import sherpa_onnx
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise DiarizationError(
                "sherpa-onnx is not installed. Run: pip install sherpa-onnx"
            ) from exc

        seg_model = self._resolve(
            "segmentation_model",
            "models/speaker-diarization/sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
        )
        emb_model = self._resolve(
            "embedding_model",
            "models/speaker-diarization/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx",
        )
        for path in (seg_model, emb_model):
            if not path.exists():
                raise DiarizationError(
                    f"Diarization model not found: {path}. "
                    f"See doc/OPERATIONS.md section 4f for the download commands."
                )

        num_clusters = int(self._settings.get("num_speakers") or -1)
        threshold = float(self._settings.get("cluster_threshold", 0.5))

        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                    model=str(seg_model)
                ),
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(emb_model)
            ),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=num_clusters, threshold=threshold
            ),
            min_duration_on=float(self._settings.get("min_duration_on", 0.3)),
            min_duration_off=float(self._settings.get("min_duration_off", 0.5)),
        )
        if not config.validate():
            raise DiarizationError(f"Invalid sherpa-onnx diarization config: {config}")

        self._instance = sherpa_onnx.OfflineSpeakerDiarization(config)
        log.info(
            "Sherpa diarizer ready: segmentation=%s embedding=%s clusters=%s threshold=%.2f",
            seg_model.name,
            emb_model.name,
            num_clusters if num_clusters > 0 else "auto",
            threshold,
        )
        return self._instance

    # -- API ------------------------------------------------------------------

    def diarize(
        self, wav_path: str | Path, num_speakers: Optional[int] = None
    ) -> list[SpeakerTurn]:
        if num_speakers is not None:
            # Rebuild with an explicit cluster count (per-file override).
            self._settings = {**self._settings, "num_speakers": num_speakers}
            self._instance = None

        pipeline = self._build()
        samples = read_wav_mono16k(wav_path)
        if len(samples) == 0:
            return []

        result = pipeline.process(samples).sort_by_start_time()
        turns = [
            SpeakerTurn(
                start=float(seg.start), end=float(seg.end), speaker=int(seg.speaker)
            )
            for seg in result
        ]
        log.info(
            "Diarization: %d turn(s), %d speaker(s), %.1fs of speech",
            len(turns),
            len({turn.speaker for turn in turns}),
            sum(turn.duration for turn in turns),
        )
        return turns

    def describe(self) -> str:
        return "sherpa-onnx"


class PyannoteDiarizer(Diarizer):
    """pyannote.audio diarization pipeline (optional backend).

    The models are **free** but gated: a HuggingFace account, an accepted model
    licence and a read token are required.  Inference runs locally, so there is
    no per-minute cost and no quota.
    """

    name = "pyannote"

    def __init__(self, settings: dict) -> None:
        self._settings = settings
        self._instance: Any = None

    def _token(self) -> str:
        token = (
            self._settings.get("hf_token")
            or os.getenv("HF_TOKEN")
            or os.getenv("HUGGINGFACE_TOKEN")
            or os.getenv("HUGGINGFACEHUB_API_TOKEN")
            or ""
        )
        if not token:
            raise DiarizationError(
                "pyannote diarization needs a free HuggingFace token: accept the "
                "model terms at https://hf.co/pyannote/speaker-diarization-3.1, then "
                "set HF_TOKEN (environment) or transcribe.diarization.hf_token "
                "(config).  Alternatively use transcribe.diarization.backend: sherpa."
            )
        return str(token)

    def _build(self) -> Any:
        if self._instance is not None:
            return self._instance
        try:
            from pyannote.audio import Pipeline
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise DiarizationError(
                "pyannote.audio is not installed. Run: pip install pyannote.audio "
                "(or set transcribe.diarization.backend to 'sherpa')"
            ) from exc

        model = str(
            self._settings.get("pyannote_model", "pyannote/speaker-diarization-3.1")
        )
        log.info("Loading pyannote pipeline %s (first run downloads the models)", model)
        self._instance = Pipeline.from_pretrained(model, use_auth_token=self._token())
        return self._instance

    def diarize(
        self, wav_path: str | Path, num_speakers: Optional[int] = None
    ) -> list[SpeakerTurn]:
        pipeline = self._build()
        kwargs: dict[str, Any] = {}
        if num_speakers is not None:
            kwargs["num_speakers"] = num_speakers

        annotation = pipeline(str(wav_path), **kwargs)
        turns = [
            SpeakerTurn(
                start=float(turn.start),
                end=float(turn.end),
                speaker=_stable_speaker_index(str(speaker)),
            )
            for turn, _, speaker in annotation.itertracks(yield_label=True)
        ]
        log.info(
            "pyannote: %d turn(s), %d speaker(s)",
            len(turns),
            len({turn.speaker for turn in turns}),
        )
        return turns

    def describe(self) -> str:
        return f"pyannote ({self._settings.get('pyannote_model', 'speaker-diarization-3.1')})"


_PYANNOTE_LABEL_INDEX: dict[str, int] = {}


def _stable_speaker_index(label: str) -> int:
    """Map pyannote labels (`SPEAKER_00`) to stable 0-based indices."""
    if label not in _PYANNOTE_LABEL_INDEX:
        _PYANNOTE_LABEL_INDEX[label] = len(_PYANNOTE_LABEL_INDEX)
    return _PYANNOTE_LABEL_INDEX[label]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_diarizer(config: Config, overrides: Optional[dict] = None) -> Diarizer:
    """Build the diarizer named by `transcribe.diarization.backend`.

    Args:
        config: Resolved daemon configuration.
        overrides: Optional settings merged over `transcribe.diarization`
            (used by the CLI to switch backend or tune the cluster threshold).

    Returns:
        A diarizer instance.  Unknown or disabled backends yield
        :class:`NoDiarizer`, so callers always have something to call.
    """
    settings = {**config.transcribe_diarization, **(overrides or {})}
    backend = str(settings.get("backend", "sherpa")).strip().lower()

    if backend in ("", "none", "off", "false"):
        return NoDiarizer()
    if backend == "sherpa":
        return SherpaDiarizer(settings, config.project_root)
    if backend == "pyannote":
        return PyannoteDiarizer(settings)

    log.warning("Unknown diarization backend %r — diarization disabled", backend)
    return NoDiarizer()


def speaker_names_from_spec(spec: Optional[str], count: int = 0) -> dict[int, str]:
    """Parse a `1=张三,2=李四` speaker-mapping specification.

    Args:
        spec: Raw specification from the CLI or config (may be absent).
        count: Number of clusters found by the diarizer (logging only).

    Returns:
        Mapping of 1-based speaker numbers to real names.
    """
    if not spec:
        return {}

    names: dict[int, str] = {}
    for chunk in str(spec).replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, _, value = chunk.partition("=")
        try:
            index = int(key.strip())
        except ValueError:
            log.warning("Ignoring speaker mapping %r (left side must be a number)", chunk)
            continue
        if value.strip():
            names[index] = value.strip()

    if names and count:
        missing = [i for i in range(1, count + 1) if i not in names]
        if missing:
            log.info(
                "Speaker mapping covers %d/%d cluster(s); %s keep the default label",
                len(names),
                count,
                ", ".join(f"Speaker {i}" for i in missing),
            )
    return names
