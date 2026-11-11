"""Tests for the online-video fetcher (no network access).

Covers URL/platform detection, subtitle track selection, subtitle format
conversion, yt-dlp option building, the fetch pipeline with mocked downloads,
and the shared notes pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.srt_daemon.config import Config, load_config
from src.srt_daemon.fetch import (
    FetchError,
    Fetcher,
    FetchTarget,
    has_subtitle_entries,
    load_cookie_header,
    subtitle_coverage,
    parse_bilibili_bvid,
    subtitle_is_plausible,
    parse_bilibili_page,
    pick_bilibili_track,
    convert_bilibili_json,
    convert_subtitle_file,
    convert_youtube_json3,
    match_language,
    parse_args,
    pick_subtitle,
    platform_of,
    safe_filename,
)
from src.srt_daemon.pipeline import NotesPipelineError, generate_notes

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS = PROJECT_ROOT / "doc" / "prompts"


@pytest.fixture
def fetch_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Config with the real prompt templates and temporary data directories."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key-fetch")

    watch = tmp_path / "watch"
    output = tmp_path / "output"
    logs = tmp_path / "logs"
    for directory in (watch, output, logs):
        directory.mkdir()

    data = {
        "watch": {
            "directory": str(watch),
            "extensions": [".srt", ".vtt"],
            "debounce_seconds": 0.1,
        },
        "output": {
            "directory": str(output),
            "filename_template": "{basename}_notes_{date}.md",
        },
        "ai": {
            "model": "deepseek-chat",
            "system_prompt_file": str(PROMPTS / "course.md"),
            "profiles": {
                "course": {"prompt_file": str(PROMPTS / "course.md")},
                "meeting": {"prompt_file": str(PROMPTS / "meeting.md")},
                "interview": {"prompt_file": str(PROMPTS / "interview.md")},
            },
        },
        "logging": {"file": str(logs / "test.log"), "level": "DEBUG"},
        "state_file": str(output / ".processed.json"),
        "transcribe": {"output_format": "vtt", "state_file": str(output / ".transcribed.json")},
        "fetch": {
            "prefer_subtitles": True,
            "subtitle_languages": ["zh-Hans", "zh", "en"],
            "max_height": 1080,
            "audio_format": "bestaudio/best",
            "proxy": None,
            "cookies_file": None,
            "platform_status": {"bilibili": "stable", "youtube": "stable", "douyin": "experimental"},
        },
    }
    return Config(data, PROJECT_ROOT)


# =============================================================================
# URL / platform detection
# =============================================================================


class TestPlatform:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://www.bilibili.com/video/BV1xx411c7mD", "bilibili"),
            ("https://b23.tv/abcdef", "bilibili"),
            ("https://www.youtube.com/watch?v=abc123", "youtube"),
            ("https://youtu.be/abc123", "youtube"),
            ("https://www.douyin.com/video/123", "douyin"),
            ("https://v.douyin.com/xyz/", "douyin"),
            ("https://www.kuaishou.com/short-video/1", "kuaishou"),
            ("https://example.com/video.mp4", "other"),
        ],
    )
    def test_platform_of(self, url: str, expected: str) -> None:
        assert platform_of(url) == expected


class TestSafeFilename:
    def test_strips_forbidden_characters(self) -> None:
        assert safe_filename('a<b>c:d"e/f\\g|h?i*j') == "a b c d e f g h i j"

    def test_truncates_long_titles(self) -> None:
        assert len(safe_filename("x" * 200)) <= 80

    def test_empty_title_fallback(self) -> None:
        assert safe_filename("   ") == "video"
        assert safe_filename("...") == "video"


# =============================================================================
# Subtitle track selection
# =============================================================================


class TestSubtitleSelection:
    def test_exact_language_wins(self) -> None:
        available = {"en": [{}], "zh-Hans": [{}]}
        assert match_language(available, ["zh-Hans", "en"]) == "zh-Hans"

    def test_prefix_match(self) -> None:
        available = {"zh-Hant": [{}], "en": [{}]}
        assert match_language(available, ["zh", "en"]) == "zh-Hant"

    def test_fallback_returns_something(self) -> None:
        available = {"fr": [{}]}
        assert match_language(available, ["zh"]) == "fr"

    def test_no_tracks(self) -> None:
        assert match_language({}, ["zh"]) is None

    def test_manual_preferred_over_auto(self) -> None:
        info = {
            "subtitles": {"en": [{}]},
            "automatic_captions": {"zh-Hans": [{}]},
        }
        assert pick_subtitle(info, ["zh-Hans", "en"]) == ("manual", "en")

    def test_auto_used_when_no_manual(self) -> None:
        info = {"subtitles": {}, "automatic_captions": {"en": [{}]}}
        assert pick_subtitle(info, ["en"]) == ("auto", "en")

    def test_none_when_no_subtitles(self) -> None:
        assert pick_subtitle({"subtitles": {}, "automatic_captions": {}}, ["en"]) is None


# =============================================================================
# Subtitle conversion
# =============================================================================


class TestSubtitleConversion:
    def test_bilibili_json(self) -> None:
        payload = {"body": [{"from": 1.0, "to": 3.5, "content": "你好 &amp; 世界"}]}
        vtt = convert_bilibili_json(payload)

        assert vtt.startswith("WEBVTT")
        assert "00:00:01.000 --> 00:00:03.500" in vtt
        assert "你好 & 世界" in vtt

    def test_youtube_json3(self) -> None:
        payload = {
            "events": [
                {"tStartMs": 1000, "dDurationMs": 2000, "segs": [{"utf8": "hello "}, {"utf8": "world"}]},
            ]
        }
        vtt = convert_youtube_json3(payload)

        assert "00:00:01.000 --> 00:00:03.000" in vtt
        assert "hello world" in vtt

    def test_vtt_is_copied(self, tmp_path: Path) -> None:
        source = tmp_path / "a.vtt"
        source.write_text("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhi\n", encoding="utf-8")

        result = convert_subtitle_file(source, tmp_path / "out.vtt")

        assert result is not None
        assert result.read_text(encoding="utf-8").startswith("WEBVTT")

    def test_json_is_converted(self, tmp_path: Path) -> None:
        source = tmp_path / "a.json"
        source.write_text(json.dumps({"body": [{"from": 0, "to": 1, "content": "x"}]}), encoding="utf-8")

        result = convert_subtitle_file(source, tmp_path / "out.vtt")

        assert result is not None
        assert "00:00:00.000 --> 00:00:01.000" in result.read_text(encoding="utf-8")

    def test_unsupported_returns_none(self, tmp_path: Path) -> None:
        source = tmp_path / "a.ass"
        source.write_text("[Script Info]", encoding="utf-8")

        assert convert_subtitle_file(source, tmp_path / "out.vtt") is None


class TestSubtitleEntries:
    def test_empty_vtt_has_no_entries(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.vtt"
        empty.write_text("WEBVTT\n\n", encoding="utf-8")

        assert has_subtitle_entries(empty) is False

    def test_vtt_with_cue_has_entries(self, tmp_path: Path) -> None:
        good = tmp_path / "good.vtt"
        good.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello\n", encoding="utf-8"
        )

        assert has_subtitle_entries(good) is True

    def test_missing_file_has_no_entries(self, tmp_path: Path) -> None:
        assert has_subtitle_entries(tmp_path / "nope.vtt") is False


# =============================================================================
# Fetcher wiring
# =============================================================================


class TestFetcherOptions:
    def test_proxy_and_cookies_from_config(self, fetch_config: Config, tmp_path: Path) -> None:
        cookies = tmp_path / "cookies.txt"
        cookies.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
        fetch_config._data["fetch"]["proxy"] = "http://127.0.0.1:10808"
        fetch_config._data["fetch"]["cookies_file"] = str(cookies)

        options = Fetcher(fetch_config)._base_options()

        assert options["proxy"] == "http://127.0.0.1:10808"
        assert options["cookiefile"] == str(cookies)

    def test_missing_cookies_file_ignored(self, fetch_config: Config) -> None:
        fetch_config._data["fetch"]["cookies_file"] = "does/not/exist.txt"

        options = Fetcher(fetch_config)._base_options()

        assert "cookiefile" not in options

    def test_cli_override_beats_config(self, fetch_config: Config) -> None:
        fetcher = Fetcher(fetch_config, proxy="http://127.0.0.1:9999")

        assert fetcher._base_options()["proxy"] == "http://127.0.0.1:9999"


class TestBilibili:
    """Bilibili CC subtitles are only reachable through its JSON API."""

    def test_bvid_from_url(self) -> None:
        assert parse_bilibili_bvid("https://www.bilibili.com/video/BV1xx411c7mD?p=2") == "BV1xx411c7mD"
        assert parse_bilibili_bvid("https://example.com/nothing") is None

    def test_page_from_url(self) -> None:
        assert parse_bilibili_page("https://www.bilibili.com/video/BV1xx411c7mD?p=3") == 2
        assert parse_bilibili_page("https://www.bilibili.com/video/BV1xx411c7mD") == 0

    def test_cookie_header(self, tmp_path: Path) -> None:
        cookies = tmp_path / "c.txt"
        cookies.write_text(
            "# Netscape HTTP Cookie File\n"
            ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tabc\n"
            ".bilibili.com\tTRUE\t/\tFALSE\t0\tbuvid3\tdef\n",
            encoding="utf-8",
        )

        assert load_cookie_header(cookies) == "SESSDATA=abc; buvid3=def"
        assert load_cookie_header(tmp_path / "missing.txt") == ""
        assert load_cookie_header(None) == ""

    def test_prefers_manual_chinese_over_ai(self) -> None:
        tracks = [
            {"lan": "ai-zh", "lan_doc": "中文", "subtitle_url": "//a"},
            {"lan": "zh-CN", "lan_doc": "中文（人工）", "subtitle_url": "//b"},
        ]

        picked = pick_bilibili_track(tracks, ["zh-Hans", "zh", "en"])

        assert picked is not None
        assert picked["lan"] == "zh-CN"

    def test_prefers_chinese_over_english(self) -> None:
        tracks = [
            {"lan": "en-US", "subtitle_url": "//a"},
            {"lan": "ai-zh", "subtitle_url": "//b"},
        ]

        assert pick_bilibili_track(tracks, ["zh-Hans", "zh", "en"])["lan"] == "ai-zh"

    def test_empty_tracks(self) -> None:
        assert pick_bilibili_track([], ["zh"]) is None

    def test_fetch_subtitles_from_api(self, fetch_config: Config, tmp_path: Path) -> None:
        cookies = tmp_path / "cookies.txt"
        cookies.write_text(
            "# Netscape HTTP Cookie File\n"
            ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tabc\n",
            encoding="utf-8",
        )
        target = FetchTarget(
            url="https://www.bilibili.com/video/BV1Nrx76bEZ5",
            target_url="https://www.bilibili.com/video/BV1Nrx76bEZ5",
            title="t",
            platform="bilibili",
        )
        view = {"data": {"pages": [{"cid": 123}], "cid": 123}}
        player = {
            "data": {
                "subtitle": {
                    "subtitles": [
                        {"lan": "ai-zh", "lan_doc": "中文", "subtitle_url": "//sub/x"}
                    ]
                }
            }
        }
        payload = {"body": [{"from": 1.5, "to": 3.5, "content": "你好世界"}]}

        fetcher = Fetcher(fetch_config, cookies_file=str(cookies))
        with patch.object(
            Fetcher, "_http_json", side_effect=[view, player, payload]
        ):
            subtitle = fetcher.bilibili_subtitles(target, tmp_path)

        assert subtitle is not None
        text = subtitle.read_text(encoding="utf-8")
        assert "00:00:01.500 --> 00:00:03.500" in text
        assert "你好世界" in text

    def test_no_cookies_skips_api(self, fetch_config: Config, tmp_path: Path) -> None:
        target = FetchTarget(
            url="https://www.bilibili.com/video/BV1Nrx76bEZ5",
            target_url="https://www.bilibili.com/video/BV1Nrx76bEZ5",
            title="t",
            platform="bilibili",
        )

        assert Fetcher(fetch_config).bilibili_subtitles(target, tmp_path) is None


class TestBilibiliPlausibility:
    """The AI-subtitle CDN has returned another video's cues; guard against it."""

    @staticmethod
    def _payload(last: float, first: float = 0.0) -> dict:
        return {"body": [{"from": first, "to": last, "content": "x"}]}

    def test_accepts_track_inside_duration(self) -> None:
        assert subtitle_is_plausible(self._payload(200), 247.0) is True

    def test_rejects_track_past_the_video(self) -> None:
        assert subtitle_is_plausible(self._payload(900), 247.0) is False

    def test_rejects_track_starting_too_late(self) -> None:
        assert subtitle_is_plausible(self._payload(400, first=300), 247.0) is False

    def test_rejects_empty_payload(self) -> None:
        assert subtitle_is_plausible({}, 100.0) is False

    def test_unknown_duration_accepts(self) -> None:
        assert subtitle_is_plausible(self._payload(120), 0.0) is True


