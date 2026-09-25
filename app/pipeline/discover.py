"""Wikimedia Pageviews API -> new `candidate` rows (DESIGN.md #26 — this is the
*primary* discovery signal; pytrends and everything else was dropped, see
WORK_CONTENT.md §2).

Takes no id: called once per run, creates rows for whichever channel exists.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Candidate, Channel
from app.http import get_http_client, http_retry
from app.status import Status

PAGEVIEWS_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia.org/all-access/"
    "{date:%Y/%m/%d}"
)

#: Wikipedia namespace/meta pages that show up in "top" lists but aren't real topics.
_SKIP_PREFIXES = ("Special:", "Main_Page", "Wikipedia:", "Portal:", "File:", "Talk:", "-")

#: Cap per run — this is a weekly discovery signal, not a firehose (DESIGN.md §3.1).
_MAX_CANDIDATES_PER_RUN = 20


@http_retry
def _fetch_top_articles(day: date) -> list[dict]:
    client = get_http_client()
    resp = client.get(PAGEVIEWS_URL.format(date=day))
    resp.raise_for_status()
    return resp.json()["items"][0]["articles"]


def run(session: Session) -> None:
    """Pull yesterday's most-viewed English Wikipedia articles into `candidate` rows."""
    channel = session.execute(select(Channel).order_by(Channel.created_at)).scalars().first()
    if channel is None:
        raise RuntimeError("no channel configured — run scripts/load_config.py first")

    yesterday = date.today() - timedelta(days=1)
    articles = _fetch_top_articles(yesterday)

    existing_titles = set(
        session.execute(
            select(Candidate.title).filter_by(channel_id=channel.id, source="wikimedia_pageviews")
        ).scalars()
    )

    created = 0
    for article in articles:
        if created >= _MAX_CANDIDATES_PER_RUN:
            break
        raw_title = article.get("article", "")
        title = raw_title.replace("_", " ").strip()
        if not title or raw_title.startswith(_SKIP_PREFIXES) or title in existing_titles:
            continue

        session.add(
            Candidate(
                channel_id=channel.id,
                source="wikimedia_pageviews",
                title=title,
                raw=article,
                status=Status.CANDIDATE_NEW,
            )
        )
        existing_titles.add(title)
        created += 1
