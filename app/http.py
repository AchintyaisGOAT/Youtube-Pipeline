"""Shared HTTP client factory. Every module that calls an external API imports from
here — never build an ad-hoc ``httpx.Client`` elsewhere.
"""

from __future__ import annotations

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app import __version__
from app.config import get_settings

_client: httpx.Client | None = None


def get_http_client() -> httpx.Client:
    """One process-wide client: connection pooling + a consistent User-Agent.

    Wikimedia tightened its bot policy on 2026-09-23 (days before this was verified
    live): any request to upload.wikimedia.org without a URL — not just an email —
    in the User-Agent's contact parenthetical now gets a flat 403 "please honor our
    robot policy", even a bare email address that the *general* documented policy
    says is acceptable. Empirically verified the `+URL` form works and a trailing
    "; email" after the URL (Wikimedia's own documented example format) does not —
    this enforcement looks rushed/rough at the edges. `WIKIMEDIA_CONTACT` must be a
    URL now, not an email; see .env.example.
    """
    global _client
    if _client is None:
        settings = get_settings()
        _client = httpx.Client(
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"User-Agent": f"yt-pipeline/{__version__} (+{settings.wikimedia_contact})"},
        )
    return _client


#: Shared retry policy for flaky external calls — apply as a decorator:
#: ``@http_retry`` above a function that calls ``get_http_client()``.
http_retry = retry(
    retry=retry_if_exception_type((httpx.TransportError, httpx.HTTPStatusError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
