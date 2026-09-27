"""One interface over Gemini + Groq (README §5). Stages ask for a *role*, never a model:

    writer   script, metadata          (config.llm.writer  -> writer_fallback)
    checker  script vs articles        (config.llm.checker, no fallback)
    worker   gate, scene plan          (config.llm.worker  -> worker_fallback)
    image    AI illustration fallback  (config.llm.image)

The provider is inferred from the model ID: `gemini-*` goes to Gemini (`google-genai`),
anything else to Groq's OpenAI-compatible endpoint (`openai` SDK).

For each role the primary model is tried, then the fallback. A model that's rate-limited
or overloaded raises ``RetryLater`` internally; any other failure (bad model ID, bad JSON,
request too large for Groq's 8K tokens/min) is logged and the fallback tried. If any
model in the chain was merely unavailable, ``RetryLater`` reaches the orchestrator and the
video waits for the next run; only when every model really failed is the error raised.

Every attempt is written to `llm_call`; text responses are cached in `llm_cache` so
re-runs during development cost nothing. Provider SDKs are imported lazily, so stages
stay importable and testable without network access.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Literal

from loguru import logger
from sqlalchemy.orm import Session

from app import quota
from app.config import get_config, get_settings
from app.db import LlmCache, LlmCall
from app.quota import RetryLater

Role = Literal["writer", "checker", "worker"]

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

_gemini_client = None
_groq_client = None


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #
def provider_of(model: str) -> str:
    return "gemini" if model.startswith("gemini") else "groq"


def models_for(role: Role) -> list[str]:
    llm = get_config().llm
    chain = {
        "writer": [llm.writer, llm.writer_fallback],
        "checker": [llm.checker],
        "worker": [llm.worker, llm.worker_fallback],
    }[role]
    return [m for m in chain if m]


# --------------------------------------------------------------------------- #
# Gemini
# --------------------------------------------------------------------------- #
def _gemini():
    global _gemini_client
    if _gemini_client is None:
        from google import genai

        _gemini_client = genai.Client(api_key=get_settings().gemini_api_key)
    return _gemini_client


def _is_transient(exc: BaseException) -> bool:
    """A 503 ("high demand") is transient per Google's own error message — verified live
    that a newly launched Flash model can 503 for minutes under load. A 429
    (RESOURCE_EXHAUSTED) is a "slow down" too — Google's response carries a retryDelay.
    A 404/400/403 (retired model, bad request, access denied) is NOT transient and must
    fail fast rather than be retried for minutes."""
    from google.genai import errors as genai_errors

    if isinstance(exc, genai_errors.ServerError):
        return True
    return isinstance(exc, genai_errors.ClientError) and getattr(exc, "code", None) == 429


def _generate_with_retry(model: str, prompt: str, config):
    """Retries transient Gemini errors in-process for a few minutes; if they're still
    failing after that, raises ``RetryLater`` — a spent daily quota won't clear in minutes."""
    from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential

    try:
        for attempt in Retrying(
            retry=retry_if_exception(_is_transient),
            wait=wait_exponential(multiplier=3, min=5, max=60),
            stop=stop_after_attempt(5),
            reraise=True,
        ):
            with attempt:
                return _gemini().models.generate_content(model=model, contents=prompt, config=config)
    except Exception as exc:
        if _is_transient(exc):
            raise RetryLater(f"{model} still unavailable after retries: {exc}") from exc
        raise


def _gemini_config(**kwargs):
    """No tools are ever passed, so automatic function calling is switched off — left on,
    google-genai logs a warning on every call (seen live)."""
    from google.genai import types

    return types.GenerateContentConfig(
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True), **kwargs
    )


def _call_gemini(model: str, prompt: str, json_mode: bool) -> tuple[str, int | None, int | None]:
    config = _gemini_config(response_mime_type="application/json" if json_mode else "text/plain")
    response = _generate_with_retry(model, prompt, config)
    usage = getattr(response, "usage_metadata", None)
    return (
        response.text or "",
        getattr(usage, "prompt_token_count", None),
        getattr(usage, "candidates_token_count", None),
    )


# --------------------------------------------------------------------------- #
# Groq
# --------------------------------------------------------------------------- #
def _groq():
    global _groq_client
    if _groq_client is None:
        from openai import OpenAI

        # The SDK itself retries 429s honoring Groq's retry-after header — that's what
        # waits out the 8K tokens/minute cap between long prompts (README §5).
        _groq_client = OpenAI(
            api_key=get_settings().groq_api_key, base_url=GROQ_BASE_URL, max_retries=4, timeout=180
        )
    return _groq_client


