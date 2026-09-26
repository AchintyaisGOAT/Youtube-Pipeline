"""For each segment missing an image, search Wikimedia Commons (primary) then the
Library of Congress (secondary) for a public-domain-us/cc0 image, create an `asset`
row, and point `segment.image_asset_id` at it. Never store an asset without `license` +
`rights_url` populated (DESIGN.md #4) — a segment where nothing license-clean turns up
is left without an image rather than risk a takedown; Media's assemble stage decides
what to do with an image-less segment.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_channel_config
from app.db import Asset, Segment, Video
from app.http import get_http_client, http_retry
from app.status import Status
from app.storage import atomic_write_bytes, cache_dir

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
LOC_API = "https://www.loc.gov/search/"

#: Commons `extmetadata.LicenseShortName` values we treat as safe (DESIGN.md #4) —
#: explicit public-domain/CC0 only, never a bare CC-BY (attribution/Content-ID risk).
_COMMONS_SAFE_LICENSES = {"public domain", "pd", "cc0", "cc0 1.0"}


@http_retry
def _search_commons(query: str, limit: int = 5) -> list[dict]:
    resp = get_http_client().get(
        COMMONS_API,
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": f"filetype:bitmap {query}",
            "gsrnamespace": 6,
            "gsrlimit": limit,
            "prop": "imageinfo",
            "iiprop": "url|extmetadata",
            "format": "json",
        },
    )
    resp.raise_for_status()
    return list(resp.json().get("query", {}).get("pages", {}).values())


def _commons_license(page: dict) -> tuple[str, str] | None:
    imageinfo = (page.get("imageinfo") or [{}])[0]
    meta = imageinfo.get("extmetadata", {})
    license_name = meta.get("LicenseShortName", {}).get("value", "")
    if license_name.strip().lower() not in _COMMONS_SAFE_LICENSES:
        return None
    rights_url = meta.get("LicenseUrl", {}).get("value") or imageinfo.get("descriptionurl", "")
    return license_name, rights_url


@http_retry
def _search_loc(query: str, limit: int = 5) -> list[dict]:
    resp = get_http_client().get(LOC_API, params={"q": query, "fo": "json", "c": limit})
    resp.raise_for_status()
    return resp.json().get("results", [])


def _loc_license(item: dict) -> tuple[str, str] | None:
    rights = (item.get("rights") or "").lower()
    if "no known restrictions" not in rights and "public domain" not in rights:
        return None
    return "public-domain-us", item.get("url", "")


def _safe_search(fn, query: str, source_name: str) -> list[dict]:
    """A source being unreachable (down, blocked, rate-limited) must not crash the
    whole stage -- verified live that loc.gov now sits behind a Cloudflare bot
    challenge (a 403 "Just a moment..." interstitial no plain HTTP client can pass),
    which used to propagate all the way up and fail the entire video over a single
    segment's fallback image lookup. Treat any request failure as "no results from
    this source" so the caller falls through to the next source, or leaves the
    segment image-less -- both already-handled, non-fatal outcomes."""
    try:
        return fn(query)
    except httpx.HTTPError as exc:
        logger.warning("{} image search failed for {!r}: {}", source_name, query, exc)
        return []


def _find_image(query: str, sources: list[str]) -> dict | None:
    if "wikimedia" in sources:
        for page in _safe_search(_search_commons, query, "wikimedia"):
            license_info = _commons_license(page)
            if license_info is None:
                continue
            license_name, rights_url = license_info
            imageinfo = (page.get("imageinfo") or [{}])[0]
            uri = imageinfo.get("url", "")
            if not uri:
                continue
            return {
                "source": "wikimedia",
                "source_id": str(page.get("pageid", "")),
                "license": license_name,
                "rights_url": rights_url,
                "uri": uri,
                "attribution": page.get("title", ""),
            }
    if "loc" in sources:
        for item in _safe_search(_search_loc, query, "loc"):
            license_info = _loc_license(item)
            if license_info is None:
                continue
            license_name, rights_url = license_info
            uri = (item.get("image_url") or [None])[0]
            if not uri:
                continue
            return {
                "source": "loc",
                "source_id": str(item.get("id", "")),
                "license": license_name,
                "rights_url": rights_url,
                "uri": uri,
                "attribution": item.get("title", ""),
            }
    return None


@http_retry
def _download_image(remote_uri: str, source: str, source_id: str) -> str:
    """Media reads `asset.uri` as a local file path, never a remote URL (WORK_MEDIA.md's
    handoff contract: "asset rows ... each with a real uri on disk"). Verified live
    that leaving `uri` as the Wikimedia/LoC download URL directly broke assemble.py
    outright: its `Path(asset.uri).exists()` check is never true for a URL string, so
    every segment silently looked image-less and the whole render failed with "no
    segment images available." Cached by (source, source_id) so re-running never
    re-downloads an asset already on disk."""
    suffix = Path(urlsplit(remote_uri).path).suffix or ".jpg"
    dest = cache_dir() / "images" / f"{source}_{source_id}{suffix}"
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        response = get_http_client().get(remote_uri, follow_redirects=True)
        response.raise_for_status()
        atomic_write_bytes(dest, response.content)
    return str(dest)


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.FETCHING_IMAGES:
        return

    config = get_channel_config(session, video.channel_id)
    segments = session.execute(select(Segment).filter_by(video_id=video_id).order_by(Segment.idx)).scalars()

    for segment in segments:
        if segment.image_asset_id is not None or not segment.image_query:
            continue
        found = _find_image(segment.image_query, config.images.sources)
        if found is None:
            continue

        asset = session.execute(
            select(Asset).filter_by(source=found["source"], source_id=found["source_id"])
        ).scalar_one_or_none()
        if asset is None:
            try:
                local_path = _download_image(found["uri"], found["source"], found["source_id"])
            except httpx.HTTPError as exc:
                logger.warning("failed to download image for {!r}: {}", segment.image_query, exc)
                continue
            found = {**found, "uri": local_path}
            asset = Asset(kind="image", **found)
            session.add(asset)
            session.flush()  # need asset.id before pointing the segment at it

        segment.image_asset_id = asset.id

    video.status = Status.SYNTHESIZING_VOICE
