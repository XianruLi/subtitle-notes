"""Online video -> subtitle (or transcript) -> Markdown note.

Resolves a video URL with **yt-dlp** (Python API, no console windows), prefers
the platform's own subtitles — they are instant and free — and only falls back
to downloading the audio and running the local faster-whisper model when no
subtitle track exists.

Platform status (fetch.platform_status in config.yaml):

* bilibili — stable.  CC subtitles require a logged-in cookies file
  (SESSDATA); without one the video is transcribed instead.
* youtube — stable.  Automatic captions usually work without login.
* douyin / kuaishou — experimental: cookies and a current yt-dlp
  are usually required.

Usage::

    python -m src.srt_daemon.fetch https://www.bilibili.com/video/BV1xx411c7mD
    python -m src.srt_daemon.fetch --dry-run https://youtu.be/xxxxxxxxxxx
    python -m src.srt_daemon.fetch --part 2 --profile course <url>
    python -m src.srt_daemon.fetch --cookies cookies.txt <url>
"""

from __future__ import annotations

import html
import json
import re
import sys
import tempfile
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Sequence

from src.srt_daemon import __version__
from src.srt_daemon.config import Config, load_config
from src.srt_daemon.logger import get_logger, setup_logging
from src.srt_daemon.pipeline import NotesPipelineError

log = get_logger(__name__)

#: URL host pattern -> platform id.
PLATFORM_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"bilibili\.com|b23\.tv", "bilibili"),
    (r"youtube\.com|youtu\.be|youtube-nocookie\.com", "youtube"),
    (r"douyin\.com|iesdouyin\.com", "douyin"),
    (r"kuaishou\.com", "kuaishou"),
)

#: Bilibili video id.
_BILIBILI_BVID = re.compile(r"(BV[0-9A-Za-z]{10})")

#: Browser user agent used for the Bilibili JSON API.
_BILIBILI_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

#: Characters Windows forbids in file names.
_WIN_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: How many times the Bilibili subtitle API is polled when it returns junk.
_BILIBILI_SUBTITLE_ATTEMPTS = 2

#: Backoff (seconds) between download attempts.
_DOWNLOAD_RETRY_DELAYS: tuple[int, ...] = (5, 20)

#: Preferred subtitle container formats, best first.
_SUBTITLE_EXT_ORDER = ("vtt", "srt", "json3", "json", "ass", "srv3", "ttml")


class FetchError(RuntimeError):
    """Raised when a URL cannot be resolved, downloaded or transcribed."""


# ---------------------------------------------------------------------------
# Small helpers (unit-tested)
# ---------------------------------------------------------------------------


def platform_of(url: str) -> str:
    """Return the platform id for *url* (other when unknown)."""
    lowered = url.lower()
    for pattern, name in PLATFORM_PATTERNS:
        if re.search(pattern, lowered):
            return name
    return "other"


def safe_filename(title: str, *, max_length: int = 80) -> str:
    """Turn a video title into a usable Windows file name stem."""
    text = _WIN_FORBIDDEN.sub(" ", title or "video")
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > max_length:
        text = text[:max_length].rstrip(" .")
    return text or "video"


