"""Tests for the audio/video transcription front-end.

ASR and ffmpeg are always mocked: these tests cover speaker assignment,
subtitle writing (including a round-trip through the daemon's own parsers),
state tracking, job discovery and the CLI.  No model is loaded and no audio is
decoded, so the suite stays fast.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from src.srt_daemon.config import Config
from src.srt_daemon.diarize import (
    DiarizationError,
    NoDiarizer,
    SherpaDiarizer,
    SpeakerTurn,
    get_diarizer,
    label_for,
    speaker_names_from_spec,
)
from src.srt_daemon.speakers import analyse_speakers
from src.srt_daemon.srt_parser import SrtParser
from src.srt_daemon.transcribe import (
    AsrSegment,
    AsrWord,
    Cue,
    Transcriber,
    TranscriptionError,
    TranscriptionState,
    assign_speakers,
    discover_sources,
    escape_vtt,
    format_timestamp,
    parse_args,
    write_srt,
    write_vtt,
)
from src.srt_daemon.vtt_parser import VttParser

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def transcribe_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    """Config with the transcribe block pointed at temporary directories."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-api-key-transcribe")

    source_dir = tmp_path / "media"
    output_dir = tmp_path / "watch"
    logs_dir = tmp_path / "logs"
    for directory in (source_dir, output_dir, logs_dir):
        directory.mkdir()

    config_data = {
        "watch": {
            "directory": str(output_dir),
            "extensions": [".srt", ".vtt"],
            "debounce_seconds": 0.1,
        },
        "output": {
            "directory": str(tmp_path / "notes"),
            "filename_template": "{basename}_notes_{date}.md",
        },
        "ai": {
            "model": "deepseek-chat",
            "system_prompt_file": str(PROJECT_ROOT / "doc" / "prompts" / "course.md"),
        },
        "logging": {"file": str(logs_dir / "test.log"), "level": "DEBUG"},
        "state_file": str(tmp_path / "notes" / ".processed.json"),
        "transcribe": {
            "source_dir": str(source_dir),
            "output_dir": str(output_dir),
            "model": "small",
            "device": "cpu",
            "compute_type": "int8",
            "cpu_threads": 2,
            "beam_size": 1,
            "language": "en",
            "vad_filter": True,
            "min_silence_ms": 400,
            "word_timestamps": False,
            "output_format": "vtt",
            "state_file": str(tmp_path / "output" / ".transcribed.json"),
            "diarization": {"backend": "none"},
        },
    }
    return Config(config_data, PROJECT_ROOT)


# =============================================================================
# Formatting helpers
# =============================================================================


class TestFormatting:
    def test_vtt_timestamp(self) -> None:
        assert format_timestamp(0) == "00:00:00.000"
        assert format_timestamp(3661.5) == "01:01:01.500"

    def test_srt_timestamp_uses_comma(self) -> None:
        assert format_timestamp(62.25, separator=",") == "00:01:02,250"

    def test_escape_vtt(self) -> None:
        assert escape_vtt("a & b <c>") == "a &amp; b &lt;c&gt;"


# =============================================================================
# Speaker assignment
# =============================================================================


