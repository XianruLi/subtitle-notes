"""Subtitle (SRT / WebVTT) to Markdown Study Notes Daemon.

A Windows background service that watches a directory for subtitle files,
auto-detects whether each file is SRT or WebVTT, picks the content-type profile
(course / meeting / interview) and converts everything into structured Markdown
notes using AI.

An optional companion command turns audio/video recordings into
speaker-labelled subtitles first:

    python -m src.srt_daemon.transcribe --new
"""

__version__ = "0.3.0"
__author__ = "Daemon Project"
