"""AI client module for the Subtitle Daemon.

Wraps the DeepSeek API (OpenAI-compatible) to process subtitle text and
generate the Markdown document requested by a **content-type profile**
(course notes, meeting minutes, interview digest).  Supports large-file
chunking, automatic retry with exponential backoff, per-profile system
prompts, and a cheap content-type classifier.

Usage::

    from src.srt_daemon.config import load_config
    from src.srt_daemon.ai_client import AiClient, AiClientError
    from src.srt_daemon.profiles import ProfileRegistry

    cfg = load_config()
    profile = ProfileRegistry(cfg).get("meeting")
    client = AiClient(cfg, profile=profile)
    try:
        markdown = client.process(subtitle_text, "standup.vtt", profile=profile)
        # write markdown to output...
    except AiClientError as e:
        # handle failure after all retries...
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional, Sequence

import httpx
import openai
from openai import (
    APIError,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAI,
    RateLimitError,
)

from src.srt_daemon.config import Config
from src.srt_daemon.profiles import Profile
from src.srt_daemon.speakers import SpeakerInfo, build_speaker_instruction

# ---------------------------------------------------------------------------
# Module-level logger
# ---------------------------------------------------------------------------

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class AiClientError(Exception):
    """Raised when a DeepSeek API call fails after all retry attempts are
    exhausted.

    The original exception is available via ``__cause__``.
    """


class RetryableAiError(AiClientError):
    """A call that technically succeeded but produced nothing usable.

    Observed in practice: the API returns HTTP 200 with an empty message body
    (for example when the model spends its whole budget and finishes with
    finish_reason=length). Treating that as success wrote a note with a header
    and no body, so it is retried like a transient failure instead.
    """


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# DeepSeek API base URL
_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Loaded system prompts, keyed by prompt file (one per content-type profile).
_PROMPT_CACHE: dict[Path, str] = {}


# ---------------------------------------------------------------------------
# AiClient
# ---------------------------------------------------------------------------

class AiClient:
    """DeepSeek API client for processing SRT subtitle text.

    Loads a domain-specific system prompt from a Markdown file, sends
    subtitle text to the DeepSeek API, and returns structured Markdown
    study notes.

    For large subtitle sets (exceeding the configured chunk threshold),
    the text is automatically split into batches, processed separately,
    and the results are merged into a single cohesive document.

    Attributes:
        config: The daemon :class:`Config` instance supplying all AI settings.
    """

    # Regex: a line consisting solely of one or more digits (SRT index)
    _SUBTITLE_INDEX_RE = re.compile(r"^\d+$", re.MULTILINE)

    # Regex: SRT timestamp line (HH:MM:SS,mmm --> HH:MM:SS,mmm)
    _TIMESTAMP_RE = re.compile(
        r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->\s*\d{2}:\d{2}:\d{2}[,.]\d{3}$"
    )

    # Regex: timestamp marker from extract_text(with_timestamps=True)
    # Matches "[HH:MM:SS] " prefix inserted by SrtParser.extract_text()
    _TS_MARKER_RE = re.compile(r"^\[\d{2}:\d{2}:\d{2}\]\s")

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self, config: Config, profile: Optional[Profile] = None) -> None:
        """Initialise the AI client from a fully-resolved :class:`Config`.

        Loads the system prompt of *profile* (or of the configured default
        prompt file when no profile is given) and creates the
        OpenAI-compatible SDK client pointed at the DeepSeek API endpoint.
        Retries are handled internally by this module (SDK-level retries are
        disabled).

        Args:
            config: Fully-resolved configuration instance.
            profile: Content-type profile whose prompt template this client
                should use.  ``None`` keeps the legacy behaviour of reading
                ``ai.system_prompt_file``.

        Raises:
            FileNotFoundError: If the system prompt file does not exist.
            RuntimeError: If ``DEEPSEEK_API_KEY`` is not set.
        """
        self.config = config
        self._profile = profile
        self._system_prompt = self._load_system_prompt(profile)

        # OpenAI-compatible client pointed at DeepSeek API.
        # SDK-level retries are disabled — we manage retry policy ourselves.
        self._client = OpenAI(
            api_key=config.api_key,
            base_url=_DEEPSEEK_BASE_URL,
            timeout=httpx.Timeout(
                connect=config.ai_connection_timeout,
                read=config.ai_read_timeout,
                write=60.0,
                pool=10.0,
            ),
            max_retries=0,
        )

        log.info(
            "AiClient initialised: provider=deepseek  model=%s  profile=%s  "
            "max_tokens=%d  temperature=%.2f  reasoning_effort=%s  "
            "retries=%d  chunk_threshold=%d",
            config.ai_model,
            profile.id if profile is not None else "(default)",
            config.ai_max_tokens,
            config.ai_temperature,
            config.ai_reasoning_effort or "(none)",
            config.ai_max_retries,
            config.ai_chunk_threshold,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(
        self,
        text: str,
        filename: str,
        *,
        profile: Optional[Profile] = None,
        speaker: Optional[SpeakerInfo] = None,
        source_format: Optional[str] = None,
        has_video: bool = False,
    ) -> str:
        """Process subtitle text and return the requested Markdown document.

        Automatically detects whether chunking is needed based on the
        configured subtitle-count threshold.  For single-chunk files a
        single API call is made; for multi-chunk files the chunks are
        processed sequentially and the final responses merged.

        Args:
            text: Subtitle content.  May include SRT/VTT timestamps and
                index numbers, or be concatenated plain text.
            filename: Original subtitle filename, used for AI context.
            profile: Content-type profile for this file.  Falls back to the
                profile this client was constructed with.
            speaker: Speaker analysis of the transcript — drives the
                "use these exact labels" / "inference only" instruction.
            has_video: True when a video is on disk for screenshots, so the
                model is told that picture markers will actually be rendered.
            source_format: Subtitle format label (``SRT`` / ``VTT``).

        Returns:
            Markdown-formatted structured document.

        Raises:
            ValueError: If *text* is empty or whitespace-only.
            AiClientError: If all API call attempts (including retries)
                are exhausted.
        """
        if not text or not text.strip():
            raise ValueError("Cannot process empty subtitle text")

        profile = profile or self._profile
        system_prompt = self._system_prompt_for(profile)
        context = self._build_context_block(
            filename, profile, speaker, source_format, has_video=has_video
        )

        subtitle_count = self._count_subtitles(text)
        log.info(
            "process() file=%r  subtitle_count=%d  threshold=%d  profile=%s  "
            "speaker_mode=%s",
            filename,
            subtitle_count,
            self.config.ai_chunk_threshold,
            profile.id if profile is not None else "(default)",
            speaker.mode if speaker is not None else "(unknown)",
        )

        if subtitle_count > self.config.ai_chunk_threshold:
            log.info("Subtitle count exceeds threshold — chunking enabled")
            chunks = self._chunk_text(text)
            log.info("Split into %d chunk(s)", len(chunks))
            responses = self._process_chunks(
                chunks, filename, system_prompt, context
            )
            return self._merge_responses(
                responses, filename, system_prompt, context, speaker
            )

        user_message = self._build_user_message(text, filename, context)
        return self._call_api_with_retry(system_prompt, user_message)

    def _system_prompt_for(self, profile: Optional[Profile]) -> str:
        """Return the cached system prompt of *profile* (or the default one)."""
        if profile is None or profile is self._profile:
            return self._system_prompt
        return self._load_system_prompt(profile)

    def classify_profile(
        self,
        candidates: Sequence[Profile],
        sample: str,
    ) -> Optional[str]:
        """Ask the model which content-type profile fits *sample*.

        The call is deliberately cheap: a tiny system prompt, a short sample
        and a capped output budget (see ``ai.classifier_max_tokens`` and
        ``ai.classifier_reasoning_effort``).

        Args:
            candidates: Profiles the answer is allowed to map to.
            sample: Transcript sample (head + middle of the extracted text).

        Returns:
            The matching profile id, or nothing when the call failed or the
            answer could not be mapped — callers then keep the default
            profile.
        """
        candidate_ids = [p.id for p in candidates]
        if not candidate_ids or not sample.strip():
            return None

        system_prompt = (
            "You classify subtitle transcripts for an automatic note-taking "
            "pipeline. Answer with exactly one word and nothing else: "
            + ", ".join(candidate_ids)
            + "."
        )
        user_message = (
            "Classify the transcript sample below.\n"
            "course = a lecture, lesson or tutorial that teaches a topic; "
            "meeting = a work meeting with an agenda, status updates, "
            "decisions or action items; "
            "interview = an interview, podcast or conversation between a host "
            "and one or more guests.\n\n"
            f"{sample}"
        )

        try:
            answer = self._call_api_with_retry(
                system_prompt,
                user_message,
                overrides={
                    "max_tokens": self.config.ai_classifier_max_tokens,
                    "temperature": 0.0,
                    "reasoning_effort": self.config.ai_classifier_reasoning_effort,
                },
            )
        except AiClientError as exc:
            log.warning("Content-type classification failed: %s", exc)
            return None

        cleaned = re.sub(r"[^a-z_]", "", answer.strip().lower())
        for profile_id in candidate_ids:
            if cleaned == profile_id.lower():
                log.info("Content type detected: %s", profile_id)
                return profile_id

        lowered = answer.lower()
        for profile_id in candidate_ids:
            if profile_id.lower() in lowered:
                log.info(
                    "Content type detected: %s (from %r)", profile_id, answer[:40]
                )
                return profile_id

        log.warning(
            "Content-type classifier returned an unusable answer: %r", answer[:80]
        )
        return None

    # ------------------------------------------------------------------
    # Internal: system prompt loading
    # ------------------------------------------------------------------

    def _load_system_prompt(self, profile: Optional[Profile] = None) -> str:
        """Load the system prompt for *profile* from its Markdown file.

        The prompt file contains a metadata header followed by the actual
        prompt body.  This method locates the ``## System Prompt`` heading
        and returns everything from that point onward.  Loaded prompts are
        cached per file, so switching profiles costs nothing.

        Args:
            profile: Content-type profile to load the prompt for.  When
                ``None`` the legacy ``ai.system_prompt_file`` is used.

        Returns:
            The system prompt string.

        Raises:
            FileNotFoundError: If the prompt file cannot be found.
        """
        prompt_file = (
            profile.prompt_file
            if profile is not None
            else self.config.ai_system_prompt_file
        )

        cached = _PROMPT_CACHE.get(prompt_file)
        if cached is not None:
            return cached

        if not prompt_file.exists():
            raise FileNotFoundError(
                f"System prompt file not found: {prompt_file}"
            )

        content = prompt_file.read_text(encoding="utf-8")

        # Extract content starting from the "## System Prompt" heading.
        # This skips the file's own metadata block (lines 1-7).
        heading_marker = "## System Prompt"
        idx = content.find(heading_marker)
        if idx != -1:
            # Advance past the heading line + any trailing blank line
            newline_idx = content.find("\n", idx)
            if newline_idx != -1:
                start = newline_idx + 1
                if start < len(content) and content[start] == "\n":
                    start += 1
                prompt = content[start:].strip()
                log.debug(
                    "Loaded system prompt from %s (%d chars)",
                    prompt_file, len(prompt),
                )
                _PROMPT_CACHE[prompt_file] = prompt
                return prompt

        # Fallback: heading not found — use the entire file
        log.warning(
            "System prompt heading %r not found in %s — using entire file",
            heading_marker, prompt_file,
        )
        prompt = content.strip()
        _PROMPT_CACHE[prompt_file] = prompt
        return prompt

    # ------------------------------------------------------------------
    # Internal: subtitle counting
    # ------------------------------------------------------------------

    def _count_subtitles(self, text: str) -> int:
        """Count the number of subtitle entries in *text*.

        Two formats are recognised:

        1. **Raw SRT** — a line containing only a positive integer,
           immediately followed by a timestamp line (``HH:MM:SS,mmm -->``).
        2. **Extracted text** — lines starting with ``[HH:MM:SS]`` markers
           (as produced by ``SrtParser.extract_text(with_timestamps=True)``).

        If neither structure is detected the return value is ``1``, causing
        the text to be treated as a single logical unit.

        Args:
            text: Subtitle content (raw SRT or timestamp-marked text).

        Returns:
            Estimated subtitle entry count.  Minimum ``1``.
        """
        lines = text.strip().splitlines()
        count = 0

        for i, line in enumerate(lines):
            stripped = line.strip()
            # Method 1: raw SRT (index line followed by timestamp line)
            if stripped.isdigit() and int(stripped) > 0:
                if i + 1 < len(lines) and self._TIMESTAMP_RE.match(
                    lines[i + 1].strip()
                ):
                    count += 1
            # Method 2: timestamp-marker from extract_text(with_timestamps=True)
            elif self._TS_MARKER_RE.match(stripped):
                count += 1

        return count if count > 0 else 1

    # ------------------------------------------------------------------
    # Internal: text chunking
    # ------------------------------------------------------------------

    def _chunk_text(self, text: str) -> list[str]:
        """Split subtitle text into chunks respecting the entry threshold.

        Each chunk contains at most ``ai_chunk_threshold`` SRT entries.
        Splitting is performed on subtitle-entry boundaries (blank-line
        separators) so that no entry is broken across chunks.

        Args:
            text: Subtitle text with recognisable SRT structure.

        Returns:
            List of text chunks, each a string of complete SRT entries.
        """
        threshold = self.config.ai_chunk_threshold
        entries = self._split_entries(text)

        chunks: list[str] = []
        for i in range(0, len(entries), threshold):
            batch = entries[i : i + threshold]
            chunk_text = "\n\n".join(batch)
            chunks.append(chunk_text)

        return chunks

    def _split_entries(self, text: str) -> list[str]:
        """Parse subtitle text into a list of individual entries.

        Handles two input formats:

        1. **Raw SRT** — numbered blocks with ``HH:MM:SS,mmm -->`` timestamps.
        2. **Extracted text** — ``[HH:MM:SS]`` marker-prefixed lines
           (as produced by ``SrtParser.extract_text(with_timestamps=True)``).

        For format (2), entries are split on every ``[HH:MM:SS]`` marker
        boundary — each marker starts a new entry.  For format (1), blank-line
        separators between SRT blocks are used.

        Args:
            text: Subtitle text (raw SRT or timestamp-marked).

        Returns:
            List where each element is a single subtitle entry string.
        """
        # Normalise line endings
        normalised = text.replace("\r\n", "\n").replace("\r", "\n")

        # Detect format: do we have timestamp markers?
        has_ts_markers = bool(self._TS_MARKER_RE.search(normalised))

        if has_ts_markers:
            # --- Format 2: [HH:MM:SS] marker text ---
            # Split the text on every [HH:MM:SS] marker boundary.
            # Each marker starts a new entry block.
            parts = re.split(
                r"\n(?=\[\d{2}:\d{2}:\d{2}\]\s)", normalised.strip()
            )
            entries = [p.strip() for p in parts if p.strip()]
            log.debug(
                "_split_entries: %d entries parsed (timestamp-marker format)",
                len(entries),
            )
            return entries
        else:
            # --- Format 1: raw SRT blocks ---
            raw_blocks = re.split(r"\n\s*\n", normalised.strip())

            entries: list[str] = []
            current_lines: list[str] = []

            for block in raw_blocks:
                stripped = block.strip()
                if not stripped:
                    continue

                block_lines = stripped.splitlines()
                first_line = block_lines[0].strip()

                # Does this block start a new SRT entry?
                is_new_entry = (
                    first_line.isdigit()
                    and int(first_line) > 0
                    and len(block_lines) >= 2
                    and self._TIMESTAMP_RE.match(block_lines[1].strip())
                )

                if is_new_entry:
                    if current_lines:
                        entries.append("\n".join(current_lines))
                    current_lines = [stripped]
                else:
                    current_lines.append(stripped)

            if current_lines:
                entries.append("\n".join(current_lines))

            log.debug("_split_entries: %d entries parsed (raw SRT format)", len(entries))
            return entries

    # ------------------------------------------------------------------
    # Internal: chunk processing
    # ------------------------------------------------------------------

    def _process_chunks(
        self,
        chunks: list[str],
        filename: str,
        system_prompt: Optional[str] = None,
        context: str = "",
    ) -> list[str]:
        """Process each chunk sequentially through the DeepSeek API.

        The first chunk's user message includes a "Part 1 of N" header;
        subsequent chunks carry "Part X of N — continue from Part X-1" plus an
        instruction to stay consistent with the previous parts (terminology,
        heading numbering, speaker identifiers).

        Args:
            chunks: List of text chunks (strings of subtitle entries).
            filename: Original subtitle filename for AI context.
            system_prompt: System prompt to use (defaults to this client's).
            context: Per-file context block (profile + speaker information).

        Returns:
            List of AI response strings, one per chunk, in order.
        """
        system_prompt = system_prompt or self._system_prompt
        total = len(chunks)
        responses: list[str] = []

        for i, chunk in enumerate(chunks):
            part_num = i + 1

            if part_num == 1:
                heading = (
                    f"This is Part 1 of {total}.  Process this first portion "
                    f"and produce the Markdown document defined in the system "
                    f"prompt."
                )
            else:
                heading = (
                    f"This is Part {part_num} of {total}.  Continue from where "
                    f"Part {part_num - 1} left off.  Process the next portion, "
                    f"keeping terminology, heading numbering and speaker "
                    f"identifiers consistent with the previous parts."
                )

            blocks = [f"Transcript file: {filename}", heading]
            if context:
                blocks.append(context)
            blocks.append(chunk)
            user_message = "\n\n".join(blocks)

            log.info(
                "Chunk %d/%d: %d chars → sending to API",
                part_num, total, len(chunk),
            )
            response = self._call_api_with_retry(
                system_prompt, user_message,
            )
            responses.append(response)
            log.info(
                "Chunk %d/%d complete: %d chars response",
                part_num, total, len(response),
            )

        return responses

    # ------------------------------------------------------------------
    # Internal: response merging
    # ------------------------------------------------------------------

    def _merge_responses(
        self,
        responses: list[str],
        filename: str,
        system_prompt: Optional[str] = None,
        context: str = "",
        speaker: Optional[SpeakerInfo] = None,
    ) -> str:
        """Merge multiple chunk responses into a single cohesive document.

        When only one response is provided it is returned unchanged.
        Otherwise all responses are concatenated and sent to the AI with
        a merge instruction that requests a unified, well-structured
        Markdown document with no duplication.  Inferred speaker labels are
        reconciled here, because each chunk was processed in isolation.

        Args:
            responses: AI response strings from individual chunks.
            filename: Original subtitle filename for context.
            system_prompt: System prompt to use (defaults to this client's).
            context: Per-file context block (profile + speaker information).
            speaker: Speaker analysis of the transcript.

        Returns:
            A single merged Markdown document.

        Raises:
            AiClientError: If the merge API call fails.
        """
        if len(responses) == 1:
            return responses[0]

        system_prompt = system_prompt or self._system_prompt

        # Build the combined input for the merge call
        combined_parts: list[str] = []
        for i, r in enumerate(responses):
            combined_parts.append(f"### Part {i + 1}\n{r}")
        combined = "\n\n---\n\n".join(combined_parts)

        instructions = [
            f"You previously processed the transcript '{filename}' in "
            f"{len(responses)} parts.  Below are the separate responses.",
            "Please **merge them into a SINGLE cohesive, well-structured "
            "Markdown document** that follows the output format defined in the "
            "system prompt.",
            "Eliminate any duplication and ensure one consistent heading "
            "hierarchy.",
        ]
        if speaker is not None and not speaker.labeled:
            instructions.append(
                "Speaker attribution was inferred separately per part and is "
                "NOT reliable: make sure each person maps to exactly one "
                "identifier across the whole document, and keep the statement "
                "that the speaker split is inferred."
            )
        if context:
            instructions.append(context)

        merge_prompt = "\n\n".join(instructions) + "\n\n" + combined

        log.info("Merging %d chunk responses (merge input: %d chars)",
                 len(responses), len(merge_prompt))
        result = self._call_api_with_retry(system_prompt, merge_prompt)
        log.info("Merge complete: %d chars output", len(result))
        return result

    # ------------------------------------------------------------------
    # Internal: user message builder (single-chunk)
    # ------------------------------------------------------------------

    def _build_user_message(
        self, text: str, filename: str, context: str = ""
    ) -> str:
        """Build the user message for a single-chunk API call.

        Args:
            text: Subtitle text to process.
            filename: Original subtitle filename.
            context: Optional context block (profile + speaker information)
                describing this particular file.

        Returns:
            A formatted user-message string ready for the API.
        """
        blocks = [
            "Please process the following subtitle transcript and generate the "
            "Markdown document described in the system prompt.",
            f"**Source file**: {filename}",
        ]
        if context:
            blocks.append(context)
        blocks.append(f"**Subtitle content**:\n\n{text}")
        return "\n\n".join(blocks)

    def _build_context_block(
        self,
        filename: str,
        profile: Optional[Profile],
        speaker: Optional[SpeakerInfo],
        source_format: Optional[str],
        has_video: bool = False,
    ) -> str:
        """Build the per-file context block used by every API call.

        The block tells the model which content type it is dealing with, which
        subtitle format the text came from, and how speakers are represented —
        the last one decides between "use these exact labels" and "inference
        only, say so" behaviour.

        Args:
            filename: Source file name.
            profile: Resolved content-type profile (may be absent).
            speaker: Speaker analysis (may be absent).
            source_format: Subtitle format label (``SRT`` / ``VTT``).

        Returns:
            A Markdown-ish context block, or an empty string when there is
            nothing to say.
        """
        lines: list[str] = []
        if has_video:
            lines.append(
                "【重要】本次已提供原视频，截图标记会被真正渲染成图片。"
                "请输出 **至少 3 个、最多 8 个** [[screenshot:HH:MM:SS]] 标记，"
                "分别放在出现画面内容的时刻（幻灯片、代码、图表、演示操作、实物、屏幕文字）。"
                "标记必须独占一行；时间戳必须从字幕里原样复制，不得编造。"
                "宁可选该句靠后的时间戳——画面通常比话语晚出现。"
            )
        if source_format:
            lines.append(f"**Subtitle format**: {source_format}")
        if profile is not None:
            lines.append(
                f"**Content type (profile)**: {profile.id} — {profile.label}"
            )
        if speaker is not None:
            lines.append(
                f"**Speaker mode**: {speaker.mode} "
                f"({speaker.labeled_entries}/{speaker.total_entries} subtitle "
                f"entries carry a speaker label)"
            )
            instruction = build_speaker_instruction(
                speaker, profile.id if profile is not None else ""
            )
            if instruction:
                lines.append(instruction)
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal: API call with retry logic
    # ------------------------------------------------------------------

    def _call_api_with_retry(
        self,
        system_prompt: str,
        user_message: str,
        overrides: Optional[dict] = None,
    ) -> str:
        """Call the DeepSeek API with automatic retry on transient errors.

        **Retry classification**:

        +------------------------+-----------------+
        | Error type             | Action          |
        +========================+=================+
        | 429 RateLimitError     | retry + backoff |
        +------------------------+-----------------+
        | 5xx APIStatusError     | retry + backoff |
        +------------------------+-----------------+
        | 4xx APIStatusError     | raise immediately (no retry) |
        +------------------------+-----------------+
        | APITimeoutError        | retry + backoff |
        +------------------------+-----------------+
        | APIConnectionError     | retry + backoff |
        +------------------------+-----------------+
        | Other APIError         | retry + backoff |
        +------------------------+-----------------+

        Backoff formula: ``delay = retry_backoff_base * 2^(attempt - 1)``
        seconds, where *attempt* is 1-indexed, capped at 300 seconds.

        Args:
            system_prompt: System-level instruction for the AI.
            user_message: The user message (subtitle text + context).
            overrides: Optional per-call parameter overrides
                (``max_tokens`` / ``temperature`` /
                ``reasoning_effort``) — used by the cheap content-type
                classifier call.

        Returns:
            The AI's text response content.

        Raises:
            AiClientError: If all attempts (initial + retries) fail.
        """
        max_retries = self.config.ai_max_retries
        base_delay = self.config.ai_retry_backoff_base
        total_attempts = max_retries + 1

        last_error: Optional[Exception] = None

        for attempt in range(1, total_attempts + 1):
            try:
                log.debug(
                    "API call attempt %d/%d", attempt, total_attempts,
                )
                return self._call_api(system_prompt, user_message, overrides)

            except RateLimitError as e:
                last_error = e
                if attempt >= total_attempts:
                    break
                delay = self._backoff_delay(base_delay, attempt)
                log.warning(
                    "Rate limited (attempt %d/%d), retrying in %.1fs: %s",
                    attempt, total_attempts, delay, e,
                )
                time.sleep(delay)

            except APITimeoutError as e:
                last_error = e
                if attempt >= total_attempts:
                    break
                delay = self._backoff_delay(base_delay, attempt)
                log.warning(
                    "API timeout (attempt %d/%d), retrying in %.1fs: %s",
                    attempt, total_attempts, delay, e,
                )
                time.sleep(delay)

            except APIConnectionError as e:
                last_error = e
                if attempt >= total_attempts:
                    break
                delay = self._backoff_delay(base_delay, attempt)
                log.warning(
                    "Connection error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt, total_attempts, delay, e,
                )
                time.sleep(delay)

            except APIStatusError as e:
                last_error = e
                status = e.status_code

                if status == 429:
                    # RateLimitError *should* catch 429, but handle here too.
                    if attempt >= total_attempts:
                        break
                    delay = self._backoff_delay(base_delay, attempt)
                    log.warning(
                        "HTTP 429 (attempt %d/%d), retrying in %.1fs",
                        attempt, total_attempts, delay,
                    )
                    time.sleep(delay)

                elif 500 <= status < 600:
                    # Server error — transient, worth retrying
                    if attempt >= total_attempts:
                        break
                    delay = self._backoff_delay(base_delay, attempt)
                    log.warning(
                        "HTTP %d server error (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        status, attempt, total_attempts, delay, e,
                    )
                    time.sleep(delay)

                else:
                    # 4xx client error (non-429) — not retryable
                    log.error(
                        "HTTP %d client error — not retrying: %s", status, e,
                    )
                    raise AiClientError(
                        f"API client error (HTTP {status}): {e}"
                    ) from e

            except RetryableAiError as e:
                # Empty or incomplete answer from the model — retry with backoff.
                last_error = e
                if attempt >= total_attempts:
                    break
                delay = self._backoff_delay(base_delay, attempt)
                log.warning(
                    "Empty response (attempt %d/%d), retrying in %.1fs: %s",
                    attempt, total_attempts, delay, e,
                )
                time.sleep(delay)

            except APIError as e:
                # Catch-all for any other OpenAI / DeepSeek SDK error
                last_error = e
                if attempt >= total_attempts:
                    break
                delay = self._backoff_delay(base_delay, attempt)
                log.warning(
                    "API error (attempt %d/%d), retrying in %.1fs: %s",
                    attempt, total_attempts, delay, e,
                )
                time.sleep(delay)

        # All attempts exhausted
        log.error("All %d API call attempt(s) failed", total_attempts)
        raise AiClientError(
            f"API call failed after {total_attempts} attempt(s). "
            f"Last error: {last_error}"
        ) from last_error

    # ------------------------------------------------------------------
    # Internal: single (non-retried) API call
    # ------------------------------------------------------------------

    def _call_api(
        self,
        system_prompt: str,
        user_message: str,
        overrides: Optional[dict] = None,
    ) -> str:
        """Make a single call to the DeepSeek Chat Completions API.

        This is the lowest-level API wrapper — retry logic is handled by
        :meth:`_call_api_with_retry`.

        For reasoning-capable models (``deepseek-v4-pro``) the
        ``reasoning_effort`` parameter is passed via ``extra_body`` to control
        the depth of internal chain-of-thought before responding.
        ``deepseek-flash`` accepts the parameter but ignores it.

        Args:
            system_prompt: System-level instruction for the AI.
            user_message: The user message content.
            overrides: Optional per-call overrides for ``max_tokens``,
                ``temperature`` and ``reasoning_effort``.

        Returns:
            The text content of the AI's response.

        Raises:
            Various ``openai.*Error`` subclasses on failure.
        """
        overrides = overrides or {}

        # Build the base request parameters
        params: dict = {
            "model": self.config.ai_model,
            "max_tokens": int(
                overrides.get("max_tokens", self.config.ai_max_tokens)
            ),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
        }

        # Temperature: reasoning models typically ignore this, but we pass it
        # for non-reasoning models such as deepseek-flash.
        params["temperature"] = float(
            overrides.get("temperature", self.config.ai_temperature)
        )

        # Reasoning effort: DeepSeek-specific parameter passed via extra_body.
        # Only meaningful for reasoning-capable models; silently ignored by
        # non-reasoning models.  An explicit override of None disables it.
        effort = (
            overrides["reasoning_effort"]
            if "reasoning_effort" in overrides
            else self.config.ai_reasoning_effort
        )
        if effort is not None:
            params["extra_body"] = {"reasoning_effort": effort}

        response = self._client.chat.completions.create(**params)

        # Extract the assistant's text response
        choice = response.choices[0]
        result = choice.message.content or ""
        finish_reason = getattr(choice, "finish_reason", None)

        if not result.strip():
            # An empty answer is a failure, not a result: it used to write a
            # note whose body was empty. Retry it like a transient error.
            usage = getattr(response, "usage", None)
            log.warning(
                "API returned an empty response (finish_reason=%s, "
                "completion_tokens=%s) — retrying",
                finish_reason,
                getattr(usage, "completion_tokens", "?"),
            )
            raise RetryableAiError(
                f"empty response from the model (finish_reason={finish_reason})"
            )

        log.debug(
            "API call OK: %d chars input → %d chars output (finish_reason=%s)",
            len(user_message), len(result), finish_reason,
        )
        return result

    # ------------------------------------------------------------------
    # Internal: helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _backoff_delay(base: float, attempt: int) -> float:
        """Compute exponential backoff delay.

        Formula: ``base * 2^(attempt - 1)``, capped at 300 seconds.

        Args:
            base: Base delay in seconds.
            attempt: 1-indexed attempt number.

        Returns:
            Delay in seconds.
        """
        delay = base * (2 ** (attempt - 1))
        return min(delay, 300.0)