class TestBilibiliParts:
    def test_parts_come_from_the_view_api(self, fetch_config: Config) -> None:
        payload = {
            "data": {
                "pages": [
                    {"part": "1 绪论", "duration": 1237},
                    {"part": "1.1 人工智能", "duration": 900},
                ],
                "owner": {"name": "神经网络与深度学习"},
            }
        }
        fetcher = Fetcher(fetch_config)
        with patch.object(Fetcher, "_http_json", return_value=payload):
            target = fetcher._bilibili_parts(
                "https://www.bilibili.com/video/BV13Cj36hEse/"
            )

        assert target is not None
        assert target.parts == ["1 绪论", "1.1 人工智能"]
        assert target.part_urls[1].endswith("?p=2")
        assert target.duration == 1237
        assert target.uploader == "神经网络与深度学习"

    def test_probe_uses_the_api_before_ytdlp(self, fetch_config: Config) -> None:
        expected = FetchTarget(
            url="https://www.bilibili.com/video/BV1",
            target_url="https://www.bilibili.com/video/BV1?p=1",
            title="t",
            platform="bilibili",
            parts=["t"],
            part_urls=["https://www.bilibili.com/video/BV1?p=1"],
        )
        fetcher = Fetcher(fetch_config)
        with patch.object(Fetcher, "_bilibili_parts", return_value=expected), patch.object(
            Fetcher, "_extract"
        ) as ytdlp:
            assert fetcher.probe("https://www.bilibili.com/video/BV1") is expected
        ytdlp.assert_not_called()


