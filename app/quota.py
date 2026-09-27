"""Daily API budget enforcement against `api_quota_ledger`.

Call ``check_and_increment()`` before every metered call (YouTube upload units, Gemini
requests/tokens). It raises ``QuotaExceeded`` instead of proceeding once today's budget
for that API is spent. ``QuotaExceeded`` is a ``RetryLater``: the orchestrator leaves the
row's status untouched and picks it up again on a later run, rather than failing it.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import ApiQuotaLedger


class RetryLater(Exception):
    """A temporary condition (quota spent, provider rate-limited/overloaded). The stage
    should be retried on a later run — never treated as a permanent failure."""


class QuotaExceeded(RetryLater):
    """Raised when a call would push today's usage for an API past its daily cap."""


#: Conservative daily caps per external API, keyed by counter. `units` is YouTube quota
#: units for "youtube" and the request count for the Gemini APIs. A missing API or
#: counter means "counted, but no local cap" — the provider's own 429 still applies and
#: surfaces as RetryLater via app/llm.py. Set Gemini request caps once the real free-tier
#: limits for the pinned models are known.
DAILY_LIMITS: dict[str, dict[str, int]] = {
    "youtube": {"units": 10_000},
    "gemini": {"tokens": 1_000_000},
    "gemini_image": {},
}


def check_and_increment(session: Session, api: str, units: int = 0, tokens: int = 0) -> None:
    today = date.today()
    row = session.execute(select(ApiQuotaLedger).filter_by(api=api, day=today)).scalar_one_or_none()
    used_units = row.units_used if row else 0
    used_tokens = row.tokens_used if row else 0

    limits = DAILY_LIMITS.get(api, {})
    unit_cap, token_cap = limits.get("units"), limits.get("tokens")
    if units and unit_cap is not None and used_units + units > unit_cap:
        raise QuotaExceeded(f"{api} daily unit budget exhausted ({used_units + units} > {unit_cap})")
    if tokens and token_cap is not None and used_tokens + tokens > token_cap:
        raise QuotaExceeded(f"{api} daily token budget exhausted ({used_tokens + tokens} > {token_cap})")

    if row is None:
        session.add(ApiQuotaLedger(api=api, day=today, units_used=units, tokens_used=tokens))
    else:
        row.units_used = used_units + units
        row.tokens_used = used_tokens + tokens
