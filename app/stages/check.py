"""Check (README §4.1 step 6): a *different* model from the writer (config.llm.checker)
compares every claim in the script with the researched articles.

The checker returns only the sentences that need a fix — {original, replacement, reason}
— and this code applies them. It never hands back a whole rewritten script, so nothing
can change without being recorded. A change is rejected (kept in the record, not
applied) when its original can't be found verbatim, when it changes nothing, or when
applying it would break the [SHORT]/[QUOTE] markup. Applied and rejected changes are
kept in ``video.research["check"]`` for `ogh review`.

A checked script that lost more than 40% of its words means the check itself went
wrong, so the stage fails instead.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import llm
from app.db import Video
from app.stages.script import articles_block, validate_markup, word_count
from app.status import Status

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "check.md"

#: The checked script must keep at least this share of the original's words.
MIN_KEPT_WORDS = 0.6


def apply_changes(script: str, changes: list) -> tuple[str, list[dict], list[dict]]:
    """(checked script, applied changes, rejected changes-with-why)."""
    applied: list[dict] = []
    rejected: list[dict] = []
    for change in changes:
        if not isinstance(change, dict):
            continue
        original = str(change.get("original") or "").strip()
        replacement = str(change.get("replacement") or "").strip()
        record = {"original": original, "replacement": replacement, "reason": change.get("reason", "")}
        if not original or original not in script:
            rejected.append({**record, "why": "original sentence not found in the script"})
            continue
        if replacement == original:
            rejected.append({**record, "why": "no actual change"})
            continue
        candidate = re.sub(r"[ \t]{2,}", " ", script.replace(original, replacement, 1))
        try:
            validate_markup(candidate)
        except ValueError as exc:
            rejected.append({**record, "why": f"would break markup: {exc}"})
            continue
        script = candidate
        applied.append(record)
    return script.strip(), applied, rejected


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.SCRIPTED:
        return
    if not video.script:
        raise ValueError(f"video {video_id}: no script to check")

    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        articles=articles_block(video.research), script=video.script
    )
    result = llm.generate(session, prompt, role="checker", step="check", inputs={"video_id": str(video_id)})
    changes = result.get("changes")
    if not isinstance(changes, list):
        raise ValueError(f"video {video_id}: checker returned no change list")

    checked, applied, rejected = apply_changes(video.script, changes)
    before, after = word_count(video.script), word_count(checked)
    if after < before * MIN_KEPT_WORDS:
        raise ValueError(f"video {video_id}: checker cut the script from {before} to {after} words")

    video.research = {
        **video.research,
        "check": {"applied": applied, "rejected": rejected, "words_before": before, "words_after": after},
    }
    video.script = checked
    video.status = Status.CHECKED
