"""Gemini writes the script from verified research. Mark short-worthy spans inline with
`[SHORT]...[/SHORT]` — this markup is internal to Content's code only; segment.py is the
only other module that parses it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from sqlalchemy.orm import Session

from app import llm
from app.config import get_channel_config
from app.db import Video
from app.status import Status

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "script.md"


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.SCRIPTING:
        return

    config = get_channel_config(session, video.channel_id)
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        topic=video.title,
        research_json=json.dumps(video.research or {}, indent=2),
        target_seconds=config.video.long_form.target_seconds,
        words_per_second=config.voice.words_per_second,
        tone=config.channel.tone,
        shorts_count=config.video.shorts.per_long_form,
    )

    script_text = llm.generate(session, prompt, inputs={"video_id": str(video_id)}, json_mode=False)
    if not isinstance(script_text, str) or "[SHORT]" not in script_text:
        raise ValueError(f"video {video_id}: script has no [SHORT] spans")

    video.script = script_text
    video.script_prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    video.status = Status.SEGMENTING
