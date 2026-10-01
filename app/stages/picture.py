"""Picture (README §4.1 step 5, §6.1): the video's image inventory, gathered *before* the
script is written, so the script can be written around what the archives actually have.

1. **Gather** public-domain images (app/archives.py): the images the researched Wikipedia
   articles themselves show, plus one search of every archive per researched article.
   The survey stage has usually done the topic's own search already (`topic.raw
   ["inventory"]`), so only the linked articles are new. Results must be relevant to their
   query (a person's name whole), usable (no photographs of the dead, no logos/flags/icons)
   and at least 600 px.
2. **Check** (`vet`): one free worker-LLM call reads every image's title and description and
   keeps only images that show something specific from the story — a person in it, a place
   as it was, an object or document, the event itself — with a one-line "shows" for each.
   No period filler: a live run filled Lizzie Borden scenes with random 1890s dining rooms
   and an artist who shared a neighbour's name.
3. **Download** the kept images, measure their real size, and drop near-duplicates (the
   same scan uploaded twice), by a tiny perceptual hash.

Writes `video.research["images"]` = [{"n", "asset_id", "shows", "title", "source"}], numbered
from 1 in story order — the script marks each passage with `[IMG n]`.
"""

from __future__ import annotations

import hashlib
import io
import re
import uuid
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path

from loguru import logger
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import archives, llm
from app.archives import SOURCES, Candidate
from app.config import get_config
from app.db import Asset, Topic, Video
from app.http import get_http_client, http_retry
from app.quota import RetryLater
from app.status import Status
from app.storage import atomic_write_bytes, cache_dir

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "vet.md"
#: Images one check call reads, answering keep/drop for each (a "list only the keepers"
#: prompt kept 11 of 97 on-topic Nuremberg images, seen live) — inside Groq's 8K tokens/minute.
VET_BATCH = 50
#: Near-duplicates: perceptual hashes this close are the same picture.
_SAME_PICTURE = 6

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"the", "and", "for", "with", "from", "into", "his", "her", "their", "its", "was", "were",
              "file", "jpg", "jpeg", "png", "tif", "tiff", "photo", "photograph", "image", "picture",
              "that", "this", "they", "them", "then", "than", "when", "after", "before", "while", "had",
              "has", "have", "been", "who", "what", "which", "she", "him", "one", "two", "out", "over",
              "not", "but", "all", "just", "only", "even", "also", "would", "could", "said", "there",
              "list", "history", "of", "in", "on"}
#: Real photographs of dead bodies are a YouTube policy risk and wrong for the channel's
#: tone (a live run picked a crime-scene photo of a murder victim).
_GRAPHIC = re.compile(
    r"\b(corpse|corpses|dead bod(?:y|ies)|autopsy|post[- ]mortem|skull|skulls|severed|mutilat\w*|"
    r"lynch\w*|execution|executed|hanged|beheaded|gore|slain|murdered|victims?|crime scene|remains)\b",
    re.IGNORECASE,
)
#: Site furniture that Wikipedia articles also "show" (seen live: open-access logos, lock
#: icons, flags and seals on a biography).
_ICON = re.compile(r"\b(logo|icon|lock|flag|seal of|coat of arms|emblem|symbol|pictogram|vip|wikisource|"
                   r"wiktionary|commons-logo|stub|question book|edit-clear|ambox|disambig)\b", re.IGNORECASE)
#: A run of capitalised words: a person's or place's name ("John Wilkes Booth").
_NAME = re.compile(r"^(?:[A-Z][a-z'’-]+|[A-Z]\.)(?:\s+(?:[A-Z][a-z'’-]+|[A-Z]\.)){1,3}$")


def _normalize(text: str) -> str:
    return " ".join(_TOKEN.findall(text.lower().replace("_", " ")))


def tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if len(t) >= 3 and t not in _STOPWORDS}


def _text(c: Candidate) -> str:
    return f"{c.source_id} {c.attribution} {c.description}"


