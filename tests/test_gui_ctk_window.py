"""Window smoke test: every page must build without raising.

The head-less tests never construct the widgets, so a missing import or a typo
in a page builder would only show up when the user opens the app.  Skipped when
no display is available (CI).
"""

from __future__ import annotations

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
