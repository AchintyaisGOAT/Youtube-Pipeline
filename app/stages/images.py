"""Images (README §4.1 step 8, §6.1): one image per scene, archives first.

For each scene without an image, in order:
1. the scene's own search query on each archive in `images.sources` (Wikimedia Commons,
   then Smithsonian Open Access if SMITHSONIAN_API_KEY is set) — public domain / CC0 only;
2. broader, topic-level queries (the video's title and its researched articles' titles),
   searched as exact phrases;
3. an AI illustration in the configured vintage `ai_style`, if `images.ai_fallback` and
   the video hasn't used `images.ai_max_per_video` of them yet;
4. reuse of the image this video showed longest ago (the assemble stage gives a reused
   image a different pan/zoom, so it doesn't look repeated).

Results must pass a relevance check on their titles (`relevant`), and real photographs of
dead bodies are skipped (`_GRAPHIC`). An image already used earlier in this video is
skipped in favour of the next result, and
once `images.max_images` distinct images are used, remaining scenes reuse. Every image is
stored as an `asset` with its license, author and source page for the credits, and
cached on disk — the same file is never downloaded twice, across videos too.

The Library of Congress is not a source: since 2026-09 its API answers every
non-browser client with a Cloudflare challenge (403), and getting past that would mean
evading its bot protection. Much of its public-domain photography is on Commons anyway.

A search source that errors (after the HTTP client's own retries) is skipped for the rest
of the run. If every archive errored and no image at all could be placed, the video
waits for the next run instead of failing.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import llm
from app.config import get_config, get_settings
from app.db import Asset, Scene, Video
from app.http import get_http_client, http_retry
from app.quota import RetryLater
from app.status import Status
from app.storage import atomic_write_bytes, cache_dir

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
SMITHSONIAN_API = "https://api.si.edu/openaccess/api/v1.0/search"

#: Archive images smaller than this on their long side look too soft at 1080p. 600 keeps
#: the genuine period photos (a live run at 800 lost Lizzie Borden's real 640px portraits);
#: slightly soft is the normal look of archival footage.
MIN_LONG_SIDE = 600
_RESULTS_PER_SEARCH = 10
_HTML = re.compile(r"<[^>]+>")
_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = {"the", "and", "for", "with", "from", "into", "his", "her", "their", "its", "was", "were",
              "file", "jpg", "jpeg", "png", "tif", "tiff", "photo", "photograph", "image", "picture"}
#: Real photographs of dead bodies are a YouTube policy risk and wrong for the channel's
#: tone (a live run picked a crime-scene photo of a murder victim). The narration can
#: still tell what happened; the picture just doesn't show a corpse.
_GRAPHIC = re.compile(
    r"\b(corpse|corpses|dead bod(?:y|ies)|autopsy|post[- ]mortem|skull|skulls|severed|mutilat\w*|"
    r"lynch\w*|execution|executed|hanged|beheaded|gore|slain|murdered|victims?|crime scene|remains)\b",
    re.IGNORECASE,
)
_QUOTE_TAGS = re.compile(r"\[/?QUOTE\]")


@dataclass(frozen=True)
class Candidate:
    source: str
    source_id: str
    url: str  # what gets downloaded (a ≤1920px rendition where the archive offers one)
    license: str
    rights_url: str
    attribution: str
    width: int
    height: int

    @property
    def key(self) -> tuple[str, str]:
        """Identity for "already used in this video": the file name without its extension,
        so a .tif and a .jpg of the same scan count as one image."""
        return self.source, re.sub(r"\.\w{3,4}$", "", self.source_id.lower())


def _normalize(text: str) -> str:
    return " ".join(_TOKEN.findall(text.lower().replace("_", " ")))


def tokens(text: str) -> set[str]:
    return {t for t in _TOKEN.findall(text.lower()) if len(t) >= 3 and t not in _STOPWORDS}


def relevant(candidate: Candidate, query: str, *, exact: bool) -> bool:
    """A result must actually be about the query, judged by its title and attribution:
    the whole phrase for an exact (topic-level) query, else at least two of its words (or
    the only one). Commons full-text search is loose — a live run returned theatre floor plans
    for "Andrew Borden on sofa" and census sheets for a lawyer's surname."""
    text = f"{candidate.source_id} {candidate.attribution}"
    if exact:  # the whole phrase, not just its long words: "M. C. D. Borden" != any Borden
        return _normalize(query) in _normalize(text)
    wanted, have = tokens(query), tokens(text)
    return bool(wanted) and len(wanted & have) >= min(2, len(wanted))


# --------------------------------------------------------------------------- #
# archive sources
# --------------------------------------------------------------------------- #
def _clean(html: str) -> str:
    return " ".join(_HTML.sub(" ", html or "").split())


