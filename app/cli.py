"""The `ogh` command (README §4.4) — the only entry point:

    uv run ogh run [--discover]   advance everything as far as possible
    uv run ogh review             review packaged videos
    uv run ogh doctor [--live]    green/red health check (--live: keys + model IDs online)
    uv run ogh assets             write/update the music + sound-effect manifests

`review` owns its own session/commit because a human, not the orchestrator, drives it.
Send-back, title picking and mark-uploaded arrive in S8.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from sqlalchemy import select, text

from app import assets, llm, orchestrator
from app.config import get_config, get_settings
from app.db import Base, Short, Video, get_engine, session_scope
from app.ffmpeg import pick_encoder
from app.status import Status
from app.storage import output_dir


# --------------------------------------------------------------------------- #
# review
# --------------------------------------------------------------------------- #
def _open_in_default_player(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # a local file this pipeline itself rendered
    else:
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
            print("Skipped — still waiting for review.")
            return
        print("Unrecognized choice.")


def review() -> None:
    with session_scope() as session:
        pending = session.execute(select(Video).filter_by(status=Status.PACKAGED)).scalars().all()
        if not pending:
            print("Nothing waiting for review.")
            return
        print(f"{len(pending)} video(s) waiting for review.")
        for video in pending:
            _review_one(session, video)


# --------------------------------------------------------------------------- #
# doctor
# --------------------------------------------------------------------------- #
def _check_config() -> tuple[bool, str]:
    try:
        get_config()
        return True, get_settings().config_path
    except Exception as exc:
        return False, str(exc)


def _check_env_keys() -> tuple[bool, str]:
    settings = get_settings()
    missing = [name for name in ("gemini_api_key", "groq_api_key") if not getattr(settings, name)]
    return (False, f"missing in .env: {', '.join(missing)}") if missing else (True, "present")


def _check_db() -> tuple[bool, str]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("select 1"))
        return True, get_settings().database_url
    except Exception as exc:
        return False, str(exc)


def _check_ffmpeg() -> tuple[bool, str]:
    path = shutil.which("ffmpeg")
    if not path:
        return False, "not on PATH"
    return True, f"{path} (encoder: {pick_encoder()})"


def _check_disk_space() -> tuple[bool, str]:
    base = Path(get_settings().storage_base)
    base.mkdir(parents=True, exist_ok=True)
    free_gb = shutil.disk_usage(base).free / (1024**3)
    try:
        needed = get_config().ops.min_free_gb
    except Exception:
        needed = 15
    return free_gb >= needed, f"{free_gb:.1f} GB free (need {needed})"


def _available_models(provider: str) -> set[str]:
    """Model IDs the configured key can use. Listing models costs no generation quota."""
    if provider == "gemini":
        return {m.name.removeprefix("models/") for m in llm._gemini().models.list()}
    return {m.id for m in llm._groq().models.list()}


def _check_models() -> tuple[bool, str]:
    """--live only: every model ID in config.yaml exists for its provider's key."""
    cfg = get_config().llm
    wanted = {m for m in (cfg.writer, cfg.writer_fallback, cfg.checker, cfg.worker,
                          cfg.worker_fallback, cfg.image) if m}
    problems = []
    for provider in sorted({llm.provider_of(m) for m in wanted}):
        try:
            available = _available_models(provider)
        except Exception as exc:
            problems.append(f"{provider} key rejected or unreachable ({type(exc).__name__}: {exc})")
            continue
        problems += [f"{m} not available" for m in sorted(wanted) if llm.provider_of(m) == provider
                     and m not in available]
    return (False, "; ".join(problems)) if problems else (True, f"{len(wanted)} configured models found")


CHECKS = {
    "config": _check_config,
    "env_keys": _check_env_keys,
    "db": _check_db,
    "ffmpeg": _check_ffmpeg,
    "disk_space": _check_disk_space,
}


def doctor(*, live: bool = False) -> int:
    checks = {**CHECKS, "models": _check_models} if live else CHECKS
    all_ok = True
    for name, check in checks.items():
        ok, detail = check()
        all_ok &= ok
        print(f"[{'OK  ' if ok else 'FAIL'}] {name:<12} {detail}")
    return 0 if all_ok else 1


# --------------------------------------------------------------------------- #
# assets
# --------------------------------------------------------------------------- #
def assets_cmd() -> int:
    """Write/update assets/music and assets/sfx manifest.yaml from the files present."""
    report = assets.write_manifests()
    root = Path(get_settings().assets_dir)
    print(f"music tracks: {len(assets.music_tracks())}  (new: {len(report['music'])})")
    print(f"sound effects: {len(assets.sound_effects())} usable  (new: {len(report['sfx'])})")
    print(f"effect tags available to the scene plan: {', '.join(assets.sfx_tags()) or 'none'}")
    if report["untagged"]:
        print()
        print(f"{len(report['untagged'])} effect(s) have no tag yet — edit {root / 'sfx' / 'manifest.yaml'}")
        print("and give each a tag from: " + ", ".join(assets.SFX_TAGS))
        for name in report["untagged"]:
            print(f"  - {name}")
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ogh", description="OurGreatHistory pipeline.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="advance everything as far as possible")
    run_parser.add_argument(
        "--discover", action="store_true", help="look for new topics even if some are waiting"
    )
    commands.add_parser("review", help="review packaged videos")
    commands.add_parser("assets", help="write/update the music and sound-effect manifests")
    doctor_parser = commands.add_parser("doctor", help="green/red health check")
    doctor_parser.add_argument(
        "--live", action="store_true", help="also check the API keys and model IDs online"
    )
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return doctor(live=args.live)
    if args.command == "assets":
        return assets_cmd()

    get_config()  # fail fast on a bad config.yaml, before touching anything
    Base.metadata.create_all(get_engine())
    if args.command == "run":
        orchestrator.run(force_discover=args.discover)
    elif args.command == "review":
        review()
    return 0


if __name__ == "__main__":
    sys.exit(main())
