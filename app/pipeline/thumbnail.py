"""Pillow thumbnail template: a subject cut-out (the video's first available segment
image) + a 3-4 word headline + a brand frame -> video.thumbnail_uri. One template
(WORK_MEDIA.md §2) — a second template / A-B variant is DESIGN.md #39, not built here.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import Asset, Segment, Video
from app.status import Status
from app.storage import output_dir

THUMBNAIL_SIZE = (1280, 720)
_BRAND_FRAME_COLOR = (20, 20, 20)
_BRAND_FRAME_WIDTH = 24
_TEXT_COLOR = (255, 255, 255)
_TEXT_STROKE_COLOR = (0, 0, 0)


def _headline(title: str, max_words: int = 4) -> str:
    return " ".join((title or "").split()[:max_words]).upper()


def _subject_image_path(session: Session, video_id: uuid.UUID) -> Path | None:
    asset_id = (
        session.execute(
            select(Segment.image_asset_id)
            .filter_by(video_id=video_id)
            .where(Segment.image_asset_id.is_not(None))
            .order_by(Segment.idx)
        )
        .scalars()
        .first()
    )
    if asset_id is None:
        return None
    asset = session.get(Asset, asset_id)
    if asset is None or not asset.uri:
        return None
    path = Path(asset.uri)
    return path if path.exists() else None


def _cover_resize(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    target_w, target_h = size
    ratio = max(target_w / image.width, target_h / image.height)
    resized = image.resize((round(image.width * ratio), round(image.height * ratio)))
    left = (resized.width - target_w) // 2
    top = (resized.height - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def _font(size: int) -> ImageFont.ImageFont | ImageFont.FreeTypeFont:
    candidates = sorted(Path(get_settings().assets_dir).glob("fonts/*.ttf"))
    if candidates:
        return ImageFont.truetype(str(candidates[0]), size)
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1
    except TypeError:
        return ImageFont.load_default()


def _draw_outlined_headline(draw: ImageDraw.ImageDraw, text: str, font, canvas_size: tuple[int, int]) -> None:
    if not text:
        return
    bbox = draw.textbbox((0, 0), text, font=font, stroke_width=6)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    x = (canvas_size[0] - text_w) // 2
    y = canvas_size[1] - text_h - 80
    draw.text((x, y), text, font=font, fill=_TEXT_COLOR, stroke_width=6, stroke_fill=_TEXT_STROKE_COLOR)


def render_thumbnail(title: str, subject_path: Path | None, font_size: int = 90) -> Image.Image:
    """The pure compositing step — split out from `run()` so it's testable without a DB."""
    canvas = Image.new("RGB", THUMBNAIL_SIZE, color=(30, 30, 30))
    if subject_path is not None:
        try:
            subject = _cover_resize(Image.open(subject_path).convert("RGB"), THUMBNAIL_SIZE)
            canvas.paste(subject, (0, 0))
        except OSError:
            pass  # keep the plain background if the image can't be decoded

    draw = ImageDraw.Draw(canvas)
    draw.rectangle(
        [(0, 0), (THUMBNAIL_SIZE[0] - 1, THUMBNAIL_SIZE[1] - 1)],
        outline=_BRAND_FRAME_COLOR,
        width=_BRAND_FRAME_WIDTH,
    )
    _draw_outlined_headline(draw, _headline(title), _font(font_size), THUMBNAIL_SIZE)
    return canvas


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.GENERATING_THUMBNAIL:
        return

    canvas = render_thumbnail(video.title or "", _subject_image_path(session, video_id))

    out_path = output_dir(video_id) / "thumbnail.jpg"
    canvas.save(out_path, quality=92)

    video.thumbnail_uri = str(out_path)
    video.status = Status.AWAITING_REVIEW
