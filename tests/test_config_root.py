"""Where the data root is when the app runs as a frozen bundle."""

from __future__ import annotations

import sys
from pathlib import Path

from src.srt_daemon.config import _find_project_root


def _freeze(monkeypatch, exe: Path) -> None:
    monkeypatch.delenv("SRT_DAEMON_HOME", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))


def test_development_build_uses_the_checkout(monkeypatch, tmp_path: Path) -> None:
    """dist/SRTDaemon/SRTDaemon.exe inside a checkout shares the project data."""
    checkout = tmp_path / "proj"
    (checkout / "src" / "srt_daemon").mkdir(parents=True)
    (checkout / "src" / "srt_daemon" / "config.yaml").write_text("x", encoding="utf-8")
    exe = checkout / "dist" / "SRTDaemon" / "SRTDaemon.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")

    _freeze(monkeypatch, exe)

    assert _find_project_root() == checkout.resolve()


def test_release_zip_stays_next_to_the_exe(monkeypatch, tmp_path: Path) -> None:
    """A portable download has no checkout marker and must stay self-contained."""
    exe_dir = tmp_path / "SRTDaemon"
    exe_dir.mkdir()
    exe = exe_dir / "SRTDaemon.exe"
    exe.write_text("", encoding="utf-8")
    (exe_dir / "config.yaml").write_text("x", encoding="utf-8")

    _freeze(monkeypatch, exe)

    assert _find_project_root() == exe_dir.resolve()


def test_srt_daemon_home_wins(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "custom-home"
    home.mkdir()
    exe = tmp_path / "somewhere" / "SRTDaemon.exe"
    exe.parent.mkdir()
    exe.write_text("", encoding="utf-8")
    monkeypatch.setenv("SRT_DAEMON_HOME", str(home))
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(exe))

    assert _find_project_root() == home.resolve()