def force_utf8_console() -> None:
    """Make the Windows console print CJK titles instead of crashing.

    The default console code page here is GBK, so printing a title that
    contains characters outside GBK raises UnicodeEncodeError.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def match_language(available: dict[str, Any], wanted: Sequence[str]) -> Optional[str]:
    """Pick the best language key from *available*.

    The preference order is honoured strictly: for each wanted language the
    exact key is tried first, then prefix matches (zh -> zh-Hant),
    and only when nothing matches is any track taken so that a subtitle is
    never silently ignored.
    """
    if not available:
        return None
    for language in wanted:
        if language in available:
            return language
        for key in sorted(available):
            if key.startswith(language):
                return key
    return sorted(available)[0]


def pick_subtitle(
    info: dict[str, Any], wanted: Sequence[str]
) -> Optional[tuple[str, str]]:
    """Return (kind, language) for the best subtitle track, or None.

    Manual subtitles always win over automatic captions.
    """
    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}

    language = match_language(manual, wanted)
    if language is not None:
        return "manual", language
    language = match_language(auto, wanted)
    if language is not None:
        return "auto", language
    return None


def _format_vtt_timestamp(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    millis = int(round(seconds * 1000))
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def convert_bilibili_json(payload: dict[str, Any]) -> str:
    """Convert Bilibili subtitle JSON to WebVTT text.

    Bilibili ships {"body": [{"from": 1.2, "to": 3.4, "content": "..."}]}.
    """
    body = payload.get("body") or payload.get("data", {}).get("body") or []
    lines = ["WEBVTT", ""]
    index = 0
    for item in body:
        text = html.unescape(str(item.get("content", "")).strip())
        if not text:
            continue
        index += 1
        start = _format_vtt_timestamp(item.get("from", 0))
        end = _format_vtt_timestamp(item.get("to", item.get("from", 0)))
        lines.append(f"{start} --> {end}")
        lines.append(text)
        lines.append("")
    return "\n".join(lines)


def convert_youtube_json3(payload: dict[str, Any]) -> str:
    """Convert YouTube json3 captions to WebVTT text."""
    lines = ["WEBVTT", ""]
    for event in payload.get("events") or []:
        segments = event.get("segs") or []
        text = "".join(str(seg.get("utf8", "")) for seg in segments).strip()
        if not text:
            continue
        start = event.get("tStartMs", 0) / 1000.0
        end = start + event.get("dDurationMs", 0) / 1000.0
        lines.append(f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}")
        lines.append(html.unescape(text))
        lines.append("")
    return "\n".join(lines)


def parse_bilibili_bvid(url: str) -> Optional[str]:
    """Extract the BV... id from a Bilibili URL."""
    match = _BILIBILI_BVID.search(url or "")
    return match.group(1) if match else None


def parse_bilibili_page(url: str) -> int:
    """Return the 0-based part index from ?p=N (0 when absent)."""
    match = re.search(r"[?&]p=(\d+)", url or "")
    if match:
        return max(0, int(match.group(1)) - 1)
    return 0


def load_cookie_header(cookies_file: Optional[Path]) -> str:
    """Read a Netscape cookies.txt into a k=v; k2=v2 header string."""
    if not cookies_file:
        return ""
    path = Path(cookies_file)
    if not path.exists():
        return ""
    pairs: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("#") or line.count("\t") < 6:
            continue
        fields = line.split("\t")
        name, value = fields[5], fields[6]
        if name and value:
            pairs.append(f"{name}={value}")
    return "; ".join(pairs)


def pick_bilibili_track(
    tracks: Sequence[dict[str, Any]], wanted: Sequence[str]
) -> Optional[dict[str, Any]]:
    """Choose the best Bilibili CC track.

    Bilibili ships uploader CC (zh-CN) and machine (ai-zh) tracks.  The
    language preference wins first; at equal preference a human track beats the
    machine one.
    """
    if not tracks:
        return None

    def score(track: dict[str, Any]) -> tuple[int, int]:
        language = str(track.get("lan", "")).lower()
        is_machine = language.startswith("ai-")
        base = language[3:] if is_machine else language
        rank = len(wanted)
        for index, preference in enumerate(wanted):
            if base == preference.lower() or base.startswith(preference.lower()):
                rank = index
                break
        return (rank, 1 if is_machine else 0)

    return sorted(tracks, key=score)[0]


def subtitle_is_plausible(
    payload: dict[str, Any], duration: float
) -> bool:
    """Guard against the wrong subtitle object coming back from the CDN.

    Bilibili's ai-zh subtitle CDN has been observed returning an unrelated
    video's cues for a signed URL.  A track that runs far past the video's own
    duration cannot belong to it.
    """
    cues = payload.get("body") or []
    if not cues:
        return False
    try:
        last_end = max(float(cue.get("to", 0) or 0) for cue in cues)
        first_start = min(float(cue.get("from", 0) or 0) for cue in cues)
    except (TypeError, ValueError):
        return False
    if duration > 0 and last_end > duration + 60:
        return False
    if duration > 0 and first_start > max(60.0, duration * 0.25):
        return False
    return True


def subtitle_coverage(payload: dict[str, Any], duration: float) -> float:
    """How much of the video the subtitle track covers (0.0 - 1.0).

    Bilibili's ai-zh tracks are sometimes partial: one 20-minute lecture had
    cues for only the first 18 %, so a note built from it would silently
    describe a fraction of the video.
    """
    cues = payload.get("body") or []
    if not cues:
        return 0.0
    if duration <= 0:
        return 1.0
    try:
        last_end = max(float(cue.get("to", 0) or 0) for cue in cues)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, last_end / duration))


def has_subtitle_entries(path: Path) -> bool:
    """True when *path* parses to at least one subtitle entry.

    A music-only or silent clip can transcribe to zero cues — that must be a
    clean error, not an exception deep inside the notes pipeline.
    """
    from src.srt_daemon.subtitle_parser import UnsupportedSubtitleFormat, parse_subtitle

    try:
        return bool(parse_subtitle(path).entries)
    except (UnsupportedSubtitleFormat, OSError, ValueError):
        return False


def convert_subtitle_file(source: Path, target: Path) -> Optional[Path]:
    """Normalise a downloaded subtitle into WebVTT at *target*.

    .vtt and .srt are copied as-is (both parsers accept them);
    Bilibili JSON and YouTube json3 are converted.  Unsupported containers
    return None so the caller can fall back to transcription.
    """
    suffix = source.suffix.lower()
    if suffix in (".vtt", ".srt"):
        target.write_text(source.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
        return target
    if suffix == ".json3":
        payload = json.loads(source.read_text(encoding="utf-8", errors="replace"))
        target.write_text(convert_youtube_json3(payload), encoding="utf-8")
        return target
    if suffix == ".json":
        payload = json.loads(source.read_text(encoding="utf-8", errors="replace"))
        target.write_text(convert_bilibili_json(payload), encoding="utf-8")
        return target
    return None


# ---------------------------------------------------------------------------
# Data holders
# ---------------------------------------------------------------------------


@dataclass
class FetchTarget:
    """What a URL resolves to before downloading anything."""

    url: str
    target_url: str
    title: str
    platform: str
    duration: float = 0.0
    uploader: str = ""
    part_index: int = 0
    parts: list[str] = field(default_factory=list)
    part_urls: list[str] = field(default_factory=list)
    subtitles: dict[str, Any] = field(default_factory=dict)
    automatic_captions: dict[str, Any] = field(default_factory=dict)


@dataclass
class FetchOutcome:
    """Result of turning one URL into a note."""

    url: str
    note_path: Path
    title: str
    platform: str
    source: str  # "subtitles" | "transcript"
    elapsed: float
    profile: str
    speaker_mode: str


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------


class Fetcher:
    """Resolves URLs, grabs subtitles (or audio) and produces notes."""

    def __init__(
        self,
        config: Config,
        *,
        proxy: Optional[str] = None,
        cookies_file: Optional[str] = None,
        cookies_from_browser: Optional[str] = None,
        model: Optional[str] = None,
        prefer_subtitles: Optional[bool] = None,
        diarizer: Any = None,
        screenshots: Optional[bool] = None,
    ) -> None:
        self._config = config
        self._proxy = proxy if proxy is not None else config.fetch_proxy
        self._cookies_file = (
            cookies_file if cookies_file is not None else config.fetch_cookies_file
        )
        self._cookies_from_browser = (
            cookies_from_browser
            if cookies_from_browser is not None
            else config.fetch_cookies_from_browser
        )
        # None means "use the configured model" — that also lets the
        # long-recording policy pick a faster one.
        self._model = model
        self._prefer_subtitles = (
            config.fetch_prefer_subtitles
            if prefer_subtitles is None
            else prefer_subtitles
        )
        self._languages = list(config.fetch_subtitle_languages)
        self._diarizer = diarizer
        self._screenshots = screenshots
        self._ydl_logger = _YdlLogger()

    # -- yt-dlp plumbing ------------------------------------------------------

    def _base_options(self) -> dict[str, Any]:
        """Common yt-dlp options (network, auth, quiet logging)."""
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "socket_timeout": 30,
            "retries": 5,
            "extractor_retries": 2,
            "logger": self._ydl_logger,
        }
        if self._proxy:
            options["proxy"] = self._proxy
        if self._cookies_file:
            cookie_path = Path(self._cookies_file)
            if cookie_path.exists():
                options["cookiefile"] = str(cookie_path)
            else:
                log.warning("Cookies file not found: %s (continuing without)", cookie_path)
        elif self._cookies_from_browser:
            options["cookiesfrombrowser"] = (self._cookies_from_browser,)
        return options

    def _download_with_retry(
        self, options: dict[str, Any], url: str
    ) -> dict[str, Any]:
        """Run one yt-dlp download, retrying transient rejections.

        Bilibili answers the CDN with HTTP 567 when it throttles an IP; a short
        backoff usually turns that into a success.
        """
        from yt_dlp import YoutubeDL

        last_error: Optional[Exception] = None
        for attempt in range(1, len(_DOWNLOAD_RETRY_DELAYS) + 2):
            try:
                with YoutubeDL(options) as ydl:
                    return ydl.extract_info(url, download=True) or {}
            except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
                last_error = exc
                if attempt <= len(_DOWNLOAD_RETRY_DELAYS):
                    wait = _DOWNLOAD_RETRY_DELAYS[attempt - 1]
                    log.warning(
                        "Download failed (attempt %d): %s — retrying in %ds",
                        attempt,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
        detail = self._ydl_logger.last_problem
        message = " ".join(f"{last_error} {detail}".split())
        raise FetchError(message)

    # -- Bilibili CC subtitles (yt-dlp does not expose them) ----------------

    def _cookie_header(self) -> str:
        """Cookie header built from the configured cookies file."""
        if not self._cookies_file:
            return ""
        return load_cookie_header(Path(self._cookies_file))

    def _http_json(self, url: str, *, referer: str = "https://www.bilibili.com/") -> dict[str, Any]:
        """GET *url* with the saved cookies and parse JSON."""
        headers = {"User-Agent": _BILIBILI_UA, "Referer": referer}
        cookie = self._cookie_header()
        if cookie:
            headers["Cookie"] = cookie
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))

    def bilibili_subtitles(self, target: FetchTarget, workdir: Path) -> Optional[Path]:
        """Fetch Bilibili CC (uploader or ai-zh) subtitles directly.

        yt-dlp's Bilibili extractor reports no subtitle tracks at all, but the
        JSON API exposes them once a logged-in cookies file is available.
        """
        bvid = parse_bilibili_bvid(target.target_url) or parse_bilibili_bvid(target.url)
        if not bvid:
            return None
        if not self._cookie_header():
            log.info("Bilibili CC subtitles need a cookies file — skipping the direct path")
            return None

        payload: Optional[dict[str, Any]] = None
        track: Optional[dict[str, Any]] = None
        for attempt in range(1, _BILIBILI_SUBTITLE_ATTEMPTS + 1):
            try:
                payload, track = self._bilibili_track(bvid, target)
            except Exception as exc:  # noqa: BLE001 - network/JSON problems are non-fatal
                log.warning("Bilibili subtitle API failed: %s", exc)
                return None
            if payload is None or track is None:
                return None
            coverage = subtitle_coverage(payload, target.duration)
            minimum = self._config.fetch_min_subtitle_coverage
            if subtitle_is_plausible(payload, target.duration) and coverage >= minimum:
                break
            log.warning(
                "Bilibili subtitle unusable for %s: %d cue(s), covers %.0f%% "
                "(need %.0f%%) — attempt %d/%d",
                bvid,
                len(payload.get("body") or []),
                coverage * 100,
                minimum * 100,
                attempt,
                _BILIBILI_SUBTITLE_ATTEMPTS,
            )
            time.sleep(1)
        else:
            log.warning(
                "Bilibili has no usable CC for %s — falling back to transcription", bvid
            )
            return None

        vtt = convert_bilibili_json(payload or {})
        output = workdir / "bilibili.vtt"
        output.write_text(vtt, encoding="utf-8")
        first_line = next(
            (line for line in vtt.splitlines() if line and "-->" not in line and not line.startswith("WEBVTT")),
            "",
        )
        log.info(
            "Bilibili CC ready: %s (%s) — starts with %r",
            (track or {}).get("lan_doc"),
            (track or {}).get("lan"),
            first_line[:40],
        )
        return output if has_subtitle_entries(output) else None

    def _bilibili_track(
        self, bvid: str, target: FetchTarget
    ) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
        """Resolve the subtitle JSON payload and its track metadata."""
        view = self._http_json(
            f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
        )
        data = view.get("data") or {}
        pages = data.get("pages") or []
        index = parse_bilibili_page(target.target_url)
        if pages:
            index = min(index, len(pages) - 1)
            cid = pages[index].get("cid")
        else:
            cid = data.get("cid")
        if not cid:
            return None, None

        player = self._http_json(
            f"https://api.bilibili.com/x/player/v2?bvid={bvid}&cid={cid}"
        )
        tracks = (
            ((player.get("data") or {}).get("subtitle") or {}).get("subtitles") or []
        )
        if not tracks:
            log.info("Bilibili reports no CC subtitles for this video")
            return None, None

        track = pick_bilibili_track(tracks, self._languages)
        if not track:
            return None, None
        subtitle_url = str(track.get("subtitle_url") or "")
        if subtitle_url.startswith("//"):
            subtitle_url = "https:" + subtitle_url
        if not subtitle_url:
            return None, None
        return self._http_json(subtitle_url), track

    def describe_bilibili_cc(self, target: FetchTarget) -> None:
        """Print what Bilibili's CC API offers (used by --dry-run)."""
        bvid = parse_bilibili_bvid(target.target_url) or parse_bilibili_bvid(target.url)
        if not bvid:
            return
        if not self._cookie_header():
            print("bilibili cc: unknown (needs a cookies file)")
            return
        try:
            payload, track = self._bilibili_track(bvid, target)
        except Exception as exc:  # noqa: BLE001
            print(f"bilibili cc: api error ({exc})")
            return
        if payload is None or track is None:
            print("bilibili cc: none")
            return
        coverage = subtitle_coverage(payload, target.duration)
        usable = (
            subtitle_is_plausible(payload, target.duration)
            and coverage >= self._config.fetch_min_subtitle_coverage
        )
        print(
            "bilibili cc: {} ({}) - {} cue(s), covers {:.0f}% -> {}".format(
                track.get("lan_doc"),
                track.get("lan"),
                len(payload.get("body") or []),
                coverage * 100,
                "usable" if usable else "NOT usable, transcription instead",
            )
        )

    def probe(self, url: str) -> FetchTarget:
        """Resolve *url* quickly, without downloading.

        Playlists (Bilibili 分P, YouTube playlists) are extracted **flat**:
        resolving every entry's full metadata took 224 s for an 88-part video,
        while the flat list takes ~2 s.  The chosen part is then resolved on its
        own by :meth:resolve_part.
        """
        platform = platform_of(url)
        if platform == "bilibili":
            target = self._bilibili_parts(url)
            if target is not None:
                return target
        return self._extract(url, flat=True)

    def _bilibili_parts(self, url: str) -> Optional[FetchTarget]:
        """Build the part list from Bilibili's own API (titles + durations).

        yt-dlp's flat extraction only returns URLs, so the parts would show up
        as "Part 1 / Part 2 ...".  One request to the view API gives the real
        section titles and durations.
        """
        bvid = parse_bilibili_bvid(url)
        if not bvid:
            return None
        try:
            data = self._http_json(
                f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
            ).get("data") or {}
        except Exception as exc:  # noqa: BLE001 - fall back to yt-dlp
            log.debug("Bilibili view API failed: %s", exc)
            return None

        pages = data.get("pages") or []
        if not pages:
            return None

        titles = [
            str(page.get("part") or f"Part {position + 1}")
            for position, page in enumerate(pages)
        ]
        urls = [
            f"https://www.bilibili.com/video/{bvid}?p={position + 1}"
            for position in range(len(pages))
        ]
        first = pages[0]
        return FetchTarget(
            url=url,
            target_url=urls[0],
            title=titles[0],
            platform="bilibili",
            duration=float(first.get("duration") or 0.0),
            uploader=str((data.get("owner") or {}).get("name") or ""),
            part_index=0,
            parts=titles,
            part_urls=urls,
        )

    def _extract(self, url: str, *, flat: bool = False) -> FetchTarget:
        """Run yt-dlp metadata extraction and convert it to a FetchTarget."""
        from yt_dlp import YoutubeDL

        options = {**self._base_options(), "skip_download": True}
        if flat:
            options["extract_flat"] = "in_playlist"
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as exc:  # noqa: BLE001 - yt-dlp raises many types
            raise FetchError(f"Cannot resolve {url}: {exc}") from exc

        if not info:
            raise FetchError(f"No metadata returned for {url}")

        return self._to_target(url, info)

    def resolve_part(self, target: FetchTarget, part: int = 0) -> FetchTarget:
        """Fully resolve one part (duration, subtitles) of a flat playlist."""
        if not target.parts:
            if target.duration > 0:
                return target
            return self._extract(target.target_url or target.url)

        index = min(max(part, 0), len(target.parts) - 1)
        part_url = (
            target.part_urls[index]
            if len(target.part_urls) > index
            else target.target_url
        )
        resolved = self._extract(part_url)
        resolved.url = target.url
        resolved.part_index = index
        resolved.parts = list(target.parts)
        resolved.part_urls = list(target.part_urls)
        resolved.title = target.parts[index] or resolved.title
        return resolved

    def _to_target(self, url: str, info: dict[str, Any], part: int = 0) -> FetchTarget:
        entries = [entry for entry in (info.get("entries") or []) if entry]
        if entries:
            index = min(max(part, 0), len(entries) - 1)
            entry = entries[index]
            parts = [
                str(item.get("title") or f"Part {position + 1}")
                for position, item in enumerate(entries)
            ]
            part_urls = [
                str(item.get("url") or item.get("webpage_url") or url)
                for item in entries
            ]
            return FetchTarget(
                url=url,
                target_url=str(entry.get("webpage_url") or url),
                title=str(entry.get("title") or info.get("title") or "video"),
                platform=platform_of(url),
                duration=float(entry.get("duration") or 0.0),
                uploader=str(entry.get("uploader") or ""),
                part_index=index,
                parts=parts,
                part_urls=part_urls,
                subtitles=entry.get("subtitles") or {},
                automatic_captions=entry.get("automatic_captions") or {},
            )

        return FetchTarget(
            url=url,
            target_url=str(info.get("webpage_url") or url),
            title=str(info.get("title") or "video"),
            platform=platform_of(url),
            duration=float(info.get("duration") or 0.0),
            uploader=str(info.get("uploader") or ""),
            subtitles=info.get("subtitles") or {},
            automatic_captions=info.get("automatic_captions") or {},
        )

    # -- downloads ------------------------------------------------------------

    def download_subtitle(self, target: FetchTarget, workdir: Path) -> Optional[Path]:
        """Download the best subtitle track and normalise it to WebVTT."""
        if target.platform == "bilibili":
            direct = self.bilibili_subtitles(target, workdir)
            if direct is not None:
                return direct

        choice = pick_subtitle(
            {
                "subtitles": target.subtitles,
                "automatic_captions": target.automatic_captions,
            },
            self._languages,
        )
        if choice is None:
            log.info("No subtitles available for %s", target.title)
            return None

        kind, language = choice
        log.info("Downloading %s subtitles (%s)", kind, language)
        options = {
            **self._base_options(),
            "skip_download": True,
            "writesubtitles": kind == "manual",
            "writeautomaticsub": kind == "auto",
            "subtitleslangs": [language],
            "subtitlesformat": "/".join(_SUBTITLE_EXT_ORDER),
            "outtmpl": str(workdir / "%(id)s.%(ext)s"),
            "paths": {"home": str(workdir)},
        }
        try:
            info = self._download_with_retry(options, target.target_url)
        except FetchError as exc:
            log.warning("Subtitle download failed (%s) — will transcribe instead", exc)
            return None

        candidates = self._find_subtitle_files(workdir, language, info)
        for candidate in candidates:
            converted = convert_subtitle_file(candidate, workdir / "subtitle.vtt")
            if converted is not None:
                log.info("Subtitle ready: %s", candidate.name)
                return converted
        log.warning("Downloaded subtitle could not be converted — transcribing instead")
        return None

    @staticmethod
    def _find_subtitle_files(
        workdir: Path, language: str, info: Optional[dict[str, Any]]
    ) -> list[Path]:
        """Locate the subtitle file yt-dlp just wrote."""
        found: list[Path] = []

        requested = (info or {}).get("requested_subtitles") or {}
        entry = requested.get(language) or {}
        filename = entry.get("_filename")
        if filename and Path(filename).exists():
            found.append(Path(filename))

        for pattern in (f"*.{language}.*", "*.vtt", "*.srt", "*.json3", "*.json"):
            for path in workdir.glob(pattern):
                if path not in found and path.suffix.lower() in (
                    ".vtt", ".srt", ".json3", ".json",
                ):
                    found.append(path)
        return found

    def download_audio(self, target: FetchTarget, workdir: Path) -> Path:
        """Download the best audio stream for transcription."""
        log.info("Downloading audio for transcription: %s", target.title)
        options = {
            **self._base_options(),
            "format": self._config.fetch_audio_format,
            "outtmpl": str(workdir / "%(id)s.%(ext)s"),
            "paths": {"home": str(workdir)},
            "noplaylist": True,
            "progress_hooks": [_ProgressLogger()],
        }
        try:
            info = self._download_with_retry(options, target.target_url)
        except FetchError as exc:
            raise FetchError(f"Audio download failed: {exc}") from exc

        downloads = (info or {}).get("requested_downloads") or []
        if downloads and downloads[0].get("filepath"):
            path = Path(downloads[0]["filepath"])
            if path.exists():
                return path

        for pattern in ("*.m4a", "*.webm", "*.mp3", "*.opus", "*.wav", "*.mp4"):
            for path in workdir.glob(pattern):
                return path
        raise FetchError("Audio download produced no file")

    def download_video(self, target: FetchTarget, workdir: Path) -> Path:
        """Download the video (never above fetch.max_height) for screenshots."""
        from yt_dlp import YoutubeDL  # noqa: F401 - kept for symmetry with callers

        max_height = self._config.fetch_max_height
        log.info(
            "Downloading video (<=%dp) for screenshots: %s", max_height, target.title
        )
        options = {
            **self._base_options(),
            "format": (
                f"bv*[height<={max_height}][ext=mp4]+ba[ext=m4a]"
                f"/bv*[height<={max_height}]+ba"
                f"/b[height<={max_height}]/bv*+ba/b"
            ),
            "merge_output_format": "mp4",
            "outtmpl": str(workdir / "%(id)s.%(ext)s"),
            "paths": {"home": str(workdir)},
            "noplaylist": True,
            "progress_hooks": [_ProgressLogger()],
        }
        try:
            info = self._download_with_retry(options, target.target_url)
        except FetchError as exc:
            raise FetchError(f"Video download failed: {exc}") from exc

        downloads = (info or {}).get("requested_downloads") or []
        if downloads and downloads[0].get("filepath"):
            path = Path(downloads[0]["filepath"])
            if path.exists():
                return path

        for pattern in ("*.mp4", "*.mkv", "*.webm", "*.flv", "*.mov"):
            for path in workdir.glob(pattern):
                return path
        raise FetchError("Video download produced no file")

    # -- transcription --------------------------------------------------------

    def transcribe_to_subtitle(self, audio: Path, workdir: Path) -> Path:
        """Run the local whisper pipeline on *audio* and return its subtitle."""
        from src.srt_daemon.transcribe import Transcriber, TranscriptionState

        log.info(
            "Transcribing locally (model=%s) — this is the slow path",
            self._model or self._config.transcribe_model,
        )
        transcriber = Transcriber(
            self._config,
            model=self._model,
            diarizer=self._diarizer,
            output_dir=workdir,
            output_format="vtt",
            state=TranscriptionState(workdir / "state.json"),
        )
        result = transcriber.transcribe(audio, force=True)
        if result is None or not result.subtitles:
            raise FetchError("Transcription produced no subtitle")

        subtitle = result.subtitles[0]
        if not has_subtitle_entries(subtitle):
            raise FetchError(
                "Transcription found no speech in the audio "
                "(music-only or silent clip) — no note was generated"
            )
        return subtitle

    # -- full pipeline --------------------------------------------------------

    def run(
        self,
        url: str,
        *,
        part: int = 0,
        forced_profile: Optional[str] = None,
    ) -> FetchOutcome:
        """Resolve, fetch and turn one URL into a Markdown note."""
        from src.srt_daemon.pipeline import generate_notes

        started = time.time()
        platform = platform_of(url)
        target = self.resolve_part(self.probe(url), part)
        if target.parts:
            log.info(
                "Multi-part video: %d part(s); using part %d",
                len(target.parts),
                target.part_index + 1,
            )
        log.info(
            "Resolved: %s  platform=%s  duration=%.0fs", target.title, platform, target.duration
        )

        want_shots = (
            self._config.screenshots_enabled
            if self._screenshots is None
            else self._screenshots
        )

        with tempfile.TemporaryDirectory(prefix="fetch_") as tmp:
            workdir = Path(tmp)
            subtitle = self.download_subtitle(target, workdir) if self._prefer_subtitles else None
            video: Optional[Path] = None
            source = "subtitles"
            if subtitle is None:
                if want_shots:
                    video = self.download_video(target, workdir)
                    audio = video
                else:
                    audio = self.download_audio(target, workdir)
                subtitle = self.transcribe_to_subtitle(audio, workdir)
                source = "transcript"
            elif want_shots:
                # Subtitles were free, but frames still need the picture.
                video = self.download_video(target, workdir)

            stem = safe_filename(target.title)
            final_subtitle = workdir / f"{stem}.vtt"
            final_subtitle.write_text(
                subtitle.read_text(encoding="utf-8", errors="replace"), encoding="utf-8"
            )

            result = generate_notes(
                self._config,
                final_subtitle,
                forced_profile=forced_profile,
                extra_metadata={
                    "source_url": url,
                    "platform": platform,
                },
                video_path=video,
                screenshots=want_shots,
            )

        return FetchOutcome(
            url=url,
            note_path=result.output_path,
            title=target.title,
            platform=platform,
            source=source,
            elapsed=time.time() - started,
            profile=result.profile.profile.id,
            speaker_mode=result.speaker.mode,
        )


