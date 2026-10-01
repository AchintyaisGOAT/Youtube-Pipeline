"""English Wikipedia's action API — shared by discover (page descriptions) and research
(article text, links, references). Every call goes through the shared httpx client, whose
User-Agent carries WIKIMEDIA_CONTACT (Wikimedia returns 403 without it).
"""

from __future__ import annotations

from app.http import get_http_client, http_retry

ACTION_API_URL = "https://en.wikipedia.org/w/api.php"

#: The action API returns extracts for at most 20 titles per request.
BATCH = 20


def _query(**params) -> dict:
    resp = get_http_client().get(
        ACTION_API_URL, params={"action": "query", "format": "json", "formatversion": 2, "redirects": 1, **params}
    )
    resp.raise_for_status()
    return resp.json().get("query", {})


@http_retry
def describe_pages(titles: list[str]) -> dict[str, dict]:
    """{requested title: {"title": canonical, "description", "extract"}} for up to 20
    titles in one request (2-sentence intro extract). Redirects are followed; missing
    pages are left out."""
    query = _query(prop="extracts|description", exintro=1, explaintext=1, exsentences=2,
                   exlimit=BATCH, titles="|".join(titles))
    hops = {h["from"]: h["to"] for h in query.get("normalized", []) + query.get("redirects", [])}
    pages = {p["title"]: p for p in query.get("pages", []) if not p.get("missing")}
    found = {}
    for requested in titles:
        canonical = requested
        while canonical in hops:
            canonical = hops[canonical]
        page = pages.get(canonical)
        if page is not None:
            found[requested] = {
                "title": page["title"],
                "description": page.get("description") or "",
                "extract": page.get("extract") or "",
            }
    return found


@http_retry
def article(title: str) -> dict | None:
    """{"title", "url", "text"} — the full plain-text article with `== Heading ==` markers,
    or None if the page doesn't exist."""
    pages = _query(prop="extracts|info", explaintext=1, exsectionformat="wiki", inprop="url",
                   titles=title).get("pages", [])
    if not pages or pages[0].get("missing") or not pages[0].get("extract"):
        return None
    page = pages[0]
    return {"title": page["title"], "url": page.get("fullurl", ""), "text": page["extract"]}


@http_retry
def all_links(title: str, limit: int = 5000) -> list[str]:
    """Every main-namespace link on a page (following continuation, up to `limit`) — the
    catalog source reads Wikipedia's list pages this way."""
    links: list[str] = []
    cont: dict = {}
    while len(links) < limit:
        resp = get_http_client().get(ACTION_API_URL, params={
            "action": "query", "format": "json", "formatversion": 2, "redirects": 1, "titles": title,
            "prop": "links", "plnamespace": 0, "pllimit": "max", **cont})
        resp.raise_for_status()
        data = resp.json()
        pages = data.get("query", {}).get("pages", [])
        links += [link["title"] for link in (pages[0].get("links", []) if pages else [])]
        if "continue" not in data:
            break
        cont = data["continue"]
    return links[:limit]


@http_retry
def links_and_sources(title: str) -> tuple[list[str], list[str]]:
    """(article links in the main namespace, external reference URLs) — first page of each
    (up to 500), which is plenty for picking linked articles and listing sources."""
    pages = _query(prop="links|extlinks", plnamespace=0, pllimit="max", ellimit="max",
                   titles=title).get("pages", [])
    page = pages[0] if pages else {}
    return [link["title"] for link in page.get("links", [])], [link["url"] for link in page.get("extlinks", [])]
