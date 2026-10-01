"""Public-domain / CC0 image archives (README §6.1): one search function per archive, all
returning `Candidate`s, plus the images of the researched Wikipedia articles themselves.

Every source is free. Two need a free key and are skipped without one (Smithsonian,
Europeana). Only public domain or CC0 is ever returned — no attribution-required or
non-commercial licences — and images too small for 1080p are dropped here.

| name              | what it is                                           | key |
|-------------------|------------------------------------------------------|-----|
| wikimedia_commons | Wikimedia Commons file search                        | –   |
| openverse         | one search over Flickr Commons, museums, Commons…    | –   |
| met               | The Metropolitan Museum of Art, Open Access          | –   |
| artic             | Art Institute of Chicago, public-domain works        | –   |
| cleveland         | Cleveland Museum of Art, CC0                         | –   |
| smithsonian       | Smithsonian Open Access, CC0                         | SMITHSONIAN_API_KEY |
| europeana         | Europeana (European archives), PD mark / CC0 only    | EUROPEANA_API_KEY |

Not sources: the Library of Congress (its API answers non-browser clients with a
Cloudflare challenge since 2026-09 — getting past it would mean evading bot protection)
and the US National Archives catalog (its API now needs a key and returns a web page
without one); much of both is on Commons anyway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from app.config import get_settings
from app.http import get_http_client, http_retry

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"
MET_SEARCH = "https://collectionapi.metmuseum.org/public/collection/v1/search"
MET_OBJECT = "https://collectionapi.metmuseum.org/public/collection/v1/objects/{}"
ARTIC_API = "https://api.artic.edu/api/v1/artworks/search"
ARTIC_IIIF = "https://www.artic.edu/iiif/2/{}/full/1686,/0/default.jpg"
CLEVELAND_API = "https://openaccess-api.clevelandart.org/api/artworks/"
SMITHSONIAN_API = "https://api.si.edu/openaccess/api/v1.0/search"
EUROPEANA_API = "https://api.europeana.eu/record/v2/search.json"

#: Archive images smaller than this on their long side look too soft at 1080p. 600 keeps
#: the genuine period photos (a live run at 800 lost Lizzie Borden's real 640px portraits);
#: slightly soft is the normal look of archival footage. 0 = size unknown (museum APIs
#: that don't report it serve full-size originals).
MIN_LONG_SIDE = 600
RESULTS_PER_SEARCH = 20
#: The Met's search returns ids only; each object is one more request.
_MET_OBJECTS = 8
_HTML = re.compile(r"<[^>]+>")
_PUBLIC_DOMAIN = re.compile(r"publicdomain/(mark|zero)", re.IGNORECASE)
_WIKIDATA = re.compile(r"\b(?:date |title |label )?QS:\S*")


@dataclass(frozen=True)
class Candidate:
    source: str
    source_id: str
    url: str  # what gets downloaded (a ≤1920px rendition where the archive offers one)
    license: str
    rights_url: str  # the page crediting and describing the image
    attribution: str  # "<title> — <author>"
    width: int
    height: int
    #: What the archive says the image shows (Commons' description), for the image check.
    description: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """Identity for "already used in this video": the file name without its extension,
        so a .tif and a .jpg of the same scan count as one image."""
        return self.source, re.sub(r"\.\w{3,4}$", "", self.source_id.lower())

    @property
    def title(self) -> str:
        return self.attribution.split(" — ")[0]


def _clean(html) -> str:
    """Plain text: HTML tags and Wikidata codes ("date QS:P,+1861-00-") removed."""
    return " ".join(_WIKIDATA.sub(" ", _HTML.sub(" ", str(html or ""))).split())


def _big_enough(width, height) -> bool:
    width, height = int(width or 0), int(height or 0)
    return (width == 0 and height == 0) or max(width, height) >= MIN_LONG_SIDE


# --------------------------------------------------------------------------- #
# Wikimedia Commons (search, and the files a Wikipedia article shows)
# --------------------------------------------------------------------------- #
def _commons_candidates(pages: list[dict]) -> list[Candidate]:
    found = []
    for page in sorted(pages, key=lambda p: p.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        meta = {k: (v or {}).get("value", "") for k, v in (info.get("extmetadata") or {}).items()}
        if meta.get("License", "").lower() not in ("pd", "cc0") or info.get("mime") == "image/gif":
            continue
        width, height = info.get("width", 0), info.get("height", 0)
        if not width or max(width, height) < MIN_LONG_SIDE:
            continue
        artist = _clean(meta.get("Artist", "")) or "Unknown author"
        title = _clean(meta.get("ObjectName", "")) or page["title"].removeprefix("File:")
        found.append(Candidate(
            source="wikimedia_commons", source_id=page["title"],
            url=info.get("thumburl") or info["url"], license=meta.get("LicenseShortName") or "Public domain",
            rights_url=info.get("descriptionurl", ""), attribution=f"{title} — {artist}",
            width=width, height=height,
            description=" ".join(p for p in (_clean(meta.get("DateTimeOriginal", ""))[:30],
                                             _clean(meta.get("ImageDescription", ""))[:220]) if p),
        ))
    return found


_IMAGEINFO = {"prop": "imageinfo", "iiprop": "url|size|mime|extmetadata", "iiurlwidth": 1920,
              "iiextmetadatafilter": "License|LicenseShortName|Artist|ObjectName|ImageDescription|DateTimeOriginal"}


@http_retry
def search_commons(query: str, *, exact: bool = False) -> list[Candidate]:
    resp = get_http_client().get(COMMONS_API, params={
        "action": "query", "format": "json", "formatversion": 2, "generator": "search",
        "gsrsearch": f'"{query}" filetype:bitmap' if exact else f"{query} filetype:bitmap",
        "gsrnamespace": 6, "gsrlimit": RESULTS_PER_SEARCH, **_IMAGEINFO,
    })
    resp.raise_for_status()
    return _commons_candidates(resp.json().get("query", {}).get("pages", []))


@http_retry
def article_images(title: str) -> list[Candidate]:
    """The public-domain images a Wikipedia article itself shows — chosen by its editors,
    so always on topic (the Lizzie Borden article has 15). Non-free (fair-use) files are
    dropped by the licence check, like every Commons result."""
    resp = get_http_client().get(WIKIPEDIA_API, params={
        "action": "query", "format": "json", "formatversion": 2, "redirects": 1, "titles": title,
        "generator": "images", "gimlimit": "max", **_IMAGEINFO,
    })
    resp.raise_for_status()
    # files hosted on Commons show up as "missing" locally but still carry their imageinfo
    pages = [p for p in resp.json().get("query", {}).get("pages", []) if p.get("imageinfo")]
    return _commons_candidates(pages)


# --------------------------------------------------------------------------- #
# aggregators and museums
# --------------------------------------------------------------------------- #
@http_retry
def search_openverse(query: str, *, exact: bool = False) -> list[Candidate]:
    resp = get_http_client().get(OPENVERSE_API, params={
        "q": f'"{query}"' if exact else query, "license": "cc0,pdm", "page_size": RESULTS_PER_SEARCH,
        "mature": "false",
    })
    resp.raise_for_status()
    found = []
    for row in resp.json().get("results", []):
        if not row.get("url") or not _big_enough(row.get("width"), row.get("height")):
            continue
        license_name = "CC0" if row.get("license") == "cc0" else "Public domain"
        found.append(Candidate(
            source="openverse", source_id=row["id"], url=row["url"], license=license_name,
            rights_url=row.get("foreign_landing_url") or row["url"],
            attribution=f"{_clean(row.get('title')) or 'Untitled'} — {_clean(row.get('creator')) or 'Unknown author'}",
            width=int(row.get("width") or 0), height=int(row.get("height") or 0),
        ))
    return found


@http_retry
def _met_object(object_id: int) -> dict:
    resp = get_http_client().get(MET_OBJECT.format(object_id))
    resp.raise_for_status()
    return resp.json()


@http_retry
def search_met(query: str, *, exact: bool = False) -> list[Candidate]:
    resp = get_http_client().get(MET_SEARCH, params={"q": query, "hasImages": "true"})
    resp.raise_for_status()
    found = []
    for object_id in (resp.json().get("objectIDs") or [])[:_MET_OBJECTS]:
        try:
            obj = _met_object(object_id)
        except httpx.HTTPStatusError:  # the search index lists objects that no longer exist (404, seen live)
            continue
        image = obj.get("primaryImageSmall") or obj.get("primaryImage")
        if not obj.get("isPublicDomain") or not image:
            continue
        date = f", {obj['objectDate']}" if obj.get("objectDate") else ""
        found.append(Candidate(
            source="met", source_id=str(object_id), url=image, license="CC0",
            rights_url=obj.get("objectURL") or image,
            attribution=f"{obj.get('title') or 'Untitled'}{date} — {obj.get('artistDisplayName') or 'Unknown author'}",
            width=0, height=0,
        ))
    return found


@http_retry
def search_artic(query: str, *, exact: bool = False) -> list[Candidate]:
    resp = get_http_client().get(ARTIC_API, params={
        "q": query, "query[term][is_public_domain]": "true", "limit": RESULTS_PER_SEARCH,
        "fields": "id,title,image_id,artist_display,thumbnail,date_display",
    })
    resp.raise_for_status()
    found = []
    for row in resp.json().get("data", []):
        thumb = row.get("thumbnail") or {}
        if not row.get("image_id") or not _big_enough(thumb.get("width"), thumb.get("height")):
            continue
        date = f", {row['date_display']}" if row.get("date_display") else ""
        found.append(Candidate(
            source="artic", source_id=str(row["id"]), url=ARTIC_IIIF.format(row["image_id"]), license="CC0",
            rights_url=f"https://www.artic.edu/artworks/{row['id']}",
            attribution=(f"{row.get('title') or 'Untitled'}{date} — "
                         f"{_clean(row.get('artist_display')) or 'Unknown author'}"),
            width=int(thumb.get("width") or 0), height=int(thumb.get("height") or 0),
        ))
    return found


@http_retry
def search_cleveland(query: str, *, exact: bool = False) -> list[Candidate]:
    resp = get_http_client().get(CLEVELAND_API, params={"q": query, "cc0": 1, "has_image": 1,
                                                        "limit": RESULTS_PER_SEARCH})
    resp.raise_for_status()
    found = []
    for row in resp.json().get("data", []):
        web = (row.get("images") or {}).get("web") or {}
        if not web.get("url") or not _big_enough(web.get("width"), web.get("height")):
            continue
        creator = ((row.get("creators") or [{}])[0] or {}).get("description") or "Unknown author"
        date = f", {row['creation_date']}" if row.get("creation_date") else ""
        found.append(Candidate(
            source="cleveland", source_id=str(row.get("id") or web["url"]), url=web["url"], license="CC0",
            rights_url=row.get("url") or web["url"],
            attribution=f"{row.get('title') or 'Untitled'}{date} — {_clean(creator)}",
            width=int(web.get("width") or 0), height=int(web.get("height") or 0),
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
        params={"q": f"{terms} AND online_media_type:Images", "rows": RESULTS_PER_SEARCH, "api_key": key},
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


@http_retry
def search_europeana(query: str, *, exact: bool = False) -> list[Candidate]:
    key = get_settings().europeana_api_key
    if not key:
        return []
    resp = get_http_client().get(EUROPEANA_API, params={
        "query": f'"{query}"' if exact else query, "wskey": key, "reusability": "open", "media": "true",
        "qf": "TYPE:IMAGE", "rows": RESULTS_PER_SEARCH,
    })
    resp.raise_for_status()
    found = []
    for row in resp.json().get("items", []):
        rights = " ".join(row.get("rights") or [])
        image = (row.get("edmIsShownBy") or [None])[0]
        if not image or not _PUBLIC_DOMAIN.search(rights):  # "open" also includes CC BY: not for us
            continue
        title = (row.get("title") or ["Untitled"])[0]
        creator = (row.get("dcCreator") or ["Unknown author"])[0]
        found.append(Candidate(
            source="europeana", source_id=row.get("id") or image, url=image,
            license="CC0" if "zero" in rights else "Public domain",
            rights_url=(row.get("guid") or image).split("?")[0],
            attribution=f"{_clean(title)} — {_clean(creator)}", width=0, height=0,
        ))
    return found


#: name -> search function, in the default order.
SOURCES = {
    "wikimedia_commons": search_commons,
    "openverse": search_openverse,
    "met": search_met,
    "artic": search_artic,
    "cleveland": search_cleveland,
    "smithsonian": search_smithsonian,
    "europeana": search_europeana,
}
