"""Cleanup (README §8): run at the start of every `ogh run`, so data/ only holds what is
still needed.

- **Work files** (clips, narration, timing — ~400 MB a video) are deleted once a video no
  longer needs them: approved, uploaded or rejected.
- **Upload kits** are deleted `ops.keep_output_days` after the video was marked uploaded
  or rejected (its files are on YouTube by then, or unwanted).
- **Cached images** that no video's scene uses any more, and **LLM answers** unused, are
  deleted after `ops.keep_cache_days`, so a redo soon after still finds them for free.
- Leftover render folders of videos that no longer exist, and cached image files no
  `asset` row points to (an interrupted download), are deleted.
Run logs prune themselves (data/logs/, 30 days).
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import Asset, LlmCache, Scene, Video
from app.stages.package import kit_dir
from app.status import Status
from app.storage import cache_dir, output_root, work_dir

_DONE = (Status.APPROVED, Status.UPLOADED, Status.REJECTED)


def _remove(path: Path) -> bool:
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
        return True
    return False


def run(session: Session, now: datetime | None = None) -> dict[str, int]:
    """Returns how many of each thing were removed (also logged)."""
    ops = get_config().ops
    now = now or datetime.now(UTC)
    removed = {"work": 0, "kits": 0, "images": 0, "llm_answers": 0, "orphans": 0, "stray_files": 0}
    videos = list(session.execute(select(Video)).scalars())

    for video in videos:
        status = Status(video.status)
        if status in _DONE:
            removed["work"] += _remove(work_dir(video.id, create=False))
        expired = video.updated_at < now - timedelta(days=ops.keep_output_days)
        if status in (Status.UPLOADED, Status.REJECTED) and expired:
            kit = kit_dir(video)
            removed["kits"] += bool(kit and _remove(kit))
            removed["kits"] += _remove(output_root() / str(video.id))

    known = {str(v.id) for v in videos}
    for folder in output_root().iterdir():  # render folders are named by video id until packaged
        if folder.is_dir() and _looks_like_id(folder.name) and folder.name not in known:
            removed["orphans"] += _remove(folder)

    cutoff = now - timedelta(days=ops.keep_cache_days)
    in_use = select(Scene.image_asset_id).where(Scene.image_asset_id.is_not(None))
    for asset in session.execute(select(Asset).where(Asset.id.not_in(in_use), Asset.created_at < cutoff)).scalars():
        Path(asset.uri).unlink(missing_ok=True)
        session.delete(asset)
        removed["images"] += 1
    session.flush()
    known_files = {Path(uri).resolve() for uri in session.execute(select(Asset.uri)).scalars()}
    for sub in ("images", "illustrations"):
        for path in (cache_dir() / sub).rglob("*"):
            if path.is_file() and path.resolve() not in known_files:
                path.unlink(missing_ok=True)
                removed["stray_files"] += 1
    stale = delete(LlmCache).where(LlmCache.created_at < cutoff,
                                   (LlmCache.last_used_at.is_(None)) | (LlmCache.last_used_at < cutoff))
    removed["llm_answers"] = session.execute(stale).rowcount or 0

    if any(removed.values()):
        logger.info("cleanup: {}", ", ".join(f"{k} {v}" for k, v in removed.items() if v))
    return removed


def _looks_like_id(name: str) -> bool:
    try:
        uuid.UUID(name)
        return True
    except ValueError:
        return False
