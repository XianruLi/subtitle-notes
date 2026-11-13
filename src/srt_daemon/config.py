"""Configuration loader for SRT Daemon.

Loads config.yaml and .env files, resolving paths relative to the project root.
Provides a typed Config class for safe access to all settings.

Usage:
    from src.srt_daemon.config import load_config
    cfg = load_config()
    print(cfg.watch_dir)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Optional

import yaml
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# Path resolution helpers
# ---------------------------------------------------------------------------

def _find_project_root() -> Path:
    """Locate the data root (config, prompts, models, output, logs).

    Resolution order:

    1. The ``SRT_DAEMON_HOME`` environment variable, when it points at a
       directory — useful for portable installs and tests.
    2. The folder holding the executable when running as a frozen
       PyInstaller bundle: ``SRTDaemon.exe`` sits next to ``config.yaml``,
       ``doc/``, ``models/``, ``output/`` and ``logs/``.
    3. The source checkout, found by walking upwards for a ``.claude/``
       directory or a ``doc/`` + ``src/`` sibling pair.

    Returns:
        Absolute Path to the root.

    Raises:
        RuntimeError: If the root cannot be located.
    """
    override = os.getenv("SRT_DAEMON_HOME")
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_dir():
            return candidate.resolve()

    if getattr(sys, "frozen", False):
        # PyInstaller bundle: data lives next to the executable, not inside
        # the temporary extraction folder.
        exe_dir = Path(sys.executable).resolve().parent
        # A *development* build sits inside the checkout (dist/SRTDaemon/...).
        # Then use the checkout as the data root so the packaged app and the
        # source share one output/ and one state file.  Release zips carry no
        # src/srt_daemon/config.yaml marker and stay self-contained.
        for ancestor in [exe_dir, *exe_dir.parents]:
            if (ancestor / "src" / "srt_daemon" / "config.yaml").is_file():
                return ancestor
        return exe_dir

    # Start from this file's directory:  <root>/src/srt_daemon/config.py
    start = Path(__file__).resolve().parent

    for ancestor in [start] + list(start.parents):
        if (ancestor / ".claude").is_dir():
            return ancestor
        if (ancestor / "doc").is_dir() and (ancestor / "src").is_dir():
            return ancestor

    # Last resort: assume standard layout (<root>/src/srt_daemon/)
    if (start.parent.parent / ".claude").is_dir():
        return start.parent.parent

    raise RuntimeError(
        "Cannot locate project root. "
        "Ensure you are running from within the daemons project directory."
    )


def _resolve_path(raw: str, project_root: Path) -> Path:
    """Convert a config path string to an absolute Path.

    If *raw* is already absolute it is returned unchanged; otherwise it is
    resolved relative to *project_root*.
    """
    p = Path(raw)
    if p.is_absolute():
        return p
    return (project_root / p).resolve()


# ---------------------------------------------------------------------------
# Config class
# ---------------------------------------------------------------------------

class Config:
    """Typed, read-only view of the daemon's YAML configuration.

    All filesystem paths are stored as absolute ``pathlib.Path`` objects.
    Secrets (API key) come from the environment.
    """

    def __init__(self, data: dict[str, Any], project_root: Path) -> None:
        self._data = data
        self._project_root = project_root

    # -- watch ----------------------------------------------------------------

    @property
    def watch_dir(self) -> Path:
        """Absolute path to the directory monitored for SRT files."""
        return _resolve_path(self._data["watch"]["directory"], self._project_root)

    @property
    def watch_extensions(self) -> list[str]:
        """File extensions (lowercase, with dot) that trigger processing."""
        return [ext.lower() for ext in self._data["watch"]["extensions"]]

    @property
    def watch_poll_interval_ms(self) -> int:
        """Polling interval in ms (fallback when native events unavailable)."""
        return self._data["watch"].get("poll_interval_ms", 1000)

    @property
    def watch_debounce_seconds(self) -> float:
        """Seconds to wait after a filesystem event before processing."""
        return self._data["watch"].get("debounce_seconds", 2.0)

    # -- output ---------------------------------------------------------------

    @property
    def output_dir(self) -> Path:
        """Absolute path where generated Markdown files are written."""
        return _resolve_path(self._data["output"]["directory"], self._project_root)

    @property
    def output_filename_template(self) -> str:
        """Template for output filenames ({basename}, {date}, {time})."""
        return self._data["output"]["filename_template"]

    # -- ai -------------------------------------------------------------------

    @property
    def ai_model(self) -> str:
        """AI model ID (overridable via ``SRT_AI_MODEL`` env var)."""
        return os.getenv("SRT_AI_MODEL", self._data["ai"]["model"])

    @property
    def ai_max_tokens(self) -> int:
        return self._data["ai"].get("max_tokens", 8192)

    @property
    def ai_temperature(self) -> float:
        return self._data["ai"].get("temperature", 0.3)

    @property
    def ai_reasoning_effort(self) -> str | None:
        """Reasoning effort for DeepSeek reasoning models.

        One of ``low``, ``medium``, ``high``, ``xhigh``, ``max``, or ``None``
        to disable.  Only ``deepseek-v4-pro`` uses it; ``deepseek-flash`` accepts
        the parameter but ignores it, so keep it ``None`` while flash is selected.
        """
        return self._data["ai"].get("reasoning_effort")

    @property
    def ai_connection_timeout(self) -> float:
        """Connection timeout in seconds."""
        return self._data["ai"].get("connection_timeout_sec", 10)

    @property
    def ai_read_timeout(self) -> float:
        """Read timeout in seconds."""
        return self._data["ai"].get("read_timeout_sec", 120)

    @property
    def ai_max_retries(self) -> int:
        """Maximum retry attempts on transient API failures."""
        return self._data["ai"].get("max_retries", 3)

    @property
    def ai_retry_backoff_base(self) -> float:
        """Base seconds for exponential backoff: ``base * 2^(attempt-1)``."""
        return self._data["ai"].get("retry_backoff_base_sec", 2)

    @property
    def ai_chunk_threshold(self) -> int:
        """Subtitle count above which text is split into chunks for AI."""
        return self._data["ai"].get("chunk_threshold", 2000)

    @property
    def ai_system_prompt_file(self) -> Path:
        """Absolute path to the markdown file containing the AI system prompt.

        Used for legacy single-prompt configurations and as the prompt of the
        default content-type profile.
        """
        path = self._data["ai"].get("system_prompt_file", "doc/prompts/course.md")
        return _resolve_path(path, self._project_root)

    # -- ai: content-type profiles --------------------------------------------

    @property
    def ai_default_profile(self) -> str:
        """Profile id used when neither the CLI nor detection picks one."""
        return str(self._data["ai"].get("default_profile", "course"))

    @property
    def ai_auto_detect_profile(self) -> bool:
        """Whether the content type may be auto-detected with a cheap AI call."""
        return bool(self._data["ai"].get("auto_detect_profile", True))

    @property
    def ai_profile_sample_chars(self) -> int:
        """Characters of subtitle text sampled for content-type detection."""
        return int(self._data["ai"].get("profile_sample_chars", 2000))

    @property
    def ai_classifier_max_tokens(self) -> int:
        """Output-token budget for the content-type classifier call."""
        return int(self._data["ai"].get("classifier_max_tokens", 128))

    @property
    def ai_classifier_reasoning_effort(self) -> str | None:
        """Reasoning effort for the classifier call (``None`` disables it)."""
        return self._data["ai"].get("classifier_reasoning_effort", "low")

    @property
    def ai_profile_settings(self) -> dict[str, Any]:
        """Raw ``ai.profiles`` mapping (empty dict for legacy configs)."""
        raw = self._data["ai"].get("profiles") or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def api_key(self) -> str:
        """DeepSeek API key from the ``DEEPSEEK_API_KEY`` environment variable.

        Raises:
            RuntimeError: If the variable is not set.
        """
        key = os.getenv("DEEPSEEK_API_KEY", "")
        if not key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY is not set. "
                "Copy .env.example to .env and fill in your key, "
                "or set it as a system environment variable."
            )
        return key

    # -- transcribe (audio/video -> speaker-labelled subtitle) ----------------

    @property
    def transcribe_settings(self) -> dict[str, Any]:
        """Raw ``transcribe`` block (empty dict when the feature is unconfigured)."""
        raw = self._data.get("transcribe") or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def transcribe_source_dir(self) -> Path:
        """Directory scanned by default when transcribing recordings."""
        raw = self.transcribe_settings.get("source_dir", ".")
        return _resolve_path(str(raw), self._project_root)

    @property
    def transcribe_output_dir(self) -> Path:
        """Where generated subtitles go (default: the watch directory)."""
        raw = self.transcribe_settings.get("output_dir")
        if not raw:
            return self.watch_dir
        return _resolve_path(str(raw), self._project_root)

    @property
    def transcribe_extensions(self) -> list[str]:
        """Media extensions picked up when scanning a directory."""
        raw = self.transcribe_settings.get("extensions")
        if not raw:
            return [
                ".mp3", ".mp4", ".wav", ".m4a", ".mkv", ".mov",
                ".flac", ".aac", ".opus", ".wma", ".webm",
            ]
        return [str(ext).lower() for ext in raw]

    @property
    def transcribe_model(self) -> str:
        """faster-whisper model name (large-v3 / medium / small / base ...)."""
        return str(self.transcribe_settings.get("model", "medium"))

    @property
    def transcribe_device(self) -> str:
        """Inference device (cpu / cuda)."""
        return str(self.transcribe_settings.get("device", "cpu"))

    @property
    def transcribe_compute_type(self) -> str:
        """CTranslate2 compute type (int8 / int8_float16 / float16 / float32)."""
        return str(self.transcribe_settings.get("compute_type", "int8"))

    @property
    def transcribe_cpu_threads(self) -> int:
        """CPU threads used by the ASR model."""
        return int(self.transcribe_settings.get("cpu_threads", 8))

    @property
    def transcribe_beam_size(self) -> int:
        """Beam size for decoding (5 = faster-whisper default)."""
        return int(self.transcribe_settings.get("beam_size", 5))

    @property
    def transcribe_long_media_settings(self) -> dict[str, Any]:
        """Raw transcribe.long_media block."""
        raw = self.transcribe_settings.get("long_media") or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def transcribe_long_media_enabled(self) -> bool:
        """Use a faster model automatically for long recordings."""
        return bool(self.transcribe_long_media_settings.get("enabled", False))

    @property
    def transcribe_long_media_threshold_minutes(self) -> float:
        """Recordings at least this long use the fallback model."""
        raw = self.transcribe_long_media_settings.get("threshold_minutes", 30)
        try:
            return max(0.0, float(raw))
        except (TypeError, ValueError):
            return 30.0

    @property
    def transcribe_long_media_model(self) -> str:
        """Model used for long recordings (default: medium)."""
        return str(self.transcribe_long_media_settings.get("model") or "medium")

    @property
    def transcribe_language(self) -> str | None:
        """Forced language (``None`` / ``auto`` = detect per recording)."""
        return self.transcribe_settings.get("language")

    @property
    def transcribe_vad_filter(self) -> bool:
        """Whether voice-activity filtering is applied before decoding."""
        return bool(self.transcribe_settings.get("vad_filter", True))

    @property
    def transcribe_min_silence_ms(self) -> int:
        """Silence length (ms) that splits speech when VAD is enabled."""
        return int(self.transcribe_settings.get("min_silence_ms", 500))

    @property
    def transcribe_word_timestamps(self) -> bool:
        """Word-level timestamps (slower, but makes speaker splits precise)."""
        return bool(self.transcribe_settings.get("word_timestamps", False))

    @property
    def transcribe_output_format(self) -> str:
        """Subtitle format to write: ``vtt`` / ``srt`` / ``both``."""
        return str(self.transcribe_settings.get("output_format", "vtt")).lower()

    @property
    def transcribe_diarization(self) -> dict[str, Any]:
        """Raw ``transcribe.diarization`` block (backend + model settings)."""
        raw = self.transcribe_settings.get("diarization") or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def transcribe_auto_media(self) -> bool:
        """Transcribe audio/video files that land in the watch directory."""
        return bool(self.transcribe_settings.get("auto_transcribe_media", True))

    @property
    def transcribe_media_extensions(self) -> list[str]:
        """Media extensions accepted by the watch directory."""
        raw = self.transcribe_settings.get("extensions") or []
        return [str(item).lower() for item in raw]

    @property
    def transcribe_state_file(self) -> Path:
        """JSON file remembering which recordings were already transcribed."""
        raw = self.transcribe_settings.get("state_file", "output/.transcribed.json")
        return _resolve_path(str(raw), self._project_root)

    # -- fetch (online video -> subtitle) -------------------------------------

    @property
    def fetch_enabled(self) -> bool:
        """Whether the online-video feature is available (GUI page + CLI)."""
        return bool(self.fetch_settings.get("enabled", True))

    @property
    def fetch_settings(self) -> dict[str, Any]:
        """Raw ``fetch`` block (empty dict when the feature is unconfigured)."""
        raw = self._data.get("fetch") or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def fetch_prefer_subtitles(self) -> bool:
        """Prefer the platform's own subtitles over local transcription."""
        return bool(self.fetch_settings.get("prefer_subtitles", True))

    @property
    def fetch_subtitle_languages(self) -> list[str]:
        """Subtitle language preference order."""
        raw = self.fetch_settings.get("subtitle_languages")
        if not raw:
            return ["zh-Hans", "zh-CN", "zh-TW", "zh", "en", "en-US", "en-orig"]
        return [str(item) for item in raw]

    @property
    def fetch_min_subtitle_coverage(self) -> float:
        """Fraction of the video a subtitle track must cover to be usable."""
        raw = self.fetch_settings.get("min_subtitle_coverage", 0.6)
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            return 0.6

    @property
    def fetch_max_height(self) -> int:
        """Maximum video height used when a video has to be downloaded."""
        return int(self.fetch_settings.get("max_height", 1080))

    @property
    def fetch_proxy(self) -> Optional[str]:
        """Proxy for yt-dlp (config value, else the environment)."""
        configured = self.fetch_settings.get("proxy")
        if configured:
            return str(configured)
        return (
            os.getenv("HTTPS_PROXY")
            or os.getenv("https_proxy")
            or os.getenv("ALL_PROXY")
            or os.getenv("all_proxy")
        )

    @property
    def fetch_cookies_file(self) -> Optional[Path]:
        """Netscape cookies.txt used for logged-in subtitle access."""
        raw = self.fetch_settings.get("cookies_file")
        if not raw:
            return None
        return _resolve_path(str(raw), self._project_root)

    @property
    def fetch_cookies_from_browser(self) -> Optional[str]:
        """Browser name to read cookies from (alternative to a cookies file)."""
        raw = self.fetch_settings.get("cookies_from_browser")
        return str(raw) if raw else None

    @property
    def fetch_audio_format(self) -> str:
        """yt-dlp format selector used for the transcription fallback."""
        return str(self.fetch_settings.get("audio_format", "bestaudio/best"))

    def fetch_platform_status(self, platform: str) -> str:
        """Return stable / beta / experimental for *platform*."""
        raw = self.fetch_settings.get("platform_status") or {}
        if isinstance(raw, dict) and platform in raw:
            return str(raw[platform]).lower()
        return "stable" if platform in ("bilibili", "youtube") else "experimental"

    # -- screenshots ----------------------------------------------------------

    @property
    def screenshots_settings(self) -> dict[str, Any]:
        """Raw ``screenshots`` block."""
        raw = self._data.get("screenshots") or {}
        return raw if isinstance(raw, dict) else {}

    @property
    def screenshots_enabled(self) -> bool:
        """Whether notes may embed frames from the source video."""
        return bool(self.screenshots_settings.get("enabled", False))

    @property
    def screenshots_max_count(self) -> int:
        """Upper bound on embedded images per note."""
        return int(self.screenshots_settings.get("max_count", 12))

    @property
    def screenshots_width(self) -> int:
        """Rendered image width in pixels (0 = keep the source resolution)."""
        return int(self.screenshots_settings.get("width", 0))

    @property
    def screenshots_best_frame_window(self) -> int:
        """Seconds to search after a marker for the most detailed frame."""
        return int(self.screenshots_settings.get("best_frame_window", 6))

    @property
    def screenshots_quality(self) -> int:
        """ffmpeg JPEG quality (2 = best, 31 = worst)."""
        return int(self.screenshots_settings.get("quality", 3))

    # -- logging --------------------------------------------------------------

    @property
    def log_file(self) -> Path:
        """Absolute path to the daemon log file."""
        return _resolve_path(self._data["logging"]["file"], self._project_root)

    @property
    def log_level(self) -> str:
        return self._data["logging"].get("level", "INFO")

    @property
    def log_format(self) -> str:
        return self._data["logging"].get(
            "format",
            "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        )

    @property
    def log_date_format(self) -> str:
        return self._data["logging"].get("date_format", "%Y-%m-%d %H:%M:%S")

    @property
    def log_rotation(self) -> str:
        return self._data["logging"].get("rotation", "midnight")

    @property
    def log_backup_count(self) -> int:
        return self._data["logging"].get("backup_count", 30)

    # -- state ----------------------------------------------------------------

    @property
    def state_file(self) -> Path:
        """Absolute path to the JSON file tracking processed SRT files."""
        return _resolve_path(self._data["state_file"], self._project_root)

    # -- misc -----------------------------------------------------------------

    @property
    def project_root(self) -> Path:
        """Absolute path to the project root directory."""
        return self._project_root


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(config_path: Optional[str | Path] = None) -> Config:
    """Load the full daemon configuration.

    Args:
        config_path: Optional explicit path to ``config.yaml``.  When *None*
            the file is discovered automatically relative to the project root.

    Returns:
        A fully-resolved :class:`Config` instance.

    Raises:
        FileNotFoundError: If ``config.yaml`` cannot be found.
        RuntimeError: If ``DEEPSEEK_API_KEY`` is not set.
    """
    # 1. Locate project root & config file
    if config_path:
        config_path = Path(config_path).resolve()
        project_root = _find_project_root()
    else:
        project_root = _find_project_root()
        # Prefer config in src/srt_daemon/; fall back to project root
        config_path = project_root / "src" / "srt_daemon" / "config.yaml"
        if not config_path.exists():
            config_path = project_root / "config.yaml"

    # 2. Load .env
    env_file = project_root / ".env"
    if env_file.exists():
        # utf-8-sig: Windows editors (Notepad, PowerShell Set-Content) like to
        # write a BOM, which would otherwise hide the first key.
        load_dotenv(env_file, encoding="utf-8-sig")
    else:
        load_dotenv(encoding="utf-8-sig")  # search cwd upwards

    # 3. Parse YAML
    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    return Config(raw, project_root)
