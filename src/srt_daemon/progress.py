"""Progress files shared by long jobs and the GUI.

The GUI cannot see a child process's memory, so each long job publishes its
state as a small JSON file in the output directory.  The window polls it and
renders a real progress bar instead of an indeterminate spinner.

Files are written atomically (temp file + replace) so a poll never reads a
half-written JSON document.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from src.srt_daemon.logger import get_logger

log = get_logger(__name__)

#: Kinds of job that publish progress.
TRANSCRIBE = "transcribe"
NOTES = "notes"

#: Stages a transcription reports.
STAGE_LOADING = "loading"
STAGE_ASR = "asr"
STAGE_DIARIZE = "diarize"
STAGE_WRITING = "writing"

#: Stages a note generation reports.
STAGE_PARSE = "parse"
STAGE_AI = "ai"
STAGE_SCREENSHOTS = "screenshots"
STAGE_NOTE_WRITE = "write"

STAGE_DONE = "done"


def progress_path(output_dir: str | Path, kind: str) -> Path:
    """Where the progress document for *kind* lives."""
    return Path(output_dir) / f".progress-{kind}.json"


def write_progress(
    output_dir: str | Path,
    kind: str,
    *,
    stage: str,
    done: float = 0.0,
    total: float = 0.0,
    detail: str = "",
    file: str = "",
    started_at: float = 0.0,
) -> None:
    """Publish the current state of a running job (best effort)."""
    payload: dict[str, Any] = {
        "kind": kind,
        "stage": stage,
        "done": round(float(done), 3),
        "total": round(float(total), 3),
        "detail": detail,
        "file": file,
        "started_at": float(started_at or 0.0),
        "pid": os.getpid(),
        "updated_at": time.time(),
    }
    path = progress_path(output_dir, kind)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def read_progress(output_dir: str | Path, kind: str) -> Optional[dict[str, Any]]:
    """Read a job's progress document, or None when there is none."""
    path = progress_path(output_dir, kind)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def clear_progress(
    output_dir: str | Path, kind: str, pid: Optional[int] = None
) -> None:
    """Remove a job's progress document.

    When *pid* is given, the document is only removed if it was written by that
    process.  A short one-shot run (for example a --scan that only generates
    notes) must not wipe the progress of a transcription that is still going.
    """
    path = progress_path(output_dir, kind)
    if pid is not None:
        current = read_progress(output_dir, kind)
        if current and int(current.get("pid", 0) or 0) not in (0, pid):
            log.debug(
                "Keeping %s progress owned by pid %s (we are %s)",
                kind,
                current.get("pid"),
                pid,
            )
            return
    for candidate in (path, path.with_suffix(path.suffix + ".tmp")):
        try:
            candidate.unlink()
        except OSError:
            pass


#: Where each stage sits on the single overall progress bar.  Mapping the
#: stages onto fixed ranges keeps the bar monotonic: without this, a new stage
#: (done=0 again) would reset a half-filled bar to zero.
STAGE_RANGES: dict[str, dict[str, tuple[float, float]]] = {
    TRANSCRIBE: {
        STAGE_LOADING: (0.0, 0.02),
        STAGE_ASR: (0.02, 0.80),
        STAGE_DIARIZE: (0.80, 0.95),
        STAGE_WRITING: (0.95, 1.0),
    },
    NOTES: {
        STAGE_PARSE: (0.0, 0.05),
        STAGE_AI: (0.05, 0.70),
        STAGE_SCREENSHOTS: (0.70, 0.95),
        STAGE_NOTE_WRITE: (0.95, 1.0),
    },
}


def overall_fraction(
    kind: str, stage: str, done: float, total: float
) -> float:
    """Fraction (0..1) of the whole job for one stage position."""
    ranges = STAGE_RANGES.get(kind, {})
    low, high = ranges.get(stage, (0.0, 1.0))
    inner = 0.0
    if total > 0:
        inner = max(0.0, min(1.0, float(done) / float(total)))
    return low + (high - low) * inner