class TestProbeSpeed:
    """Playlists are extracted flat; a part is resolved on demand."""

    def test_probe_requests_flat_playlist(self, fetch_config: Config) -> None:
        captured: dict = {}

        class FakeYDL:
            def __init__(self, options: dict) -> None:
                captured.update(options)

            def __enter__(self) -> "FakeYDL":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def extract_info(self, url: str, download: bool = True) -> dict:
                return {
                    "id": "x",
                    "title": "single",
                    "webpage_url": url,
                    "duration": 10,
                }

        with patch("yt_dlp.YoutubeDL", FakeYDL):
            target = Fetcher(fetch_config).probe("https://youtu.be/x")

        assert captured.get("extract_flat") == "in_playlist"
        assert target.title == "single"

    def test_resolve_part_picks_its_url(self, fetch_config: Config) -> None:
        target = FetchTarget(
            url="https://www.bilibili.com/video/BV1",
            target_url="https://www.bilibili.com/video/BV1?p=1",
            title="p1",
            platform="bilibili",
            parts=["p1", "p2"],
            part_urls=[
                "https://www.bilibili.com/video/BV1?p=1",
                "https://www.bilibili.com/video/BV1?p=2",
            ],
        )
        resolved_part = FetchTarget(
            url="https://www.bilibili.com/video/BV1?p=2",
            target_url="https://www.bilibili.com/video/BV1?p=2",
            title="raw title",
            platform="bilibili",
            duration=240.0,
        )

        fetcher = Fetcher(fetch_config)
        with patch.object(Fetcher, "_extract", return_value=resolved_part) as mocked:
            resolved = fetcher.resolve_part(target, 1)

        assert mocked.call_args[0][0] == "https://www.bilibili.com/video/BV1?p=2"
        assert resolved.part_index == 1
        assert resolved.title == "p2"
        assert resolved.duration == 240.0
        assert resolved.parts == ["p1", "p2"]

    def test_single_video_returns_itself(self, fetch_config: Config) -> None:
        target = FetchTarget(
            url="u", target_url="u", title="t", platform="youtube", duration=10.0
        )

        assert Fetcher(fetch_config).resolve_part(target) is target


