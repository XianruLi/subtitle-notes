"""Tests for content-type profiles (course / meeting / interview).

Covers:

* speaker-label analysis (:mod:`src.srt_daemon.speakers`)
* the profile registry and the resolution order
  (``--profile`` > file name > content classification > default)
* profile-aware prompts, classifier and context block in :class:`AiClient`
* the watcher pipeline producing profile-specific Markdown metadata
* the ``--profile`` CLI flag

The AI is always mocked; no API calls are made.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from src.srt_daemon.ai_client import AiClient, AiClientError
from src.srt_daemon.config import Config
from src.srt_daemon.main import parse_args, run_once
from src.srt_daemon.profiles import (
    Profile,
    ProfileRegistry,
    UnknownProfileError,
    resolve_profile,
)
from src.srt_daemon.speakers import (
    EMPTY_SPEAKER_INFO,
    SpeakerInfo,
    analyse_speakers,
    build_speaker_instruction,
)
from src.srt_daemon.subtitle_base import SubtitleEntry
from src.srt_daemon.watcher import FileWatcher

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = PROJECT_ROOT / "doc" / "prompts"

_MOCK_AI_OUTPUT = "# Mock document\n\nProfile-aware test output.\n"

_MEETING_VTT = (
    "WEBVTT\n\n"
    "00:00:01.000 --> 00:00:04.000\n"
    "Alice Chen: Let's review the motor control firmware.\n\n"
    "00:00:04.000 --> 00:00:08.000\n"
    "Bob Li: The FOC loop needs the rotor angle.\n\n"
    "00:00:08.000 --> 00:00:12.000\n"
    "Alice Chen: Agreed, I will add the encoder driver by Friday.\n"
)


def _entry(index: int, text: str) -> SubtitleEntry:
    """Build a SubtitleEntry with a synthetic timestamp."""
    return SubtitleEntry(
        index=index,
        start_time=f"00:00:{index:02d}.000",
        end_time=f"00:00:{index + 1:02d}.000",
        text=text,
    )


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def profile_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """A config wired to the three real prompt templates.

    The output filename template includes ``{profile}`` so that the token is
    exercised by the pipeline tests as well.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key-profiles")

    watch_dir = tmp_path / "watch"
    output_dir = tmp_path / "output"
    logs_dir = tmp_path / "logs"
    for directory in (watch_dir, output_dir, logs_dir):
        directory.mkdir()

    config_data = {
        "watch": {
            "directory": str(watch_dir),
            "extensions": [".srt", ".vtt"],
            "debounce_seconds": 0.1,
        },
        "output": {
            "directory": str(output_dir),
            "filename_template": "{basename}_{profile}_notes_{date}.md",
        },
        "ai": {
            "model": "deepseek-chat",
            "max_tokens": 100,
            "temperature": 0.0,
            "connection_timeout_sec": 5,
            "read_timeout_sec": 10,
            "max_retries": 0,
            "retry_backoff_base_sec": 0.1,
            "chunk_threshold": 100,
            "system_prompt_file": str(PROMPTS_DIR / "course.md"),
            "default_profile": "course",
            "auto_detect_profile": True,
            "profile_sample_chars": 200,
            "classifier_max_tokens": 16,
            "classifier_reasoning_effort": "low",
            "profiles": {
                "course": {
                    "label": "课程/教程",
                    "prompt_file": str(PROMPTS_DIR / "course.md"),
                    "filename_hints": ["课程", "lecture"],
                },
                "meeting": {
                    "label": "会议",
                    "prompt_file": str(PROMPTS_DIR / "meeting.md"),
                    "filename_hints": ["会议", "meeting", "standup"],
                },
                "interview": {
                    "label": "访谈/播客",
                    "prompt_file": str(PROMPTS_DIR / "interview.md"),
                    "filename_hints": ["访谈", "interview", "podcast"],
                },
            },
        },
        "logging": {
            "file": str(logs_dir / "test.log"),
            "level": "DEBUG",
            "format": "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            "date_format": "%Y-%m-%d %H:%M:%S",
            "rotation": "midnight",
            "backup_count": 30,
        },
        "state_file": str(output_dir / ".processed.json"),
    }

    return Config(config_data, PROJECT_ROOT)


# =============================================================================
# Speaker analysis
# =============================================================================


