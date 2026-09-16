"""Smoke tests: db reachable, .env keys present, ffmpeg on PATH, disk space.

Usage: uv run python scripts/doctor.py
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from sqlalchemy import text

from app.config import get_settings
from app.db import get_engine


def check_db() -> tuple[bool, str]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("select 1"))
        return True, str(get_settings().database_url)
    except Exception as exc:
        return False, str(exc)


def check_env_keys() -> tuple[bool, str]:
    settings = get_settings()
    required = ["gemini_api_key", "youtube_client_id", "youtube_client_secret", "youtube_refresh_token"]
    missing = [name for name in required if not getattr(settings, name)]
    return (False, f"missing: {', '.join(missing)}") if missing else (True, "present")


def check_ffmpeg() -> tuple[bool, str]:
    path = shutil.which("ffmpeg")
    return (True, path) if path else (False, "not on PATH")


def check_disk_space() -> tuple[bool, str]:
    base = Path(get_settings().storage_base)
    base.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(base).free / (1024**3)
    return free_gb >= 15, f"{free_gb:.1f} GB free"


CHECKS = {
    "db": check_db,
    "env_keys": check_env_keys,
    "ffmpeg": check_ffmpeg,
    "disk_space": check_disk_space,
}


def main() -> int:
    all_ok = True
    for name, check in CHECKS.items():
        ok, detail = check()
        all_ok &= ok
        print(f"[{'OK  ' if ok else 'FAIL'}] {name:<12} {detail}")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
