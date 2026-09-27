"""Schedule strings from config.yaml ("weekly Mon 06:00", "daily 12:00", "Tue 15:00"),
evaluated in the channel's configured timezone.

There's no daemon: Task Scheduler just runs the pipeline periodically, and a scheduled
job is "due" when its most recent scheduled occurrence is newer than the last time it
actually ran. A missed slot (machine off) therefore runs on the next tick, once.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_SPEC = re.compile(
    r"^(?:(?P<daily>daily)|(?:weekly\s+)?(?P<day>mon|tue|wed|thu|fri|sat|sun))\s+"
    r"(?P<hh>[01]\d|2[0-3]):(?P<mm>[0-5]\d)$",
    re.IGNORECASE,
)


def validate(spec: str) -> str:
    if not _SPEC.match(spec.strip()):
        raise ValueError(f"schedule {spec!r} must look like 'daily HH:MM' or '[weekly] Mon HH:MM'")
    return spec


def last_occurrence(spec: str, now: datetime, timezone: str) -> datetime:
    """The most recent scheduled time at or before `now` (an aware datetime)."""
    match = _SPEC.match(spec.strip())
    if match is None:
        raise ValueError(f"invalid schedule {spec!r}")
    local_now = now.astimezone(ZoneInfo(timezone))
    slot = local_now.replace(hour=int(match["hh"]), minute=int(match["mm"]), second=0, microsecond=0)
    if match["daily"]:
        return slot if slot <= local_now else slot - timedelta(days=1)
    slot -= timedelta(days=(local_now.weekday() - _DAYS.index(match["day"].lower())) % 7)
    return slot if slot <= local_now else slot - timedelta(days=7)


def is_due(spec: str, last_run: datetime | None, now: datetime, timezone: str) -> bool:
    return last_run is None or last_run < last_occurrence(spec, now, timezone)
