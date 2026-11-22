"""Desktop GUI for the Subtitle Daemon (Tkinter + system tray).

Launch without arguments to open the window; launch with --tray to start hidden
in the notification area (that is what the auto-start entry uses).

Everything long-running (a scan, a single-file run, an audio transcription) is
executed as a *child process* of this same executable, so the window never
blocks and a crash inside a native library cannot take the GUI down:

    SRTDaemon.exe --transcribe <args>     -> audio -> subtitle CLI
    SRTDaemon.exe --scan / --once <args>  -> notes pipeline CLI

The watcher itself runs in a background thread of this process, because it
needs to be stoppable and shares the loaded configuration.

Data layout (portable install): the folder that contains the executable also
holds config.yaml, doc/prompts/, models/, output/ and logs/.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from src.srt_daemon import __version__
from src.srt_daemon.config import load_config

APP_NAME = "SRT Daemon"
APP_TITLE = f"{APP_NAME} v{__version__}"

AUTOSTART_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
AUTOSTART_VALUE = "SRTDaemonGUI"

#: Hide the console window of child processes on Windows.
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

PROFILE_CHOICES = ("auto", "course", "meeting", "interview")
WHISPER_MODELS = (
    "large-v3",
    "large-v3-turbo",
    "medium",
    "small",
    "base",
    "tiny",
    "distil-large-v3",
)
DEVICES = ("auto", "cpu", "cuda")
COMPUTE_TYPES = ("auto", "int8", "int8_float16", "float16", "float32")
DIARIZERS = ("sherpa", "pyannote", "none")
OUTPUT_FORMATS = ("vtt", "srt", "both")
AI_MODELS = ("deepseek-flash", "deepseek-v4-pro")
LANGS = ("auto", "en", "zh", "ja", "ko", "de")


# ---------------------------------------------------------------------------
# Small pure helpers (unit-tested without a display)
# ---------------------------------------------------------------------------


def child_command(*args: str) -> list[str]:
    """Command line that re-invokes this program for a CLI sub-task."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    script = Path(__file__).resolve().parent / "app.py"
    return [sys.executable, str(script), *args]


def autostart_command() -> str:
    """Registry command line that starts the GUI hidden in the tray."""
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable)}" --tray'
    script = Path(__file__).resolve().parent / "app.py"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    launcher = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{launcher}" "{script}" --tray'


def is_autostart_enabled() -> bool:
    """True when the per-user Run entry exists."""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            try:
                winreg.QueryValueEx(key, AUTOSTART_VALUE)
                return True
            except FileNotFoundError:
                return False
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    """Create or delete the per-user auto-start entry."""
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
        if enabled:
            winreg.SetValueEx(
                key, AUTOSTART_VALUE, 0, winreg.REG_SZ, autostart_command()
            )
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_VALUE)
            except FileNotFoundError:
                pass


def _yaml_scalar(value: Any) -> str:
    """Render *value* as a YAML scalar the daemon config accepts."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace('"', '\\"') + '"'


def set_config_value(path: Path, section: str, key: str, value: Any) -> bool:
    """Rewrite one section/key value line, keeping comments and layout.

    Args:
        path: Path to config.yaml.
        section: Top-level section name (for example "ai").
        key: Key inside that section (for example "model").
        value: New value.

    Returns:
        True when the line was found and rewritten.
    """
    if not path.exists():
        return False

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_section = False

    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            in_section = stripped.endswith(":") and stripped[:-1].strip() == section
            continue
        if not in_section:
            continue
        name = stripped.split(":", 1)[0].strip()
        if name != key:
            continue
        eol = "\n" if raw.endswith("\n") else ""
        lines[index] = f"{raw[:indent]}{key}: {_yaml_scalar(value)}{eol}"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.replace(path)
        return True

    return False


def set_nested_value(
    path: Path, section: str, subsection: str, key: str, value: Any
) -> bool:
    """Rewrite a value nested two levels deep (section > subsection > key)."""
    if not path.exists():
        return False

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    in_section = False
    in_subsection = False
    subsection_indent = 0

    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        if indent == 0:
            in_section = stripped.endswith(":") and stripped[:-1].strip() == section
            in_subsection = False
            continue
        if not in_section:
            continue
        if stripped.endswith(":") and stripped[:-1].strip() == subsection:
            in_subsection = True
            subsection_indent = indent
            continue
        if in_subsection and indent <= subsection_indent:
            in_subsection = False
        if not in_subsection:
            continue
        name = stripped.split(":", 1)[0].strip()
        if name != key:
            continue
        eol = "\n" if raw.endswith("\n") else ""
        lines[index] = f"{raw[:indent]}{key}: {_yaml_scalar(value)}{eol}"
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("".join(lines), encoding="utf-8")
        tmp.replace(path)
        return True

    return False


# ---------------------------------------------------------------------------
# GUI preferences (kept out of config.yaml so its comments survive)
# ---------------------------------------------------------------------------


@dataclass
class GuiSettings:
    """Preferences that belong to the window, not to the daemon."""

    start_watcher_on_launch: bool = True
    minimize_to_tray: bool = True
    start_minimized: bool = False
    dark_mode: bool = False
    last_profile: str = "auto"
    last_whisper_model: str = ""
    last_diarizer: str = ""
    last_language: str = "auto"
    last_transcribe_dir: str = ""

    @classmethod
    def load(cls, path: Path) -> "GuiSettings":
        """Load preferences from *path*, falling back to defaults."""
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls()
        known = {
            field: raw[field] for field in cls.__dataclass_fields__ if field in raw
        }
        return cls(**known)

    def save(self, path: Path) -> None:
        """Write preferences to *path*."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(asdict(self), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Background helpers
