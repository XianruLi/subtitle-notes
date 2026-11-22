"""Tests for the desktop GUI helpers and the unified entry point.

Only the headless parts are covered here: configuration editing, preference
storage, log tailing, command construction and the entry-point dispatch.
Widget construction is exercised by the packaged self-test instead.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.srt_daemon import app as app_module
from src.srt_daemon.gui import (
    GuiSettings,
    LogTail,
    _yaml_scalar,
    autostart_command,
    child_command,
    set_config_value,
    set_nested_value,
)


# =============================================================================
# YAML scalar rendering
# =============================================================================


class TestYamlScalar:
    def test_string_is_quoted(self) -> None:
        assert _yaml_scalar("deepseek-flash") == '"deepseek-flash"'

    def test_quotes_are_escaped(self) -> None:
        assert _yaml_scalar('a"b') == '"a\\"b"'

    def test_bool_and_number_are_plain(self) -> None:
        assert _yaml_scalar(True) == "true"
        assert _yaml_scalar(False) == "false"
        assert _yaml_scalar(5) == "5"


# =============================================================================
# config.yaml editing (comments must survive)
# =============================================================================


_SAMPLE_CONFIG = (
    "# header comment\n"
    "watch:\n"
    "  # where to look\n"
    '  directory: "old"\n'
    '  extensions: [".srt"]\n'
    "\n"
    "ai:\n"
    '  model: "deepseek-v4-pro"\n'
    "  temperature: 0.1\n"
    "\n"
    "transcribe:\n"
    '  model: "medium"\n'
    "  diarization:\n"
    '    backend: "sherpa"\n'
)


class TestConfigEditing:
    def _write(self, tmp_path: Path) -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(_SAMPLE_CONFIG, encoding="utf-8")
        return path

    def test_only_the_target_line_changes(self, tmp_path: Path) -> None:
        path = self._write(tmp_path)

        assert set_config_value(path, "ai", "model", "deepseek-flash") is True

        text = path.read_text(encoding="utf-8")
        assert 'model: "deepseek-flash"' in text
        assert "# header comment" in text        # comments kept
        assert 'directory: "old"' in text        # other sections untouched
        assert '  model: "medium"' in text       # same key in another section kept

    def test_missing_key_reports_false(self, tmp_path: Path) -> None:
        path = self._write(tmp_path)

        assert set_config_value(path, "ai", "nope", "x") is False

    def test_nested_value(self, tmp_path: Path) -> None:
        path = self._write(tmp_path)

        assert (
            set_nested_value(path, "transcribe", "diarization", "backend", "none")
            is True
        )
        assert 'backend: "none"' in path.read_text(encoding="utf-8")

    def test_missing_file_reports_false(self, tmp_path: Path) -> None:
        assert set_config_value(tmp_path / "gone.yaml", "ai", "model", "x") is False
        assert (
            set_nested_value(tmp_path / "gone.yaml", "a", "b", "c", "d") is False
        )


# =============================================================================
# Window preferences
# =============================================================================


class TestGuiSettings:
    def test_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "gui_settings.json"
        GuiSettings(start_watcher_on_launch=False, last_profile="meeting").save(path)

        loaded = GuiSettings.load(path)
        assert loaded.start_watcher_on_launch is False
        assert loaded.last_profile == "meeting"
        assert loaded.minimize_to_tray is True

    def test_missing_or_broken_file_uses_defaults(self, tmp_path: Path) -> None:
        assert GuiSettings.load(tmp_path / "missing.json").start_minimized is False

        broken = tmp_path / "broken.json"
        broken.write_text("{not json", encoding="utf-8")
        assert GuiSettings.load(broken).last_profile == "auto"

    def test_unknown_keys_ignored(self, tmp_path: Path) -> None:
        path = tmp_path / "gui_settings.json"
        path.write_text('{"last_profile": "course", "future_key": 1}', encoding="utf-8")

        assert GuiSettings.load(path).last_profile == "course"


# =============================================================================
# Log tailing
# =============================================================================


class TestLogTail:
    def test_follows_appended_lines(self, tmp_path: Path) -> None:
        log = tmp_path / "app.log"
        log.write_text("first line\n", encoding="utf-8")
        tail = LogTail(log)

        assert "first line" in tail.initial()

        with open(log, "a", encoding="utf-8") as handle:
            handle.write("second line\n")
        assert "second line" in tail.read_new()
        assert tail.read_new() == ""

    def test_missing_file_is_silent(self, tmp_path: Path) -> None:
        tail = LogTail(tmp_path / "nope.log")

        assert tail.initial() == ""
        assert tail.read_new() == ""

    def test_rotation_restarts_from_zero(self, tmp_path: Path) -> None:
        log = tmp_path / "app.log"
        log.write_text("older content\n" * 20, encoding="utf-8")
        tail = LogTail(log)
        tail.initial()

        log.write_text("after rotation\n", encoding="utf-8")
        assert "after rotation" in tail.read_new()


# =============================================================================
# Command construction
# =============================================================================


class TestCommands:
    def test_child_command_in_source_mode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delattr(sys, "frozen", raising=False)

        command = child_command("--scan")

        assert command[0] == sys.executable
        assert command[1].endswith("app.py")
        assert command[2] == "--scan"

    def test_child_command_when_frozen(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        executable = str(tmp_path / "SRTDaemon.exe")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", executable)

        assert child_command("--transcribe") == [executable, "--transcribe"]

    def test_autostart_command_starts_hidden(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        executable = str(tmp_path / "SRTDaemon.exe")
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        monkeypatch.setattr(sys, "executable", executable)

        command = autostart_command()

        assert command == f'"{executable}" --tray'


# =============================================================================
# Entry-point dispatch
# =============================================================================


class TestEntryPointDispatch:
    def test_no_args_opens_the_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}
        monkeypatch.setattr(
            "src.srt_daemon.gui_ctk.main",
            lambda **kwargs: captured.update(kwargs),
        )

        assert app_module.main([]) == 0
        assert captured["start_hidden"] is False
        assert captured["config_path"] is None

    def test_tray_flag_starts_hidden(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}
        monkeypatch.setattr(
            "src.srt_daemon.gui_ctk.main",
            lambda **kwargs: captured.update(kwargs),
        )

        app_module.main(["--tray"])
        assert captured["start_hidden"] is True

    def test_gui_flag_with_config_path(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict = {}
        monkeypatch.setattr(
            "src.srt_daemon.gui_ctk.main",
            lambda **kwargs: captured.update(kwargs),
        )

        app_module.main(["--gui", "--config", "C:/tmp/settings.yaml"])
        assert captured["config_path"] == "C:/tmp/settings.yaml"

    def test_classic_flag_opens_the_ttk_window(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict = {}
        monkeypatch.setattr(
            "src.srt_daemon.gui.main",
            lambda **kwargs: captured.update(kwargs),
        )

        def modern(**kwargs: object) -> None:  # must NOT be used for --classic
            captured["modern"] = True

        monkeypatch.setattr("src.srt_daemon.gui_ctk.main", modern)

        app_module.main(["--classic"])
        assert "start_hidden" in captured
        assert "modern" not in captured

    def test_scan_flag_uses_the_notes_cli(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = {"cli": False}
        monkeypatch.setattr(
            "src.srt_daemon.main.main", lambda: called.__setitem__("cli", True)
        )

        app_module.main(["--scan"])
        assert called["cli"] is True

    def test_transcribe_flag_strips_its_own_flag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: dict = {}
        monkeypatch.setattr(
            "src.srt_daemon.transcribe.main",
            lambda: seen.setdefault("argv", list(sys.argv)),
        )

        app_module.main(["--transcribe", "--list", "meeting.mp3"])

        assert seen["argv"][1:] == ["--list", "meeting.mp3"]
        assert "--transcribe" not in seen["argv"][1:]
