"""File I/O helpers. Every stage writes media/intermediates through here — never a
hardcoded ``data/...`` path — so ``STORAGE_BASE`` stays the single place that changes.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from app.config import get_settings


def _storage_base() -> Path:
    return Path(get_settings().storage_base)


def work_dir(video_id: uuid.UUID) -> Path:
    """Scratch space for one video's in-progress intermediates."""
    p = _storage_base() / "work" / str(video_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def output_dir(video_id: uuid.UUID) -> Path:
    """Finished, retained outputs (final renders, sidecar files) for one video."""
    p = _storage_base() / "output" / str(video_id)
    p.mkdir(parents=True, exist_ok=True)
    return p


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write via a ``.tmp`` file + rename so a crash mid-write never leaves a partial file."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)
