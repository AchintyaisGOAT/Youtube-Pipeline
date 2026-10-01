"""Schedule strings from config.yaml ("daily 12:00", "Tue 15:00", "weekly Mon 06:00").

Runs are manual (README §2), so nothing is triggered by these — they're the suggested
YouTube Studio publish slots written into each upload kit's checklist. Validated here so
a typo is caught when the config loads, not when a kit is written.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_SPEC = re.compile(
    r"^(?:daily|(?:weekly\s+)?(?:mon|tue|wed|thu|fri|sat|sun))\s+(?:[01]\d|2[0-3]):[0-5]\d$",
    re.IGNORECASE,
)


def validate(spec: str) -> str:
    if not _SPEC.match(spec.strip()):
        raise ValueError(f"schedule {spec!r} must look like 'daily HH:MM' or '[weekly] Mon HH:MM'")
    return spec


_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _parse(spec: str) -> tuple[set[int], int, int]:
    """(weekdays it fires on, hour, minute)."""
    words = spec.strip().lower().split()
    hour, minute = (int(n) for n in words[-1].split(":"))
    day = words[-2]
    return (set(range(7)) if day == "daily" else {_DAYS.index(day)}), hour, minute


def next_slot(specs: list[str], after: datetime, tz: str) -> datetime:
    """The first slot of any of `specs` strictly after `after`, as an aware datetime in `tz`.
    No specs = the day after `after`, same time."""
    zone = ZoneInfo(tz)
    local = after.astimezone(zone)
    if not specs:
        return local + timedelta(days=1)
    parsed = [_parse(spec) for spec in specs]
    for offset in range(8):
        day = (local + timedelta(days=offset)).date()
        times = sorted(datetime(day.year, day.month, day.day, h, m, tzinfo=zone)
                       for days, h, m in parsed if day.weekday() in days)
        for slot in times:
            if slot > local:
                return slot
    raise AssertionError("unreachable: every spec fires at least weekly")
