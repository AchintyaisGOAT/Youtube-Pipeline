"""Send a video back to an earlier stage (README §4.2, `ogh review`): set its status to the
one *before* the stage to redo, and clear exactly what that stage and every later one
produced, so the next `ogh run` rebuilds from there and nothing stale survives.

Kept on purpose, because a redo never needs to pay for them again:
- downloaded images (the cache and `asset` rows): gathering pictures again finds the same
  files for free;
- LLM answers (`llm_cache`): an unchanged prompt costs nothing to ask again.

The script is written around the image inventory, so redoing the pictures redoes the
script too (its [IMG n] marks number the old inventory).
"""

from __future__ import annotations

import os
import shutil

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.db import Scene, Short, Video
from app.stages.package import kit_dir
from app.status import Status
from app.storage import output_dir, work_dir

#: The stage you can redo -> the status the video goes back to (the stage runs from it).
REDO: dict[str, Status] = {
    "research": Status.SELECTED,
    "pictures": Status.RESEARCHED,  # gather + check the images again, then a new script
    "script": Status.PICTURED,
    "check": Status.SCRIPTED,
    "scenes": Status.CHECKED,
    "narration": Status.SEGMENTED,
    "timing": Status.NARRATED,
    "render": Status.ALIGNED,
    "shorts": Status.ASSEMBLED,  # metadata, thumbnail and the Shorts
    "kit": Status.SHORTS_READY,
}
_ORDER = list(Status)


def _before(status: Status, stage_status: Status) -> bool:
    """True if a video sent back to `status` must redo the stage that runs at `stage_status`."""
    return _ORDER.index(status) <= _ORDER.index(stage_status)


def send_back(session: Session, video: Video, stage: str) -> Status:
    """Returns the video's new status. Raises ValueError for an unknown stage."""
    if stage not in REDO:
        raise ValueError(f"unknown stage {stage!r}; choose from {', '.join(REDO)}")
    to = REDO[stage]
    rows = list(session.execute(select(Scene).filter_by(video_id=video.id).order_by(Scene.idx)).scalars())

    meta = dict(video.video_metadata or {})
    if _before(to, Status.SELECTED):
        video.research = None
    if _before(to, Status.RESEARCHED) and video.research:
        video.research = {k: v for k, v in video.research.items() if k not in ("images", "plan")}
    if _before(to, Status.PICTURED):
        video.script = video.script_prompt_hash = None
        if video.research:
            video.research = {k: v for k, v in video.research.items() if k not in ("plan", "check")}
    # (check rewrites the script in place: redoing it re-checks the checked script)
    if _before(to, Status.CHECKED):
        session.execute(delete(Scene).where(Scene.video_id == video.id))
        rows = []
    if _before(to, Status.SEGMENTED):
        for scene in rows:
            scene.start_s = scene.end_s = None
        video.duration_s = None
        shutil.rmtree(work_dir(video.id, create=False), ignore_errors=True)  # narration, timing, clips
    if _before(to, Status.NARRATED):
        session.execute(delete(Short).where(Short.video_id == video.id))
        (work_dir(video.id, create=False) / "words.json").unlink(missing_ok=True)
    if _before(to, Status.ALIGNED):
        meta.pop("music", None)
        meta.pop("sfx", None)
    if _before(to, Status.ASSEMBLED):  # metadata, thumbnail and Shorts are remade: drop the kit too
        for key in ("title_candidates", "tags", "description", "thumbnail", "shorts", "image_credits",
                    "kit_dir", "publish"):
            meta.pop(key, None)
        kit = kit_dir(video)
        if not _before(to, Status.ALIGNED) and kit is not None:
            # only the Shorts are redone: the long-form goes back where the package stage expects it
            for kit_name, rendered in (("long.mp4", "final.mp4"), ("long.srt", "final.srt")):
                if (kit / kit_name).exists():
                    os.replace(kit / kit_name, output_dir(video.id) / rendered)
        if kit is not None:
            shutil.rmtree(kit, ignore_errors=True)
        if _before(to, Status.ALIGNED):
            shutil.rmtree(output_dir(video.id), ignore_errors=True)
        video.thumbnail_uri = None
        video.title = meta.get("topic") or video.title
    video.video_metadata = meta or None
    video.error = None
    video.status = to
    return to