def relevant(candidate: Candidate, query: str) -> bool:
    """About the query: a name must appear whole ("John Wilkes Booth", not any Booth);
    otherwise at least two of the query's words (or its only one). Full-text search is
    loose — museum searches return quilts for "Fall River"."""
    query = re.sub(r"\s*\(.*\)$", "", query)  # "Titanic (1997 film)" -> "Titanic"
    if _NAME.match(query):
        return _normalize(query) in _normalize(_text(candidate))
    wanted, have = tokens(query), tokens(_text(candidate))
    return bool(wanted) and len(wanted & have) >= min(2, len(wanted))


def usable(candidate: Candidate) -> bool:
    """Not a photograph of the dead, not site furniture."""
    text = f"{candidate.source_id} {candidate.attribution}"
    return not _GRAPHIC.search(text) and not _ICON.search(text)


# --------------------------------------------------------------------------- #
# gather
# --------------------------------------------------------------------------- #
def gather(titles: Iterable[str], sources: list[str], skip: set[tuple[str, str]] = frozenset()) -> list[Candidate]:
    """Each title's own article images, then its search results across `sources`,
    relevant + usable, deduplicated, without `skip`. A source that errors is left out for
    the rest of the call."""
    found: dict[tuple[str, str], Candidate] = {}
    broken: set[str] = set()
    for title in titles:
        try:
            for c in archives.article_images(title):
                if usable(c) and c.key not in skip:
                    found.setdefault(c.key, c)
        except Exception as exc:
            logger.warning("images of article {!r} unavailable: {}", title, exc)
        for name in sources:
            if name in broken or name not in SOURCES:
                continue
            try:
                results = SOURCES[name](title)
            except Exception as exc:
                logger.warning("{} search failed, skipping it: {}", name, exc)
                broken.add(name)
                continue
            for c in results:
                if usable(c) and relevant(c, title) and c.key not in skip:
                    found.setdefault(c.key, c)
    return list(found.values())


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def _line(n: int, c: Candidate) -> str:
    creator = c.attribution.split(" — ")[-1][:40]
    return f"i{n}: {c.title[:90]} | {c.description[:160]} | {creator}"


def vet(session: Session, subject: str, candidates: list[Candidate], *, key: str) -> list[tuple[Candidate, str]]:
    """(candidate, what it shows) for the images worth showing, in story order. One worker
    call per `VET_BATCH` images. If a call fails outright, that batch is dropped (a wrong
    picture is worse than a missing one); RetryLater propagates so the stage waits."""
    kept: list[tuple[Candidate, str]] = []
    for start in range(0, len(candidates), VET_BATCH):
        batch = candidates[start : start + VET_BATCH]
        prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
            subject=subject, images="\n".join(_line(n, c) for n, c in enumerate(batch)))
        try:
            result = llm.generate(session, prompt, role="worker", step="vet", inputs={"key": key, "start": start})
        except RetryLater:
            raise
        except Exception as exc:
            logger.warning("image check failed for {!r} (batch {}): {}", subject, start, exc)
            continue
        seen: set[int] = set()
        for entry in result.get("images", []):
            if not isinstance(entry, dict) or entry.get("keep") is not True:
                continue
            match = re.fullmatch(r"i(\d+)", str(entry.get("id") or ""))
            n = int(match.group(1)) if match else -1
            if 0 <= n < len(batch) and n not in seen:
                seen.add(n)
                shows = " ".join(str(entry.get("shows") or batch[n].title).split())[:120]
                kept.append((batch[n], shows))
    return kept


def to_dict(candidate: Candidate, shows: str) -> dict:
    return {**asdict(candidate), "shows": shows}


def from_dict(data: dict) -> tuple[Candidate, str]:
    fields = {k: v for k, v in data.items() if k in Candidate.__dataclass_fields__}
    return Candidate(**fields), data.get("shows") or fields.get("attribution", "")


