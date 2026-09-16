"""The one status vocabulary — used by DB columns, the dashboard, and the CLI.

Never write a bare status string anywhere else.
"""

from __future__ import annotations

from enum import StrEnum


class Status(StrEnum):
    # --- candidate lifecycle ---
    CANDIDATE_NEW = "candidate_new"
    CANDIDATE_VETOED = "candidate_vetoed"  # auto_veto matched
    CANDIDATE_MANUAL_REVIEW = "candidate_manual_review"
    CANDIDATE_APPROVED = "candidate_approved"
    CANDIDATE_REJECTED = "candidate_rejected"

    # --- video lifecycle (roughly in pipeline order) ---
    RESEARCHING = "researching"
    FACT_CHECKING = "fact_checking"
    SCRIPTING = "scripting"
    SEGMENTING = "segmenting"
    FETCHING_IMAGES = "fetching_images"
    SYNTHESIZING_VOICE = "synthesizing_voice"
    ALIGNING = "aligning"
    ASSEMBLING = "assembling"
    CUTTING_SHORTS = "cutting_shorts"
    GENERATING_METADATA = "generating_metadata"
    GENERATING_THUMBNAIL = "generating_thumbnail"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    FAILED = "failed"


#: Terminal states — no further work is enqueued.
TERMINAL: frozenset[Status] = frozenset(
    {
        Status.CANDIDATE_VETOED,
        Status.CANDIDATE_REJECTED,
        Status.REJECTED,
        Status.PUBLISHED,
        Status.FAILED,
    }
)

#: States where a human must act before the pipeline can proceed.
NEEDS_HUMAN: frozenset[Status] = frozenset({Status.CANDIDATE_MANUAL_REVIEW, Status.AWAITING_REVIEW})
