"""SRT Daemon — main entry point.

Provides a CLI for the subtitle-to-Markdown daemon (SRT + WebVTT) with three
operating modes:

* **daemon** (default) — monitors the watch directory for new ``.srt`` /
  ``.vtt`` files, detects each file's format and processes it
  automatically.  Runs until stopped by SIGINT (Ctrl+C) or SIGTERM.
* **single-file** (``--once``) — processes one subtitle file and exits.
* **scan** (``--scan``) — processes all unprocessed subtitle files in the
  watch directory and exits.

Usage::

    python -m src.srt_daemon.main                 # daemon mode
    python -m src.srt_daemon.main --once file.srt # single SRT file
    python -m src.srt_daemon.main --once file.vtt # single WebVTT file
    python -m src.srt_daemon.main --scan          # scan mode
    python -m src.srt_daemon.main --once m.vtt --profile meeting   # force a profile
    python -m src.srt_daemon.main --version       # print version

The Markdown shape (course notes / meeting minutes / interview digest) is
chosen per file: an explicit ``--profile`` wins, then a file-name hint
(``[会议]standup.vtt``), then automatic content classification, and finally
``ai.default_profile``.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import Optional, Sequence

from src.srt_daemon import __version__
from src.srt_daemon.config import Config, load_config
from src.srt_daemon.logger import get_logger, setup_logging

# ---------------------------------------------------------------------------
# Module-level logger (created early; handlers attached by setup_logging())
# ---------------------------------------------------------------------------

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        argv: Optional command-line argument list (defaults to ``sys.argv[1:]``).

    Returns:
        Parsed namespace with attributes: ``config``, ``once``, ``scan``,
        ``version``.
    """
    parser = argparse.ArgumentParser(
        prog="srt-daemon",
        description=(
            "Subtitle to Markdown Study Notes Daemon (SRT + WebVTT). "
            "Monitors a directory for subtitle files, auto-detects whether "
            "each file is SRT or WebVTT, and automatically converts them "
            "into structured Markdown study notes using AI."
        ),
    )

    parser.add_argument(
        "--config",
        type=str,
        default=None,
        metavar="PATH",
        help="Path to config.yaml (default: auto-discovered from project root).",
    )
    parser.add_argument(
        "--once",
        type=str,
        default=None,
        metavar="FILE",
        help="Process a single subtitle file (.srt or .vtt) and exit. "
        "Does not start the watcher.",
    )
    parser.add_argument(
        "--profile",
        type=str,
        default=None,
        metavar="ID",
        help="Force a content-type profile for this run (e.g. course, "
        "meeting, interview). Without it the profile is auto-detected from "
        "the file name, then from the content.",
    )
    parser.add_argument(
        "--scan",
        action="store_true",
        default=False,
        help="Scan the watch directory for unprocessed files, process them, "
        "then exit. Does not start the watcher.",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        default=False,
        help="Print version number and exit.",
    )

    return parser.parse_args(argv)


def setup_signal_handlers(watcher: object) -> None:
    """Register SIGINT and SIGTERM handlers for graceful shutdown.

    The handlers call ``watcher.stop()`` to trigger the main processing loop
    to exit cleanly, allowing the current file (if any) to finish processing.

    On Windows only SIGINT (Ctrl+C) has native OS support; SIGTERM is
    registered for compatibility when the daemon is run under nssm or in
    a terminal emulator that supports it.

    Args:
        watcher: A :class:`~watcher.FileWatcher` instance whose ``stop()``
            method will be called on signal receipt.
    """

    def _handler(signum: int, _frame: object) -> None:
        sig_name = signal.Signals(signum).name
        log.info("Received %s, initiating graceful shutdown...", sig_name)
        # Notify the watcher to stop; its main loop will drain the current
        # file and then exit.
        watcher.stop()  # type: ignore[union-attr]

    signal.signal(signal.SIGINT, _handler)
    # SIGTERM is not natively supported on Windows, but nssm sends it.
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handler)

    log.debug("Signal handlers registered: SIGINT%s", ", SIGTERM" if hasattr(signal, "SIGTERM") else "")


def run_daemon(config: Config, profile: Optional[str] = None) -> None:
    """Start the daemon in long-running watcher mode.

    Creates a :class:`~watcher.FileWatcher`, registers signal handlers for
    graceful shutdown, and enters the blocking :meth:`~watcher.FileWatcher.run`
    loop.  This function returns when the watcher is stopped (by signal or
    an unrecoverable error).

    Args:
        config: Fully-resolved :class:`Config` instance.
        profile: Optional profile id forced for every file in this run.
    """
    from src.srt_daemon.watcher import FileWatcher

    watcher = FileWatcher(config, forced_profile=profile)
    setup_signal_handlers(watcher)

    log.info(
        "SRT Daemon v%s starting in daemon mode  watch_dir=%s",
        __version__,
        config.watch_dir,
    )

    try:
        watcher.run()
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt received — shutting down")
        watcher.stop()
    except Exception:
        log.exception("Unrecoverable error in daemon mode")
        watcher.stop()
        raise

    log.info("SRT Daemon stopped")