class TestSpeakerAssignment:
    def test_without_diarization_no_labels(self) -> None:
        cues = assign_speakers([AsrSegment(0, 2, "hello there")], [])

        assert len(cues) == 1
        assert cues[0].speaker is None

    def test_dominant_overlap_wins(self) -> None:
        turns = [
            SpeakerTurn(0.0, 2.0, 0),
            SpeakerTurn(2.0, 6.0, 1),
        ]
        cues = assign_speakers([AsrSegment(1.5, 5.0, "mostly the second speaker")], turns)

        assert cues[0].speaker == label_for(1)

    def test_continuity_when_no_overlap(self) -> None:
        """A segment that falls into a diarization gap keeps the previous label."""
        turns = [SpeakerTurn(0.0, 2.0, 0), SpeakerTurn(8.0, 10.0, 1)]
        cues = assign_speakers(
            [AsrSegment(0.5, 1.5, "first"), AsrSegment(4.0, 5.0, "gap"), AsrSegment(8.5, 9.5, "second")],
            turns,
        )

        assert [c.speaker for c in cues] == [label_for(0), label_for(0), label_for(1)]

    def test_word_level_split(self) -> None:
        """A segment straddling a speaker change is split at the word boundary."""
        turns = [SpeakerTurn(0.0, 2.0, 0), SpeakerTurn(2.0, 10.0, 1)]
        segment = AsrSegment(
            0.5,
            4.0,
            "hello world how are you",
            words=[
                AsrWord(0.5, 1.0, "hello"),
                AsrWord(1.0, 1.8, "world"),
                AsrWord(2.2, 3.0, "how"),
                AsrWord(3.0, 4.0, "are you"),
            ],
        )

        cues = assign_speakers([segment], turns, split_on_words=True)

        assert [c.speaker for c in cues] == [label_for(0), label_for(1)]
        assert cues[0].text == "hello world"
        assert cues[1].text == "how are you"

    def test_label_map_renames_speakers(self) -> None:
        turns = [SpeakerTurn(0, 5, 0)]
        cues = assign_speakers(
            [AsrSegment(1, 2, "hi")], turns, label_map={1: "Alice"}
        )

        assert cues[0].speaker == "Alice"

    def test_empty_segments_skipped(self) -> None:
        cues = assign_speakers([AsrSegment(0, 1, "   ")], [SpeakerTurn(0, 3, 0)])

        assert cues == []


# =============================================================================
# Subtitle writers (round-trip through the daemon's own parsers)
# =============================================================================


class TestSubtitleWriters:
    def test_vtt_round_trip(self, tmp_path: Path) -> None:
        cues = [
            Cue(0.0, 2.5, "Good morning & welcome", "Speaker 1"),
            Cue(2.5, 5.0, "Let us start", "Alice"),
        ]
        target = write_vtt(
            cues, tmp_path / "meeting.vtt",
            source=Path("meeting.mp3"), model="small",
            diarization="sherpa", language="en",
        )

        parsed = VttParser().parse(target)
        assert [e.text for e in parsed] == [
            "Speaker 1: Good morning & welcome",
            "Alice: Let us start",
        ]
        info = analyse_speakers(parsed)
        assert info.labeled
        assert set(info.names) == {"Speaker 1", "Alice"}

    def test_srt_round_trip(self, tmp_path: Path) -> None:
        cues = [Cue(1.0, 3.0, "Hello there", "Speaker 1")]
        target = write_srt(
            cues, tmp_path / "meeting.srt",
            source=Path("meeting.mp3"), model="small",
            diarization="none", language="en",
        )

        parsed = SrtParser().parse(target)
        assert parsed[0].text == "Speaker 1: Hello there"
        assert parsed[0].start_time == "00:00:01,000"
        assert analyse_speakers(parsed).labeled

    def test_unlabelled_cues_stay_unlabelled(self, tmp_path: Path) -> None:
        target = write_vtt(
            [Cue(0.0, 1.0, "plain text")], tmp_path / "plain.vtt",
            source=Path("x.mp3"), model="small",
            diarization="none", language="en",
        )

        assert analyse_speakers(VttParser().parse(target)).labeled is False


# =============================================================================
# State tracking
# =============================================================================


class TestTranscriptionState:
    def test_round_trip(self, tmp_path: Path) -> None:
        media = tmp_path / "meeting.mp3"
        media.write_bytes(b"fake audio")
        state = TranscriptionState(tmp_path / "state.json")

        assert state.is_done(media) is False

        from src.srt_daemon.transcribe import TranscriptionResult

        state.mark(
            media,
            [tmp_path / "meeting.vtt"],
            TranscriptionResult(
                source=media, subtitles=[tmp_path / "meeting.vtt"], language="en",
                duration=10.0, cue_count=3, speaker_count=2, diarization="sherpa",
                elapsed=1.0,
            ),
        )

        reloaded = TranscriptionState(tmp_path / "state.json")
        assert reloaded.is_done(media) is True

    def test_changed_file_is_not_done(self, tmp_path: Path) -> None:
        media = tmp_path / "meeting.mp3"
        media.write_bytes(b"fake audio")
        state = TranscriptionState(tmp_path / "state.json")
        from src.srt_daemon.transcribe import TranscriptionResult

        state.mark(
            media, [], TranscriptionResult(
                source=media, subtitles=[], language="en", duration=1.0,
                cue_count=0, speaker_count=0, diarization="none", elapsed=0.0,
            ),
        )

        media.write_bytes(b"re-recorded, definitely longer")
        assert TranscriptionState(tmp_path / "state.json").is_done(media) is False


