"""Daily API budget enforcement against `api_quota_ledger`.

Call ``check_and_increment()`` before every metered call (YouTube upload units, Gemini
tokens). It raises ``QuotaExceeded`` instead of proceeding once today's budget for that
API is spent — the orchestrator treats that as "retry later", not a hard failure.
"""

from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import ApiQuotaLedger

#: Conservative daily caps per external API (DESIGN.md §3.1 / §6E). Tune once real usage
#: is known; a missing entry means "no cap enforced".
DAILY_LIMITS: dict[str, int] = {
    "youtube": 10_000,  # quota units/day
    "gemini": 1_000_000,  # tokens/day, free-tier ballpark
}


class QuotaExceeded(Exception):
    """Raised when a call would push today's usage for an API past its daily cap."""


def check_and_increment(session: Session, api: str, units: int = 0, tokens: int = 0) -> None:
    today = date.today()
    row = session.execute(select(ApiQuotaLedger).filter_by(api=api, day=today)).scalar_one_or_none()
    used_units = row.units_used if row else 0
    used_tokens = row.tokens_used if row else 0

    limit = DAILY_LIMITS.get(api)
    if limit is not None:
        if units and used_units + units > limit:
            raise QuotaExceeded(f"{api} daily unit budget exhausted ({used_units + units} > {limit})")
        if tokens and used_tokens + tokens > limit:
            raise QuotaExceeded(f"{api} daily token budget exhausted ({used_tokens + tokens} > {limit})")

    if row is None:
        session.add(ApiQuotaLedger(api=api, day=today, units_used=units, tokens_used=tokens))
    else:
        row.units_used = used_units + units
        row.tokens_used = used_tokens + tokens
