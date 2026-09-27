"""Sequential pipeline orchestrator — no queue, no worker process.

Run manually or from a Windows Task Scheduler job. One run:

1. discover   — only fetches when `discovery.run` is due (see app/pipeline/discover.py)
2. gate       — every `candidate_new` row
3. rank       — promotes at most one approved candidate to a video
4. videos     — each non-terminal video is walked through as many stages as it can go
                in this run, until it reaches a human gate, fails, or is deferred
5. analytics  — due 48h/7d snapshots for published uploads

A stage module is looked up by the row's current `Status` and exposes
``run(session, row_id)`` (discover/rank/analytics take no id). No `session.commit()`
inside a stage — every stage call gets its own session here, committed on success.

Outcomes of a stage call:
- success          -> committed; a video moves straight on to its next stage
- ``RetryLater``   -> (quota spent, provider rate-limited/overloaded) committed as-is
                      (keeps partial progress + quota accounting) with the row's status
                      left unchanged, so a later run resumes it; never a failure
- any other error  -> rolled back; the row is marked failed/rejected and an alert sent

A stage that isn't implemented yet is skipped, not fatal.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import uuid
from collections.abc import Callable
from enum import Enum

from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import notify
from app.db import Candidate, Video, get_sessionmaker
from app.quota import RetryLater
from app.status import TERMINAL, Status

GATE_STAGE = "app.pipeline.gate"
DISCOVER_STAGE = "app.pipeline.discover"
RANK_STAGE = "app.pipeline.rank"
ANALYTICS_STAGE = "app.pipeline.analytics"

#: video-stage status -> module (takes video_id)
VIDEO_STAGES: dict[Status, str] = {
    Status.RESEARCHING: "app.pipeline.research",
    Status.FACT_CHECKING: "app.pipeline.factcheck",
    Status.SCRIPTING: "app.pipeline.script",
    Status.SEGMENTING: "app.pipeline.segment",
    Status.FETCHING_IMAGES: "app.pipeline.images",
    Status.SYNTHESIZING_VOICE: "app.pipeline.tts",
    Status.ALIGNING: "app.pipeline.align",
    Status.ASSEMBLING: "app.pipeline.assemble",
    Status.CUTTING_SHORTS: "app.pipeline.shorts",
    Status.GENERATING_METADATA: "app.pipeline.metadata",
    Status.GENERATING_THUMBNAIL: "app.pipeline.thumbnail",
    Status.APPROVED: "app.pipeline.upload",
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


def _mark(model, row_id: uuid.UUID, status: Status, error: Exception | None = None) -> None:
    session = get_sessionmaker()()
    try:
        row = session.get(model, row_id)
        row.status = status
        if error is not None and hasattr(row, "error"):
            row.error = repr(error)
        session.commit()
    finally:
        session.close()


def _gate_candidates() -> None:
    gate = _load_stage(GATE_STAGE)
    if gate is None:
        return
    with get_sessionmaker()() as session:
        pending = list(
            session.execute(select(Candidate.id).filter_by(status=Status.CANDIDATE_NEW)).scalars()
        )

    for candidate_id in pending:
        outcome, _ = _attempt(f"candidate {candidate_id} gate", lambda s, c=candidate_id: gate.run(s, c))
        if outcome is Outcome.DEFERRED:
            break  # quota/provider trouble hits every remaining candidate the same way
        if outcome is Outcome.FAILED:
            _mark(Candidate, candidate_id, Status.CANDIDATE_REJECTED)
            notify.alert(f"candidate {candidate_id} failed at gate")


def _advance_video(video_id: uuid.UUID) -> None:
    """Walk one video through consecutive stages until it stops moving."""
    for _ in range(len(VIDEO_STAGES) + 1):  # bound: no video can advance more often than this
        with get_sessionmaker()() as session:
            status = Status(session.get(Video, video_id).status)
        module_path = VIDEO_STAGES.get(status)
        if module_path is None:
            return  # awaiting_review, terminal, etc. — waiting on a human or finished
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one pass of the pipeline.")
    parser.add_argument(
        "--force-discover", action="store_true", help="run discovery now even if it isn't scheduled"
    )
    args = parser.parse_args(argv)

    _run_singleton(DISCOVER_STAGE, force=args.force_discover)
    _gate_candidates()
    _run_singleton(RANK_STAGE)
    _advance_videos()
    _run_singleton(ANALYTICS_STAGE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