def is_stale(progress: Optional[dict[str, Any]], max_age_seconds: float = 30.0) -> bool:
    """True when a progress document has not been refreshed recently."""
    if not progress:
        return True
    try:
        return (time.time() - float(progress.get("updated_at", 0))) > max_age_seconds
    except (TypeError, ValueError):
        return True

# ---------------------------------------------------------------------------
# CLI: inspect progress without a screenshot
# ---------------------------------------------------------------------------

STAGE_LABELS = {
    STAGE_LOADING: "加载模型 / 解码音频",
    STAGE_ASR: "识别语音",
    STAGE_DIARIZE: "说话人聚类",
    STAGE_WRITING: "写入字幕",
    STAGE_PARSE: "读取字幕",
    STAGE_AI: "AI 总结",
    STAGE_SCREENSHOTS: "插入截图",
    STAGE_NOTE_WRITE: "写入笔记",
    STAGE_DONE: "完成",
}


def _format_span(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def describe(output_dir: str | Path, kind: str) -> str:
    """One human-readable line for the current state of *kind*."""
    data = read_progress(output_dir, kind)
    if not data:
        return f"{kind}: 没有正在进行的任务（{progress_path(output_dir, kind).name} 不存在）"
    stage = str(data.get("stage", "?"))
    label = STAGE_LABELS.get(stage, stage)
    done = float(data.get("done", 0) or 0)
    total = float(data.get("total", 0) or 0)
    fraction = overall_fraction(kind, stage, done, total)
    detail = str(data.get("detail", "") or "")
    age = time.time() - float(data.get("updated_at", 0) or 0)
    started = float(data.get("started_at", 0) or 0)
    eta = ""
    if started > 0 and fraction > 0.001:
        elapsed = max(1.0, time.time() - started)
        remain = elapsed * (1.0 - fraction) / fraction
        eta = f"  剩余约 {_format_span(remain)}"
    parts = [
        f"{kind}: {label}",
        f" 总体 {fraction * 100:.1f}%",
    ]
    if total > 0:
        parts.append(f"{_format_span(done)}/{_format_span(total)}")
    if detail:
        parts.append(detail)
    if eta:
        parts.append(eta.strip())
    parts.append(f"（{age:.1f}s 前刷新{'，可能已停止' if age > 30 else ''}）")
    return "  ".join(parts)


def _main(argv: Optional[list[str]] = None) -> int:
    """Print progress once, or watch it every few seconds."""
    import argparse
    import sys

    try:  # Windows consoles default to GBK; keep the Chinese labels readable
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - best effort
        pass

    parser = argparse.ArgumentParser(
        description="Show the progress a running job publishes (no screenshot needed)."
    )
    parser.add_argument("--config", default=None, help="Path to config.yaml")
    parser.add_argument(
        "--watch",
        type=float,
        nargs="?",
        const=2.0,
        default=None,
        metavar="SECONDS",
        help="Keep polling (default 2s). Ctrl+C to stop.",
    )
    args = parser.parse_args(argv)

    from src.srt_daemon.config import load_config

    config = load_config(args.config)
    output_dir = config.output_dir

    def snapshot() -> None:
        stamp = time.strftime("%H:%M:%S")
        print(f"[{stamp}] {describe(output_dir, TRANSCRIBE)}")
        print(f"[{stamp}] {describe(output_dir, NOTES)}")
        print()

    try:
        if args.watch is None:
            snapshot()
            return 0
        while True:
            os.system("")  # keep the console happy on Windows
            print("\033[2J", end="") if os.name != "nt" else print("\n" * 2, end="")
            snapshot()
            time.sleep(max(0.5, float(args.watch)))
    except KeyboardInterrupt:
        print("已停止观察。")
        return 0


if __name__ == "__main__":
    raise SystemExit(_main())
