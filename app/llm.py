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

MODEL = "gemini-2.5-flash"

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
    response = _client_instance().models.generate_content(model=model, contents=prompt, config=config)

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
