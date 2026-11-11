"""Content-type profiles (课程 / 会议 / 访谈) for the subtitle daemon.

Different source material needs differently shaped notes: a course wants a
knowledge tree with code blocks and a glossary, a meeting wants decisions,
action items with owners and open questions, an interview wants per-speaker
viewpoints and a topic thread.

A *profile* bundles the system-prompt template used for the AI call, the
file-name hints that force it, and the label shown in logs and in the
Markdown front matter.

Resolution order for every processed file:

1. explicit ``--profile`` flag (CLI)
2. file-name hint (``[会议]standup.vtt``, ``standup.meeting.srt``, ``访谈-xxx.vtt``)
3. automatic content classification (cheap AI call over a text sample)
4. ``ai.default_profile`` (defaults to ``course``)

Usage::

    from src.srt_daemon.profiles import ProfileRegistry, resolve_profile

    registry = ProfileRegistry(config)
    resolution = resolve_profile(config, "standup.vtt", text, registry=registry)
    print(resolution.profile.id, resolution.source)
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from src.srt_daemon.config import Config
from src.srt_daemon.logger import get_logger

log = get_logger(__name__)

#: Profile used when nothing else decides.
DEFAULT_PROFILE = "course"

#: Built-in defaults — merged with (and overridable by) ``ai.profiles``.
BUILTIN_PROFILES: dict[str, dict[str, object]] = {
    "course": {
        "label": "课程/教程",
        "prompt_file": "doc/prompts/course.md",
        "filename_hints": [
            "课程", "教程", "课堂", "讲座", "教学",
            "lecture", "course", "tutorial", "lesson", "class",
        ],
    },
    "meeting": {
        "label": "会议",
        "prompt_file": "doc/prompts/meeting.md",
        "filename_hints": [
            "会议", "例会", "周会", "晨会", "评审", "复盘", "对齐",
            "meeting", "standup", "sync", "review", "retro",
        ],
    },
    "interview": {
        "label": "访谈/播客",
        "prompt_file": "doc/prompts/interview.md",
        "filename_hints": [
            "访谈", "采访", "播客", "对话", "连麦",
            "interview", "podcast", "talk", "conversation",
        ],
    },
}


class UnknownProfileError(ValueError):
    """Raised when a profile id is not present in the registry."""


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Profile:
    """A content-type profile.

    Attributes:
        id: Short identifier (``course`` / ``meeting`` / ``interview``).
        label: Human-readable label (used in logs and front matter).
        prompt_file: Absolute path to the system-prompt template.
        filename_hints: Substrings that force this profile when found in a
            file name (case-insensitive, matched against the stem).
    """

    id: str
    label: str
    prompt_file: Path
    filename_hints: tuple[str, ...] = ()

    def matches_filename(self, filename: str) -> bool:
        """True when *filename* contains one of this profile's hints."""
        return matched_hint(self, filename) is not None


@dataclass(frozen=True)
class ProfileResolution:
    """How the profile for one file was decided.

    Attributes:
        profile: The chosen profile.
        source: ``cli``, ``filename``, ``content`` or ``default``.
        detail: Human-readable explanation for the log line.
    """

    profile: Profile
    source: str
    detail: str = ""

    @property
    def forced(self) -> bool:
        """True when the user pinned the profile with ``--profile``."""
        return self.source == "cli"


