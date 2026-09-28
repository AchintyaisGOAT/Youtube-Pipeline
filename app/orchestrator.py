"""`ogh run` — one sequential pass over the whole pipeline (README §4.1). No queue, no
worker, no scheduler: you run it when you choose, and it's safe to stop and restart.

1. discover   — only when no topic is waiting (app/stages/discover.py)
2. gate       — every `candidate` topic, in batched LLM calls
3. rank       — starts a video from the best passed topic, if none is in progress
4. videos     — each non-terminal video is walked through as many stages as it can go,
                until it reaches review, fails, or is deferred

A stage module is looked up by the video's current `Status` and exposes
``run(session, video_id)`` (discover/rank take no id). No `session.commit()` inside a
stage — every stage call gets its own session here, committed on success.

Outcomes of a stage call:
- success          -> committed; a video moves straight on to its next stage
- ``RetryLater``   -> (quota spent, provider rate-limited/overloaded) committed as-is
                      (keeps partial progress + quota accounting) with the status left
                      unchanged, so the next run resumes it; never a failure
- any other error  -> rolled back; the video is marked `failed` (topic: `vetoed`) with
                      the error saved, and an alert sent

A stage that isn't implemented yet is skipped: the video waits at that status.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Callable
from enum import Enum

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import notify
from app.db import Topic, Video, get_sessionmaker
from app.quota import RetryLater
from app.status import TERMINAL, Status, TopicStatus

DISCOVER_STAGE = "app.stages.discover"
GATE_STAGE = "app.stages.gate"
RANK_STAGE = "app.stages.rank"

#: video status -> the stage module that moves it on (README §4.2)
VIDEO_STAGES: dict[Status, str] = {
    Status.SELECTED: "app.stages.research",
    Status.RESEARCHED: "app.stages.script",
    Status.SCRIPTED: "app.stages.check",
    Status.CHECKED: "app.stages.segment",
    Status.SEGMENTED: "app.stages.images",
    Status.IMAGES_READY: "app.stages.narrate",
    Status.NARRATED: "app.stages.align",
    Status.ALIGNED: "app.stages.assemble",
    Status.ASSEMBLED: "app.stages.shorts",
    Status.SHORTS_READY: "app.stages.package",
}


class Outcome(Enum):
    DONE = "done"
    DEFERRED = "deferred"
    FAILED = "failed"


def _load_stage(module_path: str):
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError as exc:
        if exc.name != module_path:
            raise  # the module exists but one of *its* imports is missing — a real error
        logger.debug("{} not implemented yet, skipping", module_path)
        return None


def _attempt(label: str, work: Callable[[Session], None]) -> tuple[Outcome, Exception | None]:
    """Run `work` in its own session, committing or rolling back per the module docstring."""
    session = get_sessionmaker()()
    try:
        work(session)
        session.commit()
        return Outcome.DONE, None
    except RetryLater as exc:
        logger.warning("{} deferred to a later run: {}", label, exc)
        session.commit()
        return Outcome.DEFERRED, exc
    except Exception as exc:
        session.rollback()
        logger.exception("{} failed", label)
        return Outcome.FAILED, exc
    finally:
        session.close()


def _run_singleton(module_path: str, **kwargs) -> None:
    module = _load_stage(module_path)
    if module is None:
        return
    outcome, _ = _attempt(module_path, lambda session: module.run(session, **kwargs))
    if outcome is Outcome.FAILED:
        notify.alert(f"{module_path} failed")


def _mark(model, row_id: uuid.UUID, status: str, error: Exception | None = None) -> None:
    session = get_sessionmaker()()
    try:
        row = session.get(model, row_id)
        row.status = status
        if error is not None:
            if hasattr(row, "error"):
                row.error = repr(error)
            elif hasattr(row, "rationale"):
                row.rationale = f"gate error: {error!r}"
        session.commit()
    finally:
        session.close()


def _gate_topics() -> None:
    """One batched gate call per run. If the gate itself breaks (every model errored),
    the waiting candidates are vetoed with the reason — otherwise they'd sit in the pool
    forever and discovery (which waits for an empty pool) would never run again."""
    gate = _load_stage(GATE_STAGE)
    if gate is None:
        return
    outcome, error = _attempt("gate", gate.run)
    if outcome is not Outcome.FAILED:
        return
    with get_sessionmaker()() as session:
        pending = list(session.execute(select(Topic.id).filter_by(status=TopicStatus.CANDIDATE)).scalars())
    for topic_id in pending:
        _mark(Topic, topic_id, TopicStatus.VETOED, error)
    notify.alert(f"gate failed; {len(pending)} candidate topic(s) vetoed")


def _advance_video(video_id: uuid.UUID) -> None:
    """Walk one video through consecutive stages until it stops moving."""
    for _ in range(len(VIDEO_STAGES) + 1):  # bound: no video can advance more often than this
        with get_sessionmaker()() as session:
            status = Status(session.get(Video, video_id).status)
        module_path = VIDEO_STAGES.get(status)
        if module_path is None:
            return  # packaged / approved / terminal — waiting on you, or finished
        module = _load_stage(module_path)
        if module is None:
            return

        def work(session: Session, module=module, status=status) -> None:
            video = session.get(Video, video_id)
            try:
                module.run(session, video_id)
            except RetryLater:
                video.status = status  # a stage may have moved it mid-flight; park it where it was
                raise

        outcome, error = _attempt(f"video {video_id} at {status}", work)
        if outcome is Outcome.FAILED:
            _mark(Video, video_id, Status.FAILED, error)
            notify.alert(f"video {video_id} failed at {status}")
            return
        if outcome is Outcome.DEFERRED:
            return

        with get_sessionmaker()() as session:
            if Status(session.get(Video, video_id).status) == status:
                return  # the stage ran but didn't advance — don't spin on it


def _advance_videos() -> None:
    with get_sessionmaker()() as session:
        pending = [
            v.id for v in session.execute(select(Video)).scalars() if Status(v.status) not in TERMINAL
        ]
    for video_id in pending:
        _advance_video(video_id)


def run(*, force_discover: bool = False) -> None:
    _run_singleton(DISCOVER_STAGE, force=force_discover)
    _gate_topics()
    _run_singleton(RANK_STAGE)
    _advance_videos()