# =============================================================================
# Transcriber
# =============================================================================


def _fake_asr(segments):
    def _run(self, wav_path):
        return list(segments), "en", 30.0

    return _run


class TestTranscriber:
    def test_full_run_writes_labelled_subtitle(self, transcribe_config: Config) -> None:
        source = transcribe_config.transcribe_source_dir / "standup.mp3"
        source.write_bytes(b"fake audio")

        transcriber = Transcriber(
            transcribe_config,
            diarizer=NoDiarizer(),
        )
        transcriber._diarizer = NoDiarizer()

        with patch("src.srt_daemon.transcribe.decode_audio"), patch.object(
            Transcriber, "_run_asr", _fake_asr([AsrSegment(0.0, 2.0, "hello"), AsrSegment(2.0, 4.0, "world")])
        ):
            result = transcriber.transcribe(source)

        assert result is not None
        assert result.cue_count == 2
        subtitle = transcribe_config.transcribe_output_dir / "standup.vtt"
        assert subtitle.exists()
        assert transcriber.state.is_done(source) is True

    def test_skip_when_already_done(self, transcribe_config: Config) -> None:
        source = transcribe_config.transcribe_source_dir / "standup.mp3"
        source.write_bytes(b"fake audio")

        transcriber = Transcriber(transcribe_config, diarizer=NoDiarizer())
        with patch("src.srt_daemon.transcribe.decode_audio"), patch.object(
            Transcriber, "_run_asr", _fake_asr([AsrSegment(0.0, 2.0, "hello")])
        ):
            transcriber.transcribe(source)
            second = transcriber.transcribe(source)

        assert second is None

    def test_diarization_failure_still_writes(self, transcribe_config: Config) -> None:
        source = transcribe_config.transcribe_source_dir / "broken.mp3"
        source.write_bytes(b"fake audio")

        class Boom(NoDiarizer):
            name = "sherpa"

            def diarize(self, wav_path, num_speakers=None):
                raise DiarizationError("models missing")

        transcriber = Transcriber(transcribe_config, diarizer=Boom())
        with patch("src.srt_daemon.transcribe.decode_audio"), patch.object(
            Transcriber, "_run_asr", _fake_asr([AsrSegment(0.0, 2.0, "hello")])
        ):
            result = transcriber.transcribe(source)

        assert result is not None
        assert result.speaker_count == 0
        assert (transcribe_config.transcribe_output_dir / "broken.vtt").exists()

    def test_diarization_labels_reach_the_subtitle(self, transcribe_config: Config) -> None:
        source = transcribe_config.transcribe_source_dir / "team.mp3"
        source.write_bytes(b"fake audio")

        class TwoSpeakers(NoDiarizer):
            name = "sherpa"

            def diarize(self, wav_path, num_speakers=None):
                return [SpeakerTurn(0.0, 2.0, 0), SpeakerTurn(2.0, 4.0, 1)]

        transcriber = Transcriber(transcribe_config, diarizer=TwoSpeakers())
        with patch("src.srt_daemon.transcribe.decode_audio"), patch.object(
            Transcriber, "_run_asr",
            _fake_asr([AsrSegment(0.5, 1.5, "first person"), AsrSegment(2.5, 3.5, "second person")]),
        ):
            result = transcriber.transcribe(source, speaker_names={1: "Alice", 2: "Bob"})

        assert result.speaker_count == 2
        parsed = VttParser().parse(transcribe_config.transcribe_output_dir / "team.vtt")
        assert [e.text for e in parsed] == ["Alice: first person", "Bob: second person"]

    def test_both_formats(self, transcribe_config: Config) -> None:
        source = transcribe_config.transcribe_source_dir / "both.mp3"
        source.write_bytes(b"fake audio")

        transcriber = Transcriber(
            transcribe_config, diarizer=NoDiarizer(), output_format="both"
        )
        with patch("src.srt_daemon.transcribe.decode_audio"), patch.object(
            Transcriber, "_run_asr", _fake_asr([AsrSegment(0.0, 2.0, "hello")])
        ):
            result = transcriber.transcribe(source)

        names = sorted(p.name for p in result.subtitles)
        assert names == ["both.srt", "both.vtt"]

    def test_missing_file_raises(self, transcribe_config: Config) -> None:
        transcriber = Transcriber(transcribe_config, diarizer=NoDiarizer())

        with pytest.raises(TranscriptionError):
            transcriber.transcribe(Path("does-not-exist.mp3"))