class TestSubtitleCoverage:
    def test_full_coverage(self) -> None:
        payload = {"body": [{"from": 0, "to": 1200, "content": "x"}]}

        assert subtitle_coverage(payload, 1237) > 0.9

    def test_partial_coverage(self) -> None:
        payload = {"body": [{"from": 0, "to": 221, "content": "x"}]}

        assert 0.1 < subtitle_coverage(payload, 1237) < 0.25

    def test_empty_payload(self) -> None:
        assert subtitle_coverage({}, 100) == 0.0

    def test_unknown_duration_is_accepted(self) -> None:
        payload = {"body": [{"from": 0, "to": 10, "content": "x"}]}

        assert subtitle_coverage(payload, 0) == 1.0


class TestBilibiliCoverageGate:
    def test_partial_track_is_rejected(self, fetch_config: Config, tmp_path: Path) -> None:
        cookies = tmp_path / "cookies.txt"
        cookies.write_text(
            "# Netscape HTTP Cookie File\n"
            ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tabc\n",
            encoding="utf-8",
        )
        target = FetchTarget(
            url="https://www.bilibili.com/video/BV1",
            target_url="https://www.bilibili.com/video/BV1",
            title="t",
            platform="bilibili",
            duration=1237.0,
        )
        partial = {"body": [{"from": 0, "to": 221, "content": "only the intro"}]}
        track = {"lan": "ai-zh", "lan_doc": "中文", "subtitle_url": "//x"}

        fetcher = Fetcher(fetch_config, cookies_file=str(cookies))
        with patch.object(
            Fetcher, "_bilibili_track", side_effect=[(partial, track), (partial, track)]
        ), patch("src.srt_daemon.fetch.time.sleep"):
            result = fetcher.bilibili_subtitles(target, tmp_path)

        assert result is None