@http_retry
def search_commons(query: str, *, exact: bool = False) -> list[Candidate]:
    resp = get_http_client().get(
        COMMONS_API,
        params={
            "action": "query", "format": "json", "formatversion": 2, "generator": "search",
            "gsrsearch": f'"{query}" filetype:bitmap' if exact else f"{query} filetype:bitmap", "gsrnamespace": 6, "gsrlimit": _RESULTS_PER_SEARCH,
            "prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1920,
            "iiextmetadatafilter": "License|LicenseShortName|Artist|ObjectName",
        },
    )
    resp.raise_for_status()
    pages = sorted(resp.json().get("query", {}).get("pages", []), key=lambda p: p.get("index", 0))
    found = []
    for page in pages:
        info = (page.get("imageinfo") or [{}])[0]
        meta = {k: (v or {}).get("value", "") for k, v in (info.get("extmetadata") or {}).items()}
        if meta.get("License", "").lower() not in ("pd", "cc0") or info.get("mime") == "image/gif":
            continue
        width, height = info.get("width", 0), info.get("height", 0)
        if max(width, height) < MIN_LONG_SIDE:
            continue
        artist = _clean(meta.get("Artist", "")) or "Unknown author"
        title = _clean(meta.get("ObjectName", "")) or page["title"].removeprefix("File:")
        found.append(Candidate(
            source="wikimedia_commons", source_id=page["title"],
            url=info.get("thumburl") or info["url"], license=meta.get("LicenseShortName") or "Public domain",
            rights_url=info.get("descriptionurl", ""), attribution=f"{title} — {artist}",
            width=width, height=height,
        ))
    return found


@http_retry
def search_smithsonian(query: str, *, exact: bool = False) -> list[Candidate]:
    key = get_settings().smithsonian_api_key
    if not key:
        return []
    terms = f'"{query}"' if exact else query
    resp = get_http_client().get(
        SMITHSONIAN_API,
        params={"q": f"{terms} AND online_media_type:Images", "rows": _RESULTS_PER_SEARCH, "api_key": key},
    )
    resp.raise_for_status()
    found = []
    for row in resp.json().get("response", {}).get("rows", []):
        content = row.get("content") or {}
        unit = row.get("unitCode") or "Smithsonian"
        for media in ((content.get("descriptiveNonRepeating") or {}).get("online_media") or {}).get("media", []):
            if media.get("type") != "Images" or (media.get("usage") or {}).get("access") != "CC0":
                continue
            best = next((r for r in media.get("resources", []) if r.get("label") == "High-resolution JPEG"), None)
            width, height = (best or {}).get("width") or 0, (best or {}).get("height") or 0
            if best is None or not best.get("url") or max(width, height) < MIN_LONG_SIDE:
                continue
            found.append(Candidate(
                source="smithsonian", source_id=media.get("idsId") or best["url"], url=best["url"],
                license="CC0", rights_url=media.get("content") or best["url"],
                attribution=f"{row.get('title') or 'Untitled'} — Smithsonian {unit}",
                width=width, height=height,
            ))
            break  # one image per record
    return found


SOURCES = {"wikimedia_commons": search_commons, "smithsonian": search_smithsonian}


# --------------------------------------------------------------------------- #
# finding + storing
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


class Finder:
    """Archive search for one video run: caches each (source, query) result, skips a
    source for the rest of the run once it errors, and never returns an image this video
    already uses."""

    def __init__(self, session: Session, source_names: list[str], used: set[tuple[str, str]]):
        self.session = session
        self.sources = [s for s in source_names if s in SOURCES]
        self.broken: set[str] = set()
        self.used = used
        self._results: dict[tuple[str, str, bool], list[Candidate]] = {}

    @property
    def all_broken(self) -> bool:
        return bool(self.sources) and set(self.sources) <= self.broken

    def _search(self, source: str, query: str, exact: bool) -> list[Candidate]:
        if (source, query, exact) not in self._results:
            try:
                found = SOURCES[source](query, exact=exact)
            except Exception as exc:
                logger.warning("{} search failed, skipping it this run: {}", source, exc)
                self.broken.add(source)
                found = []
            self._results[source, query, exact] = [
                c for c in found
                if relevant(c, query, exact=exact) and not _GRAPHIC.search(f"{c.source_id} {c.attribution}")
            ]
        return self._results[source, query, exact]

    def _store(self, candidate: Candidate) -> Asset | None:
        existing = self.session.execute(
            select(Asset).filter_by(source=candidate.source, source_id=candidate.source_id)
        ).scalar_one_or_none()
        if existing is not None and Path(existing.uri).exists():
            return existing
        try:
            data = _download(candidate.url)
        except Exception as exc:
            logger.warning("download failed for {}: {}", candidate.source_id, exc)
            return None
        path = _file_path(candidate.source, candidate.source_id, candidate.url)
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(path, data)
        asset = existing or Asset(kind="image", source=candidate.source, source_id=candidate.source_id)
        asset.uri, asset.sha256 = str(path), hashlib.sha256(data).hexdigest()
        asset.license, asset.rights_url, asset.attribution = candidate.license, candidate.rights_url, candidate.attribution
        asset.meta = {"width": candidate.width, "height": candidate.height}
        if existing is None:
            self.session.add(asset)
        self.session.flush()
        return asset

    def find(self, queries: list[str], *, exact: bool = False) -> Asset | None:
        """First unused, relevant, downloadable result: queries in order, each across every
        source. `exact` = topic-level phrase search (see `relevant`)."""
        for query in (q for q in queries if q):
            for source in self.sources:
                if source in self.broken:
                    continue
                for candidate in self._search(source, query, exact):
                    if candidate.key in self.used:
                        continue
                    asset = self._store(candidate)
                    if asset is not None:
                        self.used.add(candidate.key)
                        return asset
        return None


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:24]


