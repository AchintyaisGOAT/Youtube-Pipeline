"""Package stage (README §4.1 steps 13–15): metadata + thumbnail + upload kit, then hand
the video to `ogh review`. One stage because they share one status transition
(`shorts_ready` -> `packaged`); each step is safe to redo — the metadata call is cached.
The upload kit itself (upload.md, renamed files) is built in S7.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from app.db import Video
from app.stages.metadata import write_metadata
from app.stages.thumbnail import write_thumbnail
from app.status import Status


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.SHORTS_READY:
        return

    write_metadata(session, video)
    write_thumbnail(session, video)
    video.status = Status.PACKAGED
