"""Rank (README §4.1 step 4): start a batch of `discovery.batch_size` videos from the
best-illustrated passed topics — when no video is being built and the review queue isn't
full. No id: the orchestrator calls it once per run. "Never done before" is enforced by
`topic.title` being unique and a used topic leaving the `passed` pool.

Score (all parts 0–1, images weigh most):
- **images** ×2: the survey's count of usable free images, linear up to 60 (= 1.0) — the
  archives decide what can be shown, so they decide what gets made (log-scaling let a
  famous topic with 16 images outrank one with 60);
- **interest** ×1: last month's Wikipedia pageviews, log-scaled (breaks ties between
  equally illustrated topics, so a batch isn't all obscure or all overdone);
- **relevance** ×0.5: the gate's 0–10 niche fit;
- **trend** ×0.5: trending rank or anniversary roundness from discovery.
Only topics the survey found at least `discovery.min_images` usable images for qualify.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import Topic, Video
from app.stages.survey import interest
from app.status import Status, TopicStatus

#: Videos being built (a batch is "in progress" until every video reaches review).
BUILDING = [s.value for s in Status if list(Status).index(s) < list(Status).index(Status.PACKAGED)]
#: Videos waiting on you: to review, or approved and not uploaded yet.
WAITING_ON_YOU = [Status.PACKAGED.value, Status.APPROVED.value]
_FULL_IMAGES = 60


def _number(value) -> float:
    return float(value) if isinstance(value, int | float) else 0.0


def score(topic: Topic) -> float:
    raw = topic.raw or {}
    images = min(1.0, _number(raw.get("images")) / _FULL_IMAGES)
    return (2 * images + interest(int(_number(raw.get("views"))))
            + 0.5 * _number(raw.get("relevance")) / 10 + 0.5 * _number(raw.get("trend")))


def run(session: Session) -> None:
    cfg = get_config().discovery
    building = session.execute(select(Video.id).where(Video.status.in_(BUILDING)).limit(1)).first()
    if building is not None:
        return
    waiting = session.execute(select(Video.id).where(Video.status.in_(WAITING_ON_YOU))).all()
    if len(waiting) >= cfg.max_waiting_review:
        return  # review and upload the ones done first

    ready = [t for t in session.execute(select(Topic).filter_by(status=TopicStatus.PASSED)).scalars()
             if _number((t.raw or {}).get("images")) >= cfg.min_images]
    for topic in sorted(ready, key=score, reverse=True)[: cfg.batch_size]:
        topic.score = score(topic)
        topic.status = TopicStatus.USED
        session.add(Video(topic_id=topic.id, title=topic.title, status=Status.SELECTED))
