"""Single entry point shared by the source checkout and the packaged EXE.

Dispatch table:

    SRTDaemon.exe                        -> open the GUI window
    SRTDaemon.exe --tray                 -> start hidden in the notification area
    SRTDaemon.exe --transcribe <args>    -> audio/video -> subtitle
    SRTDaemon.exe --scan / --once ...    -> notes pipeline (same flags as the CLI)
    SRTDaemon.exe --selftest             -> headless packaging check

Keeping one entry point means the GUI can spawn its long-running jobs by
simply re-launching itself with a different flag — no console, no extra
scripts to ship.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from typing import Optional, Sequence

# --- make "import src.srt_daemon..." work when this file is run as a script ---
# The GUI spawns jobs by re-launching this entry point; without this the child
# process would only have src/srt_daemon on sys.path and fail to import the
# package.  Harmless in the frozen EXE (the path is already bundled).
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

#: Flags that belong to the GUI rather than to a CLI sub-command.
GUI_FLAGS = ("--gui", "--tray", "--windowed", "--classic")


def selftest() -> int:
    """Headless check used after packaging: imports, config, prompts, models.

    Returns:
        0 when everything needed at runtime is present, 1 otherwise.
    """
    lines: list[str] = []
    problems: list[str] = []

    def report(message: str) -> None:
        lines.append(message)
        print(message, flush=True)

    try:
        from src.srt_daemon import __version__
    except Exception as exc:  # noqa: BLE001 - bootstrap failure
        print(f"SELFTEST FAIL: cannot import the package: {exc}")
        return 1

    report(f"version      : {__version__}")
    report(f"frozen       : {getattr(sys, 'frozen', False)}")

    for module in (
        "tkinter", "watchdog", "yaml", "dotenv", "openai", "httpx",
        "faster_whisper", "ctranslate2", "av", "sherpa_onnx", "onnxruntime",
        "numpy",
    ):
        try:
            __import__(module)
            report(f"import {module:14s}: OK")
        except Exception as exc:  # noqa: BLE001 - packaging probe
            problems.append(f"import {module}: {type(exc).__name__}: {exc}")
            report(f"import {module:14s}: FAIL {type(exc).__name__}")

    try:
        import PIL  # noqa: F401
        import pystray  # noqa: F401

        report("import tray       : OK (pystray + pillow)")
    except Exception as exc:  # noqa: BLE001 - optional feature
        report(f"import tray       : unavailable ({type(exc).__name__}) - tray disabled")

    try:
        from src.srt_daemon.config import load_config
        from src.srt_daemon.diarize import get_diarizer
        from src.srt_daemon.profiles import ProfileRegistry

        config = load_config()
        registry = ProfileRegistry(config)
        report(f"data root    : {config.project_root}")
        report(f"watch dir    : {config.watch_dir}  exists={config.watch_dir.exists()}")
        report(f"output dir   : {config.output_dir}")
        report(f"ai model     : {config.ai_model}")
        report(f"profiles     : {', '.join(registry.ids())}")
        missing_prompts = [
            profile.id
            for profile in registry.all()
            if not profile.prompt_file.exists()
        ]
        if missing_prompts:
            problems.append(f"missing prompt files: {', '.join(missing_prompts)}")
        report(
            "prompts      : "
            + ("OK" if not missing_prompts else f"MISSING {missing_prompts}")
        )
        report(f"diarizer     : {get_diarizer(config).describe()}")
    except Exception as exc:  # noqa: BLE001 - packaging probe
        problems.append(f"config check: {type(exc).__name__}: {exc}")
        report(f"config check : FAIL {type(exc).__name__}: {exc}")

    ffmpeg = shutil.which("ffmpeg")
    report(f"ffmpeg       : {ffmpeg or 'NOT FOUND (transcription disabled)'}")

    report("")
    report("SELFTEST " + ("OK" if not problems else "FAILED"))
    for problem in problems:
        report(f"  - {problem}")

    # Also write the report next to the executable, so a GUI build can prove
    # itself even when stdout is not attached.
    try:
        from src.srt_daemon.config import _find_project_root

        target = Path(_find_project_root()) / "selftest.log"
        target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 - best effort
        pass

    return 0 if not problems else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Dispatch to the GUI, the transcribe CLI or the notes CLI."""
    args = list(sys.argv[1:] if argv is None else argv)

    if "--selftest" in args:
        return selftest()

    if "--transcribe" in args:
        rest = [arg for arg in args if arg != "--transcribe"]
        from src.srt_daemon.transcribe import main as transcribe_main

        previous = sys.argv
        sys.argv = [previous[0], *rest]
        try:
            transcribe_main()
        finally:
            sys.argv = previous
        return 0

    if "--fetch" in args:
        rest = [arg for arg in args if arg != "--fetch"]
        from src.srt_daemon.fetch import main as fetch_main

        previous = sys.argv
        sys.argv = [previous[0], *rest]
        try:
            fetch_main()
        finally:
            sys.argv = previous
        return 0

    wants_gui = not args or any(arg in GUI_FLAGS for arg in args)
    if not wants_gui:
        from src.srt_daemon.main import main as cli_main

        cli_main()
        return 0

    config_path: Optional[str] = None
    if "--config" in args:
        position = args.index("--config")
        if position + 1 < len(args):
            config_path = args[position + 1]

    if "--classic" in args:
        from src.srt_daemon.gui import main as gui_main

        gui_main(config_path=config_path, start_hidden="--tray" in args)
        return 0

    try:
        from src.srt_daemon.gui_ctk import main as gui_main
    except ImportError:
        # customtkinter missing -> fall back to the ttk window
        from src.srt_daemon.gui import main as gui_main

    gui_main(config_path=config_path, start_hidden="--tray" in args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
