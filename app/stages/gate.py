"""Gate (README §4.1 step 2, §4.3): every `candidate` topic -> `passed` or `vetoed`, plus
a 0–10 niche-relevance score that rank uses. A "pass" below `discovery.min_relevance`
is vetoed, so weak fits don't clog the pool.

Batched: up to 20 candidates go to the worker LLM in one call (a discovery run yields
~20), instead of one call each. Verdicts come back keyed by the candidate's number, so a
reordered or partial response can't pin a verdict on the wrong topic; a candidate the
model skipped is vetoed rather than left to block discovery.

One veto rule is resolved for free first: "events within the last N years" against any
4-digit year in the topic's text (a title never literally contains that sentence).
Sensitive-but-historical subjects pass; there is no manual-review tier. Takes no id —
the orchestrator calls it once per run.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import llm
from app.config import get_config
from app.db import Topic
from app.status import TopicStatus

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "gate.md"

#: Keeps one prompt well inside Groq's 8K tokens/minute (README §5).
BATCH_SIZE = 20

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


def _line(number: int, topic: Topic) -> str:
    description = (topic.raw or {}).get("description") or "no description"
    summary = " ".join((topic.summary or "").split())[:300]
    return f"{number}. {topic.title} — {description} — {summary or 'no summary'}"


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _judge(session: Session, batch: list[Topic]) -> None:
    config = get_config()
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        in_scope="\n".join(f"- {p}" for p in config.topics.in_scope),
        auto_veto="\n".join(f"- {p}" for p in config.topics.auto_veto),
        candidates="\n".join(_line(i, t) for i, t in enumerate(batch, start=1)),
    )
    result = llm.generate(
        session, prompt, role="worker", step="gate", inputs={"topics": [str(t.id) for t in batch]}
    )
    by_number = {_as_int(r.get("id")): r for r in result.get("results", []) if isinstance(r, dict)}

    for number, topic in enumerate(batch, start=1):
        verdict = by_number.get(number)
        if verdict is None:
            topic.status = TopicStatus.VETOED
            topic.rationale = "gate returned no verdict for this topic"
            continue
        relevance = verdict.get("relevance")
        relevance = relevance if isinstance(relevance, int | float) else 0
        passed = verdict.get("verdict") == "pass" and relevance >= config.discovery.min_relevance
        topic.status = TopicStatus.PASSED if passed else TopicStatus.VETOED
        topic.rationale = verdict.get("reason") or f"verdict {verdict.get('verdict')!r}"
        if verdict.get("verdict") == "pass" and not passed:
            topic.rationale = f"relevance {relevance} below {config.discovery.min_relevance}: {topic.rationale}"
        topic.raw = {**(topic.raw or {}), "relevance": relevance}


def run(session: Session) -> None:
    config = get_config()
    now = datetime.now(UTC)
    pending = list(session.execute(select(Topic).filter_by(status=TopicStatus.CANDIDATE)).scalars())

    to_judge: list[Topic] = []
    for topic in pending:
        topic.decided_at = now
        text = f"{topic.title} {(topic.raw or {}).get('description', '')} {topic.summary or ''}"
        veto_hit = _recency_veto(text, config.topics.auto_veto)
        if veto_hit:
            topic.status = TopicStatus.VETOED
            topic.rationale = f"auto_veto: {veto_hit!r}"
        else:
            to_judge.append(topic)

    for start in range(0, len(to_judge), BATCH_SIZE):
        _judge(session, to_judge[start : start + BATCH_SIZE])