# ---------------------------------------------------------------------------


class LogTail:
    """Follow a growing log file without re-reading everything."""

    def __init__(self, path: Path, tail_bytes: int = 12000) -> None:
        self._path = path
        self._tail_bytes = tail_bytes
        self._pos = 0

    def initial(self) -> str:
        """Return the last chunk of the log and start following from there."""
        try:
            size = self._path.stat().st_size
        except OSError:
            return ""
        start = max(0, size - self._tail_bytes)
        try:
            with open(self._path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(start)
                self._pos = handle.tell()
                return handle.read()
        except OSError:
            return ""

    def read_new(self) -> str:
        """Return everything appended since the previous call."""
        try:
            size = self._path.stat().st_size
        except OSError:
            return ""
        if size < self._pos:  # file was rotated
            self._pos = 0
        if size == self._pos:
            return ""
        try:
            with open(self._path, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._pos)
                text = handle.read()
                self._pos = handle.tell()
                return text
        except OSError:
            return ""


class ChildJob:
    """A CLI sub-task running as a child process (never blocks the window)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.lines: "queue.Queue[str]" = queue.Queue()
        self.exit_code: Optional[int] = None
        self._proc: Optional[subprocess.Popen] = None

    def start(self, args: list[str], cwd: Optional[Path] = None) -> bool:
        """Launch the sub-task; False when it could not be started."""
        if self.running:
            return False
        command = child_command(*args)
        try:
            self._proc = subprocess.Popen(
                command,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            self.lines.put(f"[无法启动] {exc}")
            self.exit_code = -1
            return False

        self.exit_code = None
        threading.Thread(
            target=self._pump, name=f"job-{self.name}", daemon=True
        ).start()
        return True

    def _pump(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                self.lines.put(line.rstrip("\r\n"))
        finally:
            self.exit_code = proc.wait()

    def stop(self) -> None:
        """Terminate the child process when it is still running."""
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


class WatcherRunner:
    """Runs FileWatcher in a background thread of this process."""

    def __init__(self) -> None:
        self._watcher: Any = None
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None

    def start(self, config: Any, forced_profile: Optional[str]) -> None:
        """Create and start the watcher."""
        from src.srt_daemon.watcher import FileWatcher

        self.last_error = None
        self._watcher = FileWatcher(config, forced_profile=forced_profile)
        self._thread = threading.Thread(
            target=self._run, name="watcher", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        try:
            if self._watcher is not None:
                self._watcher.run()
        except Exception as exc:  # noqa: BLE001 - surfaced in the GUI
            self.last_error = f"{type(exc).__name__}: {exc}"

    def stop(self) -> None:
        """Ask the watcher to stop and wait briefly for the thread."""
        if self._watcher is not None:
            try:
                self._watcher.stop()
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self._watcher = None
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------


class App:
    """The daemon window: watcher control, transcription, settings and logs."""

    def __init__(
        self,
        root: tk.Tk,
        config_path: Optional[str] = None,
        start_hidden: bool = False,
    ) -> None:
        self.root = root
        self.cfg = load_config(config_path)
        self.cfg_path = self._locate_config_file(config_path)
        self.settings = GuiSettings.load(self._gui_settings_path())

        self.watcher = WatcherRunner()
        self.scan_job = ChildJob("scan")
        self.once_job = ChildJob("once")
        self.transcribe_job = ChildJob("transcribe")
        self.log_tail = LogTail(self.cfg.log_file)

        self._tray_icon: Any = None
        self._quitting = False
        self._picked_files: list[str] = []
        self._last_status = ""

        self._configure_root()
        self._build_ui()
        self._init_tray()
        self._populate_from_config()

        if start_hidden or self.settings.start_minimized:
            self.root.withdraw()
        if self.settings.start_watcher_on_launch:
            self.root.after(500, self.start_watching)

        self.root.after(150, self._tick)

    # -- paths ---------------------------------------------------------------

    def _locate_config_file(self, explicit: Optional[str]) -> Path:
        if explicit:
            return Path(explicit).resolve()
        root = self.cfg.project_root
        for candidate in (
            root / "src" / "srt_daemon" / "config.yaml",
            root / "config.yaml",
        ):
            if candidate.exists():
                return candidate
        return root / "config.yaml"

    def _gui_settings_path(self) -> Path:
        return self.cfg.project_root / "gui_settings.json"

    # -- construction --------------------------------------------------------

    def _configure_root(self) -> None:
        from tkinter import font as tkfont

        self.root.title(APP_TITLE)
        self.root.geometry("1060x740")
        self.root.minsize(900, 620)
        try:
            style = ttk.Style(self.root)
            if "vista" in style.theme_names():
                style.theme_use("vista")
            default = tkfont.nametofont("TkDefaultFont")
            for family in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI"):
                if family in tkfont.families(self.root):
                    default.configure(family=family, size=10)
                    break
        except tk.TclError:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, padding=(14, 10, 14, 4))
        header.pack(fill="x")
        ttk.Label(header, text=APP_NAME, font=("", 15, "bold")).pack(side="left")
        ttk.Label(header, text=f"v{__version__}", foreground="#6b7280").pack(
            side="left", padx=(8, 0)
        )
        self.status_dot = tk.Canvas(header, width=14, height=14, highlightthickness=0)
        self.status_dot.pack(side="right", padx=(0, 6))
        self.status_label = ttk.Label(header, text="已停止", foreground="#b91c1c")
        self.status_label.pack(side="right")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=12, pady=(6, 4))

        self.home_tab = ttk.Frame(self.notebook, padding=12)
        self.transcribe_tab = ttk.Frame(self.notebook, padding=12)
        self.settings_tab = ttk.Frame(self.notebook, padding=12)
        self.log_tab = ttk.Frame(self.notebook, padding=12)

        self.notebook.add(self.home_tab, text="  监听  ")
        self.notebook.add(self.transcribe_tab, text="  音频转字幕  ")
        self.notebook.add(self.settings_tab, text="  设置  ")
        self.notebook.add(self.log_tab, text="  日志  ")

        self._build_home_tab()
        self._build_transcribe_tab()
        self._build_settings_tab()
        self._build_log_tab()

        footer = ttk.Frame(self.root, padding=(14, 2, 14, 8))
        footer.pack(fill="x")
        self.hint_label = ttk.Label(
            footer,
            text="提示：关闭窗口会最小化到托盘；托盘图标右键可退出。",
            foreground="#6b7280",
        )
        self.hint_label.pack(side="left")

    # -- home tab ------------------------------------------------------------

    def _build_home_tab(self) -> None:
        paths = ttk.LabelFrame(self.home_tab, text="目录", padding=10)
        paths.pack(fill="x")
        paths.columnconfigure(1, weight=1)

        ttk.Label(paths, text="监听目录").grid(row=0, column=0, sticky="w", pady=3)
        self.watch_entry = ttk.Entry(paths, state="readonly")
        self.watch_entry.grid(row=0, column=1, sticky="ew", padx=8, pady=3)
        ttk.Button(
            paths,
            text="打开",
            width=8,
            command=lambda: self._open_path(self.cfg.watch_dir),
        ).grid(row=0, column=2, pady=3)

        ttk.Label(paths, text="输出目录").grid(row=1, column=0, sticky="w", pady=3)
        self.output_entry = ttk.Entry(paths, state="readonly")
        self.output_entry.grid(row=1, column=1, sticky="ew", padx=8, pady=3)
        ttk.Button(
            paths,
            text="打开",
            width=8,
            command=lambda: self._open_path(self.cfg.output_dir),
        ).grid(row=1, column=2, pady=3)

        control = ttk.LabelFrame(self.home_tab, text="监听控制", padding=10)
        control.pack(fill="x", pady=(10, 0))
        ttk.Label(control, text="笔记类型").pack(side="left")
        self.profile_var = tk.StringVar(value=self.settings.last_profile)
        ttk.Combobox(
            control,
            textvariable=self.profile_var,
            values=PROFILE_CHOICES,
            state="readonly",
            width=10,
        ).pack(side="left", padx=(6, 14))

        self.start_button = ttk.Button(
            control, text="启动监听", command=self.start_watching
        )
        self.start_button.pack(side="left", padx=3)
        self.stop_button = ttk.Button(
            control, text="停止监听", command=self.stop_watching
        )
        self.stop_button.pack(side="left", padx=3)
        ttk.Button(control, text="立即扫描一次", command=self.run_scan).pack(
            side="left", padx=(14, 3)
        )
        ttk.Button(control, text="处理单个文件…", command=self.run_once).pack(
            side="left", padx=3
        )

        recent = ttk.LabelFrame(self.home_tab, text="最近生成的笔记", padding=10)
        recent.pack(fill="both", expand=True, pady=(10, 0))
        columns = ("name", "time")
        self.output_tree = ttk.Treeview(
            recent, columns=columns, show="headings", height=12
        )
        self.output_tree.heading("name", text="文件")
        self.output_tree.heading("time", text="生成时间")
        self.output_tree.column("name", width=520, anchor="w")
        self.output_tree.column("time", width=170, anchor="w")
        scroll = ttk.Scrollbar(
            recent, orient="vertical", command=self.output_tree.yview
        )
        self.output_tree.configure(yscrollcommand=scroll.set)
        self.output_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.output_tree.bind("<Double-1>", self._open_selected_note)

    # -- transcribe tab ------------------------------------------------------

    def _build_transcribe_tab(self) -> None:
        source = ttk.LabelFrame(self.transcribe_tab, text="输入", padding=10)
        source.pack(fill="x")
        source.columnconfigure(1, weight=1)

        ttk.Label(source, text="文件夹").grid(row=0, column=0, sticky="w")
        self.trans_dir_var = tk.StringVar(value=self.settings.last_transcribe_dir)
        ttk.Entry(source, textvariable=self.trans_dir_var).grid(
            row=0, column=1, sticky="ew", padx=8
        )
        ttk.Button(source, text="浏览…", width=10, command=self._pick_trans_dir).grid(
            row=0, column=2
        )
        self.recursive_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(source, text="含子文件夹", variable=self.recursive_var).grid(
            row=0, column=3, padx=(8, 0)
        )

        ttk.Label(source, text="单个/多个文件").grid(
            row=1, column=0, sticky="w", pady=(8, 0)
        )
        self.files_label = ttk.Label(source, text="未选择（使用上面的文件夹）")
        self.files_label.grid(row=1, column=1, sticky="w", padx=8, pady=(8, 0))
        buttons = ttk.Frame(source)
        buttons.grid(row=1, column=2, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(buttons, text="选择文件…", command=self._pick_trans_files).pack(
            side="left", padx=3
        )
        ttk.Button(buttons, text="清除", command=self._clear_trans_files).pack(
            side="left", padx=3
        )

        options = ttk.LabelFrame(self.transcribe_tab, text="参数", padding=10)
        options.pack(fill="x", pady=(10, 0))
        self.model_var = tk.StringVar(
            value=self.settings.last_whisper_model or self.cfg.transcribe_model
        )
        self.device_var = tk.StringVar(value=self.cfg.transcribe_device)
        self.lang_var = tk.StringVar(value=self.settings.last_language or "auto")
        self.diarizer_var = tk.StringVar(
            value=self.settings.last_diarizer
            or str(self.cfg.transcribe_diarization.get("backend", "sherpa"))
        )
        self.format_var = tk.StringVar(value=self.cfg.transcribe_output_format)
        self.num_speakers_var = tk.StringVar(value="0")
        self.speakers_map_var = tk.StringVar(value="")
        self.force_var = tk.BooleanVar(value=False)

        row1 = ttk.Frame(options)
        row1.pack(fill="x")
        for label, var, values, width in (
            ("Whisper 模型", self.model_var, WHISPER_MODELS, 10),
            ("设备", self.device_var, ("cpu", "cuda"), 6),
            ("语言", self.lang_var, LANGS, 6),
            ("说话人聚类", self.diarizer_var, DIARIZERS, 9),
            ("输出格式", self.format_var, OUTPUT_FORMATS, 6),
        ):
            ttk.Label(row1, text=label).pack(side="left")
            ttk.Combobox(
                row1,
                textvariable=var,
                values=values,
                state="readonly",
                width=width,
            ).pack(side="left", padx=(4, 14))

        row2 = ttk.Frame(options)
        row2.pack(fill="x", pady=(8, 0))
        ttk.Label(row2, text="说话人数").pack(side="left")
        ttk.Spinbox(
            row2, from_=0, to=12, width=5, textvariable=self.num_speakers_var
        ).pack(side="left", padx=(4, 4))
        ttk.Label(row2, text="（0 = 自动）", foreground="#6b7280").pack(side="left")
        ttk.Label(row2, text="姓名映射").pack(side="left", padx=(18, 0))
        ttk.Entry(row2, textvariable=self.speakers_map_var, width=28).pack(
            side="left", padx=(4, 4)
        )
        ttk.Label(row2, text="例如 1=Alice,2=Bob", foreground="#6b7280").pack(
            side="left"
        )
        ttk.Checkbutton(row2, text="强制重跑", variable=self.force_var).pack(
            side="right"
        )

        actions = ttk.Frame(self.transcribe_tab)
        actions.pack(fill="x", pady=(10, 0))
        ttk.Button(actions, text="列出待处理", command=self.list_transcribe_jobs).pack(
            side="left", padx=3
        )
        self.transcribe_button = ttk.Button(
            actions, text="开始转录", command=self.start_transcribe
        )
        self.transcribe_button.pack(side="left", padx=3)
        ttk.Button(actions, text="停止", command=self.stop_transcribe).pack(
            side="left", padx=3
        )
        ttk.Label(
            actions,
            text="转录在后台子进程运行；速度参考：small 约音频时长的 0.6 倍，medium 约 1.1 倍。",
            foreground="#6b7280",
        ).pack(side="left", padx=(12, 0))

        self.transcribe_output = ScrolledText(
            self.transcribe_tab, height=12, wrap="none"
        )
        self.transcribe_output.pack(fill="both", expand=True, pady=(10, 0))

    # -- settings tab --------------------------------------------------------

    def _build_settings_tab(self) -> None:
        form = ttk.LabelFrame(self.settings_tab, text="配置文件", padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        self.set_watch_var = tk.StringVar(value=str(self.cfg.watch_dir))
        self.set_output_var = tk.StringVar(value=str(self.cfg.output_dir))
        self.set_model_var = tk.StringVar(value=self.cfg.ai_model)
        self.set_profile_var = tk.StringVar(value=self.cfg.ai_default_profile)
        self.set_whisper_var = tk.StringVar(value=self.cfg.transcribe_model)
        self.set_diarizer_var = tk.StringVar(
            value=str(self.cfg.transcribe_diarization.get("backend", "sherpa"))
        )

        for index, (label, var) in enumerate(
            (("监听目录", self.set_watch_var), ("输出目录", self.set_output_var))
        ):
            ttk.Label(form, text=label).grid(row=index, column=0, sticky="w", pady=4)
            ttk.Entry(form, textvariable=var).grid(
                row=index, column=1, sticky="ew", padx=8, pady=4
            )
            ttk.Button(
                form,
                text="浏览…",
                width=9,
                command=lambda v=var: self._browse_into(v),
            ).grid(row=index, column=2, pady=4)

        ttk.Label(form, text="AI 模型").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Combobox(
            form,
            textvariable=self.set_model_var,
            values=AI_MODELS,
            state="readonly",
            width=22,
        ).grid(row=2, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(form, text="默认笔记类型").grid(row=3, column=0, sticky="w", pady=4)
        ttk.Combobox(
            form,
            textvariable=self.set_profile_var,
            values=PROFILE_CHOICES[1:],
            state="readonly",
            width=22,
        ).grid(row=3, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(form, text="转录模型").grid(row=4, column=0, sticky="w", pady=4)
        ttk.Combobox(
            form,
            textvariable=self.set_whisper_var,
            values=WHISPER_MODELS,
            state="readonly",
            width=22,
        ).grid(row=4, column=1, sticky="w", padx=8, pady=4)

        ttk.Label(form, text="说话人聚类后端").grid(row=5, column=0, sticky="w", pady=4)
        ttk.Combobox(
            form,
            textvariable=self.set_diarizer_var,
            values=DIARIZERS,
            state="readonly",
            width=22,
        ).grid(row=5, column=1, sticky="w", padx=8, pady=4)

        behaviour = ttk.LabelFrame(self.settings_tab, text="启动行为", padding=12)
        behaviour.pack(fill="x", pady=(12, 0))
        self.autostart_var = tk.BooleanVar(value=is_autostart_enabled())
        self.on_launch_var = tk.BooleanVar(value=self.settings.start_watcher_on_launch)
        self.tray_var = tk.BooleanVar(value=self.settings.minimize_to_tray)
        self.start_min_var = tk.BooleanVar(value=self.settings.start_minimized)
        ttk.Checkbutton(
            behaviour, text="开机自动启动（最小化到托盘）", variable=self.autostart_var
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            behaviour, text="启动后自动开始监听", variable=self.on_launch_var
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            behaviour, text="关闭窗口时最小化到托盘", variable=self.tray_var
        ).pack(anchor="w", pady=2)
        ttk.Checkbutton(
            behaviour, text="启动时直接隐藏在托盘", variable=self.start_min_var
        ).pack(anchor="w", pady=2)

        actions = ttk.Frame(self.settings_tab)
        actions.pack(fill="x", pady=(12, 0))
        ttk.Button(actions, text="保存设置", command=self.save_settings).pack(
            side="left", padx=3
        )
        ttk.Button(
            actions, text="打开配置文件夹", command=self._open_config_dir
        ).pack(side="left", padx=3)
        ttk.Button(actions, text="重新载入配置", command=self.reload_config).pack(
            side="left", padx=3
        )
        ttk.Label(
            actions,
            text="改监听目录/AI 模型后需要「停止监听 → 启动监听」才生效。",
            foreground="#6b7280",
        ).pack(side="left", padx=(12, 0))

    # -- log tab -------------------------------------------------------------

    def _build_log_tab(self) -> None:
        bar = ttk.Frame(self.log_tab)
        bar.pack(fill="x")
        ttk.Button(bar, text="清空显示", command=self._clear_log).pack(
            side="left", padx=3
        )
        ttk.Button(
            bar,
            text="打开日志文件",
            command=lambda: self._open_path(self.cfg.log_file),
        ).pack(side="left", padx=3)
        ttk.Label(bar, text=f"文件：{self.cfg.log_file}", foreground="#6b7280").pack(
            side="left", padx=12
        )

        self.log_text = ScrolledText(self.log_tab, height=20, wrap="none")
        self.log_text.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text.configure(state="disabled")
        self._append_log(self.log_tail.initial())

    # -- initial state -------------------------------------------------------

    def _populate_from_config(self) -> None:
        self.watch_entry.configure(state="normal")
        self.watch_entry.delete(0, "end")
        self.watch_entry.insert(0, str(self.cfg.watch_dir))
        self.watch_entry.configure(state="readonly")

        self.output_entry.configure(state="normal")
        self.output_entry.delete(0, "end")
        self.output_entry.insert(0, str(self.cfg.output_dir))
        self.output_entry.configure(state="readonly")

        if not self.trans_dir_var.get():
            self.trans_dir_var.set(str(self.cfg.transcribe_source_dir))

        self._outputs_tick = 0
        try:
            self._refresh_outputs()
            self._refresh_status()

            pending = self._count_unprocessed()
            if pending:
                self._append_log(
                    f"[提示] 发现 {pending} 个字幕文件尚未处理过，"
                    "启动监听后会逐个生成笔记（会消耗 API 额度）。\n"
                )
        except Exception as exc:  # noqa: BLE001 - never block startup
            self._append_log(f"[界面] 初始化显示失败：{type(exc).__name__}: {exc}\n")

    def _count_unprocessed(self) -> int:
        """Subtitles in the watch folder that the shared state does not know.

        Returns 0 when the state file already has entries, which is the normal
        case once the GUI and the CLI share the same absolute state path.
        """
        try:
            state_path = self.cfg.state_file
            if state_path.exists():
                data = json.loads(state_path.read_text(encoding="utf-8"))
                if data.get("files"):
                    return 0
            if not self.cfg.watch_dir.is_dir():
                return 0
            return sum(
                1
                for item in self.cfg.watch_dir.iterdir()
                if item.is_file()
                and item.suffix.lower() in self.cfg.watch_extensions
            )
        except (OSError, json.JSONDecodeError):
            return 0

    # -- periodic loop -------------------------------------------------------

    def _tick(self) -> None:
        if self._quitting:
            return
        self._drain(self.scan_job, "[扫描] ")
        self._drain(self.once_job, "[单文件] ")
        self._drain(self.transcribe_job, "", target=self.transcribe_output)

        chunk = self.log_tail.read_new()
        if chunk:
            self._append_log(chunk)

        self._refresh_status()
        self._outputs_tick += 1
        if self._outputs_tick % 12 == 0:
            self._refresh_outputs()

        self.root.after(300, self._tick)

    def _drain(self, job: ChildJob, prefix: str, target: Any = None) -> None:
        while True:
            try:
                line = job.lines.get_nowait()
            except queue.Empty:
                break
            if target is not None:
                self._append_text(target, line + "\n")
            else:
                self._append_log(prefix + line + "\n")

    # -- logging -------------------------------------------------------------

    def _append_log(self, text: str) -> None:
        self._append_text(self.log_text, text)

    def _append_text(self, widget: Any, text: str) -> None:
        if not text:
            return
        at_bottom = widget.yview()[1] > 0.98
        widget.configure(state="normal")
        widget.insert("end", text)
        widget.configure(state="disabled")
        if at_bottom:
            widget.see("end")

    def _clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # -- status / recent notes ----------------------------------------------

    def _refresh_status(self) -> None:
        running = self.watcher.running
        if self.watcher.last_error:
            text, colour = "监听出错", "#b91c1c"
        elif running:
            text, colour = "监听中", "#15803d"
        else:
            text, colour = "已停止", "#b91c1c"

        jobs = [
            job.name
            for job in (self.scan_job, self.once_job, self.transcribe_job)
            if job.running
        ]
        if jobs:
            text += " · 任务：" + "/".join(jobs)

        if text != self._last_status:
            self._last_status = text
            self.status_label.configure(text=text, foreground=colour)
            self.status_dot.delete("all")
            self.status_dot.create_oval(2, 2, 12, 12, fill=colour, outline="")

        state = "disabled" if running else "normal"
        self.start_button.configure(state=state)
        self.stop_button.configure(state="normal" if running else "disabled")
        self.transcribe_button.configure(
            state="disabled" if self.transcribe_job.running else "normal"
        )

    def _refresh_outputs(self) -> None:
        rows: list[tuple[Path, float]] = []
        try:
            for path in self.cfg.output_dir.rglob("*.md"):
                try:
                    rows.append((path, path.stat().st_mtime))
                except OSError:
                    continue
        except OSError:
            rows = []
        rows.sort(key=lambda item: item[1], reverse=True)

        self.output_tree.delete(*self.output_tree.get_children())
        for path, mtime in rows[:25]:
            stamp = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            self.output_tree.insert(
                "", "end", iid=str(path), values=(path.name, stamp)
            )

    def _open_selected_note(self, _event: Any = None) -> None:
        selection = self.output_tree.selection()
        if selection:
            self._open_path(Path(selection[0]))

    # -- shell helpers -------------------------------------------------------

    def _open_path(self, path: Path) -> None:
        try:
            target = Path(path)
            if not target.exists() and target.parent.exists():
                target = target.parent
            os.startfile(str(target))  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开", f"{path}\n{exc}")

    def _open_config_dir(self) -> None:
        self._open_path(self.cfg_path.parent)

    def _browse_into(self, var: tk.StringVar) -> None:
        initial = var.get() or str(self.cfg.project_root)
        chosen = filedialog.askdirectory(initialdir=initial, title="选择目录")
        if chosen:
            var.set(chosen)

    # -- watcher actions -----------------------------------------------------

    def start_watching(self) -> None:
        if self.watcher.running:
            return
        profile = self.profile_var.get()
        forced = None if profile == "auto" else profile
        try:
            self.watcher.start(self.cfg, forced)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            messagebox.showerror("无法启动监听", f"{type(exc).__name__}: {exc}")
            return
        self.settings.last_profile = profile
        self.settings.save(self._gui_settings_path())
        self._append_log(f"[监听] 已启动  目录={self.cfg.watch_dir}  类型={profile}\n")
        self._refresh_status()

    def stop_watching(self) -> None:
        if not self.watcher.running:
            return
        self.watcher.stop()
        self._append_log("[监听] 已停止\n")
        self._refresh_status()

    def run_scan(self) -> None:
        if self.scan_job.running:
            messagebox.showinfo("请稍候", "扫描任务还在运行。")
            return
        args = ["--scan"]
        profile = self.profile_var.get()
        if profile != "auto":
            args += ["--profile", profile]
        self._append_log("[扫描] 开始：立即扫描一次\n")
        self.notebook.select(self.log_tab)
        self.scan_job.start(args, cwd=self.cfg.project_root)

    def run_once(self) -> None:
        if self.once_job.running:
            messagebox.showinfo("请稍候", "单文件任务还在运行。")
            return
        path = filedialog.askopenfilename(
            title="选择字幕文件",
            initialdir=str(self.cfg.watch_dir),
            filetypes=[("字幕文件", "*.srt *.vtt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        args = ["--once", path]
        profile = self.profile_var.get()
        if profile != "auto":
            args += ["--profile", profile]
        self._append_log(f"[单文件] 开始：{Path(path).name}\n")
        self.once_job.start(args, cwd=self.cfg.project_root)

    # -- transcription actions ----------------------------------------------

    def _pick_trans_dir(self) -> None:
        chosen = filedialog.askdirectory(
            title="选择音频/视频文件夹",
            initialdir=self.trans_dir_var.get() or str(self.cfg.transcribe_source_dir),
        )
        if chosen:
            self.trans_dir_var.set(chosen)

    def _pick_trans_files(self) -> None:
        picked = filedialog.askopenfilenames(
            title="选择音频/视频文件",
            initialdir=self.trans_dir_var.get() or str(self.cfg.transcribe_source_dir),
            filetypes=[
                ("音视频", "*.mp3 *.mp4 *.wav *.m4a *.mkv *.mov *.flac *.aac *.opus"),
                ("所有文件", "*.*"),
            ],
        )
        if picked:
            self._picked_files = list(picked)
            self.files_label.configure(text=f"已选择 {len(self._picked_files)} 个文件")

    def _clear_trans_files(self) -> None:
        self._picked_files = []
        self.files_label.configure(text="未选择（使用上面的文件夹）")

    def _transcribe_args(self) -> Optional[list[str]]:
        args = ["--transcribe"]
        model = self.model_var.get().strip()
        if model:
            args += ["--model", model]
        device = self.device_var.get().strip()
        if device and device != "cpu":
            args += ["--device", device]
        language = self.lang_var.get().strip()
        if language and language != "auto":
            args += ["--language", language]
        diarizer = self.diarizer_var.get().strip()
        if diarizer:
            args += ["--diarizer", diarizer]
        try:
            speakers = int(self.num_speakers_var.get() or 0)
        except ValueError:
            speakers = 0
        if speakers > 0:
            args += ["--num-speakers", str(speakers)]
        mapping = self.speakers_map_var.get().strip()
        if mapping:
            args += ["--speakers", mapping]
        fmt = self.format_var.get().strip()
        if fmt:
            args += ["--format", fmt]
        if self.recursive_var.get():
            args += ["--recursive"]
        if self.force_var.get():
            args += ["--force"]

        if self._picked_files:
            args += self._picked_files
        else:
            folder = self.trans_dir_var.get().strip()
            if not folder:
                messagebox.showwarning("缺少输入", "请选择文件夹或音频文件。")
                return None
            args += ["--dir", folder]
        return args

    def list_transcribe_jobs(self) -> None:
        args = self._transcribe_args()
        if args is None:
            return
        self.transcribe_output.delete("1.0", "end")
        self.transcribe_job.start(args + ["--list"], cwd=self.cfg.project_root)

    def start_transcribe(self) -> None:
        if self.transcribe_job.running:
            messagebox.showinfo("请稍候", "转录任务还在运行。")
            return
        args = self._transcribe_args()
        if args is None:
            return
        self.settings.last_whisper_model = self.model_var.get()
        self.settings.last_diarizer = self.diarizer_var.get()
        self.settings.last_language = self.lang_var.get()
        self.settings.last_transcribe_dir = self.trans_dir_var.get()
        self.settings.save(self._gui_settings_path())

        self.notebook.select(self.transcribe_tab)
        self.transcribe_output.delete("1.0", "end")
        self.transcribe_output.insert("end", "启动转录…\n")
        self.transcribe_job.start(args, cwd=self.cfg.project_root)

    def stop_transcribe(self) -> None:
        if not self.transcribe_job.running:
            return
        self.transcribe_job.stop()
        self.transcribe_output.insert("end", "\n[已请求停止]\n")
        self.transcribe_output.see("end")

    # -- settings ------------------------------------------------------------

    def save_settings(self) -> None:
        changes: list[tuple[str, str, Any]] = [
            ("watch", "directory", self.set_watch_var.get().strip()),
            ("output", "directory", self.set_output_var.get().strip()),
            ("ai", "model", self.set_model_var.get()),
            ("ai", "default_profile", self.set_profile_var.get()),
            ("transcribe", "model", self.set_whisper_var.get()),
        ]
        failed = [
            f"{section}.{key}"
            for section, key, value in changes
            if not set_config_value(self.cfg_path, section, key, value)
        ]
        if not set_nested_value(
            self.cfg_path,
            "transcribe",
            "diarization",
            "backend",
            self.set_diarizer_var.get(),
        ):
            failed.append("transcribe.diarization.backend")

        self.settings.start_watcher_on_launch = self.on_launch_var.get()
        self.settings.minimize_to_tray = self.tray_var.get()
        self.settings.start_minimized = self.start_min_var.get()
        self.settings.save(self._gui_settings_path())

        try:
            set_autostart(self.autostart_var.get())
        except OSError as exc:
            messagebox.showerror("开机自启设置失败", str(exc))

        self.reload_config()

        if failed:
            messagebox.showwarning(
                "部分设置未写入",
                "以下配置项没有在 config.yaml 中找到，请手动修改：\n"
                + "\n".join(failed),
            )
        else:
            messagebox.showinfo("已保存", "设置已写入 config.yaml。")

    def reload_config(self) -> None:
        try:
            self.cfg = load_config(self.cfg_path)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            messagebox.showerror("重新载入失败", f"{type(exc).__name__}: {exc}")
            return
        self.log_tail = LogTail(self.cfg.log_file)
        self._populate_from_config()
        self._append_log("[配置] 已重新载入\n")

    # -- tray / lifecycle ----------------------------------------------------

    def _init_tray(self) -> None:
        try:
            import pystray

            image = _tray_image()
        except Exception:  # noqa: BLE001 - tray is optional
            self._tray_icon = None
            return

        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", self._tray_show, default=True),
            pystray.MenuItem("启动监听", self._tray_start),
            pystray.MenuItem("停止监听", self._tray_stop),
            pystray.MenuItem("打开输出目录", self._tray_output),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", self._tray_quit),
        )
        self._tray_icon = pystray.Icon("srt-daemon-gui", image, APP_TITLE, menu)
        threading.Thread(
            target=self._tray_icon.run, name="tray", daemon=True
        ).start()

    def _tray_show(self) -> None:
        self.root.after(0, self._show_window)

    def _tray_start(self) -> None:
        self.root.after(0, self.start_watching)

    def _tray_stop(self) -> None:
        self.root.after(0, self.stop_watching)

    def _tray_output(self) -> None:
        self.root.after(0, lambda: self._open_path(self.cfg.output_dir))

    def _tray_quit(self) -> None:
        self.root.after(0, self._quit)

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except tk.TclError:
            pass

    def _hide_window(self) -> None:
        self.root.withdraw()

    def _on_close(self) -> None:
        if self.tray_var.get() and self._tray_icon is not None:
            self._hide_window()
            self._append_log("[界面] 已最小化到托盘\n")
        else:
            self._quit()

    def _quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        try:
            self.settings.save(self._gui_settings_path())
        except Exception:  # noqa: BLE001 - shutdown best effort
            pass
        for job in (self.scan_job, self.once_job, self.transcribe_job):
            try:
                job.stop()
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass
        try:
            self.watcher.stop()
        except Exception:  # noqa: BLE001 - shutdown best effort
            pass
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:  # noqa: BLE001 - shutdown best effort
                pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------


def enable_dpi_awareness() -> None:
    """Make the window crisp on scaled displays (Windows only).

    Without this the process is DPI-unaware, so Windows bitmap-stretches the
    window at 125%/150% scaling and all text looks blurry.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        # 2 = PROCESS_PER_MONITOR_DPI_AWARE
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:  # noqa: BLE001 - older Windows or missing shcore
        pass
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001 - best effort only
        pass


# ---------------------------------------------------------------------------
# Tray icon artwork
# ---------------------------------------------------------------------------


def _tray_image() -> Any:
    """Draw a small subtitle-badge icon for the notification area."""
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((4, 4, 60, 60), radius=14, fill="#1f2937")
    draw.rounded_rectangle((13, 21, 51, 29), radius=4, fill="#22c55e")
    draw.rounded_rectangle((13, 36, 41, 44), radius=4, fill="#9ca3af")
    return image


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(config_path: Optional[str] = None, start_hidden: bool = False) -> None:
    """Open the window (or start hidden in the tray)."""
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        App(root, config_path=config_path, start_hidden=start_hidden)
    except Exception as exc:  # noqa: BLE001 - last-resort startup report
        try:
            messagebox.showerror("启动失败", f"{type(exc).__name__}: {exc}")
        finally:
            root.destroy()
        raise

    root.mainloop()

