"""Gemini verifies each claim in `video.research` against its cited sources with fresh
Search grounding; unsourced/unsupported claims are dropped (DESIGN.md #3).

The model returns verdicts keyed by claim index rather than a parallel list, so a
malformed or reordered response can't silently mismatch a verdict to the wrong claim.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import llm
from app.db import Video
from app.status import Status

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "factcheck.md"


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.FACT_CHECKING:
        return

    claims = (video.research or {}).get("claims", [])
    if not claims:
        raise ValueError(f"video {video_id}: no claims to fact-check")

    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        topic=video.title, claims_json=json.dumps(claims, indent=2)
    )
    result = llm.generate(session, prompt, inputs={"video_id": str(video_id)}, grounding=True)

    verdict_by_index = {v.get("index"): v.get("verdict") for v in result.get("verdicts", [])}
    verified = [claim for i, claim in enumerate(claims) if verdict_by_index.get(i) == "supported"]
    if not verified:
        raise ValueError(f"video {video_id}: no claims survived fact-checking")

    video.research = {**(video.research or {}), "claims": verified}
    video.status = Status.SCRIPTING
