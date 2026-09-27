"""Discover (README §4.1 step 1): Wikimedia Pageviews top articles -> new `topic` rows.

Takes no id: the orchestrator calls it once per run, and it only fetches when no passed
topic is waiting to become a video (or when forced). The "On this day" source and the
history pre-filter arrive in S3.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import Topic
from app.http import get_http_client, http_retry
from app.status import TopicStatus

PAGEVIEWS_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia.org/all-access/"
    "{date:%Y/%m/%d}"
)

#: Wikipedia namespace/meta pages that show up in "top" lists but aren't real topics.
_SKIP_PREFIXES = ("Special:", "Main_Page", "Wikipedia:", "Portal:", "File:", "Talk:", "-")


@http_retry
def _fetch_top_articles(day: date) -> list[dict]:
    client = get_http_client()
    resp = client.get(PAGEVIEWS_URL.format(date=day))
    resp.raise_for_status()
    return resp.json()["items"][0]["articles"]


def _topics_waiting(session: Session) -> bool:
    """Passed topics not yet used, or candidates the gate hasn't judged yet."""
    waiting = (TopicStatus.PASSED, TopicStatus.CANDIDATE)
    return session.execute(select(Topic.id).where(Topic.status.in_(waiting)).limit(1)).first() is not None


def run(session: Session, *, force: bool = False) -> None:
    if not force and _topics_waiting(session):
        return

    config = get_config()
    articles = _fetch_top_articles(date.today() - timedelta(days=1))
    known_titles = set(session.execute(select(Topic.title)).scalars())

    created = 0
    for article in articles:
        if created >= config.discovery.max_candidates:
            break
        raw_title = article.get("article", "")
        title = raw_title.replace("_", " ").strip()
        if not title or raw_title.startswith(_SKIP_PREFIXES) or title in known_titles:
            continue

        session.add(Topic(source="trending", title=title, raw=article, status=TopicStatus.CANDIDATE))
        known_titles.add(title)
        created += 1
