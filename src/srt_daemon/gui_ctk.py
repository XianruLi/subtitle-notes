"""Modern desktop UI built with customtkinter.

This module is the default window.  It reuses the proven machinery from
:mod:gui — settings persistence, log tailing, child jobs, the watcher
thread, auto-start and the tray icon — and only replaces the widgets, so the
business logic stays in one place.

Pages: 任务 / 新建任务 / 在线视频 / 设置 / 日志
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Optional

import customtkinter as ctk

from src.srt_daemon import __version__
from src.srt_daemon.gui import (
    AI_MODELS,
    APP_NAME,
    APP_TITLE,
    COMPUTE_TYPES,
    DEVICES,
    DIARIZERS,
    LANGS,
    OUTPUT_FORMATS,
    PROFILE_CHOICES,
    WHISPER_MODELS,
    ChildJob,
    GuiSettings,
    LogTail,
    WatcherRunner,
    _tray_image,
    enable_dpi_awareness,
    is_autostart_enabled,
    set_autostart,
    set_config_value,
)
from src.srt_daemon.config import load_config
from src.srt_daemon.logger import setup_logging

# ---------------------------------------------------------------------------
# Palette (light theme; customtkinter tuples keep a dark variant ready)
# ---------------------------------------------------------------------------

# Every colour is a (light, dark) tuple: customtkinter switches them
# automatically when the appearance mode changes.
BG = ("#F4F5F7", "#161A20")
SIDEBAR = ("#FFFFFF", "#1C2128")
CARD = ("#FFFFFF", "#1C2128")
BORDER = ("#E3E6EB", "#2B313A")
TEXT = ("#1F2430", "#E7EAF0")
MUTED = ("#6B7280", "#98A2B3")
ACCENT = ("#2F6FED", "#5A93FF")
ACCENT_HOVER = ("#1F5BD8", "#7AAAFF")
ACCENT_SOFT = ("#EBF1FF", "#233046")
OK = ("#16A34A", "#3DD68C")
OK_SOFT = ("#E8F7EE", "#1B3226")
WARN = ("#B45309", "#F0B429")
WARN_SOFT = ("#FEF4E6", "#33291A")
ERR = ("#DC2626", "#F87171")
ERR_SOFT = ("#FDECEC", "#3A2124")
INPUT_BG = ("#FAFBFD", "#232A33")

FONT = "Microsoft YaHei UI"

PAGE_TITLES = ("任务", "新建任务", "在线视频", "设置", "日志")


def _font(size: int = 13, weight: str = "normal") -> ctk.CTkFont:
    """Build a UI font in the system Chinese face."""
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


def diarizer_backend(config: Any) -> str:
    """Read the speaker-clustering backend from the transcribe config block."""
    block = getattr(config, "transcribe_diarization", {}) or {}
    if not block.get("enabled", True):
        return "none"
    backend = block.get("backend") or block.get("engine") or block.get("type") or "sherpa"
    return str(backend).lower()


class Card(ctk.CTkFrame):
    """White rounded panel used to group related controls."""

    def __init__(self, master: Any, title: str = "", **kwargs: Any) -> None:
        super().__init__(
            master,
            corner_radius=14,
            fg_color=CARD,
            border_width=1,
            border_color=BORDER,
            **kwargs,
        )
        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=18, pady=16)
        if title:
            ctk.CTkLabel(
                self.body, text=title, font=_font(14, "bold"), text_color=TEXT
            ).pack(anchor="w", pady=(0, 10))


class SidebarItem(ctk.CTkButton):
    """Flat sidebar navigation entry."""

    def __init__(self, master: Any, text: str, command: Any) -> None:
        super().__init__(
            master,
            text=text,
            command=command,
            anchor="w",
            height=40,
            corner_radius=9,
            fg_color="transparent",
            hover_color=ACCENT_SOFT,
            text_color=TEXT,
            font=_font(14),
            border_spacing=10,
        )

    def set_active(self, active: bool) -> None:
        """Highlight or dim this entry."""
        self.configure(
            fg_color=ACCENT_SOFT if active else "transparent",
            text_color=ACCENT if active else TEXT,
            font=_font(14, "bold") if active else _font(14),
        )



class App:
    """Main window: sidebar navigation plus five pages."""

    def __init__(
        self, config_path: Optional[str] = None, start_hidden: bool = False
    ) -> None:
        self._config_arg = config_path
        self.cfg = self._load_config(config_path)
        self.config_file = self._config_file()
        self.settings = GuiSettings.load(self._gui_settings_path())
        self.watcher = WatcherRunner()
        self.jobs: dict[str, ChildJob] = {}
        self.job_rows: dict[str, dict[str, Any]] = {}
        self._log_tail = LogTail(Path(self.cfg.log_file))
        self._pages: dict[str, ctk.CTkFrame] = {}
        self._nav: dict[str, SidebarItem] = {}
        self._current_page = PAGE_TITLES[0]
        self._ticks = 0
        self._quitting = False
        self._last_notes_refresh = 0.0
        self._status_text = ""
        self._transcribe_files: list[str] = []
        self._once_file: str = ""
        self._notes_cache: list[tuple[str, str, Path]] = []
        self._tray_icon: Any = None

        ctk.set_appearance_mode("dark" if self.settings.dark_mode else "light")
        self.root = ctk.CTk(fg_color=BG)
        self.root.title(APP_TITLE)
        # customtkinter multiplies geometry by the DPI scale, so compute the
        # logical size from the physical screen to stay inside the display.
        self.root.geometry(self._initial_geometry())
        self.root.minsize(1000, 640)

        self._build_shell()
        self._build_pages()
        self._populate_from_config()
        self._show_page(PAGE_TITLES[0])
        self._init_tray()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        # Customtkinter may scale the geometry a second time once the window is
        # mapped (frozen builds behave differently), so shrink after mapping
        # using measured pixels — unit-agnostic.
        self.root.after(350, self._clamp_to_screen)
        self.root.after(400, self._tick)
        if start_hidden:
            self.root.after(200, self._hide_window)
        # Auto-starting the watcher must not depend on the window being visible:
        # the tray/autostart path (--tray) starts hidden, and that is exactly
        # when background monitoring is wanted.
        if self.settings.start_watcher_on_launch:
            self.root.after(600, self.start_watching)

    def _initial_geometry(self) -> str:
        """Pick a starting window size (corrected after mapping)."""
        self._request_w, self._request_h = 1180, 780
        return f"{self._request_w}x{self._request_h}+40+30"

    def _clamp_to_screen(self) -> None:
        """Fit the window to the screen, compensating the toolkit's scaling.

        customtkinter multiplies a geometry string by its scaling factor before
        handing it to Tk (and the frozen build can multiply it twice), so the
        ratio between the requested and the real size is measured at runtime
        and divided out.
        """
        try:
            self.root.update_idletasks()
            actual_w = self.root.winfo_width()
            factor = max(1.0, actual_w / max(self._request_w, 1))
            screen_w = self.root.winfo_screenwidth()
            screen_h = self.root.winfo_screenheight()
            target_w = int(min(screen_w * 0.90, 1400) / factor)
            target_h = int(min(screen_h * 0.84, 900) / factor)
            self._request_w, self._request_h = target_w, target_h
            self.root.geometry(f"{target_w}x{target_h}+40+30")
        except Exception:  # noqa: BLE001 - cosmetic only
            pass

    # -- configuration --------------------------------------------------------

    @staticmethod
    def _load_config(config_path: Optional[str]) -> Any:
        from src.srt_daemon.config import load_config

        return load_config(config_path)

    def _config_file(self) -> Path:
        """Locate the config.yaml that settings are written back to."""
        if self._config_arg:
            return Path(self._config_arg)
        root = Path(self.cfg.project_root)
        for candidate in (root / "config.yaml", root / "src" / "srt_daemon" / "config.yaml"):
            if candidate.exists():
                return candidate
        return root / "config.yaml"

    def _gui_settings_path(self) -> Path:
        return Path(self.cfg.project_root) / "gui_settings.json"

    # -- shell ----------------------------------------------------------------

    def _build_shell(self) -> None:
        header = ctk.CTkFrame(self.root, fg_color=CARD, corner_radius=0, height=62)
        header.pack(fill="x")
        header.pack_propagate(False)

        title = ctk.CTkFrame(header, fg_color="transparent")
        title.pack(side="left", padx=20)
        ctk.CTkLabel(title, text=APP_NAME, font=_font(20, "bold"), text_color=TEXT).pack(
            side="left"
        )
        ctk.CTkLabel(
            title, text=f"v{__version__}", font=_font(12), text_color=MUTED
        ).pack(side="left", padx=(8, 0))

        chip = ctk.CTkFrame(header, fg_color=OK_SOFT, corner_radius=14)
        chip.pack(side="right", padx=20)
        self.status_dot = ctk.CTkLabel(
            chip, text="●", font=_font(12), text_color=OK, width=14
        )
        self.status_dot.pack(side="left", padx=(10, 2), pady=5)
        self.status_label = ctk.CTkLabel(
            chip, text="已停止", font=_font(12, "bold"), text_color=OK
        )
        self.status_label.pack(side="left", padx=(0, 12), pady=5)
        self._status_chip = chip

        body = ctk.CTkFrame(self.root, fg_color=BG, corner_radius=0)
        body.pack(fill="both", expand=True)

        sidebar = ctk.CTkFrame(body, fg_color=SIDEBAR, corner_radius=0, width=196)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        nav = ctk.CTkFrame(sidebar, fg_color="transparent")
        nav.pack(fill="x", padx=10, pady=(14, 0))
        for name in self._page_names():
            item = SidebarItem(nav, name, lambda n=name: self._show_page(n))
            item.pack(fill="x", pady=2)
            self._nav[name] = item

        footer = ctk.CTkFrame(sidebar, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=12, pady=14)
        ctk.CTkButton(
            footer,
            text="打开输出目录",
            command=lambda: self._open_path(self.cfg.output_dir),
            height=34,
            corner_radius=9,
            fg_color=INPUT_BG,
            hover_color=ACCENT_SOFT,
            text_color=TEXT,
            font=_font(12),
            border_width=1,
            border_color=BORDER,
        ).pack(fill="x")
        ctk.CTkLabel(
            footer,
            text="关闭窗口 → 最小化到托盘",
            font=_font(11),
            text_color=MUTED,
        ).pack(pady=(8, 0))

        self.content = ctk.CTkFrame(body, fg_color=BG, corner_radius=0)
        self.content.pack(side="left", fill="both", expand=True, padx=18, pady=16)
        self.content.grid_rowconfigure(0, weight=1)
        self.content.grid_columnconfigure(0, weight=1)

    def _page_names(self) -> tuple[str, ...]:
        """Which pages to show; 在线视频 disappears when fetch is disabled."""
        names = list(PAGE_TITLES)
        if not self.cfg.fetch_enabled:
            names.remove("在线视频")
        return tuple(names)

    def _build_pages(self) -> None:
        builders = {
            PAGE_TITLES[0]: self._page_tasks,
            PAGE_TITLES[1]: self._page_new,
            PAGE_TITLES[2]: self._page_online,
            PAGE_TITLES[3]: self._page_settings,
            PAGE_TITLES[4]: self._page_logs,
        }
        for name in self._page_names():
            builder = builders[name]
            wrapper = ctk.CTkFrame(self.content, fg_color="transparent")
            wrapper.grid(row=0, column=0, sticky="nsew")
            if name == PAGE_TITLES[4]:
                body: Any = ctk.CTkFrame(wrapper, fg_color="transparent")
            else:
                body = ctk.CTkScrollableFrame(
                    wrapper, fg_color="transparent", corner_radius=0
                )
            body.pack(fill="both", expand=True)
            builder(body)
            self._pages[name] = wrapper

    def _show_page(self, name: str) -> None:
        self._pages[name].tkraise()
        self._current_page = name
        for key, item in self._nav.items():
            item.set_active(key == name)

    # -- page: 任务 -----------------------------------------------------------

    def _page_tasks(self, page: ctk.CTkFrame) -> None:
        dirs = Card(page, "目录")
        dirs.pack(fill="x", pady=(0, 12))
        self.watch_var = ctk.StringVar(value=str(self.cfg.watch_dir))
        self.output_var = ctk.StringVar(value=str(self.cfg.output_dir))
        self._path_row(dirs.body, "监听目录", self.watch_var, self.cfg.watch_dir)
        self._path_row(dirs.body, "输出目录", self.output_var, self.cfg.output_dir)

        controls = Card(page, "监听控制")
        controls.pack(fill="x", pady=(0, 12))
        row = ctk.CTkFrame(controls.body, fg_color="transparent")
        row.pack(fill="x")
        ctk.CTkLabel(row, text="笔记类型", font=_font(13), text_color=MUTED, width=70).pack(
            side="left"
        )
        self.profile_var = ctk.StringVar(value=self.settings.last_profile)
        ctk.CTkComboBox(
            row,
            values=list(PROFILE_CHOICES),
            variable=self.profile_var,
            width=130,
            height=34,
            state="readonly",
            fg_color=INPUT_BG,
            border_color=BORDER,
            button_color=BORDER,
            font=_font(12),
        ).pack(side="left")
        ctk.CTkButton(
            row, text="启动监听", command=self.start_watching, width=110, height=34,
            corner_radius=9, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=_font(13, "bold"),
        ).pack(side="left", padx=(16, 6))
        for text, command in (
            ("停止监听", self.stop_watching),
            ("立即扫描一次", self.run_scan),
            ("处理单个文件…", self.run_once),
        ):
            ctk.CTkButton(
                row, text=text, command=command, width=108, height=34, corner_radius=9,
                fg_color=CARD, hover_color=ACCENT_SOFT, text_color=TEXT,
                border_width=1, border_color=BORDER, font=_font(12),
            ).pack(side="left", padx=4)

        jobs = Card(page, "任务")
        jobs.pack(fill="x", pady=(0, 12))
        self.jobs_frame = ctk.CTkFrame(jobs.body, fg_color="transparent")
        self.jobs_frame.pack(fill="x")
        for key, title in (
            ("scan", "扫描一次"),
            ("once", "单文件处理"),
            ("transcribe", "音视频转录"),
            ("fetch", "在线视频"),
        ):
            self._make_job_row(key, title)

        notes = Card(page, "最近生成的笔记")
        notes.pack(fill="x", pady=(0, 20))
        head = ctk.CTkFrame(notes.body, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(
            head, text="双击打开", font=_font(11), text_color=MUTED
        ).pack(side="left")
        ctk.CTkButton(
            head, text="刷新", command=self._refresh_outputs, width=64, height=26,
            corner_radius=8, fg_color=INPUT_BG, hover_color=ACCENT_SOFT,
            text_color=MUTED, font=_font(11),
        ).pack(side="right")
        self.notes_frame = ctk.CTkFrame(notes.body, fg_color="transparent")
        self.notes_frame.pack(fill="x", pady=(8, 0))

    def _path_row(
        self, parent: Any, label: str, variable: Any, path: Path
    ) -> None:
        """A labelled read-only path field with an "open" button."""
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            row, text=label, font=_font(13), text_color=MUTED, width=70, anchor="w"
        ).pack(side="left")
        ctk.CTkEntry(
            row, textvariable=variable, height=34, fg_color=INPUT_BG,
            border_color=BORDER, font=_font(12),
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(
            row, text="打开", command=lambda: self._open_path(Path(variable.get())),
            width=64, height=34, corner_radius=9, fg_color=CARD,
            hover_color=ACCENT_SOFT, text_color=ACCENT, border_width=1,
            border_color=BORDER, font=_font(12),
        ).pack(side="left")

    def _make_job_row(self, key: str, title: str) -> None:
        """Create the status row for one background job."""
        row = ctk.CTkFrame(self.jobs_frame, fg_color="transparent")
        row.pack(fill="x", pady=3)
        label = ctk.CTkLabel(
            row, text=title, font=_font(12), text_color=TEXT, width=96, anchor="w"
        )
        label.pack(side="left")
        bar = ctk.CTkProgressBar(
            row, height=4, corner_radius=2, progress_color=ACCENT, fg_color=BORDER
        )
        bar.set(0)
        bar.pack(side="left", fill="x", expand=True, padx=(0, 10))
        status = ctk.CTkLabel(row, text="空闲", font=_font(11), text_color=MUTED, width=72)
        status.pack(side="left")
        stop = ctk.CTkButton(
            row, text="停止", command=lambda k=key: self._stop_job(k), width=52,
            height=26, corner_radius=8, fg_color=CARD, hover_color=ERR_SOFT,
            text_color=MUTED, border_width=1, border_color=BORDER, font=_font(11),
        )
        stop.pack(side="left")
        self.job_rows[key] = {"bar": bar, "status": status, "stop": stop}


    # -- page: 新建任务 -------------------------------------------------------

    def _page_new(self, page: ctk.CTkFrame) -> None:
        local = Card(page, "本地字幕 → 笔记")
        local.pack(fill="x", pady=(0, 12))
        ctk.CTkLabel(
            local.body,
            text="选择一个 .srt / .vtt 文件，立即生成 Markdown 笔记（不启动监听）。",
            font=_font(12),
            text_color=MUTED,
        ).pack(anchor="w")
        row = ctk.CTkFrame(local.body, fg_color="transparent")
        row.pack(fill="x", pady=(10, 0))
        self.once_label = ctk.CTkLabel(
            row, text="未选择文件", font=_font(12), text_color=MUTED, anchor="w"
        )
        self.once_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            row, text="选择字幕文件…", command=self._pick_once_file, width=124,
            height=34, corner_radius=9, fg_color=CARD, hover_color=ACCENT_SOFT,
            text_color=TEXT, border_width=1, border_color=BORDER, font=_font(12),
        ).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            row, text="生成笔记", command=self._run_once_selected, width=104,
            height=34, corner_radius=9, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=_font(12, "bold"),
        ).pack(side="left")

        once_options = ctk.CTkFrame(local.body, fg_color="transparent")
        once_options.pack(fill="x", pady=(10, 0))
        self.once_profile_var = ctk.StringVar(value=self.settings.last_profile)
        ctk.CTkLabel(
            once_options, text="笔记类型", font=_font(11), text_color=MUTED
        ).pack(side="left")
        ctk.CTkComboBox(
            once_options, values=list(PROFILE_CHOICES), variable=self.once_profile_var,
            width=130, height=32, state="readonly", fg_color=INPUT_BG,
            border_color=BORDER, button_color=BORDER, font=_font(12),
        ).pack(side="left", padx=(6, 16))
        ctk.CTkLabel(
            once_options,
            text="同目录同名视频会自动用于截图（如 talk.mp4 + talk.srt）",
            font=_font(11),
            text_color=MUTED,
        ).pack(side="left")
        ctk.CTkLabel(
            local.body,
            text="字幕没有音频，无法做说话人聚类：想区分说话人请把音视频丢到下面的"
            "「音频 / 视频 → 字幕」并填说话人数。",
            font=_font(10),
            text_color=MUTED,
            wraplength=760,
            justify="left",
        ).pack(anchor="w", pady=(6, 0))

        media = Card(page, "音频 / 视频 → 字幕")
        media.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(
            media.body,
            text="本地录音或录像 → 带说话人的字幕；勾选下面「转录后自动生成笔记」可直接出笔记。",
            font=_font(12),
            text_color=MUTED,
        ).pack(anchor="w")
        pick = ctk.CTkFrame(media.body, fg_color="transparent")
        pick.pack(fill="x", pady=(10, 6))
        for text, command, primary in (
            ("选择文件…", self._pick_media_files, False),
            ("选择文件夹…", self._pick_media_dir, False),
            ("清除", self._clear_media_files, False),
            ("列出待处理", self._list_media_jobs, False),
            ("开始转录", self.start_transcribe, True),
            ("停止", self.stop_transcribe, False),
        ):
            ctk.CTkButton(
                pick, text=text, command=command, width=96, height=34, corner_radius=9,
                fg_color=ACCENT if primary else CARD,
                hover_color=ACCENT_HOVER if primary else ACCENT_SOFT,
                text_color="#FFFFFF" if primary else TEXT,
                border_width=0 if primary else 1, border_color=BORDER,
                font=_font(12, "bold" if primary else "normal"),
            ).pack(side="left", padx=(0, 8))
        self.media_label = ctk.CTkLabel(
            media.body, text="未选择文件", font=_font(12), text_color=MUTED, anchor="w"
        )
        self.media_label.pack(fill="x")

        options = ctk.CTkFrame(media.body, fg_color="transparent")
        options.pack(fill="x", pady=(12, 0))
        for column in range(5):
            options.grid_columnconfigure(column, weight=1)

        self.trans_model_var = ctk.StringVar(
            value=self.settings.last_whisper_model or self.cfg.transcribe_model
        )
        self.trans_device_var = ctk.StringVar(value=self.cfg.transcribe_device)
        self.trans_lang_var = ctk.StringVar(
            value=self.settings.last_language or self.cfg.transcribe_language or "auto"
        )
        self.trans_diar_var = ctk.StringVar(
            value=self.settings.last_diarizer or diarizer_backend(self.cfg)
        )
        self.trans_num_var = ctk.StringVar(value="")
        self.trans_map_var = ctk.StringVar(value="")
        self.trans_format_var = ctk.StringVar(value=self.cfg.transcribe_output_format)
        self.trans_force_var = ctk.BooleanVar(value=False)

        self.trans_compute_var = ctk.StringVar(value=self.cfg.transcribe_compute_type)
        self._combo_option(
            options, "模型", self.trans_model_var, WHISPER_MODELS, 0, 0, editable=True
        )
        self._combo_option(options, "设备", self.trans_device_var, DEVICES, 1, 0)
        self._combo_option(
            options, "计算精度", self.trans_compute_var, COMPUTE_TYPES, 2, 0
        )
        self._combo_option(options, "语言", self.trans_lang_var, LANGS, 3, 0)
        self._combo_option(options, "聚类后端", self.trans_diar_var, DIARIZERS, 4, 0)
        self._combo_option(options, "输出格式", self.trans_format_var, OUTPUT_FORMATS, 0, 1)
        self._entry_option(options, "说话人数", self.trans_num_var, 1, 1, "空 = 自动")
        self._entry_option(options, "姓名映射", self.trans_map_var, 2, 1, "1=张三,2=李四")
        self.trans_profile_var = ctk.StringVar(value=self.settings.last_profile)
        self.trans_notes_var = ctk.BooleanVar(value=True)
        self._combo_option(
            options, "笔记类型", self.trans_profile_var, PROFILE_CHOICES, 0, 2
        )
        ctk.CTkCheckBox(
            options, text="转录后自动生成笔记", variable=self.trans_notes_var,
            font=_font(12), checkbox_width=18, checkbox_height=18,
            corner_radius=4, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            border_color=BORDER,
        ).grid(row=2, column=1, sticky="w", padx=8, pady=6)
        ctk.CTkLabel(
            options,
            text="设备 auto = 先试 GPU，失败自动回退 CPU",
            font=_font(10),
            text_color=MUTED,
        ).grid(row=2, column=3, columnspan=2, sticky="w", padx=8)
        ctk.CTkCheckBox(
            options, text="强制重跑", variable=self.trans_force_var, font=_font(12),
            checkbox_width=18, checkbox_height=18, corner_radius=4,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER,
        ).grid(row=1, column=3, sticky="w", padx=8, pady=6)

    def _combo_option(
        self, parent: Any, label: str, variable: Any, values: Any, column: int, row: int,
        editable: bool = False,
    ) -> None:
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid(row=row, column=column, sticky="ew", padx=8, pady=6)
        ctk.CTkLabel(box, text=label, font=_font(11), text_color=MUTED).pack(anchor="w")
        ctk.CTkComboBox(
            box, values=list(values), variable=variable, height=32,
            state="normal" if editable else "readonly",
            fg_color=INPUT_BG, border_color=BORDER, button_color=BORDER, font=_font(12),
        ).pack(fill="x", pady=(2, 0))

    def _entry_option(
        self, parent: Any, label: str, variable: Any, column: int, row: int, hint: str
    ) -> None:
        box = ctk.CTkFrame(parent, fg_color="transparent")
        box.grid(row=row, column=column, sticky="ew", padx=8, pady=6)
        ctk.CTkLabel(box, text=label, font=_font(11), text_color=MUTED).pack(anchor="w")
        ctk.CTkEntry(
            box, textvariable=variable, height=32, placeholder_text=hint,
            fg_color=INPUT_BG, border_color=BORDER, font=_font(12),
        ).pack(fill="x", pady=(2, 0))

    # -- page: 在线视频 -------------------------------------------------------

    def _page_online(self, page: ctk.CTkFrame) -> None:
        address = Card(page, "视频链接")
        address.pack(fill="x", pady=(0, 12))
        row = ctk.CTkFrame(address.body, fg_color="transparent")
        row.pack(fill="x")
        self.url_var = ctk.StringVar(value="")
        ctk.CTkEntry(
            row, textvariable=self.url_var,
            placeholder_text="粘贴 B站 / YouTube / 抖音 链接",
            height=36, fg_color=INPUT_BG, border_color=BORDER, font=_font(12),
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkButton(
            row, text="解析", command=self.fetch_probe, width=76, height=36,
            corner_radius=9, fg_color=CARD, hover_color=ACCENT_SOFT, text_color=TEXT,
            border_width=1, border_color=BORDER, font=_font(12),
        ).pack(side="left", padx=(0, 8))

        options = ctk.CTkFrame(address.body, fg_color="transparent")
        options.pack(fill="x", pady=(10, 0))
        self.part_var = ctk.StringVar(value="1")
        self.part_combo = ctk.CTkComboBox(
            options, values=["1"], variable=self.part_var, width=110, height=34,
            state="readonly", fg_color=INPUT_BG, border_color=BORDER,
            button_color=BORDER, font=_font(12),
        )
        ctk.CTkLabel(options, text="分P", font=_font(11), text_color=MUTED).pack(side="left")
        self.part_combo.pack(side="left", padx=(6, 14))
        self.online_profile_var = ctk.StringVar(value=self.settings.last_profile)
        ctk.CTkLabel(options, text="笔记类型", font=_font(11), text_color=MUTED).pack(
            side="left"
        )
        ctk.CTkComboBox(
            options, values=list(PROFILE_CHOICES), variable=self.online_profile_var,
            width=120, height=34, state="readonly", fg_color=INPUT_BG,
            border_color=BORDER, button_color=BORDER, font=_font(12),
        ).pack(side="left", padx=(6, 14))
        self.online_shots_var = ctk.BooleanVar(value=self.cfg.screenshots_enabled)
        ctk.CTkCheckBox(
            options, text="插入截图", variable=self.online_shots_var, font=_font(12),
            checkbox_width=18, checkbox_height=18, corner_radius=4,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER,
        ).pack(side="left", padx=(0, 14))
        ctk.CTkButton(
            options, text="生成笔记", command=self.run_fetch, width=110, height=34,
            corner_radius=9, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=_font(12, "bold"),
        ).pack(side="left")
        ctk.CTkButton(
            options, text="停止", command=lambda: self._stop_job("fetch"), width=68,
            height=34, corner_radius=9, fg_color=CARD, hover_color=ERR_SOFT,
            text_color=TEXT, border_width=1, border_color=BORDER, font=_font(12),
        ).pack(side="left", padx=8)

        info = Card(page, "抓取信息")
        info.pack(fill="x", pady=(0, 12))
        self.online_text = ctk.CTkTextbox(
            info.body, fg_color=INPUT_BG, border_width=1, border_color=BORDER,
            corner_radius=9, font=_font(12, "normal"), wrap="word",
        )
        self.online_text.configure(height=210)
        self.online_text.pack(fill="both", expand=True)
        self.online_text.insert(
            "end",
            "先点「解析」查看标题、时长、分P 和可用字幕语言；再点「生成笔记」。\n\n"
            "有 CC 字幕时几秒完成；没有字幕会下载音频并用本地 whisper 转写（慢）。\n"
            "B站 CC 字幕需要 cookies：见 设置 → 抓取。\n",
        )
        self.online_text.configure(state="disabled")

        hint = Card(page, "提示")
        hint.pack(fill="x")
        ctk.CTkLabel(
            hint.body,
            text=(
                "抖音 / 快手 标为实验性：通常需要 cookie，且 yt-dlp 需保持更新。\n"
                "插入截图会额外下载视频（≤ 设置的清晰度上限），抽帧后自动删除。"
            ),
            font=_font(11),
            text_color=MUTED,
            justify="left",
        ).pack(anchor="w")


    # -- page: 设置 -----------------------------------------------------------

    def _page_settings(self, page: ctk.CTkFrame) -> None:
        scroller = page

        paths = Card(scroller, "路径")
        paths.pack(fill="x", pady=(0, 12))
        self.set_watch_var = ctk.StringVar()
        self.set_output_var = ctk.StringVar()
        self._settings_row(paths.body, "监听目录", self.set_watch_var, "选择…", self._browse_watch)
        self._settings_row(paths.body, "输出目录", self.set_output_var, "选择…", self._browse_output)

        ai = Card(scroller, "AI")
        ai.pack(fill="x", pady=(0, 12))
        self.ai_model_var = ctk.StringVar()
        self.set_profile_var = ctk.StringVar()
        self._settings_combo(ai.body, "模型", self.ai_model_var, AI_MODELS)
        self._settings_combo(ai.body, "默认笔记类型", self.set_profile_var, PROFILE_CHOICES)

        tr = Card(scroller, "转录")
        tr.pack(fill="x", pady=(0, 12))
        self.set_model_var = ctk.StringVar()
        self.set_device_var = ctk.StringVar()
        self.set_lang_var = ctk.StringVar()
        self.set_diar_var = ctk.StringVar()
        self.set_threads_var = ctk.StringVar()
        self._settings_combo(tr.body, "Whisper 模型", self.set_model_var, WHISPER_MODELS)
        self._settings_combo(tr.body, "设备", self.set_device_var, ("cpu", "cuda"))
        self._settings_combo(tr.body, "语言", self.set_lang_var, LANGS)
        self._settings_combo(tr.body, "说话人聚类", self.set_diar_var, DIARIZERS)
        self._settings_row(tr.body, "CPU 线程", self.set_threads_var, "", None)

        fetch = Card(scroller, "抓取（在线视频）")
        fetch.pack(fill="x", pady=(0, 12))
        self.fetch_proxy_var = ctk.StringVar()
        self.fetch_cookies_var = ctk.StringVar()
        self.fetch_height_var = ctk.StringVar()
        self._settings_row(fetch.body, "代理", self.fetch_proxy_var, "", None,
                           placeholder="http://127.0.0.1:10808（留空=直连）")
        self._settings_row(fetch.body, "Cookies 文件", self.fetch_cookies_var, "选择…",
                           self._browse_cookies, placeholder="cookies/bilibili.txt")
        self._settings_row(fetch.body, "清晰度上限", self.fetch_height_var, "", None,
                           placeholder="1080")

        shots = Card(scroller, "截图")
        shots.pack(fill="x", pady=(0, 12))
        self.shot_enabled_var = ctk.BooleanVar(value=False)
        self.shot_max_var = ctk.StringVar()
        self.shot_width_var = ctk.StringVar()
        ctk.CTkCheckBox(
            shots.body, text="笔记里插入截图（需要视频）", variable=self.shot_enabled_var,
            font=_font(12), checkbox_width=18, checkbox_height=18, corner_radius=4,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, border_color=BORDER,
        ).pack(anchor="w", pady=(0, 6))
        self._settings_row(shots.body, "最多张数", self.shot_max_var, "", None, placeholder="12")
        self._settings_row(shots.body, "图片宽度", self.shot_width_var, "", None, placeholder="1280")

        window = Card(scroller, "窗口与启动")
        window.pack(fill="x", pady=(0, 12))
        self.auto_watch_var = ctk.BooleanVar(value=True)
        self.tray_var = ctk.BooleanVar(value=True)
        self.autostart_var = ctk.BooleanVar(value=False)
        self.dark_var = ctk.BooleanVar(value=False)
        for text, variable, command in (
            ("启动后自动开始监听", self.auto_watch_var, None),
            ("关闭窗口时最小化到托盘", self.tray_var, None),
            ("开机自动启动（隐藏到托盘）", self.autostart_var, None),
            ("深色模式", self.dark_var, self.apply_theme),
        ):
            ctk.CTkCheckBox(
                window.body, text=text, variable=variable, command=command,
                font=_font(12), checkbox_width=18, checkbox_height=18,
                corner_radius=4, fg_color=ACCENT, hover_color=ACCENT_HOVER,
                border_color=BORDER,
            ).pack(anchor="w", pady=3)

        save = ctk.CTkFrame(scroller, fg_color="transparent")
        save.pack(fill="x", pady=(4, 20))
        ctk.CTkButton(
            save, text="保存设置", command=self.save_settings, width=120, height=38,
            corner_radius=10, fg_color=ACCENT, hover_color=ACCENT_HOVER,
            font=_font(13, "bold"),
        ).pack(side="left")
        self.settings_status = ctk.CTkLabel(
            save, text="", font=_font(12), text_color=MUTED
        )
        self.settings_status.pack(side="left", padx=12)

    def apply_theme(self) -> None:
        """Switch between the light and dark palette immediately."""
        try:
            ctk.set_appearance_mode("dark" if self.dark_var.get() else "light")
        except Exception:  # noqa: BLE001 - cosmetic only
            pass

    def _settings_row(
        self, parent: Any, label: str, variable: Any, button: str,
        command: Any, placeholder: str = "",
    ) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            row, text=label, font=_font(12), text_color=MUTED, width=100, anchor="w"
        ).pack(side="left")
        ctk.CTkEntry(
            row, textvariable=variable, height=32, placeholder_text=placeholder,
            fg_color=INPUT_BG, border_color=BORDER, font=_font(12),
        ).pack(side="left", fill="x", expand=True, padx=(0, 8))
        if command is not None:
            ctk.CTkButton(
                row, text=button, command=command, width=68, height=32, corner_radius=8,
                fg_color=CARD, hover_color=ACCENT_SOFT, text_color=ACCENT,
                border_width=1, border_color=BORDER, font=_font(12),
            ).pack(side="left")

    def _settings_combo(self, parent: Any, label: str, variable: Any, values: Any) -> None:
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", pady=4)
        ctk.CTkLabel(
            row, text=label, font=_font(12), text_color=MUTED, width=100, anchor="w"
        ).pack(side="left")
        ctk.CTkComboBox(
            row, values=list(values), variable=variable, height=32, state="readonly",
            fg_color=INPUT_BG, border_color=BORDER, button_color=BORDER, font=_font(12),
        ).pack(side="left", fill="x", expand=True)

    # -- page: 日志 -----------------------------------------------------------

    def _page_logs(self, page: ctk.CTkFrame) -> None:
        box = Card(page, "运行日志")
        box.pack(fill="both", expand=True)
        row = ctk.CTkFrame(box.body, fg_color="transparent")
        row.pack(fill="x", pady=(0, 8))
        ctk.CTkButton(
            row, text="清空显示", command=self._clear_log, width=88, height=30,
            corner_radius=8, fg_color=CARD, hover_color=ACCENT_SOFT, text_color=TEXT,
            border_width=1, border_color=BORDER, font=_font(12),
        ).pack(side="left")
        ctk.CTkButton(
            row, text="打开日志文件", command=lambda: self._open_path(self.cfg.log_file),
            width=104, height=30, corner_radius=8, fg_color=CARD,
            hover_color=ACCENT_SOFT, text_color=TEXT, border_width=1,
            border_color=BORDER, font=_font(12),
        ).pack(side="left", padx=8)
        self.log_text = ctk.CTkTextbox(
            box.body, fg_color="#0F1419", text_color="#D6DEE8", corner_radius=9,
            font=ctk.CTkFont(family="Consolas", size=12), wrap="none",
        )
        self.log_text.pack(fill="both", expand=True)
        self.log_text.configure(state="disabled")
        existing = self._log_tail.initial()
        if existing:
            self._append_log(existing)

    def _append_log(self, text: str) -> None:
        """Append text to the log page (thread-safe enough for our uses)."""
        widget = getattr(self, "log_text", None)
        if widget is None:
            return
        try:
            widget.configure(state="normal")
            widget.insert("end", text)
            widget.see("end")
            widget.configure(state="disabled")
        except Exception:  # noqa: BLE001 - the window may be closing
            pass

    def _append_online(self, text: str) -> None:
        widget = getattr(self, "online_text", None)
        if widget is None:
            return
        try:
            widget.configure(state="normal")
            widget.insert("end", text)
            widget.see("end")
            widget.configure(state="disabled")
        except Exception:  # noqa: BLE001
            pass

    def _clear_log(self) -> None:
        if self.log_text is None:
            return
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # -- population -----------------------------------------------------------

    def _populate_from_config(self) -> None:
        """Fill every widget from the loaded configuration."""
        try:
            self.watch_var.set(str(self.cfg.watch_dir))
            self.output_var.set(str(self.cfg.output_dir))
            self.profile_var.set(self.settings.last_profile)
            self.online_profile_var.set(self.settings.last_profile)

            self.set_watch_var.set(str(self.cfg.watch_dir))
            self.set_output_var.set(str(self.cfg.output_dir))
            self.ai_model_var.set(self.cfg.ai_model)
            self.set_profile_var.set(self.settings.last_profile)
            self.set_model_var.set(self.cfg.transcribe_model)
            self.set_device_var.set(self.cfg.transcribe_device)
            self.set_lang_var.set(self.cfg.transcribe_language or "auto")
            self.set_diar_var.set(diarizer_backend(self.cfg))
            self.set_threads_var.set(str(self.cfg.transcribe_cpu_threads))
            self.trans_model_var.set(self.cfg.transcribe_model)
            self.trans_device_var.set(self.cfg.transcribe_device)
            self.trans_lang_var.set(self.settings.last_language or self.cfg.transcribe_language or "auto")
            self.trans_diar_var.set(diarizer_backend(self.cfg))

            cookies = self.cfg.fetch_cookies_file
            self.fetch_proxy_var.set(self.cfg.fetch_proxy or "")
            self.fetch_cookies_var.set(str(cookies) if cookies else "")
            self.fetch_height_var.set(str(self.cfg.fetch_max_height))
            self.shot_enabled_var.set(self.cfg.screenshots_enabled)
            self.online_shots_var.set(self.cfg.screenshots_enabled)
            self.shot_max_var.set(str(self.cfg.screenshots_max_count))
            self.shot_width_var.set(str(self.cfg.screenshots_width))

            self.auto_watch_var.set(self.settings.start_watcher_on_launch)
            self.tray_var.set(self.settings.minimize_to_tray)
            self.autostart_var.set(is_autostart_enabled())
            self.dark_var.set(self.settings.dark_mode)
        except Exception:  # noqa: BLE001 - never block startup on cosmetics
            pass

    # -- actions --------------------------------------------------------------

    def _open_path(self, path: Path) -> None:
        try:
            import os

            os.startfile(str(path))  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[打开失败] {exc}\n")

    def _browse_watch(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(title="选择监听目录")
        if chosen:
            self.set_watch_var.set(chosen)

    def _browse_output(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(title="选择输出目录")
        if chosen:
            self.set_output_var.set(chosen)

    def _browse_cookies(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askopenfilename(
            title="选择 cookies.txt", filetypes=[("Cookies", "*.txt"), ("All", "*.*")]
        )
        if chosen:
            self.fetch_cookies_var.set(chosen)

    def save_settings(self) -> None:
        """Persist the settings page into config.yaml and gui_settings.json."""
        path = self.config_file
        try:
            if path.exists():
                set_config_value(path, "watch", "directory", self.set_watch_var.get().strip())
                set_config_value(path, "output", "directory", self.set_output_var.get().strip())
                set_config_value(path, "ai", "model", self.ai_model_var.get())
                set_config_value(path, "transcribe", "model", self.set_model_var.get())
                set_config_value(path, "transcribe", "device", self.set_device_var.get())
                set_config_value(path, "transcribe", "language", self.set_lang_var.get())
                set_config_value(path, "transcribe", "diarizer", self.set_diar_var.get())
                threads = self.set_threads_var.get().strip()
                if threads.isdigit():
                    set_config_value(path, "transcribe", "cpu_threads", int(threads))
                set_config_value(path, "fetch", "proxy", self.fetch_proxy_var.get().strip() or None)
                set_config_value(path, "fetch", "cookies_file", self.fetch_cookies_var.get().strip() or None)
                height = self.fetch_height_var.get().strip()
                if height.isdigit():
                    set_config_value(path, "fetch", "max_height", int(height))
                set_config_value(path, "screenshots", "enabled", bool(self.shot_enabled_var.get()))
                maximum = self.shot_max_var.get().strip()
                if maximum.isdigit():
                    set_config_value(path, "screenshots", "max_count", int(maximum))
                width = self.shot_width_var.get().strip()
                if width.isdigit():
                    set_config_value(path, "screenshots", "width", int(width))

            self.settings.start_watcher_on_launch = bool(self.auto_watch_var.get())
            self.settings.minimize_to_tray = bool(self.tray_var.get())
            self.settings.dark_mode = bool(self.dark_var.get())
            self.settings.last_profile = self.set_profile_var.get()
            self.settings.last_whisper_model = self.set_model_var.get()
            self.settings.last_diarizer = self.set_diar_var.get()
            self.settings.last_language = self.set_lang_var.get()
            self.settings.save(self._gui_settings_path())
            set_autostart(bool(self.autostart_var.get()))

            self.cfg = self._load_config(self._config_arg)
            self._populate_from_config()
            self.settings_status.configure(text="已保存 ✓", text_color=OK)
            self._append_log("[设置] 已保存\n")
        except Exception as exc:  # noqa: BLE001 - surfaced in the UI
            self.settings_status.configure(text=f"保存失败: {exc}", text_color=ERR)

    # -- watcher / jobs -------------------------------------------------------

    def start_watching(self) -> None:
        """Start the notes pipeline in a background thread."""
        if self.watcher.running:
            return
        try:
            self.watcher.start(self.cfg, None if self.profile_var.get() == "auto" else self.profile_var.get())
        except Exception as exc:  # noqa: BLE001
            self._append_log(f"[监听] 启动失败: {exc}\n")
            return
        self._append_log("[监听] 已启动\n")
        self._refresh_status()

    def stop_watching(self) -> None:
        """Stop the background watcher."""
        self.watcher.stop()
        self._append_log("[监听] 已停止\n")
        self._refresh_status()

    def _start_job(self, key: str, args: list[str]) -> bool:
        job = self.jobs.get(key)
        if job is None:
            job = ChildJob(key)
            self.jobs[key] = job
        if job.running:
            self._append_log(f"[任务] {key} 正在运行，请先等待或停止\n")
            return False
        row = self.job_rows.get(key)
        ok = job.start(args, cwd=Path(self.cfg.project_root))
        if ok and row is not None:
            row["status"].configure(text="运行中", text_color=ACCENT)
            row["bar"].set(0)
            row["bar"].start()
            self._append_log(f"[任务] 启动 {key}: {' '.join(args)}\n")
        elif row is not None:
            row["status"].configure(text="启动失败", text_color=ERR)
        return ok

    def _stop_job(self, key: str) -> None:
        job = self.jobs.get(key)
        if job is None:
            return
        job.stop()
        row = self.job_rows.get(key)
        if row is not None:
            row["bar"].stop()
            row["bar"].set(0)
            row["status"].configure(text="已停止", text_color=MUTED)
        self._append_log(f"[任务] 已停止 {key}\n")

    def _job_args_profile(self) -> list[str]:
        profile = self.profile_var.get()
        return [] if profile == "auto" else ["--profile", profile]

    def run_scan(self) -> None:
        """Process every unprocessed subtitle once."""
        self._start_job("scan", ["--scan", *self._job_args_profile()])

    def run_once(self) -> None:
        """Pick one subtitle file and process it."""
        from tkinter import filedialog

        chosen = filedialog.askopenfilename(
            title="选择字幕文件",
            filetypes=[("Subtitles", "*.srt *.vtt"), ("All files", "*.*")],
        )
        if not chosen:
            return
        self._start_job("once", ["--once", chosen, *self._job_args_profile()])

    def _pick_once_file(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askopenfilename(
            title="选择字幕文件", filetypes=[("Subtitles", "*.srt *.vtt"), ("All", "*.*")]
        )
        if chosen:
            # Keep the full path: the job runs with the project root as cwd, so
            # a bare file name would point at the wrong folder.
            self._once_file = chosen
            self.once_label.configure(text=Path(chosen).name, text_color=TEXT)

    def _run_once_selected(self) -> None:
        chosen = getattr(self, "_once_file", "")
        if not chosen:
            from tkinter import filedialog

            picked = filedialog.askopenfilename(
                title="选择字幕文件", filetypes=[("Subtitles", "*.srt *.vtt")]
            )
            if not picked:
                return
            self._once_file = picked
            self.once_label.configure(text=Path(picked).name, text_color=TEXT)
            chosen = picked
        profile = self.once_profile_var.get()
        extra = [] if profile == "auto" else ["--profile", profile]
        self._start_job("once", ["--once", chosen, *extra])

    # -- transcription --------------------------------------------------------

    def _pick_media_files(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askopenfilenames(
            title="选择音频或视频",
            filetypes=[("Media", "*.mp3 *.m4a *.wav *.mp4 *.mkv *.mov *.flv"), ("All", "*.*")],
        )
        if chosen:
            self._transcribe_files = list(chosen)
            self._show_media_selection()

    def _pick_media_dir(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(title="选择包含音频/视频的文件夹")
        if chosen:
            self._transcribe_files = [chosen]
            self._show_media_selection()

    def _clear_media_files(self) -> None:
        self._transcribe_files = []
        self._show_media_selection()

    def _show_media_selection(self) -> None:
        if not self._transcribe_files:
            self.media_label.configure(text="未选择文件", text_color=MUTED)
        elif len(self._transcribe_files) == 1:
            self.media_label.configure(
                text=Path(self._transcribe_files[0]).name, text_color=TEXT
            )
        else:
            self.media_label.configure(
                text=f"已选择 {len(self._transcribe_files)} 项", text_color=TEXT
            )

    def _transcribe_args(self) -> list[str]:
        args = [
            "--transcribe",
            *self._transcribe_files,
            "--model",
            self.trans_model_var.get(),
            "--device",
            self.trans_device_var.get(),
            "--compute-type",
            self.trans_compute_var.get(),
            "--diarizer",
            self.trans_diar_var.get(),
            "--format",
            self.trans_format_var.get(),
        ]
        if self.trans_lang_var.get() not in ("", "auto"):
            args += ["--language", self.trans_lang_var.get()]
        number = self.trans_num_var.get().strip()
        if number.isdigit():
            args += ["--num-speakers", number]
        mapping = self.trans_map_var.get().strip()
        if mapping:
            args += ["--speakers", mapping]
        if self.trans_force_var.get():
            args += ["--force"]
        profile = self.trans_profile_var.get()
        if self.trans_notes_var.get() or profile != "auto":
            args += ["--notes"]
        if profile != "auto":
            args += ["--profile", profile]
        return args

    def _list_media_jobs(self) -> None:
        self._start_job("transcribe", ["--transcribe", "--list", *self._transcribe_files])

    def start_transcribe(self) -> None:
        if not self._transcribe_files:
            self._append_log("[转录] 请先选择文件或文件夹\n")
            return
        self._start_job("transcribe", self._transcribe_args())

    def stop_transcribe(self) -> None:
        self._stop_job("transcribe")

    # -- online video ---------------------------------------------------------

    def fetch_probe(self) -> None:
        """Resolve the URL without downloading (title, parts, subtitles)."""
        url = self.url_var.get().strip()
        if not url:
            self._append_online("请先粘贴链接。\n")
            return
        self._append_online(f"\n--- 解析 {url} ---\n")
        self._start_job("fetch", ["--fetch", "--dry-run", url])

    def run_fetch(self) -> None:
        """Turn the URL into a note."""
        url = self.url_var.get().strip()
        if not url:
            self._append_online("请先粘贴链接。\n")
            return
        args = ["--fetch", url, "--part", self.part_var.get()]
        profile = self.online_profile_var.get()
        if profile != "auto":
            args += ["--profile", profile]
        args += ["--screenshots"] if self.online_shots_var.get() else ["--no-screenshots"]
        self._append_online(f"\n--- 生成笔记 {url} ---\n")
        self._start_job("fetch", args)

    # -- refresh --------------------------------------------------------------

    def _refresh_status(self) -> None:
        running = self.watcher.running
        if running:
            self._status_chip.configure(fg_color=OK_SOFT)
            self.status_label.configure(text="监听中", text_color=OK)
            self.status_dot.configure(text_color=OK)
        else:
            self._status_chip.configure(fg_color=ERR_SOFT)
            self.status_label.configure(text="已停止", text_color=ERR)
            self.status_dot.configure(text_color=ERR)
        error = self.watcher.last_error
        if error and error != self._status_text:
            self._status_text = error
            self._append_log(f"[监听] 异常: {error}\n")

    def _refresh_outputs(self) -> None:
        """Rebuild the recent-notes list from the output directory."""
        frame = getattr(self, "notes_frame", None)
        if frame is None:
            return
        try:
            notes = sorted(
                Path(self.cfg.output_dir).rglob("*.md"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )[:20]
        except OSError:
            notes = []
        for child in frame.winfo_children():
            child.destroy()
        if not notes:
            ctk.CTkLabel(
                frame, text="还没有笔记", font=_font(12), text_color=MUTED
            ).pack(anchor="w", pady=6)
            return
        for note in notes:
            row = ctk.CTkFrame(frame, fg_color="transparent", cursor="hand2")
            row.pack(fill="x", pady=1)
            name = ctk.CTkLabel(
                row, text=note.name, font=_font(12), text_color=TEXT, anchor="w"
            )
            name.pack(side="left", fill="x", expand=True)
            when = ctk.CTkLabel(
                row,
                text=time.strftime("%Y-%m-%d %H:%M", time.localtime(note.stat().st_mtime)),
                font=_font(11),
                text_color=MUTED,
                width=110,
                anchor="e",
            )
            when.pack(side="right")
            for widget in (row, name, when):
                widget.bind("<Button-1>", lambda _e, p=note: self._open_path(p))
                widget.bind("<Enter>", lambda _e, r=row: r.configure(fg_color=ACCENT_SOFT))
                widget.bind("<Leave>", lambda _e, r=row: r.configure(fg_color="transparent"))

    # -- main loop ------------------------------------------------------------

    def _drain_jobs(self) -> None:
        import queue as queue_module

        for key, job in self.jobs.items():
            row = self.job_rows.get(key)
            while True:
                try:
                    line = job.lines.get_nowait()
                except queue_module.Empty:
                    break
                self._append_log(line + "\n")
                if key in ("fetch", "transcribe"):
                    self._append_online(line + "\n")
            if job.running or job.exit_code is None or row is None:
                continue
            if row["status"].cget("text") != "运行中":
                continue
            ok = job.exit_code == 0
            row["bar"].stop()
            row["bar"].set(1 if ok else 0)
            row["status"].configure(
                text="完成" if ok else f"失败({job.exit_code})",
                text_color=OK if ok else ERR,
            )
            self._append_log(f"[任务] {key} 结束 (exit={job.exit_code})\n")
            self._last_notes_refresh = 0.0

    def _tick(self) -> None:
        if self._quitting:
            return
        self._ticks += 1
        try:
            self._drain_jobs()
            if self._ticks % 2 == 0:
                self._refresh_status()
                new_text = self._log_tail.read_new()
                if new_text:
                    self._append_log(new_text)
            if time.time() - self._last_notes_refresh > 4:
                self._last_notes_refresh = time.time()
                self._refresh_outputs()
        except Exception as exc:  # noqa: BLE001 - never break the loop
            self._append_log(f"[界面] 刷新异常: {exc}\n")
        self.root.after(500, self._tick)

    def run(self) -> None:
        """Enter the Tk main loop."""
        self.root.mainloop()

    # -- tray / window --------------------------------------------------------

    def _init_tray(self) -> None:
        try:
            import pystray

            image = _tray_image()
        except Exception:  # noqa: BLE001 - tray is optional
            self._tray_icon = None
            return
        menu = pystray.Menu(
            pystray.MenuItem("显示窗口", lambda: self.root.after(0, self._show_window), default=True),
            pystray.MenuItem("启动监听", lambda: self.root.after(0, self.start_watching)),
            pystray.MenuItem("停止监听", lambda: self.root.after(0, self.stop_watching)),
            pystray.MenuItem("打开输出目录", lambda: self.root.after(0, lambda: self._open_path(Path(self.cfg.output_dir)))),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("退出", lambda: self.root.after(0, self._quit)),
        )
        self._tray_icon = pystray.Icon("srt-daemon-gui", image, APP_TITLE, menu)
        threading.Thread(target=self._tray_icon.run, name="tray", daemon=True).start()

    def _show_window(self) -> None:
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:  # noqa: BLE001
            pass

    def _hide_window(self) -> None:
        self.root.withdraw()

    def _on_close(self) -> None:
        if self.tray_var.get() and self._tray_icon is not None:
            self._hide_window()
        else:
            self._quit()

    def _quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        try:
            self.settings.save(self._gui_settings_path())
        except Exception:  # noqa: BLE001
            pass
        for job in self.jobs.values():
            try:
                job.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.watcher.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:  # noqa: BLE001
                pass
        try:
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


def _setup_logging(config_path: Optional[str]) -> None:
    """Send the GUI's own logging to the same file the CLI uses.

    Without this the windowed build has no console, so everything the watcher
    thread logs (screenshots, notes, failures) was silently discarded.
    """
    try:
        config = load_config(config_path)
    except Exception:  # noqa: BLE001 - the App reports config problems itself
        return
    try:
        setup_logging(
            log_file=config.log_file,
            level=config.log_level,
            fmt=config.log_format,
            date_format=config.log_date_format,
            rotation=config.log_rotation,
            backup_count=config.log_backup_count,
        )
    except Exception:  # noqa: BLE001 - never block the window on logging
        pass


def main(config_path: Optional[str] = None, start_hidden: bool = False) -> None:
    """Open the modern window (or start hidden in the tray)."""
    import sys

    enable_dpi_awareness()
    _setup_logging(config_path)
    try:
        app = App(config_path=config_path, start_hidden=start_hidden)
    except Exception as exc:  # noqa: BLE001 - startup errors must be visible
        print(f"Cannot start the GUI: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    app.run()
