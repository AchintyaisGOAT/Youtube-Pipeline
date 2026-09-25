"""CLI review gate: list videos awaiting review, open the long-form (and Shorts) in the
default player, print metadata, and record approve/reject (WORK_MEDIA.md §2 — a CLI
script, not a web dashboard). Not a pipeline stage: it owns its own session/commit
because a human, not the orchestrator, drives it.

Usage: uv run python review.py
"""

from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

from sqlalchemy import select

from app.db import Short, Video, session_scope
from app.status import Status
from app.storage import output_dir


def _open_in_default_player(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # local file this pipeline itself just rendered
    else:
        import subprocess

        subprocess.run(["open" if sys.platform == "darwin" else "xdg-open", str(path)], check=False)


def _print_metadata(video: Video) -> None:
    meta = video.video_metadata or {}
    print(f"\nTitle: {video.title}")
    print(f"Duration: {video.duration_s:.1f}s" if video.duration_s else "Duration: unknown")
    print("\nDescription:\n" + textwrap.indent(meta.get("description", "(none)"), "  "))
    print("\nTags:", ", ".join(meta.get("tags", [])) or "(none)")
    print("Thumbnail:", video.thumbnail_uri or "(none)")


def _review_one(session, video: Video, input_fn=input) -> None:
    print("=" * 70)
    _print_metadata(video)

    long_form = output_dir(video.id) / "final.mp4"
    if long_form.exists():
        print(f"\nOpening long-form render: {long_form}")
        _open_in_default_player(long_form)
    else:
        print("\nWARNING: no long-form render found at", long_form)

    shorts = session.execute(select(Short).filter_by(video_id=video.id).order_by(Short.idx)).scalars().all()
    print(f"{len(shorts)} Short(s) attached.")

    while True:
        choice = input_fn("\n[a]pprove / [r]eject / [o]pen a short / [s]kip for now: ").strip().lower()
        if choice == "a":
            video.status = Status.APPROVED
            print("Approved.")
            return
        if choice == "r":
            reason = input_fn("Rejection reason: ").strip()
            video.status = Status.REJECTED
            video.error = reason or None
            print("Rejected.")
            return
        if choice == "o":
            idx_raw = input_fn(f"Short index (0-{max(len(shorts) - 1, 0)}): ").strip()
            if idx_raw.isdigit() and int(idx_raw) < len(shorts):
                short_path = output_dir(video.id) / f"short_{int(idx_raw)}.mp4"
                if short_path.exists():
                    _open_in_default_player(short_path)
                else:
                    print("That short hasn't been rendered.")
            continue
        if choice == "s":
            print("Skipped — still awaiting review.")
            return
        print("Unrecognized choice.")


def main() -> None:
    with session_scope() as session:
        pending = session.execute(select(Video).filter_by(status=Status.AWAITING_REVIEW)).scalars().all()
        if not pending:
            print("Nothing awaiting review.")
            return
        print(f"{len(pending)} video(s) awaiting review.")
        for video in pending:
            _review_one(session, video)


if __name__ == "__main__":
    main()
