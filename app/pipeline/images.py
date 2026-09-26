"""For each segment missing an image, generate a cartoon-style illustration with
Gemini's image model, styled by the channel's configured `art_style`.

Replaces the original archival-photo-search design (Wikimedia Commons / Library of
Congress): narrated stills-over-real-photos read as a slow documentary, not
entertainment, per direct product feedback on a real rendered video. An illustrated,
purpose-drawn scene per segment is a different medium, not a tuning knob.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import llm
from app.config import get_channel_config
from app.db import Asset, Segment, Video
from app.status import Status
from app.storage import atomic_write_bytes, cache_dir


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24]


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.FETCHING_IMAGES:
        return

    config = get_channel_config(session, video.channel_id)
    segments = session.execute(select(Segment).filter_by(video_id=video_id).order_by(Segment.idx)).scalars()

    for segment in segments:
        if segment.image_asset_id is not None or not segment.text:
            continue

        prompt = f"{config.images.art_style}\n\nIllustrate this moment: {segment.text}"
        source_id = _prompt_hash(prompt)

        asset = session.execute(
            select(Asset).filter_by(source="gemini", source_id=source_id)
        ).scalar_one_or_none()
        if asset is None:
            image_bytes, mime_type = llm.generate_image(session, prompt)
            ext = "jpg" if "jpeg" in mime_type else "png"
            local_path = cache_dir() / "illustrations" / f"{source_id}.{ext}"
            local_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(local_path, image_bytes)
            asset = Asset(
                kind="image",
                source="gemini",
                source_id=source_id,
                license="ai-generated",
                rights_url=None,
                uri=str(local_path),
                attribution="AI-generated illustration",
            )
            session.add(asset)
            session.flush()  # need asset.id before pointing the segment at it

        segment.image_asset_id = asset.id

    video.status = Status.SYNTHESIZING_VOICE
