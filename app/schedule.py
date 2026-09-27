"""Schedule strings from config.yaml ("daily 12:00", "Tue 15:00", "weekly Mon 06:00").

Runs are manual (README §2), so nothing is triggered by these — they're the suggested
YouTube Studio publish slots written into each upload kit's checklist. Validated here so
a typo is caught when the config loads, not when a kit is written.
"""

from __future__ import annotations

import re

_SPEC = re.compile(
    r"^(?:daily|(?:weekly\s+)?(?:mon|tue|wed|thu|fri|sat|sun))\s+(?:[01]\d|2[0-3]):[0-5]\d$",
    re.IGNORECASE,
)


def validate(spec: str) -> str:
    if not _SPEC.match(spec.strip()):
        raise ValueError(f"schedule {spec!r} must look like 'daily HH:MM' or '[weekly] Mon HH:MM'")
    return spec