def matched_hint(profile: Profile, filename: str) -> Optional[str]:
    """Return the file-name hint of *profile* present in *filename*, if any."""
    stem = Path(filename).stem.lower()
    for hint in profile.filename_hints:
        if hint.lower() in stem:
            return hint
    return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ProfileRegistry:
    """All profiles the daemon may use, built from the configuration.

    When ``ai.profiles`` is absent (legacy configuration) the registry
    exposes a single profile backed by ``ai.system_prompt_file``, so old
    installs keep working unchanged.
    """

    def __init__(self, config: Config) -> None:
        self._config = config
        self._profiles: dict[str, Profile] = self._build_profiles()
        self._default_id = self._resolve_default_id()
        log.debug(
            "ProfileRegistry: %s (default=%s)",
            ", ".join(
                f"{p.id}={p.prompt_file.name}" for p in self._profiles.values()
            ),
            self._default_id,
        )

    # -- public API -----------------------------------------------------------

    @property
    def default_id(self) -> str:
        """Id of the profile used as the fallback."""
        return self._default_id

    def default(self) -> Profile:
        """The fallback profile."""
        return self._profiles[self._default_id]

    def ids(self) -> list[str]:
        """Profile ids, in configuration order."""
        return list(self._profiles)

    def all(self) -> list[Profile]:
        """All profiles, in configuration order."""
        return list(self._profiles.values())

    def find(self, profile_id: Optional[str]) -> Optional[Profile]:
        """Return the profile with *profile_id*, or nothing if unknown."""
        if not profile_id:
            return None
        return self._profiles.get(str(profile_id).strip().lower())

    def get(self, profile_id: str) -> Profile:
        """Return the profile with *profile_id*.

        Raises:
            UnknownProfileError: If the id is not registered.
        """
        profile = self.find(profile_id)
        if profile is None:
            raise UnknownProfileError(
                f"Unknown profile {profile_id!r}. "
                f"Available: {', '.join(self._profiles)}"
            )
        return profile

    def for_filename(self, filename: str) -> Optional[Profile]:
        """Return the profile whose file-name hint matches *filename*."""
        for profile in self._profiles.values():
            if profile.matches_filename(filename):
                return profile
        return None

    def __len__(self) -> int:
        return len(self._profiles)

    def __iter__(self):
        return iter(self._profiles.values())

    # -- internal -------------------------------------------------------------

    def _build_profiles(self) -> dict[str, Profile]:
        root = self._config.project_root
        settings = self._config.ai_profile_settings

        if not settings:
            # Legacy single-prompt configuration: exactly one profile.
            profile_id = str(
                self._config.ai_default_profile or DEFAULT_PROFILE
            ).strip().lower()
            builtin = BUILTIN_PROFILES.get(
                profile_id, BUILTIN_PROFILES[DEFAULT_PROFILE]
            )
            return {
                profile_id: Profile(
                    id=profile_id,
                    label=str(builtin["label"]),
                    prompt_file=self._config.ai_system_prompt_file,
                    filename_hints=tuple(builtin["filename_hints"]),  # type: ignore[arg-type]
                )
            }

        profiles: dict[str, Profile] = {}
        for raw_id, raw_cfg in settings.items():
            profile_id = str(raw_id).strip().lower()
            if not profile_id:
                continue
            cfg = raw_cfg if isinstance(raw_cfg, dict) else {}
            builtin = BUILTIN_PROFILES.get(profile_id, {})
            prompt_raw = (
                cfg.get("prompt_file")
                or builtin.get("prompt_file")
                or self._config.ai_system_prompt_file
            )
            hints = cfg.get("filename_hints")
            if hints is None:
                hints = builtin.get("filename_hints") or ()
            profiles[profile_id] = Profile(
                id=profile_id,
                label=str(cfg.get("label") or builtin.get("label") or profile_id),
                prompt_file=_resolve_path(str(prompt_raw), root),
                filename_hints=tuple(str(h) for h in hints),
            )

        return profiles

    def _resolve_default_id(self) -> str:
        wanted = str(
            self._config.ai_default_profile or DEFAULT_PROFILE
        ).strip().lower()
        if wanted in self._profiles:
            return wanted
        if DEFAULT_PROFILE in self._profiles:
            log.warning(
                "Configured default profile %r is not defined — using %r",
                wanted,
                DEFAULT_PROFILE,
            )
            return DEFAULT_PROFILE
        fallback = next(iter(self._profiles))
        log.warning(
            "Configured default profile %r is not defined — using %r",
            wanted,
            fallback,
        )
        return fallback


def _resolve_path(raw: str, project_root: Path) -> Path:
    """Resolve *raw* against *project_root* unless it is already absolute."""
    path = Path(raw)
    return path if path.is_absolute() else (project_root / path).resolve()


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def _sample_text(text: str, limit: int) -> str:
    """Return a head+middle sample of *text* for classification.

    The head usually carries the introduction (``欢迎来到本节课`` vs
    ``今天的会议议程``); sampling the middle as well guards against
    meetings that open with small talk.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    half = limit // 2
    head = text[:half]
    mid_start = max(0, len(text) // 2 - half // 2)
    middle = text[mid_start : mid_start + half]
    return f"{head}\n...\n{middle}"


def resolve_profile(
    config: Config,
    filename: str,
    text: str,
    *,
    registry: Optional[ProfileRegistry] = None,
    forced: Optional[str] = None,
    classifier: Optional[Callable[[str, Sequence[Profile]], Optional[str]]] = None,
) -> ProfileResolution:
    """Decide which profile to use for one subtitle file.

    Args:
        config: Resolved configuration (supplies the default profile and the
            auto-detection switch).
        filename: Source file name (used for the hint check).
        text: Extracted subtitle text (used for content classification).
        registry: Optional pre-built registry (built on demand otherwise).
        forced: Profile id requested by the user (CLI ``--profile``).
        classifier: Callable ``(sample, candidates) -> profile_id or None``
            used for content classification — typically
            :meth:`AiClient.classify_profile`.

    Returns:
        A :class:`ProfileResolution`.  An unknown *forced* id never
        raises: it logs a warning and falls through to auto-detection.
    """
    registry = registry or ProfileRegistry(config)

    # 1. Explicit user choice wins.
    if forced:
        profile = registry.find(forced)
        if profile is not None:
            return ProfileResolution(profile, "cli", f"--profile {profile.id}")
        log.warning(
            "Unknown --profile %r (available: %s) — falling back to auto-detection",
            forced,
            ", ".join(registry.ids()),
        )

    # 2. File-name hint.
    for candidate in registry.all():
        hint = matched_hint(candidate, filename)
        if hint is not None:
            return ProfileResolution(
                candidate, "filename", f"file name contains {hint!r}"
            )

    # 3. Content classification (one cheap API call).
    if classifier is not None and config.ai_auto_detect_profile and text.strip():
        sample = _sample_text(text, config.ai_profile_sample_chars)
        try:
            detected = classifier(sample, registry.all())
        except Exception:
            log.warning(
                "Content-type detection failed — using the default profile",
                exc_info=True,
            )
            detected = None
        profile = registry.find(detected)
        if profile is not None:
            return ProfileResolution(
                profile, "content", f"classified from content as {profile.id}"
            )

    # 4. Configured default.
    default = registry.default()
    return ProfileResolution(default, "default", f"ai.default_profile={default.id}")
