"""Thumbnail (README §4.1 step 13, §6.6): a Pillow template, 1280x720.

- The picture: the image the metadata step picked as the video's most striking (archive
  images preferred), else the first scene's. Filled edge to edge, darkened toward the
  bottom-left so the text always reads.
- The hook: the metadata step's 2–4 words, in the channel's font (Arial — its heaviest
  weight installed), white with a black outline; one word in the highlight colour. As
  large as fits in two lines at most.
- A thin frame in the highlight colour. No logo: YouTube already shows the channel's
  picture beside every video.

Also shown in the Shorts' closing card, which is why it's made before the Shorts.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import Asset, Scene, Video
from app.storage import output_dir
from app.subtitles import font_path

THUMBNAIL_SIZE = (1280, 720)
_HEAVY_FONTS = (Path(r"C:\Windows\Fonts\ariblk.ttf"), Path(r"C:\Windows\Fonts\arialbd.ttf"))
_WHITE, _BLACK = (255, 255, 255), (0, 0, 0)
_MARGIN = 56
_FRAME = 10
_MAX_TEXT_WIDTH = 0.78  # of the canvas: leaves the subject visible on the right


def _rgb(hex_color: str) -> tuple[int, int, int]:
    return tuple(int(hex_color.lstrip("#")[i : i + 2], 16) for i in (0, 2, 4))


def _font_file() -> Path:
    for path in _HEAVY_FONTS:
        if path.exists():
            return path
    return font_path(get_config())


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    ratio = max(size[0] / image.width, size[1] / image.height)
    resized = image.resize((round(image.width * ratio), round(image.height * ratio)), Image.LANCZOS)
    left, top = (resized.width - size[0]) // 2, (resized.height - size[1]) // 3  # keep heads in frame
    return resized.crop((left, top, left + size[0], top + size[1]))


def _shade(canvas: Image.Image) -> Image.Image:
    """Darken toward the bottom-left, where the text sits."""
    w, h = canvas.size
    down = np.clip((np.arange(h) / h - 0.25) / 0.75, 0, 1)[:, None]
    left = np.maximum(0.35, 1 - np.arange(w) / w)[None, :]
    mask = Image.fromarray((200 * down * left).astype(np.uint8), "L")
    return Image.composite(Image.new("RGB", (w, h), _BLACK), canvas, mask)


def layout(words: list[str], font_file: Path, max_width: int) -> tuple[list[list[str]], ImageFont.FreeTypeFont]:
    """Split the hook into 1–2 lines and pick the largest font size that fits."""
    options = [[words]] + [[words[:k], words[k:]] for k in range(1, len(words))]
    for size in range(170, 59, -6):
        font = ImageFont.truetype(str(font_file), size)
        stroke = max(4, size // 14)
        for lines in sorted(options, key=lambda ls: (len(ls), abs(len(ls[0]) - len(ls[-1])))):
            if all(font.getlength(" ".join(line)) + 2 * stroke <= max_width for line in lines):
                return lines, font
    return [words[:2], words[2:]] if len(words) > 2 else [words], ImageFont.truetype(str(font_file), 60)


def render_thumbnail(text: str, highlight: str, subject_path: Path | None) -> Image.Image:
    """The pure compositing step — testable without a DB."""
    accent = _rgb(get_config().subtitles.highlight_color)
    canvas = Image.new("RGB", THUMBNAIL_SIZE, (30, 30, 30))
    if subject_path is not None:
        try:
            with Image.open(subject_path) as im:
                canvas = _cover(im.convert("RGB"), THUMBNAIL_SIZE)
        except OSError:
            pass  # keep the plain background if the image can't be decoded
    canvas = _shade(canvas)

    draw = ImageDraw.Draw(canvas)
    words = text.split()
    if words:
        lines, font = layout(words, _font_file(), int(THUMBNAIL_SIZE[0] * _MAX_TEXT_WIDTH))
        stroke = max(4, font.size // 14)
        line_h = int(font.size * 1.08)
        y = THUMBNAIL_SIZE[1] - _MARGIN - line_h * len(lines)
        for line in lines:
            x = _MARGIN
            for word in line:
                color = accent if word == highlight else _WHITE
                draw.text((x + 6, y + 8), word, font=font, fill=_BLACK)  # drop shadow
                draw.text((x, y), word, font=font, fill=color, stroke_width=stroke, stroke_fill=_BLACK)
                x += font.getlength(word + " ")
            y += line_h
    draw.rectangle([(0, 0), (THUMBNAIL_SIZE[0] - 1, THUMBNAIL_SIZE[1] - 1)], outline=accent, width=_FRAME)
    return canvas


def _subject_path(session: Session, video: Video) -> Path | None:
    chosen = ((video.video_metadata or {}).get("thumbnail") or {}).get("asset_id")
    asset = session.get(Asset, uuid.UUID(chosen)) if chosen else None
    if asset is None:
        first = session.execute(select(Scene.image_asset_id).filter_by(video_id=video.id)
                                .where(Scene.image_asset_id.is_not(None)).order_by(Scene.idx)).scalars().first()
        asset = session.get(Asset, first) if first else None
    path = Path(asset.uri) if asset and asset.uri else None
    return path if path and path.exists() else None


def write_thumbnail(session: Session, video: Video) -> Path:
    """Called by the Shorts stage after the metadata step. Redoing it makes a new one."""
    hook = (video.video_metadata or {}).get("thumbnail") or {}
    canvas = render_thumbnail(hook.get("text", ""), hook.get("highlight", ""), _subject_path(session, video))
    out = output_dir(video.id) / "thumbnail.png"
    canvas.save(out)
    video.thumbnail_uri = str(out)
    return out