def run_once(
    config: Config, file_path: str, profile: Optional[str] = None
) -> None:
    """Process a single subtitle file and print the output path.

    This is a one-shot pipeline: detect the format (SRT / WebVTT), parse,
    resolve the content-type profile, call AI, write Markdown.  The watch
    directory and state file are **not** consulted — the file is always
    (re-)processed on demand.

    Args:
        config: Fully-resolved :class:`Config` instance.
        file_path: Path to the ``.srt`` / ``.vtt`` file (absolute or
            relative to CWD).
        profile: Optional profile id that overrides auto-detection.

    Raises:
        SystemExit: With code ``1`` if the file does not exist, its
            format cannot be determined, or it contains no parseable
            subtitle entries.
    """
    from src.srt_daemon.ai_client import AiClientError
    from src.srt_daemon.pipeline import NotesPipelineError, generate_notes
    from src.srt_daemon.subtitle_parser import UnsupportedSubtitleFormat

    path = Path(file_path).resolve()

    # --- Validate input -------------------------------------------------------
    if not path.exists():
        log.error("File not found: %s", path)
        sys.exit(1)
    if not path.is_file():
        log.error("Not a regular file: %s", path)
        sys.exit(1)

    log.info(
        "SRT Daemon v%s — single-shot mode  file=%s",
        __version__,
        path.name,
    )

    # --- Parse -> profile -> AI -> write (shared pipeline) --------------------
    try:
        result = generate_notes(config, path, forced_profile=profile)
    except UnsupportedSubtitleFormat as exc:
        log.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except NotesPipelineError as exc:
        log.warning("%s", exc)
        sys.exit(1)
    except AiClientError as exc:
        log.error("AI processing failed: %s", exc)
        sys.exit(1)

    # Print the output path so the caller can find the result.
    print(str(result.output_path))
    log.info("Single-shot processing complete: %s", result.output_path)


def run_scan(config: Config, profile: Optional[str] = None) -> None:
    """Scan the watch directory and process all unprocessed subtitle files.

    Uses :class:`~watcher.FileWatcher` internally for its state tracking and
    deduplication logic, but never starts the watchdog observer.  Each file
    found by the startup scan is drained from the queue and processed
    sequentially in the current thread.

    Args:
        config: Fully-resolved :class:`Config` instance.
        profile: Optional profile id forced for every scanned file.
    """
    from src.srt_daemon.watcher import FileWatcher

    watcher = FileWatcher(config, forced_profile=profile)

    log.info(
        "SRT Daemon v%s — scan mode  watch_dir=%s",
        __version__,
        config.watch_dir,
    )

    # Trigger the startup scan (enqueues unprocessed files).
    watcher._scan_existing()

    # Drain and process every queued file.
    processed = 0
    failed = 0

    while not watcher._queue.empty():
        try:
            file_path = watcher._queue.get_nowait()
        except Exception:
            break

        log.info("Scan mode: processing %s", file_path.name)
        try:
            watcher._process_file(file_path)
            processed += 1
        except Exception:
            log.exception("Failed to process %s", file_path.name)
            failed += 1

    log.info(
        "Scan complete — %d processed, %d failed, %d skipped",
        processed,
        failed,
        max(0, watcher._queue.qsize()),
    )

    # If we ran the scan from outside the watcher context, we should clean up.
    watcher._cancel_all_pending()


def main() -> None:
    """Main entry point for the SRT Daemon CLI.

    Parses command-line arguments, loads configuration, initialises logging,
    and dispatches to the appropriate operating mode.

    Exit codes:
        ``0`` — success.
        ``1`` — configuration or processing error.
    """
    args = parse_args()

    # --- --version: print and exit (no config needed) -------------------------
    if args.version:
        print(f"SRT Daemon v{__version__}")
        return

    # --- Load configuration ---------------------------------------------------
    try:
        config = load_config(args.config)
    except FileNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    # --- Initialise logging ---------------------------------------------------
    setup_logging(
        log_file=config.log_file,
        level=config.log_level,
        fmt=config.log_format,
        date_format=config.log_date_format,
        rotation=config.log_rotation,
        backup_count=config.log_backup_count,
    )

    log.info("=" * 60)
    log.info("SRT Daemon v%s  pid=%d", __version__, os.getpid())
    log.info("Config path: %s", args.config or "(auto-discovered)")
    log.info("=" * 60)

    # --- Validate an explicitly requested profile early ------------------------
    if args.profile:
        from src.srt_daemon.profiles import ProfileRegistry

        registry = ProfileRegistry(config)
        if registry.find(args.profile) is None:
            print(
                f"Error: unknown profile {args.profile!r}. "
                f"Available: {', '.join(registry.ids())}",
                file=sys.stderr,
            )
            sys.exit(1)

    # --- Dispatch to operating mode -------------------------------------------
    if args.once:
        run_once(config, args.once, profile=args.profile)
    elif args.scan:
        run_scan(config, profile=args.profile)
    else:
        run_daemon(config, profile=args.profile)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    main()
