"""File watcher module for the Subtitle Daemon (SRT + WebVTT).

Monitors a configured directory for new or modified ``.srt`` / ``.vtt``
subtitle files and triggers the full processing pipeline:
format detection -> parse -> profile resolution -> AI -> write.

Supports startup scanning of existing files, debounced event handling,
content-based deduplication via SHA-256 hashing, and thread-safe state
tracking to avoid duplicate or concurrent processing.

Usage::

    from src.srt_daemon.config import load_config
    from src.srt_daemon.logger import setup_logging, get_logger
    from src.srt_daemon.watcher import FileWatcher

    cfg = load_config()
    setup_logging(cfg.log_file, level=cfg.log_level, ...)
    watcher = FileWatcher(cfg)
    try:
        watcher.run()   # blocking
    except KeyboardInterrupt:
        watcher.stop()
"""

from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from src.srt_daemon.config import Config
from src.srt_daemon.logger import get_logger
from src.srt_daemon.profiles import Profile, ProfileRegistry, resolve_profile
from src.srt_daemon.speakers import analyse_speakers

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Chunk size for file hashing (64 KB).
_HASH_CHUNK_SIZE = 64 * 1024

# Maximum age for a stale in-progress marker before it is discarded on startup.
_STALE_IN_PROGRESS_SECONDS = 3600  # 1 hour


# ---------------------------------------------------------------------------
# FileWatcher
# ---------------------------------------------------------------------------


