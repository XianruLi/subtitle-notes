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


def clear_progress(output_dir: str | Path, kind: str) -> None:
    """Remove a job's progress document."""
    path = progress_path(output_dir, kind)
    for candidate in (path, path.with_suffix(path.suffix + ".tmp")):
        try:
            candidate.unlink()
        except OSError:
            pass


def is_stale(progress: Optional[dict[str, Any]], max_age_seconds: float = 30.0) -> bool:
    """True when a progress document has not been refreshed recently."""
    if not progress:
        return True
    try:
        return (time.time() - float(progress.get("updated_at", 0))) > max_age_seconds
    except (TypeError, ValueError):
        return True
