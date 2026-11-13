"""Window smoke test: every page must build without raising.

The head-less tests never construct the widgets, so a missing import or a typo
in a page builder would only show up when the user opens the app.  Skipped when
no display is available (CI).
"""

from __future__ import annotations

import time
import tkinter as tk
from pathlib import Path

import pytest

from src.srt_daemon.gui import GuiSettings


def _display_available() -> bool:
    try:
        root = tk.Tk()
        root.destroy()
        return True
    except Exception:  # noqa: BLE001 - headless CI
        return False


pytestmark = pytest.mark.skipif(
    not _display_available(), reason="no display available"
)


def test_all_pages_build(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from src.srt_daemon.gui_ctk import App

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")

    settings = tmp_path / "gui_settings.json"
    GuiSettings(start_watcher_on_launch=False, minimize_to_tray=False).save(settings)

    # Never touch the user's real preferences or spawn a tray icon.
    monkeypatch.setattr(App, "_gui_settings_path", lambda self: settings)
    monkeypatch.setattr(App, "_init_tray", lambda self: None)

    app = App(start_hidden=True)
    try:
        pages = set(app._pages)
        assert {"任务", "新建任务", "在线视频", "设置", "日志"} <= pages
    finally:
        app._quitting = True
        app.root.destroy()


def test_hidden_start_still_watches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--tray (autostart) must start monitoring, not only hide the window."""
    from src.srt_daemon.gui_ctk import App

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    settings = tmp_path / "gui_settings.json"
    GuiSettings(start_watcher_on_launch=True, minimize_to_tray=True).save(settings)
    monkeypatch.setattr(App, "_gui_settings_path", lambda self: settings)
    monkeypatch.setattr(App, "_init_tray", lambda self: None)

    started: list[bool] = []
    monkeypatch.setattr(App, "start_watching", lambda self: started.append(True))

    app = App(start_hidden=True)
    try:
        for _ in range(60):
            app.root.update()
            time.sleep(0.02)
        assert started, "the watcher should start even when hidden"
    finally:
        app._quitting = True
        app.root.destroy()

def test_transcribe_args_carry_profile_and_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manual path must pass the chosen type (and the notes switch)."""
    from src.srt_daemon.gui_ctk import App

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    settings = tmp_path / "gui_settings.json"
    GuiSettings(start_watcher_on_launch=False, minimize_to_tray=False).save(settings)
    monkeypatch.setattr(App, "_gui_settings_path", lambda self: settings)
    monkeypatch.setattr(App, "_init_tray", lambda self: None)

    app = App(start_hidden=True)
    try:
        app._transcribe_files = ["meeting.mp3"]
        app.trans_notes_var.set(True)
        app.trans_profile_var.set("meeting")

        args = app._transcribe_args()
        assert "--notes" in args
        assert args[args.index("--profile") + 1] == "meeting"

        app.trans_notes_var.set(False)
        app.trans_profile_var.set("auto")
        args = app._transcribe_args()
        assert "--notes" not in args
        assert "--profile" not in args
    finally:
        app._quitting = True
        app.root.destroy()

def test_once_card_keeps_the_full_path_and_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picking a subtitle outside the project must not degrade to a bare name."""
    from src.srt_daemon.gui_ctk import App

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-not-used")
    settings = tmp_path / "gui_settings.json"
    GuiSettings(start_watcher_on_launch=False, minimize_to_tray=False).save(settings)
    monkeypatch.setattr(App, "_gui_settings_path", lambda self: settings)
    monkeypatch.setattr(App, "_init_tray", lambda self: None)

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        App, "_start_job", lambda self, key, args: captured.update(key=key, args=args) or True
    )

    app = App(start_hidden=True)
    try:
        somewhere = str(tmp_path / "projects" / "访谈项目" / "talk.srt")
        app._once_file = somewhere
        app.once_profile_var.set("interview")

        app._run_once_selected()

        assert captured["key"] == "once"
        args = captured["args"]
        assert somewhere in args
        assert args[args.index("--profile") + 1] == "interview"
    finally:
        app._quitting = True
        app.root.destroy()