class FileWatcher:
    """Subtitle file monitor that detects new files and triggers processing.

    Watches a single directory (non-recursive) for ``.srt`` / ``.vtt``
    subtitle files using the *watchdog* library.  On startup it scans for
    orphaned files; then
    it enters an event-driven loop with debounce to avoid processing files
    that are still being written.

    Processing state is persisted to a JSON file so that restarts do not
    re-process already-handled files.  Content-based deduplication
    (SHA-256) ensures that renamed or overwritten files are treated as new
    work.

    Thread safety::

        - ``_pending`` (debounce timer dict) is guarded by ``_lock``.
        - ``_state`` (processed / in-progress tracking) is only mutated
          from the main processing loop, so it is effectively single-threaded.
        - ``_queue`` is a ``queue.Queue`` (inherently thread-safe).

    Typical usage::

        cfg = load_config()
        watcher = FileWatcher(cfg)
        try:
            watcher.run()
        except KeyboardInterrupt:
            watcher.stop()
    """

    def __init__(self, config: Config, forced_profile: Optional[str] = None) -> None:
        """Initialise the file watcher.

        Loads the processing-state file (creating a fresh one if absent),
        builds the content-type profile registry, and prepares internal
        bookkeeping structures.  The *watchdog* :class:`Observer` is **not**
        started until :meth:`run` is called.

        Args:
            config: Fully-resolved :class:`Config` instance.
            forced_profile: Optional profile id that overrides automatic
                content-type detection for every file (CLI ``--profile``).
        """
        self._config = config
        self._forced_profile = forced_profile
        self._lock = threading.Lock()
        self._queue: queue.Queue[Path] = queue.Queue()
        self._pending: dict[Path, threading.Timer] = {}
        self._observer: Optional[Observer] = None
        self._running = False
        self._registry = ProfileRegistry(config)

        # Processing state: {"files": {path_str: {hash, processed_at, output}},
        #                    "in_progress": {path_str: iso_timestamp}}
        self._state: dict[str, Any] = {"files": {}, "in_progress": {}}
        self._load_state()

        # Lazily-created AI clients, one per content-type profile.  The empty
        # key holds the default client used for content-type classification.
        self._ai_clients: dict[str, Any] = {}

        log.info(
            "FileWatcher initialised: watch_dir=%s  extensions=%s  "
            "debounce=%.1fs  state_file=%s  profiles=%s%s",
            self._config.watch_dir,
            self._config.watch_extensions,
            self._config.watch_debounce_seconds,
            self._config.state_file,
            ",".join(self._registry.ids()),
            f"  forced_profile={forced_profile}" if forced_profile else "",
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(self) -> None:
        """Start the watcher (blocking call).

        1. Scans the watch directory for existing unprocessed ``.srt`` /
           ``.vtt`` files and queues them for processing.
        2. Starts a *watchdog* :class:`Observer` to detect new or changed
           files in real time.
        3. Enters the main processing loop, draining the queue as files
           arrive.

        This method blocks until :meth:`stop` is called (typically via
        ``KeyboardInterrupt`` or a signal handler) or until an
        unrecoverable *watchdog* error occurs.

        Raises:
            RuntimeError: If the *watchdog* observer cannot be started.
        """
        self._running = True

        # 1. Scan for existing files that were missed while the daemon
        #    was not running.
        self._scan_existing()

        # 2. Start the watchdog observer.
        event_handler = _SubtitleEventHandler(self)
        self._observer = Observer()
        try:
            self._observer.schedule(
                event_handler,
                str(self._config.watch_dir),
                recursive=False,
            )
            self._observer.start()
        except Exception as exc:
            log.error("Failed to start watchdog observer: %s", exc)
            self._running = False
            raise RuntimeError(
                f"Cannot start file watcher: {exc}"
            ) from exc

        log.info(
            "FileWatcher running — watching %s for %s files",
            self._config.watch_dir,
            self._config.watch_extensions,
        )

        # 3. Main processing loop.
        while self._running:
            try:
                file_path = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                self._process_file(file_path)
            except Exception:
                # Per NFR-01: single-file failure must not crash the loop.
                log.error(
                    "Unhandled exception in _process_file for %r",
                    file_path,
                    exc_info=True,
                )

        # Clean shutdown.
        log.info("FileWatcher main loop exited")
        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=5.0)
        self._cancel_all_pending()

    def stop(self) -> None:
        """Request graceful shutdown of the watcher.

        Sets the internal running flag to ``False``, which causes the
        main loop in :meth:`run` to exit after the current processing
        cycle completes.  The *watchdog* observer is stopped and pending
        debounce timers are cancelled.

        This method is safe to call from any thread and may be called
        multiple times (idempotent).
        """
        log.info("FileWatcher.stop() requested")
        self._running = False

        if self._observer and self._observer.is_alive():
            self._observer.stop()
            self._observer.join(timeout=5.0)

        self._cancel_all_pending()

    # ------------------------------------------------------------------
    # Internal: startup scan
    # ------------------------------------------------------------------

    @property
    def _allowed_extensions(self) -> set[str]:
        """Subtitle extensions, plus media ones when auto-transcription is on."""
        allowed = {str(ext).lower() for ext in self._config.watch_extensions}
        if self._config.transcribe_auto_media:
            allowed |= set(self._config.transcribe_media_extensions)
        return allowed

    def _is_media(self, path: Path) -> bool:
        """True when *path* is a recording rather than a subtitle."""
        return path.suffix.lower() in set(self._config.transcribe_media_extensions)

    def _explain_ignored_media(self, path: Path) -> None:
        """Tell the user why a recording in the watch directory is not handled."""
        if not self._is_media(path) or self._config.transcribe_auto_media:
            return
        log.info(
            "Ignoring recording %s: auto-transcription is off. Use the GUI's "
            "\"新建任务\" page, or run transcribe with --num-speakers/--profile.",
            path.name,
        )

    def _scan_existing(self) -> None:
        """Scan *watch_dir* for subtitle files (SRT / WebVTT) that have not
        yet been processed, and enqueue them.

        This is called once at startup, before the *watchdog* observer
        begins emitting events, so no file is missed during the daemon's
        downtime.
        """
        watch_dir = self._config.watch_dir

        if not watch_dir.exists():
            log.warning("Watch directory does not exist: %s", watch_dir)
            return

        if not watch_dir.is_dir():
            log.error("Watch path is not a directory: %s", watch_dir)
            return

        candidates: list[Path] = []
        for entry in watch_dir.iterdir():
            if not entry.is_file():
                continue
            if entry.suffix.lower() not in self._allowed_extensions:
                continue
            candidates.append(entry)

        if not candidates:
            log.info("_scan_existing: no subtitle files found in %s", watch_dir)
            return

        log.info(
            "_scan_existing: found %d subtitle file(s) in %s",
            len(candidates),
            watch_dir,
        )

        for file_path in sorted(candidates):
            if self._is_processed(file_path):
                log.debug("_scan_existing: skipping already-processed %s", file_path.name)
                continue
            log.info("_scan_existing: enqueuing %s", file_path.name)
            self._enqueue_file(file_path)

    # ------------------------------------------------------------------
    # Internal: file event handling
    # ------------------------------------------------------------------

    def _on_file_event(self, file_path: Path) -> None:
        """Handle a watchdog filesystem event with debounce.

        Called from the watchdog observer's internal thread.  Schedules
        (or re-schedules) a debounce timer for *file_path*.  If the same
        file receives another event before the timer fires, the timer is
        reset.  This prevents processing a partially-written file.

        Files whose extension is not in the configured watch list are
        silently ignored (defense-in-depth; the handler already filters).

        Args:
            file_path: Absolute path to the file that triggered the event.
        """
        # Defense-in-depth: filter by extension here as well (the
        # watchdog handler also filters, but this method may be called
        # directly in tests or via _scan_existing).
        if file_path.suffix.lower() not in self._allowed_extensions:
            self._explain_ignored_media(file_path)
            return

        with self._lock:
            # Cancel any existing timer for this path (debounce reset).
            existing = self._pending.pop(file_path, None)
            if existing is not None:
                existing.cancel()

            # Schedule a fresh timer.
            timer = threading.Timer(
                self._config.watch_debounce_seconds,
                self._enqueue_file,
                args=[file_path],
            )
            timer.daemon = True  # avoid blocking interpreter shutdown
            timer.start()
            self._pending[file_path] = timer

            log.debug(
                "Debounce timer (re)scheduled for %s (delay=%.1fs)",
                file_path.name,
                self._config.watch_debounce_seconds,
            )

    def _enqueue_file(self, file_path: Path) -> None:
        """Place *file_path* onto the processing queue.

        Called by a debounce timer when it fires.  Thread-safe — may be
        called from any thread.

        Args:
            file_path: Absolute path to the file that is ready for processing.
        """
        # Remove from the pending dict; the timer is done.
        with self._lock:
            self._pending.pop(file_path, None)

        self._queue.put(file_path)
        log.debug("File enqueued for processing: %s", file_path.name)

    # ------------------------------------------------------------------
    # Internal: processing pipeline
    # ------------------------------------------------------------------

    def _process_file(self, file_path: Path) -> None:
        """Run the full processing pipeline for a single subtitle file.

        Pipeline stages:
        1. Verify the file still exists and has not already been
           processed with identical content.
        2. Mark the file as *in-progress* in the state file.
        3. Detect the subtitle format (SRT / WebVTT) and parse its entries
           with :mod:`src.srt_daemon.subtitle_parser`.
        4. Analyse the speakers and resolve the content-type profile
           (course / meeting / interview) via :mod:`src.srt_daemon.profiles`.
        5. Generate structured Markdown with :class:`~ai_client.AiClient`,
           using the profile's prompt template.
        6. Write the output with :class:`~writer.MarkdownWriter`.
        7. Record the completed processing in the state file.

        On any failure the file is removed from the *in-progress* set and
        the exception is logged — subsequent files are **not** affected.

        Args:
            file_path: Absolute path to the ``.srt`` file to process.
        """
        file_key = str(file_path.resolve())

        # --- Guard: file existence -------------------------------------------
        if not file_path.exists():
            log.warning("File disappeared before processing: %s", file_path.name)
            return

        # --- Guard: already in progress? -------------------------------------
        in_progress = self._state.get("in_progress", {})
        if file_key in in_progress:
            started = in_progress[file_key]
            log.info("File already in progress (since %s): %s", started, file_path.name)
            return

        # --- Guard: already processed with same content? ---------------------
        current_hash = self._compute_file_hash(file_path)
        if self._is_processed(file_path) and self._state["files"][file_key].get("hash") == current_hash:
            log.info(
                "File already processed (hash unchanged): %s", file_path.name
            )
            return

        # --- Mark in-progress ------------------------------------------------
        self._mark_processing(file_path)

        try:
            # --- Recordings: transcribe first, then process the subtitle ------
            if self._is_media(file_path):
                sibling = self._find_subtitle_for(file_path)
                if sibling is not None:
                    log.info(
                        "Subtitle already available for %s (%s) — skipping transcription",
                        file_path.name,
                        sibling.name,
                    )
                    self._mark_processed(file_path, current_hash, sibling)
                    self._process_file(sibling)
                    return

                subtitle = self._transcribe_media(file_path)
                if subtitle is None:
                    return
                self._mark_processed(file_path, current_hash, subtitle)
                self._process_file(subtitle)
                return

            # Stage 1: Detect the subtitle format (SRT / WebVTT) and parse
            from src.srt_daemon.subtitle_parser import parse_subtitle

            parsed = parse_subtitle(file_path, with_timestamps=True)
            entries = parsed.entries
            if not entries:
                log.warning("No subtitle entries found in %s — skipping", file_path.name)
                return
            text = parsed.text
            entry_count = len(entries)
            text_length = len(text)
            log.info(
                "Parsed %d %s entries (%d chars) from %s",
                entry_count, parsed.format_label, text_length, file_path.name,
            )

            # Stage 1.5: decide how the notes should be shaped (profile) and
            # how speakers are represented in this transcript.
            speaker = analyse_speakers(entries)
            resolution = resolve_profile(
                self._config,
                file_path.name,
                text,
                registry=self._registry,
                forced=self._forced_profile,
                classifier=self._classify_content,
            )
            profile = resolution.profile
            log.info(
                "Notes profile for %s: %s (via %s — %s)  speaker_mode=%s",
                file_path.name,
                profile.id,
                resolution.source,
                resolution.detail,
                speaker.mode,
            )

            # Stage 2: AI processing
            ai_client = self._get_ai_client(profile)
            try:
                markdown = ai_client.process(
                    text,
                    file_path.name,
                    profile=profile,
                    speaker=speaker,
                    source_format=parsed.format_label,
                )
            except Exception as exc:
                log.error(
                    "AI processing failed for %s: %s", file_path.name, exc
                )
                return

            # Stage 3: Write output (screenshots included when enabled)
            from src.srt_daemon.pipeline import find_source_video, write_note

            output_path, image_count = write_note(
                self._config,
                markdown,
                file_path,
                {
                    "format": parsed.format_label,
                    "profile": profile.id,
                    "speakers": speaker.describe(),
                    "entries": entry_count,
                    "text_length": text_length,
                },
                video_path=find_source_video(file_path),
            )
            if image_count:
                log.info("Embedded %d screenshot(s) for %s", image_count, file_path.name)

            # Stage 4: Record successful processing
            self._mark_processed(file_path, current_hash, output_path)
            log.info(
                "Successfully processed: %s -> %s (%d entries, profile=%s)",
                file_path.name, output_path.name, entry_count, profile.id,
            )

        except Exception:
            log.error(
                "Failed to process %s", file_path.name, exc_info=True,
            )
            raise
        finally:
            # Always release the in-progress lock; on success this is
            # redundant (already cleared by _mark_processed), on failure
            # it ensures the file can be retried on the next restart.
            self._clear_processing(file_path)

    def _transcribe_media(self, media_path: Path) -> Optional[Path]:
        """Transcribe a recording dropped into the watch directory.

        Returns the subtitle to process next (already-existing when the file
        was transcribed before), or None when transcription failed.
        """
        from src.srt_daemon.transcribe import TranscriptionError, Transcriber

        log.info("Recording detected in the watch directory: %s", media_path.name)
        try:
            result = Transcriber(self._config).transcribe(media_path)
        except TranscriptionError as exc:
            log.error("Transcription failed for %s: %s", media_path.name, exc)
            return None
        except Exception:  # noqa: BLE001 - one bad file must not kill the daemon
            log.error("Transcription crashed for %s", media_path.name, exc_info=True)
            return None

        if result is None:
            log.info("Already transcribed earlier: %s", media_path.name)
            return self._find_subtitle_for(media_path)

        for subtitle in result.subtitles:
            if subtitle.suffix.lower() in {
                str(ext).lower() for ext in self._config.watch_extensions
            }:
                return subtitle
        return self._find_subtitle_for(media_path)

    def _find_subtitle_for(self, media_path: Path) -> Optional[Path]:
        """Locate an existing subtitle for *media_path*.

        Checks the recording's own folder first (a downloaded video plus its
        subtitle export usually sit together), then the transcription output
        directory.
        """
        from src.srt_daemon.pipeline import stems_match

        suffixes = {str(ext).lower() for ext in self._config.watch_extensions}
        loose: Optional[Path] = None
        for folder in (media_path.parent, self._config.transcribe_output_dir):
            try:
                entries = list(folder.iterdir())
            except OSError:
                continue
            for candidate in entries:
                if not candidate.is_file() or candidate == media_path:
                    continue
                if candidate.suffix.lower() not in suffixes:
                    continue
                if candidate.stem == media_path.stem:
                    return candidate
                if loose is None and stems_match(candidate.stem, media_path.stem):
                    loose = candidate
        return loose

    # ------------------------------------------------------------------
    # Internal: state file management
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Load the processing-state JSON file into ``self._state``.

        Creates a pristine state if the file does not exist or is
        corrupted.  On startup, any entries in ``in_progress`` that are
        older than ``_STALE_IN_PROGRESS_SECONDS`` are discarded under the
        assumption that the daemon was previously terminated mid-process.
        """
        state_file = self._config.state_file

        if state_file.exists():
            try:
                with open(state_file, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                self._state = loaded
            except (json.JSONDecodeError, OSError) as exc:
                log.warning(
                    "State file %s is corrupt or unreadable (%s) — "
                    "starting with fresh state",
                    state_file,
                    exc,
                )
                self._state = {"files": {}, "in_progress": {}}
        else:
            log.info("State file not found — starting with fresh state")
            self._state = {"files": {}, "in_progress": {}}

        # Normalise: ensure both keys exist (handle hand-edited files).
        self._state.setdefault("files", {})
        self._state.setdefault("in_progress", {})

        # Clear stale in-progress entries.
        stale: list[str] = []
        now = datetime.now(timezone.utc)
        for key, ts_str in self._state["in_progress"].items():
            try:
                ts = datetime.fromisoformat(ts_str)
                age = (now - ts).total_seconds()
                if age > _STALE_IN_PROGRESS_SECONDS:
                    stale.append(key)
            except (ValueError, TypeError):
                stale.append(key)

        for key in stale:
            log.info(
                "Clearing stale in-progress entry: %s", Path(key).name
            )
            del self._state["in_progress"][key]

        self._save_state()
        log.info(
            "State loaded: %d processed file(s), %d in-progress",
            len(self._state["files"]),
            len(self._state["in_progress"]),
        )

    def _save_state(self) -> None:
        """Persist ``self._state`` to disk atomically.

        Writes to a temporary file first, then atomically replaces the
        real state file (works on Windows NTFS because the files are on
        the same volume).
        """
        state_file = self._config.state_file
        state_file.parent.mkdir(parents=True, exist_ok=True)

        tmp_file = state_file.with_suffix(".tmp")
        try:
            tmp_file.write_text(
                json.dumps(self._state, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp_file.replace(state_file)
        except OSError as exc:
            log.error("Failed to write state file %s: %s", state_file, exc)

    def _is_processed(self, file_path: Path) -> bool:
        """Check whether *file_path* appears in the processed-files record.

        Args:
            file_path: Absolute path to the SRT file.

        Returns:
            ``True`` if the file has a record in the state file.
        """
        file_key = str(file_path.resolve())
        return file_key in self._state.get("files", {})

    def _mark_processed(
        self, file_path: Path, file_hash: str, output_path: Path
    ) -> None:
        """Record a successfully-processed file in the persistent state.

        Args:
            file_path: Absolute path to the source ``.srt`` file.
            file_hash: SHA-256 hex digest of the file content.
            output_path: Absolute path to the generated Markdown file.
        """
        file_key = str(file_path.resolve())
        self._state.setdefault("files", {})[file_key] = {
            "hash": file_hash,
            "processed_at": datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            ),
            "output": str(output_path),
        }
        # Remove the in-progress marker.
        self._state.setdefault("in_progress", {}).pop(file_key, None)
        self._save_state()
        log.debug("Marked as processed: %s", file_path.name)

    def _mark_processing(self, file_path: Path) -> None:
        """Register *file_path* as currently being processed.

        Prevents concurrent processing of the same file by other watcher
        instances or after a quick restart.

        Args:
            file_path: Absolute path to the source ``.srt`` file.
        """
        file_key = str(file_path.resolve())
        self._state.setdefault("in_progress", {})[file_key] = (
            datetime.now(timezone.utc).isoformat(timespec="seconds")
        )
        self._save_state()
        log.debug("Marked as in-progress: %s", file_path.name)

    def _clear_processing(self, file_path: Path) -> None:
        """Remove the *in-progress* marker for *file_path*.

        Safe to call even if no marker exists (no-op).

        Args:
            file_path: Absolute path to the source ``.srt`` file.
        """
        file_key = str(file_path.resolve())
        removed = self._state.setdefault("in_progress", {}).pop(file_key, None)
        if removed is not None:
            self._save_state()
            log.debug("Cleared in-progress marker: %s", file_path.name)

    # ------------------------------------------------------------------
    # Internal: utilities
    # ------------------------------------------------------------------

    def _compute_file_hash(self, file_path: Path) -> str:
        """Compute the SHA-256 hex digest of *file_path*.

        Reads the file in 64 KB chunks to keep memory usage bounded for
        large files.

        Args:
            file_path: Absolute path to the file to hash.

        Returns:
            Hexadecimal SHA-256 digest string (64 characters).

        Raises:
            FileNotFoundError: If *file_path* does not exist.
            OSError: If the file cannot be read.
        """
        sha = hashlib.sha256()
        with open(file_path, "rb") as fh:
            while True:
                chunk = fh.read(_HASH_CHUNK_SIZE)
                if not chunk:
                    break
                sha.update(chunk)
        return sha.hexdigest()

    def _get_ai_client(self, profile: Optional[Profile] = None) -> Any:
        """Return (or lazily create) the :class:`AiClient` for *profile*.

        One client is cached per profile, so switching content types neither
        re-reads prompt files nor rebuilds the SDK client.  With *profile*
        left out, a client using the default prompt file is returned — that
        one is used for content-type classification.

        The client is created on first access so that the watcher can
        initialise even when the API key is temporarily unavailable.

        Args:
            profile: Content-type profile the client should write notes for.

        Returns:
            Configured :class:`AiClient` instance.

        Raises:
            RuntimeError: If the API key is not set.
            FileNotFoundError: If the system-prompt file is missing.
        """
        key = profile.id if profile is not None else ""
        client = self._ai_clients.get(key)
        if client is None:
            from src.srt_daemon.ai_client import AiClient

            client = AiClient(self._config, profile=profile)
            self._ai_clients[key] = client
            log.debug("AiClient created lazily for profile=%s", key or "(default)")
        return client

    def _classify_content(
        self, sample: str, candidates: list[Profile]
    ) -> Optional[str]:
        """Classify a transcript sample into one of *candidates*.

        Wraps the cheap classifier call so that any failure (missing API key,
        network error, unusable answer) falls back to the default profile
        instead of failing the file.

        Args:
            sample: Transcript sample (head + middle of the extracted text).
            candidates: Profiles the answer may map to.

        Returns:
            Profile id, or nothing when classification was not possible.
        """
        try:
            classifier = self._get_ai_client(None)
        except Exception:
            log.warning(
                "Cannot create the content-type classifier client — "
                "falling back to the default profile",
                exc_info=True,
            )
            return None
        return classifier.classify_profile(candidates, sample)

    def _cancel_all_pending(self) -> None:
        """Cancel all outstanding debounce timers.

        Called during shutdown to prevent timers from firing after the
        main loop has exited.
        """
        with self._lock:
            for path, timer in list(self._pending.items()):
                timer.cancel()
                log.debug("Cancelled pending timer for %s", path.name)
            self._pending.clear()


# ---------------------------------------------------------------------------
# Internal: watchdog event handler
# ---------------------------------------------------------------------------


class _SubtitleEventHandler(FileSystemEventHandler):
    """watchdog event handler that forwards filesystem events to :class:`FileWatcher`.

    Only files whose extension is in the configured watch list
    (``.srt`` / ``.vtt``) trigger processing.  Directory events and
    unrelated files are silently ignored.

    Every event resets the debounce timer for the affected file, so that
    rapid sequences of writes (common when copying large files) do not
    cause premature processing.
    """

    def __init__(self, watcher: FileWatcher) -> None:
        """Initialise the handler.

        Args:
            watcher: The owning :class:`FileWatcher` instance.
        """
        super().__init__()
        self._watcher = watcher

    def on_created(self, event: Any) -> None:
        self._handle(event.src_path, event.is_directory)

    def on_modified(self, event: Any) -> None:
        self._handle(event.src_path, event.is_directory)

    def on_moved(self, event: Any) -> None:
        # For move / rename events the file's *new* location (dest_path)
        # is the one inside the watch directory that we need to process.
        self._handle(event.dest_path, event.is_directory)

    def _handle(self, raw_path: str, is_directory: bool) -> None:
        """Filter and forward a filesystem event.

        Ignored: directory events, files with extensions not in the
        configured watch list, and non-``.srt`` files.

        Args:
            raw_path: Filesystem path string from the watchdog event.
            is_directory: ``True`` if the event targets a directory.
        """
        if is_directory:
            return

        path = Path(raw_path)
        if path.suffix.lower() not in self._watcher._allowed_extensions:
            return

        self._watcher._on_file_event(path)


#: Backwards-compatible alias — the handler used to be SRT-only.
_SrtEventHandler = _SubtitleEventHandler


# ===========================================================================
# Self-test
# ===========================================================================

if __name__ == "__main__":
    """Run a quick self-test using a temporary watch directory.

    Usage::

        python -m src.srt_daemon.watcher

    This test does **not** make real AI API calls — it validates state
    management, debounce, and file detection logic only.
    """
    import tempfile
    import time
    from unittest.mock import MagicMock, patch

    from src.srt_daemon.config import Config

    # ---- Helper: minimal test logging ---------------------------------------
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    print("=" * 60)
    print("SRT WATCHER — SELF TEST")
    print("=" * 60)

    # ---- Build a synthetic Config -------------------------------------------
    tmp_root = Path(tempfile.mkdtemp(prefix="srt_watcher_test_"))
    watch_dir = tmp_root / "watch"
    watch_dir.mkdir()
    output_dir = tmp_root / "output"
    output_dir.mkdir()
    state_file = tmp_root / ".processed.json"

    cfg_data = {
        "watch": {
            "directory": str(watch_dir),
            "extensions": [".srt", ".vtt"],
            "poll_interval_ms": 1000,
            "debounce_seconds": 0.5,
        },
        "output": {
            "directory": str(output_dir),
            "filename_template": "{basename}_笔记_{date}.md",
        },
        "ai": {
            "model": "test-model",
            "max_tokens": 100,
            "temperature": 0.0,
            "connection_timeout_sec": 5,
            "read_timeout_sec": 10,
            "max_retries": 0,
            "retry_backoff_base_sec": 1,
            "chunk_threshold": 9999,
            "system_prompt_file": "",
        },
        "logging": {
            "file": str(tmp_root / "test.log"),
            "level": "DEBUG",
        },
        "state_file": str(state_file),
    }

    # The Config constructor validates the project root for path resolution.
    # Since we use absolute paths everywhere, we can set project_root to tmp_root.
    config = Config(cfg_data, tmp_root)

    # We need to mock api_key because the test env has no DEEPSEEK_API_KEY.
    # Override it on the instance for testing.
    with patch.object(Config, "api_key", new_callable=MagicMock) as mock_key:
        mock_key.return_value = "test-key"

        watcher = FileWatcher(config)

        # ---- Test 1: state initialisation -----------------------------------
        print("\n[Test 1] State initialisation...")
        assert watcher._state == {"files": {}, "in_progress": {}}
        assert state_file.exists()
        print("  PASSED — fresh state created")

        # ---- Test 2: _compute_file_hash -------------------------------------
        print("\n[Test 2] File hash computation...")
        test_file = watch_dir / "test_hash.srt"
        test_file.write_text("Hello, world!", encoding="utf-8")
        h1 = watcher._compute_file_hash(test_file)
        test_file.write_text("Different content", encoding="utf-8")
        h2 = watcher._compute_file_hash(test_file)
        assert h1 != h2, "Hash should change when content changes"
        print(f"  h1={h1[:16]}...  h2={h2[:16]}...")
        print("  PASSED")

        # ---- Test 3: _is_processed / _mark_processed ------------------------
        print("\n[Test 3] Processed-state tracking...")
        assert not watcher._is_processed(test_file)
        watcher._mark_processed(test_file, h2, output_dir / "test_output.md")
        assert watcher._is_processed(test_file)
        assert state_file.exists()

        # Verify state file content
        state_content = json.loads(state_file.read_text("utf-8"))
        file_key = str(test_file.resolve())
        assert file_key in state_content["files"]
        assert state_content["files"][file_key]["hash"] == h2
        print(f"  State recorded for: {file_key}")
        print("  PASSED")

        # ---- Test 4: _mark_processing / _clear_processing -------------------
        print("\n[Test 4] In-progress marking...")
        new_file = watch_dir / "test_in_progress.srt"
        new_file.write_text("Some SRT content...", encoding="utf-8")
        new_key = str(new_file.resolve())

        watcher._mark_processing(new_file)
        assert new_key in watcher._state["in_progress"]

        watcher._clear_processing(new_file)
        assert new_key not in watcher._state["in_progress"]
        print("  PASSED")

        # ---- Test 5: _scan_existing -----------------------------------------
        print("\n[Test 5] _scan_existing (already-processed file skipped)...")
        # test_hash.srt is already marked processed — should be skipped.
        # Add a new unprocessed file.
        fresh_file = watch_dir / "fresh_file.srt"
        fresh_file.write_text("Unprocessed content", encoding="utf-8")

        watcher._scan_existing()
        # The fresh file should be in the queue; the processed one skipped.
        found = False
        while not watcher._queue.empty():
            queued = watcher._queue.get_nowait()
            if queued.resolve() == fresh_file.resolve():
                found = True
        assert found, "Unprocessed file should be queued by _scan_existing"
        print("  PASSED")

        # ---- Test 6: deduplication — same hash = no re-processing -----------
        print("\n[Test 6] Deduplication by content hash...")
        # test_hash.srt was already marked processed with h2.
        # Calling _is_processed + hash check should skip it.
        assert watcher._is_processed(test_file)
        assert watcher._state["files"][file_key]["hash"] == h2
        # Simulate what _process_file does before processing:
        current_hash = watcher._compute_file_hash(test_file)
        skip = (
            watcher._is_processed(test_file)
            and watcher._state["files"][file_key].get("hash") == current_hash
        )
        assert skip, "Same hash should trigger skip"
        print("  PASSED")

        # ---- Test 7: changed content = re-processing ------------------------
        print("\n[Test 7] Changed content triggers re-processing...")
        # Modify the file content (hash changes).
        test_file.write_text("Brand new content after edit", encoding="utf-8")
        new_hash = watcher._compute_file_hash(test_file)
        assert new_hash != h2
        skip = (
            watcher._is_processed(test_file)
            and watcher._state["files"][file_key].get("hash") == new_hash
        )
        assert not skip, "Different hash should NOT trigger skip"
        print("  PASSED")

        # ---- Test 8: debounce timer -----------------------------------------
        print("\n[Test 8] Debounce timer scheduling and cancellation...")
        debounce_file = watch_dir / "debounce_test.srt"
        debounce_file.write_text("Trigger debounce", encoding="utf-8")

        # Simulate a file event.
        watcher._on_file_event(debounce_file)
        assert debounce_file in watcher._pending
        print(f"  Timer scheduled for: {debounce_file.name}")

        # Simulate another event for the same file (should reset timer).
        watcher._on_file_event(debounce_file)
        assert debounce_file in watcher._pending
        print("  Timer reset (2nd event)")

        # Wait for the debounce timer to fire.
        time.sleep(config.watch_debounce_seconds + 0.3)
        # The file should now be in the queue.
        enqueued = False
        while not watcher._queue.empty():
            qf = watcher._queue.get_nowait()
            if qf.resolve() == debounce_file.resolve():
                enqueued = True
        # Note: the timer may fire before our check; the file may
        # not be in _pending anymore — the test is that it enqueues.
        assert enqueued, "File should be enqueued after debounce"
        print("  PASSED")

        # ---- Test 9: stop() cancels pending timers --------------------------
        print("\n[Test 9] stop() cancels pending timers...")
        pending_file = watch_dir / "pending_cancel.srt"
        pending_file.write_text("Will be cancelled", encoding="utf-8")
        watcher._on_file_event(pending_file)
        assert pending_file in watcher._pending

        watcher._cancel_all_pending()
        assert len(watcher._pending) == 0
        print("  PASSED")

        # ---- Test 10: empty watch directory — no crash ----------------------
        print("\n[Test 10] Empty watch directory handling...")
        empty_dir = tmp_root / "empty_watch"
        empty_dir.mkdir()
        empty_cfg_data = dict(cfg_data)
        empty_cfg_data["watch"]["directory"] = str(empty_dir)
        empty_config = Config(empty_cfg_data, tmp_root)
        with patch.object(Config, "api_key", new_callable=MagicMock) as mk:
            mk.return_value = "test-key"
            empty_watcher = FileWatcher(empty_config)
            empty_watcher._scan_existing()
            assert empty_watcher._queue.empty()
            print("  PASSED — no crash on empty directory")

        # ---- Test 11: non-.srt files ignored --------------------------------
        print("\n[Test 11] Non-SRT files are ignored...")
        non_srt = watch_dir / "readme.txt"
        non_srt.write_text("Not an SRT file", encoding="utf-8")
        watcher._on_file_event(non_srt)
        assert non_srt not in watcher._pending
        print("  PASSED")

        # ---- Test 12: state file survives crash simulation ------------------
        print("\n[Test 12] State persistence and stale in-progress cleanup...")
        stale_file = watch_dir / "stale_test.srt"
        stale_file.write_text("Stale test", encoding="utf-8")
        stale_key = str(stale_file.resolve())

        # Corrupt the state to simulate a mid-process crash: add a stale
        # in-progress entry with an old timestamp.
        watcher._state["in_progress"][stale_key] = "2020-01-01T00:00:00"
        watcher._save_state()

        # Re-create the watcher (simulates restart).
        with patch.object(Config, "api_key", new_callable=MagicMock) as mk:
            mk.return_value = "test-key"
            new_watcher = FileWatcher(config)
            # The stale entry should have been cleared.
            assert stale_key not in new_watcher._state["in_progress"]
            print("  PASSED — stale in-progress entry cleared on restart")

        # ---- Test 13: file disappeared before processing --------------------
        print("\n[Test 13] File disappeared before processing...")
        ghost_file = watch_dir / "ghost.srt"
        ghost_file.write_text("Boo!", encoding="utf-8")
        ghost_file.unlink()  # remove it
        watcher._process_file(ghost_file)
        print("  PASSED — no exception for disappeared file")

        print("\n" + "=" * 60)
        print("ALL 13 TESTS PASSED")
        print("=" * 60)