def _call_groq(model: str, prompt: str, json_mode: bool) -> tuple[str, int | None, int | None]:
    import openai

    kwargs: dict = {}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if model.startswith("openai/gpt-oss"):
        kwargs["reasoning_effort"] = "low"  # hidden reasoning tokens count against the TPM cap
    try:
        response = _groq().chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}], **kwargs
        )
    except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
            openai.InternalServerError) as exc:
        raise RetryLater(f"{model} unavailable: {exc}") from exc
    usage = response.usage
    return (
        response.choices[0].message.content or "",
        getattr(usage, "prompt_tokens", None),
        getattr(usage, "completion_tokens", None),
    )


def _call(model: str, prompt: str, json_mode: bool) -> tuple[str, int | None, int | None]:
    if provider_of(model) == "gemini":
        return _call_gemini(model, prompt, json_mode)
    return _call_groq(model, prompt, json_mode)


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _parse_json(text: str) -> dict:
    """JSON mode is requested from both providers, but tolerate a ```json fence anyway."""
    parsed = json.loads(_FENCE.sub("", text.strip()))
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _cache_key(role: str, prompt: str, inputs: dict) -> str:
    payload = json.dumps({"role": role, "prompt": prompt, "inputs": inputs}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _log(session: Session, model: str, step: str, outcome: str, *, tokens=(None, None), error=None) -> None:
    session.add(
        LlmCall(
            provider=provider_of(model),
            model=model,
            step=step,
            outcome=outcome,
            prompt_tokens=tokens[0],
            response_tokens=tokens[1],
            error=str(error)[:2000] if error is not None else None,
        )
    )


def generate(
    session: Session,
    prompt: str,
    *,
    role: Role,
    step: str,
    inputs: dict | None = None,
    json_mode: bool = True,
) -> dict | str:
    """Cached, quota-checked call for `role`, with fallback. Returns the parsed JSON object
    when ``json_mode`` (the default), else the raw text. ``inputs`` are the prompt's
    variable parts (topic/video id etc.), folded into the cache key alongside the prompt."""
    key = _cache_key(role, prompt, inputs or {})
    cached = session.get(LlmCache, key)
    if cached is not None:
        cached.hits += 1
        cached.last_used_at = datetime.now(UTC)
        return cached.response if json_mode else cached.response["text"]

    deferred: RetryLater | None = None
    failed: Exception | None = None
    for model in models_for(role):
        provider = provider_of(model)
        try:
            quota.check(session, provider)
        except RetryLater as exc:
            _log(session, model, step, "over_budget", error=exc)
            deferred = exc
            continue
        try:
            text, prompt_tokens, response_tokens = _call(model, prompt, json_mode)
            response = _parse_json(text) if json_mode else {"text": text}
        except RetryLater as exc:
            _log(session, model, step, "retry_later", error=exc)
            deferred = exc
            continue
        except Exception as exc:
            logger.warning("{} ({}) failed for step {}: {}", model, provider, step, exc)
            _log(session, model, step, "error", error=exc)
            failed = exc
            continue

        _log(session, model, step, "ok", tokens=(prompt_tokens, response_tokens))
        session.add(
            LlmCache(
                key=key,
                model=model,
                response=response,
                prompt_tokens=prompt_tokens,
                response_tokens=response_tokens,
                hits=0,
                last_used_at=datetime.now(UTC),
            )
        )
        return response if json_mode else response["text"]

    # A model that's only unavailable may work next run, so waiting beats failing the
    # video — even if another model in the chain genuinely errored (that's in llm_call).
    if deferred is not None:
        raise deferred
    raise failed or RetryLater(f"no model configured for role {role!r}")


def _extract_image(response) -> tuple[bytes, str] | None:
    content = response.candidates[0].content if response.candidates else None
    parts = content.parts if content else None
    if not parts:
        return None
    for part in parts:
        if part.inline_data is not None:
            return part.inline_data.data, part.inline_data.mime_type
    return None


def generate_image(session: Session, prompt: str, *, attempts: int = 4) -> tuple[bytes, str]:
    """AI illustration (README §6.1 fallback). Returns (image_bytes, mime_type).

    Verified live that the image model doesn't reliably return an image for the
    identical prompt — observed an empty response and a text-only description from
    back-to-back calls — so it retries a few times before raising ValueError. Results
    aren't cached here (bytes, not JSON); the images stage caches files by prompt hash.
    """
    model = get_config().llm.image
    for _ in range(attempts):
        quota.check(session, "gemini")
        try:
            response = _generate_with_retry(model, prompt, _gemini_config())
        except RetryLater as exc:
            _log(session, model, "image", "retry_later", error=exc)
            raise
        image = _extract_image(response)
        _log(session, model, "image", "ok" if image else "error", error=None if image else "no image returned")
        if image is not None:
            return image
    raise ValueError(f"{model} returned no image after {attempts} attempts for prompt: {prompt[:200]!r}")
