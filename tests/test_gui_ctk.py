"""Tests for the modern (customtkinter) UI helpers.

Importing gui_ctk does not open a window, so these run head-less.
"""

from __future__ import annotations

from types import SimpleNamespace

from src.srt_daemon.gui import GuiSettings
from src.srt_daemon.gui_ctk import PAGE_TITLES, diarizer_backend


class TestDiarizerBackend:
    def test_reads_backend(self) -> None:
        config = SimpleNamespace(transcribe_diarization={"backend": "sherpa"})

        assert diarizer_backend(config) == "sherpa"

    def test_disabled_means_none(self) -> None:
        config = SimpleNamespace(transcribe_diarization={"backend": "pyannote", "enabled": False})

        assert diarizer_backend(config) == "none"

    def test_defaults_to_sherpa(self) -> None:
        config = SimpleNamespace(transcribe_diarization={})

        assert diarizer_backend(config) == "sherpa"

    def test_missing_block(self) -> None:
        config = SimpleNamespace()

        assert diarizer_backend(config) == "sherpa"

    def test_uppercase_backend_is_normalised(self) -> None:
        config = SimpleNamespace(transcribe_diarization={"backend": "PyAnnote"})

        assert diarizer_backend(config) == "pyannote"


class TestPages:
    def test_five_pages(self) -> None:
        assert PAGE_TITLES == ("任务", "新建任务", "在线视频", "设置", "日志")


class TestPageVisibility:
    """The online-video page disappears when fetch is disabled."""

    @staticmethod
    def _app(config) -> object:
        from src.srt_daemon.gui_ctk import App

        app = App.__new__(App)  # no window needed for this check
        app.cfg = config
        return app

    def test_online_page_shown_by_default(self, temp_config) -> None:
        assert "在线视频" in self._app(temp_config)._page_names()

    def test_online_page_hidden_when_disabled(self, temp_config) -> None:
        temp_config._data["fetch"] = {"enabled": False}

        names = self._app(temp_config)._page_names()

        assert "在线视频" not in names
        assert "任务" in names and "设置" in names


class TestTheme:
    def test_palette_has_light_and_dark(self) -> None:
        from src.srt_daemon import gui_ctk

        for name in (
            "BG", "SIDEBAR", "CARD", "BORDER", "TEXT", "MUTED",
            "ACCENT", "ACCENT_HOVER", "ACCENT_SOFT", "OK", "ERR", "INPUT_BG",
        ):
            value = getattr(gui_ctk, name)
            assert isinstance(value, tuple), name
            assert len(value) == 2, name
            assert all(isinstance(item, str) and item.startswith("#") for item in value), name

    def test_dark_mode_is_persisted(self, tmp_path) -> None:
        path = tmp_path / "gui_settings.json"

        GuiSettings(dark_mode=True).save(path)

        assert GuiSettings.load(path).dark_mode is True

    def test_dark_mode_defaults_off(self, tmp_path) -> None:
        assert GuiSettings().dark_mode is False
        assert GuiSettings.load(tmp_path / "missing.json").dark_mode is False
