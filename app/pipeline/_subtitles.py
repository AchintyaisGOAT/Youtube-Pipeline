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

#: Short word-bursts, not a full sentence sitting on screen for its whole duration --
#: direct feedback on a real render: a full-sentence caption at a large font covered too
#: much of the illustration. A TikTok/Reels-style caption (a few words at a time) shows
#: far less text at once while covering the same ground over the segment's duration.
_WORDS_PER_CAPTION = 4


def _word_windows(text: str, start_s: float, end_s: float) -> list[tuple[str, float, float]]:
    words = text.split()
    if not words:
        return []
    step = max(0.01, end_s - start_s) / len(words)
    return [(w, start_s + i * step, start_s + (i + 1) * step) for i, w in enumerate(words)]


def _chunk(windows: list[tuple[str, float, float]], size: int) -> list[list[tuple[str, float, float]]]:
    return [windows[i : i + size] for i in range(0, len(windows), size)]


def _caption_text(chunk: list[tuple[str, float, float]], karaoke: bool) -> str:
    """ASS `\\k` tags take centiseconds of *duration*, not absolute time."""
    if not karaoke:
        return " ".join(word for word, _, _ in chunk)
    parts = []
    for word, w_start, w_end in chunk:
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
        windows = _word_windows(segment.text, segment.start_s, segment.end_s)
        for chunk in _chunk(windows, _WORDS_PER_CAPTION):
            subs.events.append(
                pysubs2.SSAEvent(
                    start=pysubs2.make_time(s=chunk[0][1]),
                    end=pysubs2.make_time(s=chunk[-1][2]),
                    text=_caption_text(chunk, config.subtitles.karaoke),
                    style="Default",
                )
            )
    return subs
