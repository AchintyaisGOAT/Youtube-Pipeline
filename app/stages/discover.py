"""Discover (README §4.1 step 1): new `topic` rows from three Wikipedia sources.

- **catalog**: subjects linked from Wikipedia's history and true-crime list pages
  (`discovery.catalog_pages`: Vital articles/History, unsolved murders, serial killers
  before 1900, assassinations, heists, hoaxes…). Hundreds of story-rich, mostly older
  subjects — the ones the public-domain archives actually have pictures of. A sample is
  drawn each time (seeded by the date, so a rerun the same day draws the same sample).
- **trending**: yesterday's most-viewed English Wikipedia articles (Wikimedia Pageviews).
  Proven audience interest (scored on a log scale of pageview rank), but mostly
  off-niche (celebrities, sports, new films).
- **on_this_day**: Wikipedia's "On this day" events for today and the next two days.
  Always history, with an anniversary hook; round anniversaries score higher.

The survey stage then counts each passed topic's free images, and rank builds a batch
from the best-illustrated ones (README §4.1).

Every candidate goes through a free pre-filter on its Wikipedia short description before
any LLM sees it: meta pages, living people ("born 1983"), anything dated 2000 or later
(the niche is pre-2000), and entertainment/sport pages are dropped here. The gate still
makes the real decision on what's left.

Titles are resolved to the canonical article title (redirects followed), so the same
article found by both sources — or again next week — is never a second topic.

Takes no id: the orchestrator calls it once per run, and it only fetches when no topic
is waiting to be gated or picked (or when forced with `ogh run --discover`). Passed
topics unused for `discovery.max_topic_age_days` expire first, so the pool stays fresh.
"""

from __future__ import annotations

import math
import random
import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import wikipedia
from app.config import get_config
from app.db import Topic
from app.http import get_http_client, http_retry
from app.status import TopicStatus

PAGEVIEWS_URL = (
    "https://wikimedia.org/api/rest_v1/metrics/pageviews/top/en.wikipedia.org/all-access/"
    "{day:%Y/%m/%d}"
)
ON_THIS_DAY_URL = "https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{day:%m}/{day:%d}"

#: How many of the top-viewed articles to look through for enough usable candidates.
_TRENDING_SCAN = 200
_ON_THIS_DAY_DAYS = 3  # today + the next two days: a video takes a while to reach upload

_META_PREFIXES = ("Special:", "Main Page", "Wikipedia:", "Portal:", "File:", "Talk:",
                  "Category:", "Template:", "Help:", "List of", "Lists of", "Deaths in", "-")
