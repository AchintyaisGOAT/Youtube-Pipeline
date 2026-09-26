"""Gemini access for every Content stage: all calls go through `llm_cache` (dev re-runs
cost zero tokens) and `app.quota.check_and_increment` (over-budget calls raise
`QuotaExceeded`, which the orchestrator treats as "retry later").

google-genai is imported lazily inside the functions that actually need it, so this
module — and every stage that imports it — stays importable (and unit-testable) without
the package installed, as long as the call is a cache hit or is monkeypatched.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import LlmCache
from app.quota import check_and_increment

#   gemini-2.5-flash (DESIGN.md's original pick) returns 404 "no longer available to
#   new users" as of 2026-09 -- verified live against a real key.
#
#   Tried "gemini-flash-latest" next, on the theory that an auto-resolving alias
#   can't go stale the same way -- but verified live that it currently points to
#   gemini-3.8-flash, a brand-new release with a free tier capped at 5 requests/MINUTE
#   *and* 20 requests/DAY. A real gate.py run against 20 real candidates burned that
#   day's quota in two batches. Brand-new model releases often ship with very tight
#   initial free quotas before Google scales them up, and "latest" will happily land
#   on the next one too. Pinned to gemini-3.1-flash-lite instead: a few releases
#   older, verified live across many calls this session with zero rate-limit errors.
#   Trades "always the newest model" for confirmed-working today; revisit if this
#   ever 404s the way 2.5-flash did (re-check `client.models.list()`), and reconsider
#   once "latest" has had time to mature past its launch-week quota.
MODEL = "gemini-3.1-flash-lite"

_client = None


def _client_instance():
    global _client
    if _client is None:
        from google import genai

        _client = genai.Client(api_key=get_settings().gemini_api_key)
    return _client


def _cache_key(model: str, prompt: str, inputs: dict) -> str:
    payload = json.dumps({"model": model, "prompt": prompt, "inputs": inputs}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _estimate_tokens(prompt: str) -> int:
    """~4 chars/token is a rough-enough heuristic for pre-flight budget checks."""
    return max(1, len(prompt) // 4)


def _is_transient(exc: BaseException) -> bool:
    """A 503 ("high demand") is genuinely transient per Google's own error message —
    confirmed live that a freshly-launched flash model can 503 repeatedly for minutes
    at a time under load. A 429 ("RESOURCE_EXHAUSTED") is also transient — verified
    live against the free tier's per-model-per-minute cap (5 RPM for gemini-3.8-flash,
    which `gemini-flash-latest` currently resolves to): Google's own response includes
    a `RetryInfo.retryDelay`, this is a "slow down," not a hard failure. A 404/400/403
    (not verified, wrong model, access denied) is NOT transient and must fail fast —
    checking the numeric code rather than blanket-retrying every ClientError is what
    keeps a real access-denial (seen live once already this session) from being
    retried for a minute before finally surfacing."""
    from google.genai import errors as genai_errors

    if isinstance(exc, genai_errors.ServerError):
        return True
    return isinstance(exc, genai_errors.ClientError) and getattr(exc, "code", None) == 429


def _generate_with_retry(model: str, prompt: str, config):
    from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

    for attempt in Retrying(
        retry=retry_if_exception(_is_transient),
        wait=wait_exponential(multiplier=3, min=5, max=60),
        stop=stop_after_attempt(5),
        reraise=True,
    ):
        with attempt:
            return _client_instance().models.generate_content(model=model, contents=prompt, config=config)


def generate(
    session: Session,
    prompt: str,
    *,
    inputs: dict | None = None,
    grounding: bool = False,
    json_mode: bool = True,
    model: str = MODEL,
) -> dict | str:
    """Cached, quota-checked Gemini call.

    Returns the parsed JSON body when ``json_mode`` (the default), else the raw text.
    ``inputs`` are the prompt's variable parts, folded into the cache key alongside the
    prompt text — pass the actual topic/script/etc., not just a template name.
    """
    key = _cache_key(model, prompt, inputs or {})
    cached = session.get(LlmCache, key)
    if cached is not None:
        cached.hits += 1
        cached.last_used_at = datetime.now(UTC)
        return cached.response if json_mode else cached.response["text"]

    check_and_increment(session, "gemini", tokens=_estimate_tokens(prompt))

    from google.genai import types

    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())] if grounding else None,
        response_mime_type="application/json" if json_mode else "text/plain",
    )
    response = _generate_with_retry(model, prompt, config)

    parsed: dict = json.loads(response.text) if json_mode else {"text": response.text}
    usage = getattr(response, "usage_metadata", None)
    session.add(
        LlmCache(
            key=key,
            model=model,
            response=parsed,
            prompt_tokens=getattr(usage, "prompt_token_count", None) if usage else None,
            response_tokens=getattr(usage, "candidates_token_count", None) if usage else None,
            hits=0,
            last_used_at=datetime.now(UTC),
        )
    )
    return parsed if json_mode else parsed["text"]
