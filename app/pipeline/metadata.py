"""Gemini: 3-5 title candidates, a description with chapters + sources + an
attribution block (DESIGN.md #38 metadata quality, #45 legal cushion), and tags ->
`video.title` + `video.video_metadata`.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import llm
from app.config import get_channel_config
from app.db import Asset, Segment, Video
from app.status import Status

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "metadata.md"


def _sources_block(session: Session, video_id: uuid.UUID) -> str:
    asset_ids = set(
        session.execute(
            select(Segment.image_asset_id).filter_by(video_id=video_id).where(Segment.image_asset_id.is_not(None))
        ).scalars()
    )
    if not asset_ids:
        return ""
    assets = session.execute(select(Asset).filter(Asset.id.in_(asset_ids))).scalars().all()
    lines = [f"- {a.attribution or a.source_id} ({a.source}, {a.license}) — {a.rights_url}" for a in assets]
    return "Image sources:\n" + "\n".join(lines)


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.GENERATING_METADATA:
        return

    config = get_channel_config(session, video.channel_id)
    sources_block = _sources_block(session, video_id)
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        topic=video.title,
        script=video.script or "",
        category=config.publish.category,
        sources_block=sources_block,
    )

    result = llm.generate(session, prompt, inputs={"video_id": str(video_id)})
    titles = result.get("titles", [])
    if not titles:
        raise ValueError(f"video {video_id}: metadata generation returned no title candidates")

    description = result.get("description", "")
    if sources_block and sources_block not in description:
        description = f"{description}\n\n{sources_block}"

    video.title = titles[0]
    video.video_metadata = {
        "title_candidates": titles,
        "description": description,
        "tags": result.get("tags", []),
    }
    video.status = Status.GENERATING_THUMBNAIL