class TestSpeakerAnalysis:
    """The labelled / inferred decision and its instruction text."""

    def test_labeled_transcript_keeps_all_speakers(self) -> None:
        """A participant who speaks once must not be dropped."""
        entries = [
            _entry(1, "Alice Chen: kick off"),
            _entry(2, "Alice Chen: second point"),
            _entry(3, "Bob Li: I only speak once"),
        ]
        info = analyse_speakers(entries)

        assert info.labeled
        assert info.names == ("Alice Chen", "Bob Li")
        assert info.mode == "labeled"
        assert "Alice Chen" in info.describe()

    def test_dash_dialogue_style(self) -> None:
        """SRT dialogue markers (``- Name:``) are recognised too."""
        entries = [
            _entry(1, "- Speaker A: hello"),
            _entry(2, "- Speaker B: hi"),
            _entry(3, "- Speaker A: bye"),
        ]
        info = analyse_speakers(entries)

        assert info.labeled
        assert set(info.names) == {"Speaker A", "Speaker B"}

    def test_short_interview_clip_is_labeled(self) -> None:
        """A two-line clip never repeats a label, yet is clearly labeled."""
        info = analyse_speakers(
            [
                _entry(1, "主持人: 您做 FOC 调试多少年了？"),
                _entry(2, "李工: 十二年。最难的是电流环整定。"),
            ]
        )

        assert info.labeled
        assert set(info.names) == {"主持人", "李工"}

    def test_dense_labels_capped(self) -> None:
        """Too many distinct labels means noise, not a dialogue."""
        entries = [_entry(i, f"标签{i}: 内容") for i in range(1, 10)]
        info = analyse_speakers(entries)

        assert not info.labeled

    def test_course_prose_colon_is_not_a_speaker(self) -> None:
        """A one-off ``注意:`` line in a course must not create a speaker."""
        entries = [
            _entry(1, "大家好，今天我们讲 FOC 控制"),
            _entry(2, "注意: 这里的符号很重要"),
            _entry(3, "我们先看电机的数学模型"),
            _entry(4, "接下来推导电流环"),
        ]
        info = analyse_speakers(entries)

        assert not info.labeled
        assert info.names == ()
        assert info.mode == "inferred"

    def test_empty_entries(self) -> None:
        info = analyse_speakers([])

        assert info is EMPTY_SPEAKER_INFO or not info.labeled
        assert info.total_entries == 0
        assert info.describe() == "none"

    def test_instruction_labeled(self) -> None:
        info = SpeakerInfo(True, ("Alice",), 3, 3)
        instruction = build_speaker_instruction(info, "meeting")

        assert "speaker-labeled" in instruction
        assert "Alice" in instruction
        assert "never" in instruction.lower()

    def test_instruction_inferred_for_meeting(self) -> None:
        info = SpeakerInfo(False, (), 0, 3)
        instruction = build_speaker_instruction(info, "meeting")

        assert "NO speaker labels" in instruction
        assert "inferred" in instruction
        assert "Never invent real names" in instruction

    def test_instruction_empty_for_course(self) -> None:
        """Unlabeled course transcripts need no speaker instruction."""
        assert build_speaker_instruction(SpeakerInfo(False, (), 0, 5), "course") == ""


# =============================================================================
# Registry and resolution
# =============================================================================


class TestProfileRegistry:
    def test_registry_from_config(self, profile_config: Config) -> None:
        registry = ProfileRegistry(profile_config)

        assert registry.ids() == ["course", "meeting", "interview"]
        assert registry.default_id == "course"
        assert registry.get("meeting").label == "会议"
        assert registry.get("meeting").prompt_file == PROMPTS_DIR / "meeting.md"
        assert registry.get("meeting").prompt_file.is_absolute()

    def test_legacy_config_exposes_single_profile(self, temp_config: Config) -> None:
        """Without ``ai.profiles`` the legacy prompt file is used as-is."""
        registry = ProfileRegistry(temp_config)

        assert len(registry) == 1
        assert registry.default().prompt_file == temp_config.ai_system_prompt_file

    def test_unknown_profile_raises(self, profile_config: Config) -> None:
        registry = ProfileRegistry(profile_config)

        assert registry.find("nope") is None
        with pytest.raises(UnknownProfileError):
            registry.get("nope")

    def test_for_filename(self, profile_config: Config) -> None:
        registry = ProfileRegistry(profile_config)

        assert registry.for_filename("[会议]standup.vtt").id == "meeting"
        assert registry.for_filename("ROS2课程-第3讲.srt").id == "course"
        assert registry.for_filename("podcast-42.vtt").id == "interview"
        assert registry.for_filename("random.vtt") is None