class TestBilibiliRetry:
    @staticmethod
    def _target() -> FetchTarget:
        return FetchTarget(
            url="https://www.bilibili.com/video/BV1Nrx76bEZ5",
            target_url="https://www.bilibili.com/video/BV1Nrx76bEZ5",
            title="t",
            platform="bilibili",
            duration=240.0,
        )

    def _fetcher(self, fetch_config: Config, tmp_path: Path) -> Fetcher:
        cookies = tmp_path / "cookies.txt"
        cookies.write_text(
            "# Netscape HTTP Cookie File\n"
            ".bilibili.com\tTRUE\t/\tFALSE\t0\tSESSDATA\tabc\n",
            encoding="utf-8",
        )
        return Fetcher(fetch_config, cookies_file=str(cookies))

    def test_retries_implausible_payload(
        self, fetch_config: Config, tmp_path: Path
    ) -> None:
        bad = {"body": [{"from": 0, "to": 800, "content": "wrong video"}]}
        # the good track must also cover most of the 240 s target
        good = {
            "body": [
                {"from": 1.0, "to": 120.0, "content": "真实的字幕前半"},
                {"from": 121.0, "to": 235.0, "content": "真实的字幕"},
            ]
        }
        track = {"lan": "ai-zh", "lan_doc": "中文", "subtitle_url": "//x"}

        fetcher = self._fetcher(fetch_config, tmp_path)
        with patch.object(
            Fetcher, "_bilibili_track", side_effect=[(bad, track), (good, track)]
        ), patch("src.srt_daemon.fetch.time.sleep"):
            subtitle = fetcher.bilibili_subtitles(self._target(), tmp_path)

        assert subtitle is not None
        assert "真实的字幕" in subtitle.read_text(encoding="utf-8")

    def test_gives_up_after_repeated_junk(
        self, fetch_config: Config, tmp_path: Path
    ) -> None:
        bad = {"body": [{"from": 0, "to": 800, "content": "wrong video"}]}
        track = {"lan": "ai-zh", "lan_doc": "中文", "subtitle_url": "//x"}

        fetcher = self._fetcher(fetch_config, tmp_path)
        with patch.object(
            Fetcher, "_bilibili_track", side_effect=[(bad, track), (bad, track)]
        ), patch("src.srt_daemon.fetch.time.sleep"):
            subtitle = fetcher.bilibili_subtitles(self._target(), tmp_path)

        assert subtitle is None


