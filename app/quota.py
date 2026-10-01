"""Daily free-tier budgets, counted from today's `llm_call` rows (README §5).

``check()`` runs before every provider call and raises a ``RetryLater`` when the call
can't be made today:
- ``QuotaExceeded``: today's local cap for the provider or the model is spent (caps:
  Groq's documented limits below, Gemini's per-model ones from `llm.daily_requests` in
  config.yaml — copied from aistudio.google.com/rate-limit), or the provider already said
  so this run (a daily-quota 429);
- ``ProviderBlocked``: the provider refused the whole project this run (Gemini's 402
  "prepayment credits are depleted", seen live on every model at once).
app/llm.py then tries the fallback model; if that's unavailable too, the orchestrator
leaves the video's status untouched and a later run resumes it — a limit never fails a video.

"Today" is the provider's own day: Gemini's daily quotas reset at midnight Pacific time,
Groq's are counted per UTC day.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import LlmCall


class RetryLater(Exception):
    """A temporary condition (quota spent, provider rate-limited/overloaded). The stage
    should be retried on a later run — never treated as a permanent failure."""


class QuotaExceeded(RetryLater):
    """Today's requests/tokens for a provider or model are spent."""


class ProviderBlocked(RetryLater):
    """The provider refuses every request from this project for now (e.g. Gemini's 402)."""


#: Local daily caps per provider. Groq's free tier (checked 2026-09-30 from its response
#: headers + docs): 1,000 requests and 200K tokens a day. Gemini's limits are per model
#: and only visible in AI Studio, so they come from config (`llm.daily_requests`).
DAILY_LIMITS: dict[str, dict[str, int]] = {
    "groq": {"requests": 1_000, "tokens": 200_000},
}
#: When each provider's day starts.
RESET_TZ = {"gemini": "America/Los_Angeles", "groq": "UTC"}

#: Outcomes that actually reached the provider (and so count as usage).
_COUNTED = ("ok", "error", "retry_later")

#: Refusals seen during this process (one `ogh run`): later calls skip straight past them.
_blocked: dict[str, str] = {}
_exhausted: dict[str, str] = {}


def block(provider: str, reason: str) -> bool:
    """Mark `provider` unusable for the rest of this run. True the first time."""
    first = provider not in _blocked
    _blocked[provider] = reason
    return first


def exhaust(model: str, reason: str) -> None:
    """Mark `model`'s daily quota as spent for the rest of this run."""
    _exhausted[model] = reason


def reset() -> None:
    """Forget this run's refusals (tests; a new process starts clean anyway)."""
    _blocked.clear()
    _exhausted.clear()


def day_start(provider: str, now: datetime | None = None) -> datetime:
    """Midnight of the provider's current day, as an aware datetime."""
    zone = ZoneInfo(RESET_TZ.get(provider, "UTC"))
    local = (now or datetime.now(zone)).astimezone(zone)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def usage_today(session: Session, provider: str, model: str | None = None) -> tuple[int, int]:
    """(requests, tokens) sent to `provider` (or just `model`) since its day started."""
    query = select(
        func.count(LlmCall.id),
        func.coalesce(func.sum(LlmCall.prompt_tokens), 0) + func.coalesce(func.sum(LlmCall.response_tokens), 0),
    ).where(LlmCall.provider == provider, LlmCall.outcome.in_(_COUNTED),
            LlmCall.created_at >= day_start(provider))
    if model is not None:
        query = query.where(LlmCall.model == model)
    requests, tokens = session.execute(query).one()
    return requests, tokens


def model_limits() -> dict[str, int]:
    from app.config import get_config  # config imports nothing from here; keep it lazy anyway

    return dict(get_config().llm.daily_requests)


def check(session: Session, provider: str, model: str | None = None) -> None:
    if provider in _blocked:
        raise ProviderBlocked(f"{provider} refused this project earlier this run: {_blocked[provider]}")
    if model in _exhausted:
        raise QuotaExceeded(f"{model}: daily quota spent ({_exhausted[model]})")
    limits = DAILY_LIMITS.get(provider)
    if limits:
        requests, tokens = usage_today(session, provider)
        if "requests" in limits and requests >= limits["requests"]:
            raise QuotaExceeded(f"{provider}: {requests} requests today (cap {limits['requests']})")
        if "tokens" in limits and tokens >= limits["tokens"]:
            raise QuotaExceeded(f"{provider}: {tokens} tokens today (cap {limits['tokens']})")
    cap = model_limits().get(model) if model else None
    if cap is not None:
        requests, _ = usage_today(session, provider, model)
        if requests >= cap:
            raise QuotaExceeded(f"{model}: {requests} requests today (cap {cap})")
