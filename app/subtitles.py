"""Subtitles from real word timings (work_dir/words.json, written by the align stage),
shared by the long-form render and the Shorts (README §6.4).

- Captions are short bursts of `subtitles.words_per_caption` words, never crossing a
  sentence end or a quote boundary. The burned-in ASS shows the word being spoken in
  `highlight_color` (one event per word); the SRT sidecar has one plain event per caption.
- The ASS canvas (PlayResX/PlayResY) is set to the video's own resolution, so `size` is
  real pixels. Without it libass assumes a 384x288 canvas and scales everything up —
  the old `size: 34` rendered ~127 px tall at 1080p.
- Quote words are italic.
- `window` cuts out one time range (a Short) and shifts it to start at 0.
"""

from __future__ import annotations

import re
from pathlib import Path

import pysubs2
from PIL import ImageFont

from app.config import REPO_ROOT, ChannelConfig, get_settings

#: Keep a caption up through a gap shorter than this rather than flash it off and on.
_HOLD_GAP_S = 0.6
_SENTENCE_BREAK = re.compile(r"""[.!?;:]["'”’)\]]*$""")
_UNSAFE = re.compile(r"[{}\\]")
_ALIGNMENT = {"bottom-center": 2, "center": 5, "top-center": 8}
_WINDOWS_FONT = Path(r"C:\Windows\Fonts\arial.ttf")


def font_path(config: ChannelConfig) -> Path:
    """config.subtitles.font_file, else the first assets/fonts/*.ttf, else Arial."""
    configured = REPO_ROOT / config.subtitles.font_file
    if configured.exists():
        return configured
    bundled = sorted(Path(get_settings().assets_dir).glob("fonts/*.ttf"))
    return bundled[0] if bundled else _WINDOWS_FONT


def font_family(path: Path) -> str:
    """The family name libass matches on (read from the font file itself)."""
    try:
        return ImageFont.truetype(str(path), 10).getname()[0]
    except OSError:
        return "Arial"


def _ass_color(hex_color: str) -> tuple[pysubs2.Color, str]:
    r, g, b = (int(hex_color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))
    return pysubs2.Color(r, g, b), f"&H{b:02X}{g:02X}{r:02X}&"


def captions(timeline: list[dict], words_per_caption: int) -> list[list[dict]]:
    """Group each scene's timed words into short captions."""
    groups: list[list[dict]] = []
    for scene in timeline:
        current: list[dict] = []
        for word in scene["words"]:
            if current and (len(current) >= words_per_caption or word["quote"] != current[-1]["quote"]):
                groups.append(current)
                current = []
            current.append(word)
            if _SENTENCE_BREAK.search(word["text"]):
                groups.append(current)
                current = []
        if current:
            groups.append(current)
    return groups


def _render(words: list[dict], highlight: int | None, color_tag: str) -> str:
    parts = []
    for i, word in enumerate(words):
        text = _UNSAFE.sub("", word["text"])
        if i == highlight:
            text = f"{{\\c{color_tag}}}{text}{{\\c&HFFFFFF&}}"
        if word["quote"]:
            text = f"{{\\i1}}{text}{{\\i0}}"
        parts.append(text)
    return " ".join(parts)


def build_subtitles(
    timeline: list[dict],
    config: ChannelConfig,
    *,
    resolution: tuple[int, int],
    size: int,
    window: tuple[float, float] | None = None,
) -> tuple[pysubs2.SSAFile, pysubs2.SSAFile]:
    """(burned-in ASS, plain SRT sidecar)."""
    subs = config.subtitles
    width, height = resolution
    highlight_color, highlight_tag = _ass_color(subs.highlight_color)

    ass = pysubs2.SSAFile()
    ass.info.update({"PlayResX": str(width), "PlayResY": str(height), "WrapStyle": "0"})
    style = pysubs2.SSAStyle(
        fontname=font_family(font_path(config)), fontsize=size, bold=True,
        primarycolor=pysubs2.Color(255, 255, 255), secondarycolor=highlight_color,
        outlinecolor=pysubs2.Color(0, 0, 0), backcolor=pysubs2.Color(0, 0, 0, 128),
        borderstyle=1, outline=max(2, round(size * 0.07)), shadow=0,
        alignment=pysubs2.Alignment(_ALIGNMENT[subs.position]), marginl=round(width * 0.06), marginr=round(width * 0.06),
        marginv=round(height * (0.07 if subs.position != "center" else 0)),
    )
    ass.styles["Default"] = style
    srt = pysubs2.SSAFile()

    offset = window[0] if window else 0.0
    groups = captions(timeline, subs.words_per_caption)
    if window:
        groups = [[w for w in g if window[0] <= w["start"] < window[1]] for g in groups]
        groups = [g for g in groups if g]

    for n, group in enumerate(groups):
        start = group[0]["start"]
        end = group[-1]["end"]
        next_start = groups[n + 1][0]["start"] if n + 1 < len(groups) else None
        if next_start is not None and 0 <= next_start - end < _HOLD_GAP_S:
            end = next_start
        if window:
            end = min(end, window[1])
        ms = lambda t: pysubs2.make_time(s=max(0.0, t - offset))  # noqa: E731

        srt.events.append(pysubs2.SSAEvent(start=ms(start), end=ms(end), text=_render(group, None, "")))
        if not subs.highlight_current_word:
            ass.events.append(pysubs2.SSAEvent(start=ms(start), end=ms(end), text=_render(group, None, "")))
            continue
        for i, word in enumerate(group):
            word_end = group[i + 1]["start"] if i + 1 < len(group) else end
            ass.events.append(pysubs2.SSAEvent(
                start=ms(start if i == 0 else word["start"]), end=ms(word_end),
                text=_render(group, i, highlight_tag),
            ))
    return ass, srt