class TestProfileResolution:
    def test_forced_profile_wins(self, profile_config: Config) -> None:
        resolution = resolve_profile(
            profile_config,
            "[会议]standup.vtt",
            "text",
            forced="interview",
            classifier=lambda sample, candidates: "course",
        )

        assert resolution.profile.id == "interview"
        assert resolution.source == "cli"
        assert resolution.forced

    def test_filename_hint(self, profile_config: Config) -> None:
        resolution = resolve_profile(
            profile_config, "2026-10-01 meeting notes.vtt", "text"
        )

        assert resolution.profile.id == "meeting"
        assert resolution.source == "filename"
        assert "meeting" in resolution.detail

    def test_content_classification(self, profile_config: Config) -> None:
        seen: dict[str, object] = {}

        def classifier(sample: str, candidates: list[Profile]) -> str:
            seen["sample"] = sample
            seen["candidates"] = [p.id for p in candidates]
            return "interview"

        resolution = resolve_profile(
            profile_config, "episode-12.vtt", "host: welcome", classifier=classifier
        )

        assert resolution.profile.id == "interview"
        assert resolution.source == "content"
        assert seen["candidates"] == ["course", "meeting", "interview"]
        assert isinstance(seen["sample"], str)

    def test_unusable_classifier_answer_falls_back(self, profile_config: Config) -> None:
        resolution = resolve_profile(
            profile_config,
            "episode-12.vtt",
            "some text",
            classifier=lambda sample, candidates: "banana",
        )

        assert resolution.profile.id == "course"
        assert resolution.source == "default"

    def test_classifier_exception_falls_back(self, profile_config: Config) -> None:
        def boom(sample: str, candidates: list[Profile]) -> str:
            raise RuntimeError("API down")

        resolution = resolve_profile(
            profile_config, "episode-12.vtt", "some text", classifier=boom
        )

        assert resolution.profile.id == "course"
        assert resolution.source == "default"

    def test_unknown_forced_profile_falls_through(self, profile_config: Config) -> None:
        resolution = resolve_profile(
            profile_config, "[会议]standup.vtt", "text", forced="webinar"
        )

        assert resolution.profile.id == "meeting"
        assert resolution.source == "filename"

    def test_auto_detect_disabled(self, profile_config: Config, monkeypatch) -> None:
        profile_config._data["ai"]["auto_detect_profile"] = False
        calls = {"n": 0}

        def classifier(sample: str, candidates: list[Profile]) -> str:
            calls["n"] += 1
            return "meeting"

        resolution = resolve_profile(
            profile_config, "episode-12.vtt", "text", classifier=classifier
        )

        assert calls["n"] == 0
        assert resolution.source == "default"


# =============================================================================
# AiClient profile awareness
# =============================================================================


