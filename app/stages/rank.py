"""Rank (README §4.1 step 3): turn the best `passed` topic into a new video — but only
when no other video is in progress (one at a time). No id: the orchestrator calls it once
per run. "Never done before" is enforced by `topic.title` being unique and a used topic
leaving the `passed` pool.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Topic, Video
from app.status import IN_PROGRESS, Status, TopicStatus


def score(topic: Topic) -> float:
    """Trend strength (pageview rank 1 = strongest) + the gate's 0–10 niche relevance."""
    raw = topic.raw or {}
    rank = raw.get("rank")
    trend = 1.0 / rank if isinstance(rank, int | float) and rank > 0 else 0.0
    relevance = raw.get("relevance", 0)
    return trend + (relevance / 10 if isinstance(relevance, int | float) else 0.0)


def run(session: Session) -> None:
    busy = session.execute(
        select(Video.id).where(Video.status.in_([s.value for s in IN_PROGRESS])).limit(1)
    ).first()
    if busy is not None:
        return

    passed = session.execute(select(Topic).filter_by(status=TopicStatus.PASSED)).scalars().all()
    if not passed:
        return

    best = max(passed, key=score)
    best.score = score(best)
    best.status = TopicStatus.USED
    session.add(Video(topic_id=best.id, title=best.title, status=Status.SELECTED))
