"""Pick the best `candidate_approved` row and promote it to a `video` row
(DESIGN.md #26/#28). No id parameter — scans every approved candidate across every
channel and promotes at most one per call.

A candidate stays `candidate_approved` forever (that status isn't in `Status.TERMINAL`
and Content must not invent a new enum member without asking Foundation — WORK_*.md §4),
so "already promoted" is tracked the other way: a candidate with an existing `video` row
is excluded from consideration, not re-scored.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Candidate, TopicPerformance, Video
from app.status import Status


def _score(candidate: Candidate, topic_scores: dict[str, float]) -> float:
    rank = (candidate.raw or {}).get("rank")
    base = 1.0 / rank if isinstance(rank, int | float) and rank > 0 else 0.5
    return base + topic_scores.get(candidate.title, 0.0) * 0.1


def run(session: Session) -> None:
    approved = session.execute(select(Candidate).filter_by(status=Status.CANDIDATE_APPROVED)).scalars().all()
    if not approved:
        return

    already_promoted = set(
        session.execute(select(Video.candidate_id).where(Video.candidate_id.is_not(None))).scalars()
    )
    approved = [c for c in approved if c.id not in already_promoted]
    if not approved:
        return

    topic_scores = {
        row.topic_key: row.score or 0.0 for row in session.execute(select(TopicPerformance)).scalars()
    }

    best = max(approved, key=lambda c: _score(c, topic_scores))
    best.score = _score(best, topic_scores)

    session.add(
        Video(
            channel_id=best.channel_id,
            candidate_id=best.id,
            title=best.title,
            status=Status.RESEARCHING,
        )
    )