class TestAiClientProfiles:
    def test_each_profile_loads_its_own_prompt(self, profile_config: Config) -> None:
        registry = ProfileRegistry(profile_config)
        prompts = {
            profile.id: AiClient(profile_config, profile=profile)._system_prompt
            for profile in registry.all()
        }

        assert "meeting minutes" in prompts["meeting"]
        assert "podcast" in prompts["interview"].lower()
        assert "study document" in prompts["course"]
        assert len({p[:80] for p in prompts.values()}) == 3

    def test_process_uses_profile_prompt_and_context(self, profile_config: Config) -> None:
        registry = ProfileRegistry(profile_config)
        meeting = registry.get("meeting")
        client = AiClient(profile_config, profile=meeting)

        speaker = analyse_speakers(
            [_entry(1, "Alice: one"), _entry(2, "Alice: two"), _entry(3, "Bob: three")]
        )

        with patch.object(client, "_call_api", return_value=_MOCK_AI_OUTPUT) as mock_call:
            result = client.process(
                "Alice: one\n\nAlice: two",
                "standup.vtt",
                profile=meeting,
                speaker=speaker,
                source_format="VTT",
            )

        assert result == _MOCK_AI_OUTPUT
        system_prompt, user_message = mock_call.call_args[0][:2]
        assert "meeting minutes" in system_prompt
        assert "**Content type (profile)**: meeting" in user_message
        assert "**Speaker mode**: labeled" in user_message
        assert "Alice" in user_message

    def test_context_block_inferred_speakers(self, profile_config: Config) -> None:
        registry = ProfileRegistry(profile_config)
        interview = registry.get("interview")
        client = AiClient(profile_config, profile=interview)

        context = client._build_context_block(
            "episode.vtt",
            interview,
            SpeakerInfo(False, (), 0, 10),
            "VTT",
        )

        assert "**Speaker mode**: inferred" in context
        assert "NO speaker labels" in context
        assert "**Subtitle format**: VTT" in context

    def test_classify_profile_maps_answer(self, profile_config: Config) -> None:
        registry = ProfileRegistry(profile_config)
        client = AiClient(profile_config)

        with patch.object(
            client, "_call_api_with_retry", return_value=" Meeting.\n"
        ) as mock_call:
            detected = client.classify_profile(registry.all(), "sample text")

        assert detected == "meeting"
        overrides = mock_call.call_args.kwargs["overrides"]
        assert overrides["max_tokens"] == 16
        assert overrides["reasoning_effort"] == "low"

    def test_classify_profile_useless_answer(self, profile_config: Config) -> None:
        registry = ProfileRegistry(profile_config)
        client = AiClient(profile_config)

        with patch.object(client, "_call_api_with_retry", return_value="???"):
            assert client.classify_profile(registry.all(), "sample") is None

    def test_classify_profile_swallows_api_errors(self, profile_config: Config) -> None:
        registry = ProfileRegistry(profile_config)
        client = AiClient(profile_config)

        with patch.object(
            client, "_call_api_with_retry", side_effect=AiClientError("boom")
        ):
            assert client.classify_profile(registry.all(), "sample") is None

    def test_classify_profile_empty_sample(self, profile_config: Config) -> None:
        client = AiClient(profile_config)

        assert client.classify_profile(ProfileRegistry(profile_config).all(), "  ") is None

    def test_merge_prompt_reconciles_inferred_speakers(
        self, profile_config: Config
    ) -> None:
        """Multi-chunk merges must unify inferred speaker identifiers."""
        meeting = ProfileRegistry(profile_config).get("meeting")
        client = AiClient(profile_config, profile=meeting)

        with patch.object(
            client, "_call_api_with_retry", return_value="merged"
        ) as mock_call:
            result = client._merge_responses(
                ["### Part 1\nbody one", "### Part 2\nbody two"],
                "standup.vtt",
                None,
                "**Content type (profile)**: meeting",
                SpeakerInfo(False, (), 0, 900),
            )

        assert result == "merged"
        merge_prompt = mock_call.call_args[0][1]
        assert "exactly one identifier" in merge_prompt
        assert "**Content type (profile)**: meeting" in merge_prompt


# =============================================================================
# Watcher pipeline
# =============================================================================


