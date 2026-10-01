"""The one status vocabulary (README §4.2) — used by DB columns, the orchestrator and the
CLI. Video statuses name what is *done*: a video at `researched` is waiting for the
script stage. Never write a bare status string anywhere else.
"""

from __future__ import annotations

from enum import StrEnum


class TopicStatus(StrEnum):
    CANDIDATE = "candidate"  # discovered, not gated yet
    PASSED = "passed"  # cleared the gate, may be picked by rank
    VETOED = "vetoed"  # auto-veto list matched (or the gate itself failed on it)
    USED = "used"  # rank turned it into a video


class Status(StrEnum):
    """Video lifecycle, in pipeline order."""

    SELECTED = "selected"
    RESEARCHED = "researched"
    PICTURED = "pictured"  # the image inventory is gathered and checked; the script is written around it
    SCRIPTED = "scripted"
    CHECKED = "checked"
    SEGMENTED = "segmented"  # scenes cut at the script's image marks, each with its image
    NARRATED = "narrated"
    ALIGNED = "aligned"
    ASSEMBLED = "assembled"
    SHORTS_READY = "shorts_ready"
    PACKAGED = "packaged"  # upload kit built — waiting for `ogh review`
    APPROVED = "approved"  # you approved it — waiting for your manual upload
    UPLOADED = "uploaded"
    REJECTED = "rejected"
    FAILED = "failed"


#: Videos no stage will ever pick up again (a `failed` one can be sent back via review).
TERMINAL: frozenset[Status] = frozenset({Status.UPLOADED, Status.REJECTED, Status.FAILED})
