"""Shared subtitle building for assemble.py (burned-in ASS) and the SRT sidecar upload
(DESIGN.md #36). One canonical SSAFile; ASS keeps the karaoke-style `\\k` tags for
burning, SRT export strips them for the plain-text accessibility/SEO sidecar.

True per-word Whisper timestamps aren't persisted anywhere — align.py only keeps
segment-level `start_s`/`end_s` in the DB (no new column added for raw word timings,
per WORK_MEDIA.md's "don't edit app/db.py without asking Foundation"). So "karaoke"
here approximates word timing by splitting each segment's already-known duration evenly
across its words: a reasonable highlight effect, not a claim of true forced-aligned
per-word timing.
"""

from __future__ import annotations

import pysubs2

from app.config import ChannelConfig
from app.db import Segment

_ALIGNMENT_BY_POSITION = {
    "bottom-left": 1,
    "bottom-center": 2,
    "bottom-right": 3,
    "center": 5,
    "top-center": 8,
}


def _word_windows(text: str, start_s: float, end_s: float) -> list[tuple[str, float, float]]:
    words = text.split()
    if not words:
        return []
    step = max(0.01, end_s - start_s) / len(words)
    return [(w, start_s + i * step, start_s + (i + 1) * step) for i, w in enumerate(words)]


def _karaoke_text(text: str, start_s: float, end_s: float) -> str:
    """ASS `\\k` tags take centiseconds of *duration*, not absolute time."""
    parts = []
    for word, w_start, w_end in _word_windows(text, start_s, end_s):
        centis = max(1, round((w_end - w_start) * 100))
        parts.append(f"{{\\k{centis}}}{word}")
    return " ".join(parts)


def _hex_to_ass_color(hex_color: str) -> pysubs2.Color:
    hex_color = hex_color.lstrip("#")
    r, g, b = (int(hex_color[i : i + 2], 16) for i in (0, 2, 4))
    return pysubs2.Color(r, g, b)


def build_subtitles(segments: list[Segment], config: ChannelConfig) -> pysubs2.SSAFile:
    subs = pysubs2.SSAFile()
    style = pysubs2.SSAStyle()
    style.fontsize = config.subtitles.size
    style.primarycolor = pysubs2.Color(255, 255, 255)
    style.secondarycolor = _hex_to_ass_color(config.subtitles.highlight_color)
    style.outlinecolor = pysubs2.Color(0, 0, 0)
    style.borderstyle = 1
    style.outline = 2
    style.alignment = _ALIGNMENT_BY_POSITION.get(config.subtitles.position, 2)
    subs.styles["Default"] = style

    for segment in segments:
        if segment.start_s is None or segment.end_s is None:
            continue
        text = (
            _karaoke_text(segment.text, segment.start_s, segment.end_s)
            if config.subtitles.karaoke
            else segment.text
        )
        subs.events.append(
            pysubs2.SSAEvent(
                start=pysubs2.make_time(s=segment.start_s),
                end=pysubs2.make_time(s=segment.end_s),
                text=text,
                style="Default",
            )
        )
    return subs
