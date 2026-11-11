# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SRTDaemon.exe (one-folder, windowed GUI).

Build with:
    python -m PyInstaller --noconfirm --clean srt_daemon.spec

The entry point is src/srt_daemon/app.py, which dispatches to the GUI, the
transcribe CLI or the notes CLI.  Models (whisper cache, diarization ONNX) and
the editable prompts are NOT bundled: they live next to the executable so they
can be swapped without rebuilding.  build_exe.ps1 assembles that folder.
"""

from PyInstaller.utils.hooks import (
    collect_data_files,
    collect_dynamic_libs,
    collect_submodules,
)

# Modules that are imported lazily (inside functions) and would otherwise be
# missed by the static analysis.
hiddenimports = [
    "pystray._win32",
    "PIL._tkinter_finder",
    "watchdog.observers.winapi",
    "watchdog.observers.polling",
    "yaml",
    "dotenv",
    "src.srt_daemon.app",
    "src.srt_daemon.gui",
    "src.srt_daemon.main",
    "src.srt_daemon.transcribe",
    "src.srt_daemon.diarize",
    "src.srt_daemon.gui_ctk",
    "src.srt_daemon.fetch",
    "src.srt_daemon.pipeline",
    "src.srt_daemon.screenshots",
    "customtkinter",
    "darkdetect",
]

# yt-dlp imports its extractors lazily and ships hundreds of sub-modules.
try:
    hiddenimports += collect_submodules("yt_dlp")
except Exception:  # noqa: BLE001 - optional dependency
    pass

# Native libraries for ASR and diarization.
binaries = []
for package in ("ctranslate2", "sherpa_onnx", "onnxruntime", "av", "tokenizers"):
    try:
        binaries += collect_dynamic_libs(package)
    except Exception:  # noqa: BLE001 - package may be absent
        pass

# Prompts are bundled as a fallback; build_exe.ps1 also copies the editable
# copies next to the executable (those win, because the data root is the exe
# folder).
datas = [("doc/prompts", "doc/prompts")]

# customtkinter ships its theme JSON + assets outside the package modules.
try:
    datas += collect_data_files("customtkinter")
except Exception:  # noqa: BLE001 - optional dependency
    pass

a = Analysis(
    ["src/srt_daemon/app.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch", "torchvision", "torchaudio",
        "matplotlib", "scipy", "pandas", "IPython", "notebook",
        "pytest", "black", "ruff",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="SRTDaemon",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="SRTDaemon",
)
