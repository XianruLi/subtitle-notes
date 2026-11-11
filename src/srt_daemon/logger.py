"""Logging setup for SRT Daemon.

Configures Python's standard ``logging`` module with:
- A ``TimedRotatingFileHandler`` for daily log rotation
- A ``StreamHandler`` for console output
- Sensible defaults for third-party library noise suppression

Usage:
    from src.srt_daemon.config import load_config
    from src.srt_daemon.logger import setup_logging, get_logger

    cfg = load_config()
    setup_logging(
        log_file=cfg.log_file,
        level=cfg.log_level,
        fmt=cfg.log_format,
        date_format=cfg.log_date_format,
        rotation=cfg.log_rotation,
        backup_count=cfg.log_backup_count,
    )
    log = get_logger(__name__)
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def setup_logging(
    log_file: Path,
    level: str = "INFO",
    fmt: str = "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
    date_format: str = "%Y-%m-%d %H:%M:%S",
    rotation: str = "midnight",
    backup_count: int = 30,
) -> None:
    """Configure the root logger with file rotation and console output.

    This function is **idempotent** — it clears any previously attached
    handlers before adding new ones, so it is safe to call more than once.

    Args:
        log_file: Destination path for the log file.  Parent directories are
            created automatically if they do not exist.
        level: One of ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR``.
        fmt: Standard Python logging format string.
        date_format: ``strftime``-style date format for log entries.
        rotation: Rotation interval — ``"midnight"`` (daily at midnight),
            ``"H"`` (hourly), or ``"D"`` (daily at the time of first log).
        backup_count: Number of rotated files to retain before deletion.
    """
    # Ensure the log directory exists
    log_file = Path(log_file)
    log_file.parent.mkdir(parents=True, exist_ok=True)

    log_level = _to_level(level)

    root = logging.getLogger()
    root.setLevel(log_level)

    # Idempotent: remove handlers from any previous setup_logging() call
    root.handlers.clear()

    # --- File handler (rotating) --------------------------------------------
    when = _rotation_when(rotation)
    file_handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(log_file),
        when=when,
        interval=1,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(log_level)
    file_handler.setFormatter(logging.Formatter(fmt, datefmt=date_format))
    root.addHandler(file_handler)

    # --- Console handler ----------------------------------------------------
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(log_level)
    console_handler.setFormatter(logging.Formatter(fmt, datefmt=date_format))
    root.addHandler(console_handler)

    # --- Quiet down noisy third-party loggers -------------------------------
    _silence_third_party()


def get_logger(name: str) -> logging.Logger:
    """Convenience wrapper around ``logging.getLogger(name)``.

    Args:
        name: Typically ``__name__`` from the calling module.

    Returns:
        A child logger that inherits the root logger's configuration.
    """
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_level(name: str) -> int:
    """Convert a level name string to a ``logging`` level constant."""
    level = getattr(logging, name.upper(), logging.INFO)
    if not isinstance(level, int):
        return logging.INFO
    return level


def _rotation_when(rotation: str) -> str:
    """Map a short rotation key to ``TimedRotatingFileHandler`` *when* values."""
    mapping = {
        "midnight": "midnight",
        "H": "H",
        "D": "D",
    }
    return mapping.get(rotation, "midnight")


def _silence_third_party() -> None:
    """Raise the log level for libraries that are chatty at INFO/DEBUG."""
    for name in ("watchdog", "httpx", "httpcore", "urllib3"):
        logging.getLogger(name).setLevel(logging.WARNING)
