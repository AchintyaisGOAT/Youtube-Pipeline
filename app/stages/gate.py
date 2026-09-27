"""Gate (README §4.1 step 2, §4.3): one `candidate` topic -> `passed` or `vetoed`, plus a
0–10 niche-relevance score that rank uses.

One veto rule is resolved for free by a regex: "events within the last N years" against
any 4-digit year in the topic's text (a title never literally contains that sentence, so
the LLM would have to guess). Everything else — the rest of the auto-veto list and "is
this actually in scope" — needs judgment and goes to the LLM. Sensitive-but-historical
subjects pass; there is no manual-review tier. An unrecognized verdict is a veto.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app import llm
from app.config import get_config
from app.db import Topic
from app.status import TopicStatus

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "gate.md"

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


def run(session: Session, topic_id: uuid.UUID) -> None:
    topic = session.get(Topic, topic_id)
    if topic is None or TopicStatus(topic.status) != TopicStatus.CANDIDATE:
        return

    config = get_config()
    topic.decided_at = datetime.now(UTC)

    veto_hit = _recency_veto(f"{topic.title} {topic.summary or ''}", config.topics.auto_veto)
    if veto_hit:
        topic.status = TopicStatus.VETOED
        topic.rationale = f"auto_veto: {veto_hit!r}"
        return

    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        title=topic.title,
        summary=topic.summary or "(no summary)",
        in_scope="\n".join(f"- {p}" for p in config.topics.in_scope),
        auto_veto="\n".join(f"- {p}" for p in config.topics.auto_veto),
    )
    result = llm.generate(session, prompt, role="worker", step="gate", inputs={"topic_id": str(topic_id)})
    verdict = result.get("verdict")
    relevance = result.get("relevance")

    topic.status = TopicStatus.PASSED if verdict == "pass" else TopicStatus.VETOED
    topic.rationale = result.get("reason") or f"unrecognized verdict {verdict!r}"
    topic.raw = {**(topic.raw or {}), "relevance": relevance if isinstance(relevance, int | float) else 0}
