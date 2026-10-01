"""Survey (README §4.1 step 3): how well can each passed topic be illustrated for free?
Rank builds its batch from the answer, so topics are chosen by what the archives hold.

Two passes, so a discovery of dozens of topics stays quick and free:
1. **Quick count**, every passed topic not yet counted (2 requests each): the article's own
   public-domain images and the Wikimedia Commons hits for its title. Only hopeless topics
   go here — under `discovery.quick_min_images` — because a specific story's title matches
   few files while its people and places match many.
2. **Full survey**, when fewer than `discovery.batch_size` topics are ready, of the
   `discovery.survey_top` most promising counted topics: every
   archive is searched for the topic and its most-mentioned linked articles (its people and
   places, as research will pick them), and the image check (app/stages/picture.py `vet`) keeps what shows
   something specific from the story. The kept images are saved on the topic
   (`raw["inventory"]`), so the picture stage starts from them; fewer than
   `discovery.min_images` and the topic is vetoed. Also fetched: last month's Wikipedia
   pageviews (`raw["views"]`), the audience-interest part of rank's score.

Takes no id: the orchestrator calls it once per run, after the gate.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from urllib.parse import quote

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import archives, wikipedia
from app.config import get_config
from app.db import Topic
from app.http import get_http_client, http_retry
from app.stages import picture, research
from app.status import TopicStatus

PAGEVIEWS_URL = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/"
                 "{title}/monthly/{start}/{end}")


@http_retry
def _commons_hits(title: str) -> int:
    resp = get_http_client().get(archives.COMMONS_API, params={
        "action": "query", "format": "json", "formatversion": 2, "list": "search", "srnamespace": 6,
        "srsearch": f'"{title}" filetype:bitmap', "srlimit": 1, "srinfo": "totalhits"})
    resp.raise_for_status()
    return int(resp.json().get("query", {}).get("searchinfo", {}).get("totalhits", 0))


def quick_count(title: str) -> int:
    """A cheap estimate of the free images: the article's own + Commons hits (capped: a
    common word can match thousands of unrelated files)."""
    try:
        own = len([c for c in archives.article_images(title) if picture.usable(c)])
    except Exception:
        own = 0
    try:
        hits = _commons_hits(title)
    except Exception:
        hits = 0
    return own + min(hits, 60)


@http_retry
def _monthly_views(title: str) -> int:
    today = datetime.now(UTC)
    last = today.replace(day=1)
    start = (last.replace(year=last.year - 1, month=12) if last.month == 1 else last.replace(month=last.month - 1))
    resp = get_http_client().get(PAGEVIEWS_URL.format(title=quote(title.replace(" ", "_"), safe=""),
                                                      start=start.strftime("%Y%m01"), end=last.strftime("%Y%m01")))
    if resp.status_code == 404:  # no recorded views
        return 0
    resp.raise_for_status()
    return sum(item.get("views", 0) for item in resp.json().get("items", []))


def linked_titles(title: str, n: int) -> list[str]:
    """The topic's `n` most-mentioned, non-generic linked articles (research's own ranking),
    so the survey counts the images a video will actually gather."""
    main = wikipedia.article(title)
    if main is None or n == 0:
        return []
    links, _ = wikipedia.links_and_sources(main["title"])
    ranked = research.rank_links(main["title"], main["text"], links)
    descriptions: dict = {}
    for i in range(0, min(len(ranked), 40), wikipedia.BATCH):
        descriptions |= wikipedia.describe_pages(ranked[i : i + wikipedia.BATCH])
    return research.shortlist(ranked, descriptions)[:n]


def interest(views: int) -> float:
    """Monthly pageviews on a log scale: 1,000 -> 0.5, 1,000,000 -> 1.0."""
    return min(1.0, math.log10(views + 1) / 6) if views > 0 else 0.0


def run(session: Session) -> None:
    cfg = get_config()
    passed = list(session.execute(select(Topic).filter_by(status=TopicStatus.PASSED)).scalars())

    for topic in passed:  # pass 1
        raw = dict(topic.raw or {})
        if "quick_images" in raw:
            continue
        raw["quick_images"] = quick_count(topic.title)
        topic.raw = raw
        if raw["quick_images"] < cfg.discovery.quick_min_images:
            topic.status = TopicStatus.VETOED
            topic.rationale = f"too few free images ({raw['quick_images']} found in a quick count)"

    ready = [t for t in passed if t.status == TopicStatus.PASSED and (t.raw or {}).get("images", 0)
             >= cfg.discovery.min_images]
    if len(ready) >= cfg.discovery.batch_size:
        return  # enough surveyed topics for the next batch: the expensive pass can wait
    waiting = [t for t in passed if t.status == TopicStatus.PASSED and "images" not in (t.raw or {})]
    waiting.sort(key=lambda t: t.raw.get("quick_images", 0), reverse=True)
    for topic in waiting[: cfg.discovery.survey_top]:  # pass 2
        try:
            linked = linked_titles(topic.title, cfg.research.linked_articles)
        except Exception as exc:
            logger.warning("no linked articles for {!r}: {}", topic.title, exc)
            linked = []
        found = picture.gather([topic.title, *linked], cfg.images.sources)
        kept = picture.vet(session, topic.title, found, key=f"topic:{topic.id}")
        raw = dict(topic.raw or {})
        raw["images"] = len(kept)
        raw["inventory"] = [picture.to_dict(c, shows) for c, shows in kept]
        try:
            raw["views"] = _monthly_views(topic.title)
        except Exception as exc:
            logger.warning("no pageviews for {!r}: {}", topic.title, exc)
            raw["views"] = 0
        topic.raw = raw
        if raw["images"] < cfg.discovery.min_images:
            topic.status = TopicStatus.VETOED
            topic.rationale = f"too few usable free images ({raw['images']} after checking)"
        logger.info("survey: {!r} has {} usable images, {} views last month", topic.title, raw["images"],
                    raw["views"])