# ---------------------------------------------------------------------------
# yt-dlp adapters
# ---------------------------------------------------------------------------


class _YdlLogger:
    """Route yt-dlp messages into our logger and remember the last problem.

    yt-dlp raises DownloadError('ERROR: ') when it runs quiet, so the real cause
    would be lost; we keep it and append it to our own error message.
    """

    def __init__(self) -> None:
        self.last_problem: str = ""

    def _record(self, message: str) -> None:
        text = str(message).strip()
        if text:
            self.last_problem = text

    def debug(self, message: str) -> None:
        log.debug("yt-dlp: %s", message)

    def info(self, message: str) -> None:
        log.debug("yt-dlp: %s", message)

    def warning(self, message: str) -> None:
        self._record(message)
        log.debug("yt-dlp warning: %s", message)

    def error(self, message: str) -> None:
        self._record(message)
        log.debug("yt-dlp error: %s", message)


class _ProgressLogger:
    """Log yt-dlp download progress at 10 % steps."""

    def __init__(self) -> None:
        self._last_bucket = -1

    def __call__(self, data: dict[str, Any]) -> None:
        info = data.get("info_dict") or {}
        total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
        downloaded = data.get("downloaded_bytes") or 0
        if not total:
            return
        percent = int(downloaded * 100 / total)
        bucket = percent // 10
        if bucket > self._last_bucket:
            self._last_bucket = bucket
            log.info("Downloading: %d%% (%s)", percent, info.get("title", ""))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None):
    """Parse command-line arguments."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="srt-daemon-fetch",
        description=(
            "Turn an online video URL into a Markdown note.  Prefers the "
            "platform's own subtitles; falls back to local whisper transcription."
        ),
    )
    parser.add_argument("urls", nargs="*", help="Video URLs.")
    parser.add_argument("--config", default=None, metavar="PATH", help="Path to config.yaml.")
    parser.add_argument("--part", type=int, default=1, metavar="N",
                        help="Which part of a multi-part video to use (1-based).")
    parser.add_argument("--profile", default=None, metavar="ID",
                        help="Force a content-type profile (course/meeting/interview).")
    parser.add_argument("--model", default=None,
                        help="Whisper model for the transcription fallback.")
    parser.add_argument("--no-subtitles", action="store_true",
                        help="Skip subtitles and transpose the audio instead.")
    parser.add_argument("--no-diarize", action="store_true",
                        help="Disable speaker diarization when transcribing.")
    parser.add_argument("--proxy", default=None, metavar="URL",
                        help="Proxy for yt-dlp, e.g. http://127.0.0.1:10808")
    parser.add_argument("--cookies", default=None, metavar="FILE",
                        help="Netscape cookies.txt (needed for Bilibili CC subtitles).")
    parser.add_argument("--cookies-from-browser", default=None, metavar="NAME",
                        help="Read cookies from a browser (chrome/edge/firefox).")
    parser.add_argument("--screenshots", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="Embed frames where the AI marks [[screenshot:HH:MM:SS]].")
    parser.add_argument("--dry-run", action="store_true",
                        help="Resolve the URL and list what would be fetched.")
    parser.add_argument("--list-parts", action="store_true",
                        help="Only list the parts of a multi-part video.")
    parser.add_argument("--version", action="store_true", help="Print version and exit.")
    return parser.parse_args(argv)


def _describe(target: FetchTarget, languages: Sequence[str]) -> None:
    """Print a human-readable summary for --dry-run / --list-parts."""
    print(f"title    : {target.title}")
    print(f"platform : {target.platform}")
    print(f"uploader : {target.uploader or '-'}")
    if target.duration:
        print(f"duration : {int(target.duration // 60)}m{int(target.duration % 60)}s")
    if target.parts:
        print(f"parts    : {len(target.parts)}")
        for index, name in enumerate(target.parts, start=1):
            print(f"  {index}. {name}")
    manual = sorted(target.subtitles)
    auto = sorted(target.automatic_captions)
    print(f"subtitles: {', '.join(manual) if manual else 'none'}")
    print(f"auto subs: {', '.join(auto[:8]) if auto else 'none'}")
    picked = pick_subtitle(
        {"subtitles": target.subtitles, "automatic_captions": target.automatic_captions},
        languages,
    )
    if picked:
        print(f"would use: {picked[0]} subtitles ({picked[1]})")
    else:
        print("would use: local transcription (no subtitles)")


def main() -> None:
    """CLI entry point."""
    force_utf8_console()
    args = parse_args()

    if args.version:
        print(f"SRT Daemon fetch v{__version__}")
        return

    try:
        config = load_config(args.config)
    except (FileNotFoundError, RuntimeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not config.fetch_enabled:
        print(
            "Online video support is disabled (fetch.enabled: false in config.yaml).",
            file=sys.stderr,
        )
        sys.exit(1)

    setup_logging(
        log_file=config.log_file,
        level=config.log_level,
        fmt=config.log_format,
        date_format=config.log_date_format,
        rotation=config.log_rotation,
        backup_count=config.log_backup_count,
    )

    if not args.urls:
        print("Error: at least one URL is required.", file=sys.stderr)
        sys.exit(1)

    diarizer = None
    if args.no_diarize:
        from src.srt_daemon.diarize import NoDiarizer

        diarizer = NoDiarizer()

    fetcher = Fetcher(
        config,
        proxy=args.proxy,
        cookies_file=args.cookies,
        cookies_from_browser=args.cookies_from_browser,
        model=args.model,
        prefer_subtitles=False if args.no_subtitles else None,
        diarizer=diarizer,
        screenshots=args.screenshots,
    )

    log.info("=" * 60)
    log.info("SRT Daemon fetch v%s", __version__)
    log.info("=" * 60)

    failures = 0
    for url in args.urls:
        platform = platform_of(url)
        status = config.fetch_platform_status(platform)
        if status == "experimental":
            log.warning(
                "Platform %r is EXPERIMENTAL — cookies and a current yt-dlp are usually required",
                platform,
            )

        try:
            if args.dry_run or args.list_parts:
                target = fetcher.probe(url)
                if args.dry_run:
                    target = fetcher.resolve_part(target, args.part - 1)
                _describe(target, config.fetch_subtitle_languages)
                if args.dry_run and target.platform == "bilibili":
                    fetcher.describe_bilibili_cc(target)
                continue

            outcome = fetcher.run(
                url, part=max(0, args.part - 1), forced_profile=args.profile
            )
        except FetchError as exc:
            log.error("Fetch failed for %s: %s", url, exc)
            print(f"FAILED {url}: {exc}", file=sys.stderr)
            failures += 1
            continue
        except NotesPipelineError as exc:
            log.error("Note generation failed for %s: %s", url, exc)
            print(f"FAILED {url}: {exc}", file=sys.stderr)
            failures += 1
            continue
        except Exception as exc:  # noqa: BLE001 - never leak a traceback to the CLI
            log.error("Unexpected failure for %s", url, exc_info=True)
            print(f"FAILED {url}: {exc}", file=sys.stderr)
            failures += 1
            continue

        print(str(outcome.note_path))
        log.info(
            "Done: %s (%s, profile=%s, speakers=%s, %.1fs)",
            outcome.note_path.name,
            outcome.source,
            outcome.profile,
            outcome.speaker_mode,
            outcome.elapsed,
        )

    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
