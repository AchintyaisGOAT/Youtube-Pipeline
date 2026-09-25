"""Apply config's auto_veto / manual_review topic lists to one new candidate row
(DESIGN.md #44 — two-tier content-safety gate, explicitly flagged P0: "not a rubber
stamp"). One rule is resolved for free by a deterministic regex: "events within the
last N years" against any 4-digit year in the candidate's text — a real article's title
never literally contains that sentence, so string-matching the phrase itself would
never catch it. Everything else genuinely needs judgment (e.g. "still-actively-disputed
historical framing" can't be substring-matched), so it goes through a Gemini
classification against the full lists — this is DESIGN.md's `sensitivity_check.md`.
Anything the model doesn't return a clean verdict for defaults to manual_review, per
DESIGN.md #44 ("anything uncertain -> manual_review").
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app import llm
from app.config import get_channel_config
from app.db import Candidate
from app.status import Status

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "sensitivity_check.md"

_RECENCY_RULE = re.compile(r"events? within the last (\d+) years?", re.IGNORECASE)
_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")


def _recency_veto(text: str, auto_veto: list[str]) -> str | None:
    current_year = datetime.now(UTC).year
    years = [int(y) for y in _YEAR.findall(text)]
    for phrase in auto_veto:
        rule = _RECENCY_RULE.search(phrase)
        if rule and any(current_year - year <= int(rule.group(1)) for year in years):
            return phrase
    return None


def run(session: Session, candidate_id: uuid.UUID) -> None:
    candidate = session.get(Candidate, candidate_id)
    if candidate is None or Status(candidate.status) != Status.CANDIDATE_NEW:
        return

    config = get_channel_config(session, candidate.channel_id)
    haystack = f"{candidate.title} {candidate.summary or ''}"
    now = datetime.now(UTC)

    veto_hit = _recency_veto(haystack, config.topics.auto_veto)
    if veto_hit:
        candidate.status = Status.CANDIDATE_VETOED
        candidate.rationale = f"auto_veto: {veto_hit!r}"
        candidate.decided_by = "gate"
        candidate.decided_at = now
        return

    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        title=candidate.title,
        summary=candidate.summary or "(no summary)",
        auto_veto="\n".join(f"- {p}" for p in config.topics.auto_veto),
        manual_review="\n".join(f"- {p}" for p in config.topics.manual_review),
    )
    result = llm.generate(session, prompt, inputs={"candidate_id": str(candidate_id)})
    verdict = result.get("verdict")
    reason = result.get("reason", "")

    if verdict == "veto":
        candidate.status = Status.CANDIDATE_VETOED
    elif verdict == "pass":
        candidate.status = Status.CANDIDATE_APPROVED
    else:
        # "manual_review", or anything unrecognized -> manual_review (DESIGN.md #44)
        candidate.status = Status.CANDIDATE_MANUAL_REVIEW
        if verdict not in ("manual_review",):
            reason = reason or f"unrecognized verdict {verdict!r} from sensitivity check"

    candidate.rationale = reason
    candidate.decided_by = "gate"
    candidate.decided_at = now
