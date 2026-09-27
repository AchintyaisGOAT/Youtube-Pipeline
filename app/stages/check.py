"""Check (README §4.1 step 6): a *different* model from the writer (config.llm.checker)
compares every claim in the script with the researched articles and rewrites or removes
the unsupported ones. The list of changes is kept in ``video.research["check"]`` so
`ogh review` can show what the writer got wrong.

Guards against a checker that mangles the script instead of fixing it: the markup must
still be valid, and the checked script can't lose more than 40% of its words — either
means the check itself failed, not the script.
"""

from __future__ import annotations

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

    checked = (result.get("script") or "").strip()
    try:
        validate_markup(checked)
    except ValueError as exc:
        raise ValueError(f"video {video_id}: checked script is broken ({exc})") from exc
    before, after = word_count(video.script), word_count(checked)
    if after < before * MIN_KEPT_WORDS:
        raise ValueError(f"video {video_id}: checker cut the script from {before} to {after} words")

    changes = [c for c in result.get("changes", []) if isinstance(c, dict)]
    video.research = {**video.research, "check": {"changes": changes, "words_before": before, "words_after": after}}
    video.script = checked
    video.status = Status.CHECKED