# =============================================================================
# Discovery
# =============================================================================


class TestDiscovery:
    def test_directory_scan_filters_extensions(self, tmp_path: Path) -> None:
        (tmp_path / "a.mp3").write_bytes(b"x")
        (tmp_path / "b.MP4").write_bytes(b"x")
        (tmp_path / "notes.txt").write_text("nope", encoding="utf-8")

        found = discover_sources([], str(tmp_path), [".mp3", ".mp4"])
        assert [p.name for p in found] == ["a.mp3", "b.MP4"]

    def test_recursive_scan(self, tmp_path: Path) -> None:
        nested = tmp_path / "lectures"
        nested.mkdir()
        (nested / "week1.mp3").write_bytes(b"x")

        assert discover_sources([], str(tmp_path), [".mp3"]) == []
        assert len(discover_sources([], str(tmp_path), [".mp3"], recursive=True)) == 1

    def test_missing_input_raises(self, tmp_path: Path) -> None:
        with pytest.raises(TranscriptionError):
            discover_sources([str(tmp_path / "ghost.mp3")], None, [".mp3"])


# =============================================================================
# Diarizer factory
# =============================================================================


class TestDiarizerFactory:
    def test_none_backend(self, transcribe_config: Config) -> None:
        diarizer = get_diarizer(transcribe_config)

        assert isinstance(diarizer, NoDiarizer)
        assert diarizer.diarize("anything.wav") == []

    def test_unknown_backend_disables_diarization(self, transcribe_config: Config) -> None:
        transcribe_config._data["transcribe"]["diarization"] = {"backend": "whisperx"}

        assert isinstance(get_diarizer(transcribe_config), NoDiarizer)

    def test_sherpa_missing_models_reports_clearly(self, tmp_path: Path) -> None:
        diarizer = SherpaDiarizer(
            {"segmentation_model": str(tmp_path / "nope.onnx")}, tmp_path
        )

        with pytest.raises(DiarizationError, match="model not found|not installed"):
            diarizer.diarize(tmp_path / "audio.wav")

    def test_speaker_name_spec(self) -> None:
        mapping = speaker_names_from_spec("1=张三,2=李四")

        assert mapping == {1: "张三", 2: "李四"}

    def test_speaker_name_spec_ignores_junk(self) -> None:
        assert speaker_names_from_spec("") == {}
        assert speaker_names_from_spec("abc=1") == {}
        assert speaker_names_from_spec("1=Alice, junk, 2=Bob") == {1: "Alice", 2: "Bob"}


# =============================================================================
# CLI
# =============================================================================


class TestCli:
    def test_defaults(self) -> None:
        args = parse_args(["meeting.mp3"])

        assert args.sources == ["meeting.mp3"]
        assert args.model is None
        assert args.diarizer is None
        assert args.recursive is False
        assert args.format is None

    def test_full_option_set(self) -> None:
        args = parse_args([
            "--dir", "D:/media", "--new", "--model", "medium", "--language", "zh",
            "--diarizer", "sherpa", "--num-speakers", "3", "--speakers", "1=Alice",
            "--format", "both", "--limit", "2", "--recursive",
        ])

        assert args.dir == "D:/media"
        assert args.new is True
        assert args.model == "medium"
        assert args.language == "zh"
        assert args.diarizer == "sherpa"
        assert args.num_speakers == 3
        assert args.speakers == "1=Alice"
        assert args.format == "both"
        assert args.limit == 2
        assert args.recursive is True

    def test_invalid_diarizer_rejected(self) -> None:
        with pytest.raises(SystemExit):
            parse_args(["--diarizer", "whisperx"])