_YEAR = re.compile(r"\b(1\d{3}|20\d{2})\b")
_LIVING = re.compile(r"\(born\b|\bborn \d{4}", re.IGNORECASE)
_OFF_NICHE = re.compile(
    r"\b(film|films|television|tv series|sitcom|miniseries|video game|game|album|song|single|"
    r"band|rapper|singer|actor|actress|footballer|football club|basketball|baseball|cricketer|"
    r"tennis|golfer|wrestler|esports|youtuber|streamer|influencer|season|reality)\b",
    re.IGNORECASE,
)
#: List pages link places, periods and concepts too: a subject needs a story, not a map.
_GENERIC = re.compile(
    r"\b(country|sovereign state|city|town|village|county|state of|u\.s\. state|province|region|"
    r"capital|continent|river|island|disease|calendar year|day of the year|decade|century|month|"
    r"language|ethnic group|religion|surname|given name|identifier|political party|academic discipline|"
    r"field of study|branch of|concept|term|type of|form of|kind of|profession|occupation|genre|planet|"
    r"website|organization|agency|military rank)\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# pre-filter
# --------------------------------------------------------------------------- #
def prefilter_reason(title: str, description: str) -> str | None:
    """Why a candidate is dropped before the gate, or None to keep it."""
    if title.startswith(_META_PREFIXES) or "disambiguation" in description.lower():
        return "meta page"
    if _LIVING.search(description):
        return "living person"
    years = [int(y) for y in _YEAR.findall(f"{title} {description}")]
    if years and max(years) >= 2000:
        return "dated 2000 or later"
    if _OFF_NICHE.search(description):
        return "entertainment/sport"
    return None


def trend_score(rank: int) -> float:
    """Pageview rank on a log scale, so it's comparable with anniversary_score:
    rank 1 -> 1.0, 10 -> 0.67, 100 -> 0.33, 1000 -> 0."""
    return max(0.0, 1.0 - math.log10(rank) / 3) if rank > 0 else 0.0


def anniversary_score(years_ago: int) -> float:
    """Round anniversaries make better hooks: 100th > 50th > 25th > 10th > any other."""
    for step, score in ((100, 1.0), (50, 0.7), (25, 0.5), (10, 0.3)):
        if years_ago > 0 and years_ago % step == 0:
            return score
    return 0.1


# --------------------------------------------------------------------------- #
# Wikipedia/Wikimedia calls
# --------------------------------------------------------------------------- #
@http_retry
def _fetch_top_articles(day: date) -> list[dict]:
    resp = get_http_client().get(PAGEVIEWS_URL.format(day=day))
    resp.raise_for_status()
    return resp.json()["items"][0]["articles"]


@http_retry
def _fetch_on_this_day(day: date) -> list[dict]:
    resp = get_http_client().get(ON_THIS_DAY_URL.format(day=day))
    resp.raise_for_status()
    return resp.json().get("events", [])


# --------------------------------------------------------------------------- #
# sources
# --------------------------------------------------------------------------- #
def _trending(limit: int, known: set[str]) -> list[Topic]:
    articles = _fetch_top_articles(date.today() - timedelta(days=1))[:_TRENDING_SCAN]
    topics: list[Topic] = []
    for start in range(0, len(articles), wikipedia.BATCH):
        chunk = articles[start : start + wikipedia.BATCH]
        titles = [a["article"].replace("_", " ") for a in chunk]
        pages = wikipedia.describe_pages([t for t in titles if not t.startswith(_META_PREFIXES)])
        for article, requested in zip(chunk, titles, strict=True):
            page = pages.get(requested)
            if page is None or page["title"] in known:
                continue
            if prefilter_reason(page["title"], page["description"]):
                continue
            rank = article.get("rank") or 0
            topics.append(
                Topic(
                    source="trending",
                    title=page["title"],
                    summary=page["extract"] or None,
                    raw={"description": page["description"], "rank": rank, "views": article.get("views"),
                         "trend": trend_score(rank)},
                )
            )
            known.add(page["title"])
            if len(topics) >= limit:
                return topics
    return topics


def _on_this_day(limit: int, known: set[str], today: date) -> list[Topic]:
    options: list[Topic] = []
    for offset in range(_ON_THIS_DAY_DAYS):
        day = today + timedelta(days=offset)
        for event in _fetch_on_this_day(day):
            year, pages = event.get("year"), event.get("pages") or []
            if not isinstance(year, int) or year >= 2000 or not pages:
                continue
            page = pages[0]  # the event's own article; later pages are places/people it touched
            title = (page.get("titles") or {}).get("normalized") or page.get("title", "").replace("_", " ")
            description = page.get("description") or ""
            if not title or title in known or prefilter_reason(title, description):
                continue
            years_ago = today.year - year
            options.append(
                Topic(
                    source="on_this_day",
                    title=title,
                    summary=page.get("extract") or event.get("text"),
                    raw={"description": description, "event": event.get("text"), "year": year,
                         "date": f"{day:%m-%d}", "years_ago": years_ago,
                         "trend": anniversary_score(years_ago)},
                )
            )
            known.add(title)
    options.sort(key=lambda t: t.raw["trend"], reverse=True)
    return options[:limit]


# --------------------------------------------------------------------------- #
# stage
# --------------------------------------------------------------------------- #
def _catalog(limit: int, known: set[str], pages: list[str], today: date) -> list[Topic]:
    """A date-seeded sample of the subjects the catalog pages link to."""
    links = sorted({t for page in pages for t in wikipedia.all_links(page)} - known)
    random.Random(today.toordinal()).shuffle(links)
    topics: list[Topic] = []
    for start in range(0, len(links), wikipedia.BATCH):
        chunk = [t for t in links[start : start + wikipedia.BATCH] if not t.startswith(_META_PREFIXES)]
        for page in wikipedia.describe_pages(chunk).values():
            if page["title"] in known or re.fullmatch(r"\d{1,4}( BC)?", page["title"]):
                continue
            if prefilter_reason(page["title"], page["description"]) or _GENERIC.search(page["description"]):
                continue
            topics.append(Topic(source="catalog", title=page["title"], summary=page["extract"] or None,
                                raw={"description": page["description"], "trend": 0.0}))
            known.add(page["title"])
            if len(topics) >= limit:
                return topics
    return topics


def _channel_today(timezone: str) -> date:
    return datetime.now(ZoneInfo(timezone)).date()


def _expire_stale_topics(session: Session, max_age_days: int) -> None:
    """Passed-but-unused topics older than `max_age_days` are retired (trending interest
    fades, anniversaries pass), so the pool empties and discovery refreshes it."""
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    stale = session.execute(
        select(Topic).where(Topic.status == TopicStatus.PASSED, Topic.created_at < cutoff)
    ).scalars()
    for topic in stale:
        topic.status = TopicStatus.VETOED
        topic.rationale = f"expired: unused for {max_age_days} days"


def _topics_waiting(session: Session) -> bool:
    """Passed topics not yet used, or candidates the gate hasn't judged yet."""
    waiting = (TopicStatus.PASSED, TopicStatus.CANDIDATE)
    return session.execute(select(Topic.id).where(Topic.status.in_(waiting)).limit(1)).first() is not None


def run(session: Session, *, force: bool = False) -> None:
    config = get_config()
    _expire_stale_topics(session, config.discovery.max_topic_age_days)
    session.flush()
    if not force and _topics_waiting(session):
        return

    sources = config.discovery.sources
    per_source = max(1, config.discovery.max_candidates // len(sources))
    known = set(session.execute(select(Topic.title)).scalars())

    found: list[Topic] = []
    if "catalog" in sources:
        found += _catalog(config.discovery.catalog_candidates, known, config.discovery.catalog_pages,
                          _channel_today(config.timezone))
    if "on_this_day" in sources:
        found += _on_this_day(per_source, known, _channel_today(config.timezone))
    if "trending" in sources:
        found += _trending(per_source, known)
    for topic in found:
        topic.status = TopicStatus.CANDIDATE
        session.add(topic)