# --------------------------------------------------------------------------- #
# download
# --------------------------------------------------------------------------- #
def _file_path(source: str, source_id: str, url: str) -> Path:
    ext = "png" if url.lower().split("?")[0].endswith(".png") else "jpg"
    name = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:24]
    return cache_dir() / "images" / source / f"{name}.{ext}"


@http_retry
def _download(url: str) -> bytes:
    resp = get_http_client().get(url, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def average_hash(data: bytes) -> int | None:
    """A 64-bit perceptual hash: two scans of the same picture land within a few bits."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            pixels = list(im.convert("L").resize((8, 8), Image.LANCZOS).tobytes())
    except OSError:
        return None
    mean = sum(pixels) / 64
    return sum(1 << i for i, p in enumerate(pixels) if p >= mean)


def store(session: Session, candidate: Candidate) -> tuple[Asset, int | None] | None:
    """The candidate as a cached `asset` (downloaded once, reused across videos) and its
    perceptual hash; None if it can't be downloaded or decoded, or is too small."""
    asset = session.execute(
        select(Asset).filter_by(source=candidate.source, source_id=candidate.source_id)).scalar_one_or_none()
    if asset is not None and Path(asset.uri).exists():
        data = Path(asset.uri).read_bytes()
    else:
        try:
            data = _download(candidate.url)
        except Exception as exc:
            logger.warning("download failed for {}: {}", candidate.source_id, exc)
            return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
    except OSError:
        logger.warning("not an image: {}", candidate.source_id)
        return None
    if max(width, height) < archives.MIN_LONG_SIDE:
        return None
    if asset is None or not Path(asset.uri).exists():
        path = _file_path(candidate.source, candidate.source_id, candidate.url)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, data)
        asset = asset or Asset(kind="image", source=candidate.source, source_id=candidate.source_id)
        asset.uri, asset.sha256 = str(path), hashlib.sha256(data).hexdigest()
        asset.license, asset.rights_url = candidate.license, candidate.rights_url
        asset.attribution = candidate.attribution
        if asset.id is None:
            session.add(asset)
    asset.meta = {"width": width, "height": height}
    session.flush()
    return asset, average_hash(data)


# --------------------------------------------------------------------------- #
# stage
# --------------------------------------------------------------------------- #
def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.RESEARCHED:
        return

    cfg = get_config().images
    topic = session.get(Topic, video.topic_id) if video.topic_id else None
    subject = (topic.title if topic else None) or video.title or ""
    surveyed = [from_dict(d) for d in ((topic.raw or {}).get("inventory") or [])] if topic else []
    titles = [a["title"] for a in (video.research or {}).get("articles", []) if a.get("title")]
    if not surveyed:
        titles = [subject, *[t for t in titles if t != subject]]
    else:
        titles = [t for t in titles if t != subject]  # the survey already covered the topic itself

    new = gather(titles, cfg.sources, skip={c.key for c, _ in surveyed})
    kept = surveyed + vet(session, subject, new, key=f"video:{video_id}")

    inventory: list[dict] = []
    hashes: list[int] = []
    for candidate, shows in kept:
        if len(inventory) >= cfg.max_images:
            break
        stored = store(session, candidate)
        if stored is None:
            continue
        asset, phash = stored
        if phash is not None and any(bin(phash ^ h).count("1") <= _SAME_PICTURE for h in hashes):
            continue  # the same picture again (another scan or crop of it)
        if any(item["asset_id"] == str(asset.id) for item in inventory):
            continue
        if phash is not None:
            hashes.append(phash)
        inventory.append({"n": len(inventory) + 1, "asset_id": str(asset.id), "shows": shows,
                          "title": candidate.title, "source": candidate.source})

    if len(inventory) < cfg.min_per_video:
        raise ValueError(f"video {video_id}: only {len(inventory)} usable images for {subject!r} "
                         f"(need {cfg.min_per_video}); send it back or reject it")
    video.research = {**(video.research or {}), "images": inventory}
    logger.info("pictured {!r}: {} images", subject, len(inventory))
    video.status = Status.PICTURED
