"""Research (README §4.1 step 4): the topic's Wikipedia article + up to
`research.linked_articles` closely linked ones. No LLM — these articles are the video's
only source of facts, and their references become its source list.

Linked articles are the ones the main article mentions most (at least 3 times), minus
variants of the topic's own title, generic pages (places, dates, diseases…) and anything
discover's pre-filter would drop — judged by their short descriptions. A live probe
showed the lead section's own links are too few and too generic to rely on (Lizzie
Borden's: Pneumonia, New Hampshire, a cemetery). Fewer than the configured number is
fine; the main article alone is usually thousands of words.

Writes ``video.research`` = ``{"articles": [{"title", "url", "text"}, ...], "sources": [url, ...]}``.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import llm, wikipedia
from app.config import get_config
from app.db import Topic, Video
from app.quota import RetryLater
from app.stages.discover import prefilter_reason
from app.status import Status

#: Section headings after which a Wikipedia article is only apparatus, not story.
_TAIL_SECTIONS = re.compile(
    r"^==+ ?(See also|Notes|References|Works cited|Citations|Sources|Bibliography|"
    r"Further reading|External links|Footnotes) ?==+\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_MAIN_MAX_CHARS = 60_000  # ~15K tokens: plenty for a 3–8 minute script
_LINKED_MAX_CHARS = 8_000  # lead + first sections of each linked article
_MAX_SOURCES = 40
PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "research.md"
_LINK_CANDIDATES = 30  # most-mentioned links whose descriptions get checked
_GENERIC = re.compile(
    r"\b(country|sovereign state|city|town|village|county|state of|u\.s\. state|province|region|"
    r"capital|continent|river|island|cemetery|disease|calendar year|day of the year|decade|"
    r"century|month|language|ethnic group|religion|disambiguation|surname|given name)\b",
    re.IGNORECASE,
)


def _trim(text: str, max_chars: int) -> str:
    match = _TAIL_SECTIONS.search(text)
    if match:
        text = text[: match.start()]
    return text.strip()[:max_chars]


def _base(title: str) -> str:
    """Title without a trailing qualifier: 'Oak Grove Cemetery (Fall River)' -> 'Oak Grove Cemetery'."""
    return re.sub(r"\s*\(.*\)$", "", title)


def _mentions(text: str, title: str) -> int:
    """Mentions of the link's name in the text; for multi-word names also the last word
    ("Aguinaldo" for "Emilio Aguinaldo"), since articles mostly use surnames."""
    name = _base(title)
    names = {name}
    last = name.split()[-1] if " " in name else ""
    if len(last) >= 5 and last[0].isupper():
        names.add(last)
    return max(len(re.findall(rf"\b{re.escape(n)}\b", text)) for n in names) if len(name) > 3 else 0


def rank_links(main_title: str, main_text: str, links: list[str]) -> list[str]:
    """Links mentioned at least twice in the main text, most-mentioned first — minus
    variants of the topic's own title ("Lizzie (2018 film)" for "Lizzie Borden")."""
    own = _base(main_title).lower()
    counts = {}
    for title in links:
        base = _base(title).lower()
        if base in own or own in base:
            continue
        mentions = _mentions(main_text, title)
        if mentions >= 2:
            counts[title] = mentions
    return sorted(counts, key=counts.get, reverse=True)[:_LINK_CANDIDATES]


def shortlist(ranked: list[str], descriptions: dict[str, dict]) -> list[str]:
    """Ranked links that aren't generic pages (places, dates, …) or pages discover's
    pre-filter would drop (films, living people, anything dated 2000+)."""
    keep = []
    for title in ranked:
        description = (descriptions.get(title) or {}).get("description", "")
        if _GENERIC.search(description) or re.fullmatch(r"\d{1,4}( BC)?", title):
            continue
        if prefilter_reason(title, description):
            continue
        keep.append(title)
    return keep


def _pick_linked(session: Session, video_id: uuid.UUID, main: dict, candidates: list[str],
                 descriptions: dict[str, dict], n: int) -> list[str]:
    """The worker LLM picks the up-to-`n` candidates that add the most story; if that
    call can't be made, the most-mentioned candidates are used instead."""
    if n == 0 or not candidates:
        return []
    lines = [
        f"{i}. {t} — {(descriptions.get(t) or {}).get('description') or 'no description'}"
        f" — {_mentions(main['text'], t)} mentions"
        for i, t in enumerate(candidates, start=1)
    ]
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        title=main["title"], summary=main["text"][:600], n=n, candidates="\n".join(lines)
    )
    try:
        result = llm.generate(session, prompt, role="worker", step="research",
                              inputs={"video_id": str(video_id)})
    except RetryLater:
        raise
    except Exception:
        return candidates[:n]
    picks = []
    for number in result.get("picks", []):
        try:
            index = int(number) - 1
        except (TypeError, ValueError):
            continue
        if 0 <= index < len(candidates) and candidates[index] not in picks:
            picks.append(candidates[index])
    return picks[:n]


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.SELECTED:
        return

    topic = session.get(Topic, video.topic_id) if video.topic_id else None
    main = wikipedia.article(topic.title if topic else video.title)
    if main is None:
        raise ValueError(f"video {video_id}: no Wikipedia article found for {video.title!r}")
    main["text"] = _trim(main["text"], _MAIN_MAX_CHARS)

    links, sources = wikipedia.links_and_sources(main["title"])
    ranked = rank_links(main["title"], main["text"], links)
    descriptions = {}
    for i in range(0, len(ranked), wikipedia.BATCH):
        descriptions |= wikipedia.describe_pages(ranked[i : i + wikipedia.BATCH])
    linked_titles = _pick_linked(session, video_id, main, shortlist(ranked, descriptions), descriptions,
                                 get_config().research.linked_articles)

    articles = [main]
    for title in linked_titles:
        linked = wikipedia.article(title)
        if linked is not None:
            linked["text"] = _trim(linked["text"], _LINKED_MAX_CHARS)
            articles.append(linked)

    video.title = main["title"]
    video.research = {"articles": articles, "sources": list(dict.fromkeys(sources))[:_MAX_SOURCES]}
    video.status = Status.RESEARCHED