def _ai_illustration(session: Session, video: Video, scene: Scene, style: str) -> Asset:
    """Raises RetryLater (quota/overloaded) or ValueError (model returned no image)."""
    # The video's topic goes in every prompt, not just the scene's sentence — verified
    # live that a vague line ("We all know the rhyme.") alone produced an unrelated scene.
    prompt = (
        f"{style}\n\nThis is one scene from a history video about: {video.title}\n"
        f"Illustrate this specific moment: {_QUOTE_TAGS.sub('', scene.text)}"
    )
    source_id = _prompt_hash(prompt)
    asset = session.execute(select(Asset).filter_by(source="gemini", source_id=source_id)).scalar_one_or_none()
    if asset is not None and Path(asset.uri).exists():
        return asset
    image_bytes, mime_type = llm.generate_image(session, prompt)
    path = cache_dir() / "illustrations" / f"{source_id}.{'jpg' if 'jpeg' in mime_type else 'png'}"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_bytes(path, image_bytes)
    if asset is None:
        asset = Asset(kind="image", source="gemini", source_id=source_id)
        session.add(asset)
    asset.uri, asset.sha256 = str(path), hashlib.sha256(image_bytes).hexdigest()
    asset.license, asset.rights_url, asset.attribution = "ai-generated", None, "AI-generated illustration"
    session.flush()
    return asset


def _reuse(last_used: dict[uuid.UUID, int], previous: uuid.UUID | None) -> uuid.UUID | None:
    """The image this video showed longest ago — not the one just shown, if possible."""
    options = sorted(last_used, key=last_used.get)
    choices = [a for a in options if a != previous] or options
    return choices[0] if choices else None


# --------------------------------------------------------------------------- #
# stage
# --------------------------------------------------------------------------- #
def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.SEGMENTED:
        return

    cfg = get_config().images
    scenes = list(session.execute(select(Scene).filter_by(video_id=video_id).order_by(Scene.idx)).scalars())
    if not scenes:
        raise ValueError(f"video {video_id}: no scenes to find images for")

    assets = {a.id: a for a in session.execute(
        select(Asset).where(Asset.id.in_([s.image_asset_id for s in scenes if s.image_asset_id]))
    ).scalars()}
    last_used: dict[uuid.UUID, int] = {}
    for scene in scenes:
        if scene.image_asset_id in assets:
            last_used[scene.image_asset_id] = scene.idx
    finder = Finder(session, cfg.sources, set())
    for a in assets.values():
        finder.used.add(Candidate(a.source, a.source_id, "", "", "", "", 0, 0).key)
    broad = list(dict.fromkeys([video.title or "", *(a["title"] for a in (video.research or {}).get("articles", []))]))
    ai_used = sum(1 for a in assets.values() if a.source == "gemini")
    ai_available = cfg.ai_fallback and ai_used < cfg.ai_max_per_video

    previous: uuid.UUID | None = None
    for scene in scenes:
        if scene.image_asset_id is not None:
            previous = scene.image_asset_id
            continue
        asset_id = None
        if len(last_used) < cfg.max_images:
            asset = finder.find([scene.image_query or ""]) or finder.find(broad, exact=True)
            if asset is None and ai_available:
                try:
                    asset = _ai_illustration(session, video, scene, cfg.ai_style)
                    ai_used += 1
                    ai_available = ai_used < cfg.ai_max_per_video
                except RetryLater as exc:
                    logger.warning("AI illustrations unavailable for the rest of this run: {}", exc)
                    ai_available = False
                except Exception as exc:  # no image returned, or anything unexpected: reuse instead
                    logger.warning("no AI illustration for scene {}: {}", scene.idx, exc)
            asset_id = asset.id if asset is not None else None
        asset_id = asset_id or _reuse(last_used, previous)
        if asset_id is not None:
            scene.image_asset_id = asset_id
            last_used[asset_id] = scene.idx
        previous = scene.image_asset_id

    missing = [s for s in scenes if s.image_asset_id is None]
    if missing and not last_used:
        if finder.all_broken or cfg.ai_fallback:
            raise RetryLater(f"video {video_id}: no image could be placed yet (archives down or AI quota spent)")
        raise ValueError(f"video {video_id}: no archive image found for any scene and AI fallback is off")
    for scene in missing:  # scenes before the first image: take the least-recently-shown ones
        scene.image_asset_id = _reuse(last_used, None)
        last_used[scene.image_asset_id] = len(scenes) + scene.idx

    video.status = Status.IMAGES_READY