class TestWatcherProfiles:
    def test_filename_hint_selects_profile(self, profile_config: Config) -> None:
        """A ``[会议]`` file gets meeting minutes without an API classifier call."""
        meeting_file = profile_config.watch_dir / "[会议]standup.vtt"
        meeting_file.write_text(_MEETING_VTT, encoding="utf-8")

        watcher = FileWatcher(profile_config)
        with patch.object(AiClient, "process", return_value=_MOCK_AI_OUTPUT) as mock_ai, \
                patch.object(AiClient, "classify_profile") as mock_classify:
            watcher._process_file(meeting_file)

        assert not mock_classify.called, "file-name hint must skip classification"
        assert mock_ai.call_args.kwargs["profile"].id == "meeting"

        state = watcher._state["files"][str(meeting_file.resolve())]
        output_path = Path(state["output"])
        content = output_path.read_text(encoding="utf-8")

        assert output_path.name.startswith("[会议]standup_meeting_notes_")
        assert "profile: meeting" in content
        assert 'speakers: "Alice Chen, Bob Li"' in content
        assert "format: VTT" in content

    def test_forced_profile_via_watcher(self, profile_config: Config) -> None:
        plain = profile_config.watch_dir / "session-12.vtt"
        plain.write_text(_MEETING_VTT, encoding="utf-8")

        watcher = FileWatcher(profile_config, forced_profile="interview")
        with patch.object(AiClient, "process", return_value=_MOCK_AI_OUTPUT) as mock_ai, \
                patch.object(AiClient, "classify_profile") as mock_classify:
            watcher._process_file(plain)

        assert not mock_classify.called
        assert mock_ai.call_args.kwargs["profile"].id == "interview"

        state = watcher._state["files"][str(plain.resolve())]
        content = Path(state["output"]).read_text(encoding="utf-8")
        assert "profile: interview" in content

    def test_content_classification_drives_pipeline(self, profile_config: Config) -> None:
        """With no file-name hint, the classifier decides the profile."""
        plain = profile_config.watch_dir / "recording-01.vtt"
        plain.write_text(_MEETING_VTT, encoding="utf-8")

        watcher = FileWatcher(profile_config)
        with patch.object(AiClient, "process", return_value=_MOCK_AI_OUTPUT) as mock_ai, \
                patch.object(
                    AiClient, "classify_profile", return_value="meeting"
                ) as mock_classify:
            watcher._process_file(plain)

        assert mock_classify.called
        assert mock_ai.call_args.kwargs["profile"].id == "meeting"

    def test_classifier_failure_still_processes(self, profile_config: Config) -> None:
        """A broken classifier must not break the file: default profile is used."""
        plain = profile_config.watch_dir / "recording-02.vtt"
        plain.write_text(_MEETING_VTT, encoding="utf-8")

        watcher = FileWatcher(profile_config)
        with patch.object(AiClient, "process", return_value=_MOCK_AI_OUTPUT) as mock_ai, \
                patch.object(AiClient, "classify_profile", return_value=None):
            watcher._process_file(plain)

        assert mock_ai.call_args.kwargs["profile"].id == "course"
        state = watcher._state["files"][str(plain.resolve())]
        assert "profile: course" in Path(state["output"]).read_text(encoding="utf-8")

    def test_unlabeled_meeting_gets_inferred_speakers(self, profile_config: Config) -> None:
        plain = profile_config.watch_dir / "meeting-recap.vtt"
        plain.write_text(
            "WEBVTT\n\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "Let's go through the status updates.\n\n"
            "00:00:03.000 --> 00:00:06.000\n"
            "I finished the driver, next is testing.\n",
            encoding="utf-8",
        )

        watcher = FileWatcher(profile_config)
        with patch.object(AiClient, "process", return_value=_MOCK_AI_OUTPUT):
            watcher._process_file(plain)

        state = watcher._state["files"][str(plain.resolve())]
        content = Path(state["output"]).read_text(encoding="utf-8")
        assert "profile: meeting" in content
        assert 'speakers: "inferred"' in content


# =============================================================================
# CLI
# =============================================================================


class TestProfileCli:
    def test_parse_args_profile(self) -> None:
        args = parse_args(["--once", "x.vtt", "--profile", "meeting"])

        assert args.profile == "meeting"
        assert args.once == "x.vtt"

    def test_parse_args_without_profile(self) -> None:
        assert parse_args([]).profile is None

    def test_run_once_forces_profile(self, profile_config: Config, tmp_path: Path) -> None:
        subtitle = profile_config.watch_dir / "plain.vtt"
        subtitle.write_text(_MEETING_VTT, encoding="utf-8")

        captured: dict[str, object] = {}

        def fake_process(self, text, filename, **kwargs):
            captured["profile"] = kwargs.get("profile")
            return _MOCK_AI_OUTPUT

        with patch.object(AiClient, "process", fake_process):
            run_once(profile_config, str(subtitle), profile="interview")

        assert captured["profile"].id == "interview"

    def test_main_rejects_unknown_profile(self, profile_config: Config) -> None:
        from src.srt_daemon.main import main

        argv = [
            "srt-daemon",
            "--profile",
            "webinar",
            "--once",
            "whatever.vtt",
        ]
        with patch.object(sys, "argv", argv), \
                patch("src.srt_daemon.main.load_config", return_value=profile_config), \
                patch("src.srt_daemon.main.setup_logging"):
            with pytest.raises(SystemExit) as exc:
                main()

        assert exc.value.code == 1