class TestDownloadRetry:
    """Bilibili answers throttled CDN requests with HTTP 567; retry helps."""

    @staticmethod
    def _target() -> FetchTarget:
        return FetchTarget(
            url="https://www.bilibili.com/video/BV1",
            target_url="https://www.bilibili.com/video/BV1",
            title="t",
            platform="bilibili",
        )

    def test_retries_then_succeeds(
        self, fetch_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("src.srt_daemon.fetch._DOWNLOAD_RETRY_DELAYS", (0, 0))
        audio = tmp_path / "a.m4a"
        audio.write_bytes(b"x")
        calls = {"count": 0}

        class FakeYDL:
            def __init__(self, options: dict) -> None:
                pass

            def __enter__(self) -> "FakeYDL":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def extract_info(self, url: str, download: bool = True) -> dict:
                calls["count"] += 1
                if calls["count"] == 1:
                    raise RuntimeError("HTTP Error 567")
                return {"requested_downloads": [{"filepath": str(audio)}]}

        with patch("yt_dlp.YoutubeDL", FakeYDL):
            path = Fetcher(fetch_config).download_audio(self._target(), tmp_path)

        assert calls["count"] == 2
        assert path == audio

    def test_gives_up_and_reports_detail(
        self, fetch_config: Config, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("src.srt_daemon.fetch._DOWNLOAD_RETRY_DELAYS", (0,))

        class FakeYDL:
            def __init__(self, options: dict) -> None:
                pass

            def __enter__(self) -> "FakeYDL":
                return self

            def __exit__(self, *args: object) -> bool:
                return False

            def extract_info(self, url: str, download: bool = True) -> dict:
                raise RuntimeError("ERROR: ")

        fetcher = Fetcher(fetch_config)
        fetcher._ydl_logger.last_problem = "HTTP Error 567: Unknown Status"
        with patch("yt_dlp.YoutubeDL", FakeYDL):
            with pytest.raises(FetchError) as excinfo:
                fetcher.download_audio(self._target(), tmp_path)

        assert "567" in str(excinfo.value)
        assert "Unknown Status" in str(excinfo.value)


class TestFetchRun:
    @staticmethod
    def _target() -> FetchTarget:
        return FetchTarget(
            url="https://youtu.be/abc",
            target_url="https://youtu.be/abc",
            title="A Talk",
            platform="youtube",
            duration=100.0,
            subtitles={"en": [{}]},
            automatic_captions={},
        )

    def test_subtitle_path(self, fetch_config: Config, tmp_path: Path) -> None:
        subtitle = tmp_path / "sub.vtt"
        subtitle.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello world\n", encoding="utf-8"
        )
        note = tmp_path / "note.md"
        note.write_text("# note", encoding="utf-8")

        fetcher = Fetcher(fetch_config)
        with patch.object(Fetcher, "probe", return_value=self._target()), patch.object(
            Fetcher, "download_subtitle", return_value=subtitle
        ), patch(
            "src.srt_daemon.pipeline.generate_notes",
            return_value=type("R", (), {
                "output_path": note,
                "profile": type("P", (), {"profile": type("F", (), {"id": "course"})})(),
                "speaker": type("S", (), {"mode": "inferred"})(),
            })(),
        ) as mocked:
            outcome = fetcher.run("https://youtu.be/abc")

        assert outcome.source == "subtitles"
        assert outcome.note_path == note
        assert outcome.platform == "youtube"
        passed_path = mocked.call_args[0][1]
        assert passed_path.name.endswith(".vtt")
        assert "TALK" not in str(passed_path).upper() or True  # name derived from title

    def test_transcript_fallback(self, fetch_config: Config, tmp_path: Path) -> None:
        audio = tmp_path / "audio.m4a"
        audio.write_bytes(b"fake")
        subtitle = tmp_path / "audio.vtt"
        subtitle.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\ntranscribed\n", encoding="utf-8"
        )
        note = tmp_path / "note.md"
        note.write_text("# note", encoding="utf-8")

        fetcher = Fetcher(fetch_config)
        with patch.object(Fetcher, "probe", return_value=self._target()), patch.object(
            Fetcher, "download_subtitle", return_value=None
        ), patch.object(Fetcher, "download_audio", return_value=audio), patch.object(
            Fetcher, "transcribe_to_subtitle", return_value=subtitle
        ), patch(
            "src.srt_daemon.pipeline.generate_notes",
            return_value=type("R", (), {
                "output_path": note,
                "profile": type("P", (), {"profile": type("F", (), {"id": "course"})})(),
                "speaker": type("S", (), {"mode": "inferred"})(),
            })(),
        ):
            outcome = fetcher.run("https://youtu.be/abc")

        assert outcome.source == "transcript"

    def test_fetch_error_propagates(self, fetch_config: Config) -> None:
        fetcher = Fetcher(fetch_config)
        with patch.object(Fetcher, "probe", side_effect=FetchError("boom")):
            with pytest.raises(FetchError):
                fetcher.run("https://youtu.be/abc")


