"""Daily free-tier budgets, counted from today's `llm_call` rows (README §5).

``check()`` runs before every provider call and raises ``QuotaExceeded`` once today's
local cap for that provider is spent. ``QuotaExceeded`` is a ``RetryLater``: app/llm.py
tries the fallback model, and if that's unavailable too the orchestrator leaves the
video's status untouched and the next run resumes it — a limit never fails a video.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import LlmCall


class RetryLater(Exception):
    """A temporary condition (quota spent, provider rate-limited/overloaded). The stage
    should be retried on a later run — never treated as a permanent failure."""


class QuotaExceeded(RetryLater):
    """Raised when a call would push today's usage for a provider past its local cap."""


#: Local daily caps per provider (UTC day). A missing entry means "counted, no local cap"
#: — the provider's own 429 still applies and surfaces as RetryLater via app/llm.py.
#: Groq's free-tier numbers are documented (README §5); Gemini's per-model limits are
#: only visible in the AI Studio dashboard, so they're left to the provider.
DAILY_LIMITS: dict[str, dict[str, int]] = {
    "groq": {"requests": 1_000, "tokens": 200_000},
}

#: Outcomes that actually reached the provider (and so count as usage).
_COUNTED = ("ok", "error", "retry_later")


def usage_today(session: Session, provider: str) -> tuple[int, int]:
    """(requests, tokens) sent to `provider` since 00:00 UTC."""
    start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    requests, tokens = session.execute(
        select(
            func.count(LlmCall.id),
            func.coalesce(func.sum(LlmCall.prompt_tokens), 0)
            + func.coalesce(func.sum(LlmCall.response_tokens), 0),
        ).where(
            LlmCall.provider == provider,
            LlmCall.outcome.in_(_COUNTED),
            LlmCall.created_at >= start,
        )
    ).one()
    return requests, tokens


def check(session: Session, provider: str) -> None:
    limits = DAILY_LIMITS.get(provider)
    if not limits:
        return
    requests, tokens = usage_today(session, provider)
    if "requests" in limits and requests >= limits["requests"]:
        raise QuotaExceeded(f"{provider}: {requests} requests today (cap {limits['requests']})")
    if "tokens" in limits and tokens >= limits["tokens"]:
        raise QuotaExceeded(f"{provider}: {tokens} tokens today (cap {limits['tokens']})")
