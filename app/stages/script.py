"""Script (README §4.1 step 5): the writer LLM turns the researched Wikipedia articles —
and only those — into 3–8 minutes of narration.

Inline markup, parsed later by software and never spoken:
- `[SHORT]...[/SHORT]` — the passages that become the Shorts (segment -> `short` rows)
- `[QUOTE]...[/QUOTE]` — direct historical quotations, read in the quote voice
"""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import llm
from app.config import get_config
from app.db import Video
from app.status import Status

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "script.md"

_SHORT_OPEN, _SHORT_CLOSE = "[SHORT]", "[/SHORT]"
_TAG = re.compile(r"\[/?(?:SHORT|QUOTE)\]")


def articles_block(research: dict) -> str:
    """The researched articles as one prompt block (shared with the check stage)."""
    return "\n\n".join(
        f"=== ARTICLE: {a['title']} ===\n{a['text']}" for a in (research or {}).get("articles", [])
    )


def word_count(script: str) -> int:
    return len(_TAG.sub(" ", script).split())


def validate_markup(script: str) -> None:
    """[SHORT] and [QUOTE] tags must be balanced and not nested within their own kind."""
    for tag in ("SHORT", "QUOTE"):
        depth = 0
        for match in re.finditer(rf"\[(/?){tag}\]", script):
            depth += -1 if match.group(1) else 1
            if depth not in (0, 1):
                raise ValueError(f"unbalanced or nested [{tag}] markup")
        if depth != 0:
            raise ValueError(f"unclosed [{tag}] markup")
    if _SHORT_OPEN not in script:
        raise ValueError("script has no [SHORT] spans")


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.RESEARCHED:
        return
    if not (video.research or {}).get("articles"):
        raise ValueError(f"video {video_id}: no researched articles to write from")

    config = get_config()
    long_form, wps = config.video.long_form, config.voice.words_per_second
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        topic=video.title,
        tone=config.channel.tone,
        articles=articles_block(video.research),
        words_per_second=wps,
        seconds_min=long_form.seconds_min,
        seconds_max=long_form.seconds_max,
        words_min=round(long_form.seconds_min * wps),
        words_max=round(long_form.seconds_max * wps),
        shorts_count=config.video.shorts.per_video,
    )

    script_text = llm.generate(
        session, prompt, role="writer", step="script", inputs={"video_id": str(video_id)}, json_mode=False
    )
    if not isinstance(script_text, str):
        raise ValueError(f"video {video_id}: writer returned no script")
    script_text = script_text.strip()
    try:
        validate_markup(script_text)
    except ValueError as exc:
        raise ValueError(f"video {video_id}: {exc}") from exc

    video.script = script_text
    video.script_prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    video.status = Status.SCRIPTED