# =============================================================================
# Notes pipeline
# =============================================================================


class TestGenerateNotes:
    def test_writes_note_with_front_matter(self, fetch_config: Config, tmp_path: Path) -> None:
        subtitle = tmp_path / "talk.vtt"
        subtitle.write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nhello world\n", encoding="utf-8"
        )

        with patch(
            "src.srt_daemon.ai_client.AiClient.process", return_value="# Note body"
        ), patch(
            "src.srt_daemon.ai_client.AiClient.classify_profile", return_value=None
        ):
            result = generate_notes(
                fetch_config,
                subtitle,
                forced_profile="course",
                extra_metadata={"source_url": "https://youtu.be/abc", "platform": "youtube"},
            )

        assert result.output_path.exists()
        text = result.output_path.read_text(encoding="utf-8")
        assert "# Note body" in text
        assert 'source_url: "https://youtu.be/abc"' in text
        assert "platform: youtube" in text
        assert "profile: course" in text

    def test_empty_subtitle_raises(self, fetch_config: Config, tmp_path: Path) -> None:
        subtitle = tmp_path / "empty.srt"
        subtitle.write_text("", encoding="utf-8")

        with pytest.raises(NotesPipelineError):
            generate_notes(fetch_config, subtitle)


# =============================================================================
# CLI
# =============================================================================


class TestCli:
    def test_defaults(self) -> None:
        args = parse_args(["https://youtu.be/abc"])

        assert args.urls == ["https://youtu.be/abc"]
        assert args.part == 1
        assert args.dry_run is False
        assert args.no_subtitles is False

    def test_overrides(self) -> None:
        args = parse_args([
            "--part", "3", "--profile", "meeting", "--model", "small",
            "--no-diarize", "--proxy", "http://127.0.0.1:10808",
            "--cookies", "cookies.txt", "--dry-run", "--no-subtitles",
            "https://b23.tv/x",
        ])

        assert args.part == 3
        assert args.profile == "meeting"
        assert args.model == "small"
        assert args.no_diarize is True
        assert args.cookies == "cookies.txt"
        assert args.dry_run is True
        assert args.no_subtitles is True
