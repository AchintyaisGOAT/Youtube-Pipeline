"""Sequential pipeline orchestrator — no queue, no worker process.

Run manually or from a Windows Task Scheduler job. Each call walks every non-terminal
`candidate`/`video` row forward exactly one stage. A stage module is looked up by the
row's current `Status` and must expose ``run(session, row_id)`` (see the WORK_*.md
"shared conventions" section) — no `session.commit()` inside it, the orchestrator wraps
each stage call in one `session_scope()`. A stage that isn't implemented yet is skipped,
not fatal, so this runs end-to-end while Content/Media are still filling in modules.
"""

from __future__ import annotations

import importlib
import sys

from loguru import logger

from app import notify
from app.db import Candidate, Video, session_scope
from app.status import TERMINAL, Status

#: candidate-stage status -> module (takes candidate_id)
CANDIDATE_STAGES: dict[Status, str] = {
    Status.CANDIDATE_NEW: "app.pipeline.gate",
    Status.CANDIDATE_APPROVED: "app.pipeline.rank",
}

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


def _load_stage(module_path: str):
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError:
        logger.debug("{} not implemented yet, skipping", module_path)
        return None


def _advance_candidates() -> None:
    with session_scope() as session:
        pending = [
            (c.id, Status(c.status))
            for c in session.query(Candidate).all()
            if Status(c.status) in CANDIDATE_STAGES
        ]

    for candidate_id, status in pending:
        module = _load_stage(CANDIDATE_STAGES[status])
        if module is None:
            continue
        with session_scope() as session:
            candidate = session.get(Candidate, candidate_id)
            try:
                module.run(session, candidate_id)
            except Exception:
                logger.exception("candidate {} failed at {}", candidate_id, status)
                candidate.status = Status.CANDIDATE_REJECTED
                notify.alert(f"candidate {candidate_id} failed at {status}")


def _advance_videos() -> None:
    with session_scope() as session:
        pending = [
            (v.id, Status(v.status))
            for v in session.query(Video).all()
            if Status(v.status) not in TERMINAL
        ]

    for video_id, status in pending:
        module_path = VIDEO_STAGES.get(status)
        if module_path is None:
            continue  # awaiting_review / uploading / etc. — waiting on a human or Media
        module = _load_stage(module_path)
        if module is None:
            continue
        with session_scope() as session:
            video = session.get(Video, video_id)
            try:
                module.run(session, video_id)
            except Exception as exc:
                logger.exception("video {} failed at {}", video_id, status)
                video.status = Status.FAILED
                video.error = repr(exc)
                notify.alert(f"video {video_id} failed at {status}")


def main() -> None:
    discover = _load_stage("app.pipeline.discover")
    if discover is not None:
        with session_scope() as session:
            try:
                discover.run(session)
            except Exception:
                logger.exception("discover failed")
                notify.alert("discover stage failed")

    _advance_candidates()
    _advance_videos()


if __name__ == "__main__":
    sys.exit(main())
