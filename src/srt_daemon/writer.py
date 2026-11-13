"""Markdown file writer for SRT Daemon.

Writes AI-generated Markdown study notes to the output directory with a YAML
front-matter header containing processing metadata.  File naming follows the
template configured in ``config.yaml``.

Usage::

    from src.srt_daemon.config import load_config
    from src.srt_daemon.writer import MarkdownWriter

    cfg = load_config()
    writer = MarkdownWriter(cfg)
    output_path = writer.write(
        content=markdown_body,
        source_filename="lecture01.srt",
        metadata={"entries": 523, "text_length": 18420},
    )
    print(f"Written to: {output_path}")
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from src.srt_daemon.config import Config

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Characters forbidden in Windows file names (NTFS).
#   < > : " / \\ | ? *   plus ASCII control characters (0x00–0x1F).
_WIN_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# YAML front-matter delimiter for Markdown metadata block.
_FRONT_MATTER_DELIM = "---"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


class MarkdownWriter:
    """Writes AI-generated Markdown content to the output directory.

    Each source subtitle file gets its own sub-directory under the
    configured output root.  The output filename is generated from a template
    that supports ``{basename}``, ``{profile}``, ``{date}`` and
    ``{time}`` placeholders.
    """

    def __init__(self, config: Config) -> None:
        """Initialise the writer with daemon configuration.

        Args:
            config: A fully-resolved :class:`Config` instance providing
                ``output_dir``, ``output_filename_template``, and
                ``ai_model``.
        """
        self._config = config
        self._output_dir = config.output_dir
        self._template = config.output_filename_template
        self._model = config.ai_model

    # -- main entry point -------------------------------------------------------

    def write(
        self,
        content: str,
        source_filename: str,
        metadata: Optional[dict] = None,
        base_dir: Optional[Path] = None,
    ) -> Path:
        """Write AI-generated Markdown content to the output file.

        The output path is ``{output_dir}/{basename}/{filename_template}``
        where *basename* is derived from *source_filename*.

        Args:
            content: AI-generated Markdown body.
            source_filename: Original subtitle filename (used for naming
                the output sub-directory and file).
            metadata: Optional dict with extra processing metadata.
            base_dir: Root for the output folder instead of output.directory
                (output.mode="source" writes beside the source file).
                Supported keys:

                * ``format`` — subtitle format label (``SRT`` / ``VTT``)
                * ``profile`` — content-type profile id (``course`` / ``meeting`` /
                  ``interview``), also available in the filename template
                  as ``{profile}``
                * ``speakers`` — speaker summary for the front matter
                * ``entries`` — subtitle entry count (``int``)
                * ``text_length`` — processed text length in characters (``int``)
                * ``processed_at`` — ISO-8601 datetime string; if omitted
                  the current UTC time is used.

        Returns:
            Absolute :class:`Path` to the written file.

        Raises:
            OSError: If the output directory cannot be created or the file
                cannot be written.
        """
        metadata = metadata or {}

        # 1. Resolve output path
        output_path = self._resolve_output_path(
            source_filename,
            str(metadata.get("profile") or "general"),
            base_dir=base_dir,
        )

        # 2. Ensure the output directory exists
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log.error("Failed to create output directory %s: %s", output_path.parent, exc)
            raise OSError(
                f"Cannot create output directory: {output_path.parent}"
            ) from exc

        # 3. Build YAML front matter + body
        header = self._build_header(source_filename, metadata)
        full_content = f"{header}\n{content}"

        # 4. Write the file
        log.info("Writing Markdown output to %s", output_path)
        try:
            output_path.write_text(full_content, encoding="utf-8")
        except OSError as exc:
            log.error("Failed to write output file %s: %s", output_path, exc)
            raise OSError(
                f"Cannot write output file: {output_path}"
            ) from exc

        log.info(
            "Markdown written  file=%s  size=%d bytes",
            output_path.name,
            output_path.stat().st_size,
        )
        return output_path

    # -- internal helpers -------------------------------------------------------

    def _build_header(self, source_filename: str, metadata: dict) -> str:
        """Build the YAML front-matter header block.

        The header includes processing metadata visible to readers and
        machine-parsable tools (source, model, subtitle format, notes profile,
        speakers, entry count, text length).

        Args:
            source_filename: Original subtitle filename (``.srt`` / ``.vtt``).
            metadata: Dict that may contain ``format``, ``entries``,
                ``text_length``, and ``processed_at``.

        Returns:
            A string consisting of ``---`` delimiters enclosing YAML
            key-value pairs, terminated with a trailing ``---``.
        """
        processed_at = metadata.get(
            "processed_at",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        entries = metadata.get("entries", "N/A")
        text_length = metadata.get("text_length", "N/A")
        source_format = metadata.get("format", "N/A")
        profile = metadata.get("profile", "N/A")
        speakers = str(metadata.get("speakers", "N/A")).replace('"', '\\"')

        # Escape double-quotes in the source filename for safe YAML quoting.
        safe_source = source_filename.replace('"', '\\"')
        safe_processed = str(processed_at).replace('"', '\\"')
        safe_model = self._model.replace('"', '\\"')

        source_url = metadata.get("source_url")
        platform = metadata.get("platform")

        lines = [
            _FRONT_MATTER_DELIM,
            f'source: "{safe_source}"',
            f'processed: "{safe_processed}"',
            f'model: "{safe_model}"',
        ]
        if source_url:
            safe_url = str(source_url).replace('"', "")
            lines.append(f'source_url: "{safe_url}"')
        if platform:
            lines.append(f"platform: {platform}")
        images = metadata.get("images")
        if images:
            lines.append(f"images: {images}")
        source_video = metadata.get("source_video")
        if source_video:
            safe_video = str(source_video).replace('"', "")[-120:]
            lines.append(f'source_video: "{safe_video}"')
        lines += [
            f"format: {source_format}",
            f"profile: {profile}",
            f'speakers: "{speakers}"',
            f"entries: {entries}",
            f"text_length: {text_length}",
            _FRONT_MATTER_DELIM,
        ]
        return "\n".join(lines)

    def _resolve_output_path(
        self,
        source_filename: str,
        profile: str = "general",
        base_dir: Optional[Path] = None,
    ) -> Path:
        """Build the absolute output :class:`Path` for a given source file.

        The path follows the pattern::

            {output_dir}/{sanitized_basename}/{resolved_template}.md

        where *sanitized_basename* is the source filename without extension,
        with unsafe characters replaced.

        Args:
            source_filename: Original subtitle filename (may be a plain name
                or a path).
            profile: Content-type profile id, available in the filename
                template as ``{profile}``.
            base_dir: Optional root for this note instead of output.directory.

        Returns:
            Absolute output path.  Directories on the path are **not**
            created by this method.
        """
        # Extract the real filename (strip any directory components).
        source = Path(source_filename)
        raw_basename = source.stem  # filename without extension

        # Sanitise for safe use in filesystem paths.
        safe_basename = _sanitise_filename(raw_basename)
        safe_profile = _sanitise_filename(profile) if profile else "general"

        # Build template variables.
        now = datetime.now()
        template_vars = {
            "basename": safe_basename,
            "profile": safe_profile,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H%M%S"),
        }

        # Resolve the filename from the template.
        resolved_filename = self._template.format(**template_vars)

        # Assemble full path: root / basename_subdir / filename
        root = Path(base_dir) if base_dir is not None else self._output_dir
        return root / safe_basename / resolved_filename


# ---------------------------------------------------------------------------
# Internal helpers (module-level)
# ---------------------------------------------------------------------------


def _sanitise_filename(raw: str) -> str:
    """Replace characters that are unsafe for Windows file names.

    The following transformations are applied:
    1. Replace forbidden characters (``< > : " / \\ | ? *`` and ASCII
       control characters) with underscores.
    2. Strip leading periods and spaces (Windows Explorer restrictions).
    3. Strip trailing periods and spaces.
    4. Replace empty or whitespace-only names with ``"untitled"``.

    Args:
        raw: The raw filename (stem, without extension).

    Returns:
        A safe filename string suitable for use in a :class:`Path`.
    """
    safe = _WIN_FORBIDDEN_RE.sub("_", raw)

    # Collapse consecutive underscores from the replacement step (do this
    # before stripping so that a run of forbidden chars doesn't leave
    # leading/trailing underscores).
    safe = re.sub(r"_{2,}", "_", safe)

    # Strip leading / trailing dots, spaces, and underscores (Windows
    # Explorer also rejects trailing dots/spaces; underscores at the
    # boundary are just noise from replacement).
    safe = safe.strip(". _")

    if not safe:
        safe = "untitled"

    return safe
