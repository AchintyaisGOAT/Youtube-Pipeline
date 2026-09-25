"""Gemini + Search grounding: research a video's topic into sourced claims.

Writes ``video.research`` as ``{"claims": [{"text": str, "sources": [str, ...]}, ...]}``.
factcheck.py (next stage) verifies each claim against its cited sources.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import llm
from app.config import get_channel_config
from app.db import Video
from app.status import Status

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "research.md"


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.RESEARCHING:
        return

    config = get_channel_config(session, video.channel_id)
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        topic=video.title,
        audience=config.channel.audience,
        tone=config.channel.tone,
        in_scope=", ".join(config.topics.in_scope),
    )

    result = llm.generate(session, prompt, inputs={"video_id": str(video_id)}, grounding=True)
    if not isinstance(result, dict) or not result.get("claims"):
        raise ValueError(f"video {video_id}: research returned no claims")

    video.research = result
    video.status = Status.FACT_CHECKING
