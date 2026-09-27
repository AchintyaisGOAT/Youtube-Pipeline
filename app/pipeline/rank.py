"""Pick the best `candidate_approved` row and promote it to a `video` row
(DESIGN.md #26/#28). No id parameter — the orchestrator calls it once per run; it scans
every approved candidate and promotes at most one per call, and none once the channel
has already started `discovery.target_long_form_per_month` videos in the last 30 days.

A candidate stays `candidate_approved` forever (that status isn't in `Status.TERMINAL`
and Content must not invent a new enum member without asking Foundation — WORK_*.md §4),
so "already promoted" is tracked the other way: a candidate with an existing `video` row
is excluded from consideration, not re-scored.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_channel_config
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

    config = get_channel_config(session, best.channel_id)
    started_last_30_days = session.execute(
        select(func.count(Video.id)).where(
            Video.channel_id == best.channel_id,
            Video.created_at >= datetime.now(UTC) - timedelta(days=30),
        )
    ).scalar_one()
    if started_last_30_days >= config.discovery.target_long_form_per_month:
        return
    best.score = _score(best, topic_scores)

    session.add(
        Video(
            channel_id=best.channel_id,
            candidate_id=best.id,
            title=best.title,
            status=Status.RESEARCHING,
        )
    )
