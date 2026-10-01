"""The `ogh` command (README §4.4) — the only entry point:

    uv run ogh run [--discover]   advance everything as far as possible
    uv run ogh review             review packaged (and failed) videos; approve / reject / send back
    uv run ogh doctor [--live]    green/red health check (--live: keys + model IDs online)
    uv run ogh assets             write/update the music + sound-effect manifests

`review` owns its own session/commit because a human, not the orchestrator, drives it.
Review also switches the title and marks approved videos uploaded.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

from sqlalchemy import select, text

from app import assets, llm, orchestrator, quota, sendback
from app.config import get_config, get_settings
from app.db import Base, Short, Video, get_engine, session_scope
from app.ffmpeg import pick_encoder
from app.stages.package import kit_dir, video_file, write_notes
from app.status import Status
from app.storage import work_dir


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
    if kit_dir(video):
        print("Upload kit:", kit_dir(video), "(open upload.md for the Studio steps)")


def _send_back(video: Video, session, input_fn) -> bool:
    """Ask which stage to redo. True if the video moved."""
    stages = list(sendback.REDO)
    print("Redo from: " + " / ".join(f"[{n}] {s}" for n, s in enumerate(stages, 1)))
    raw = input_fn("Stage number (blank = cancel): ").strip()
    if not (raw.isdigit() and 1 <= int(raw) <= len(stages)):
        print("Not sent back.")
        return False
    stage = stages[int(raw) - 1]
    try:
        status = sendback.send_back(session, video, stage)
    except ValueError as exc:
        print(f"Not sent back: {exc}")
        return False
    print(f"Sent back: the next `ogh run` redoes {stage} and everything after it (status: {status}).")
    return True


def _review_failed(session, video: Video, input_fn) -> None:
    print(f"\nFAILED: {video.title}\n  {video.error or '(no error saved)'}")
    while True:
        choice = input_fn("\n[b]ack to a stage / [r]eject / [s]kip for now: ").strip().lower()
        if choice == "b" and _send_back(video, session, input_fn):
            return
        if choice == "r":
            video.status = Status.REJECTED
            print("Rejected.")
            return
        if choice == "s":
            print("Skipped.")
            return


def _switch_title(session, video: Video, input_fn) -> None:
    """Pick another of the metadata step's title candidates; the kit's upload.md follows."""
    options = (video.video_metadata or {}).get("title_candidates") or []
    for n, title in enumerate(options, 1):
        print(f"  [{n}] {title}{'  (current)' if title == video.title else ''}")
    raw = input_fn("Title number (blank = keep): ").strip()
    if raw.isdigit() and 1 <= int(raw) <= len(options):
        video.title = options[int(raw) - 1]
        write_notes(session, video)
        print(f"Title: {video.title} (upload.md updated)")


def _review_approved(video: Video, input_fn) -> None:
    """An approved video waits for your manual upload (README §7)."""
    print(f"\nAPPROVED, waiting for upload: {video.title}")
    if kit_dir(video):
        print(f"  Upload kit: {kit_dir(video)} (upload.md has every Studio step)")
    choice = input_fn("\n[u]ploaded (all scheduled in Studio) / [s]kip for now: ").strip().lower()
    if choice == "u":
        video.status = Status.UPLOADED
        print(f"Marked uploaded. Its kit is deleted {get_config().ops.keep_output_days} days from now.")


def _review_one(session, video: Video, input_fn=input) -> None:
    print("=" * 70)
    if Status(video.status) == Status.FAILED:
        _review_failed(session, video, input_fn)
        return
    if Status(video.status) == Status.APPROVED:
        _review_approved(video, input_fn)
        return
    _print_metadata(video)

    long_form = video_file(video, "final.mp4")
    if long_form.exists():
        print(f"\nOpening long-form render: {long_form}")
        _open_in_default_player(long_form)
    else:
        print("\nWARNING: no long-form render found at", long_form)

    shorts = session.execute(select(Short).filter_by(video_id=video.id).order_by(Short.idx)).scalars().all()
    print(f"{len(shorts)} Short(s) attached.")

    while True:
        choice = input_fn("\n[a]pprove / [r]eject / [b]ack to a stage / [t]itle / [o]pen a short / [s]kip for now: ")
        choice = choice.strip().lower()
        if choice == "a":
            video.status = Status.APPROVED
            shutil.rmtree(work_dir(video.id, create=False), ignore_errors=True)  # clips, narration: ~400 MB
            print("Approved — work files deleted. Upload it with the kit's upload.md, then mark it uploaded here.")
            return
        if choice == "t":
            _switch_title(session, video, input_fn)
            continue
        if choice == "r":
            reason = input_fn("Rejection reason: ").strip()
            video.status = Status.REJECTED
            video.error = reason or None
            print("Rejected.")
            return
        if choice == "b":
            if _send_back(video, session, input_fn):
                return
            continue
        if choice == "o":
            idx_raw = input_fn(f"Short number (1-{len(shorts)}): ").strip()
            if idx_raw.isdigit() and 1 <= int(idx_raw) <= len(shorts):
                short_path = video_file(video, f"short_{int(idx_raw):02d}.mp4")
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
        pending = session.execute(select(Video).where(
            Video.status.in_([Status.PACKAGED, Status.FAILED, Status.APPROVED]))).scalars().all()
        if not pending:
            print("Nothing waiting for review.")
            return
        print(f"{len(pending)} video(s) waiting for you (packaged, failed or approved-not-uploaded).")
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
                          cfg.worker_fallback) if m}
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


def _check_usage() -> tuple[bool, str]:
    """Today's requests per model against its cap (Gemini's day starts at midnight Pacific)."""
    from app.db import LlmCall

    caps = get_config().llm.daily_requests
    try:
        with session_scope() as session:
            used = {}
            for provider in ("gemini", "groq"):
                rows = session.execute(select(LlmCall.model).where(
                    LlmCall.provider == provider, LlmCall.outcome.in_(("ok", "error", "retry_later")),
                    LlmCall.created_at >= quota.day_start(provider))).scalars()
                for model in rows:
                    used[model] = used.get(model, 0) + 1
    except Exception as exc:
        return False, f"can't read usage: {exc}"
    models = sorted(set(used) | set(caps))
    parts = [f"{m} {used.get(m, 0)}/{caps[m]}" if m in caps else f"{m} {used.get(m, 0)}" for m in models]
    full = [m for m in caps if used.get(m, 0) >= caps[m]]
    groq_requests = sum(n for m, n in used.items() if llm.provider_of(m) == "groq")
    detail = "; ".join(parts) or "no calls today"
    return not full, f"{detail} (groq {groq_requests}/{quota.DAILY_LIMITS['groq']['requests']})" + (
        f" — spent today: {', '.join(full)}" if full else "")


def _probe(provider: str) -> tuple[bool, str]:
    """--live only: one tiny real request. The only way to see a refused project (402) or a
    spent daily quota — listing models always works. Costs one request of the day's quota."""
    cfg = get_config().llm
    model = next((m for m in (cfg.worker_fallback, cfg.worker, cfg.writer_fallback, cfg.writer)
                  if m and llm.provider_of(m) == provider), None)
    if model is None:
        return True, "not used"
    try:
        if provider == "gemini":
            llm._gemini().models.generate_content(model=model, contents="Say ok",
                                                  config=llm._gemini_config(max_output_tokens=8))
        else:
            llm._groq().chat.completions.create(model=model, messages=[{"role": "user", "content": "Say ok"}],
                                                max_tokens=16)
    except Exception as exc:
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
        if code == 402:
            return False, (f"{model}: 402 — the project is refused (prepay/billing state). Check AI Studio "
                           "→ Projects → Billing Tier; a new project with no billing is always free tier")
        if code == 429:
            return False, f"{model}: 429 — rate limit or today's quota spent ({str(exc)[:160]})"
        return False, f"{model}: {type(exc).__name__}: {str(exc)[:200]}"
    return True, f"{model} answered"


def _check_contact() -> tuple[bool, str]:
    """Wikimedia 403s every request whose User-Agent contact isn't a real URL."""
    contact = get_settings().wikimedia_contact
    ok = contact.startswith(("http://", "https://")) and "example.org" not in contact
    return ok, "set" if ok else "WIKIMEDIA_CONTACT in .env must be your channel or repo URL"


def _check_media() -> tuple[bool, str]:
    """The local voice + timing models (downloaded on first use) and the subtitle font."""
    from app.stages.narrate import MODEL_FILES
    from app.storage import models_dir
    from app.subtitles import font_path

    missing = [f for f in MODEL_FILES if not (models_dir() / "kokoro" / f).exists()]
    whisper = any((models_dir() / "whisper").glob("models--*"))
    font = font_path(get_config())
    notes = [f"font {font.name}" + ("" if font.exists() else " MISSING")]
    notes.append("voice model ok" if not missing else f"voice model downloads on first run ({', '.join(missing)})")
    notes.append("whisper ok" if whisper else "whisper downloads on first run")
    return font.exists(), "; ".join(notes)


def _check_music() -> tuple[bool, str]:
    """Tracks without a license are never used (README §6.3)."""
    import yaml

    manifest = Path(get_settings().assets_dir) / "music" / "manifest.yaml"
    listed = []
    if manifest.exists():
        listed = (yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}).get("tracks") or []
    usable = assets.music_tracks()
    unlicensed = [t.get("file") for t in listed if isinstance(t, dict) and not str(t.get("license") or "").strip()]
    detail = f"{len(usable)} usable track(s)" + (f"; no license yet: {', '.join(unlicensed)}" if unlicensed else "")
    return not unlicensed, detail + ("" if usable else " — videos render without music")


CHECKS = {
    "config": _check_config,
    "env_keys": _check_env_keys,
    "db": _check_db,
    "ffmpeg": _check_ffmpeg,
    "disk_space": _check_disk_space,
    "wikimedia": _check_contact,
    "media": _check_media,
    "music": _check_music,
    "usage_today": _check_usage,
}


def doctor(*, live: bool = False) -> int:
    checks = ({**CHECKS, "models": _check_models, "gemini_live": lambda: _probe("gemini"),
               "groq_live": lambda: _probe("groq")} if live else CHECKS)
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
    print(f"music tracks: {len(assets.music_tracks())} usable  (new: {len(report['music'])})")
    print(f"sound effects: {len(assets.sound_effects())} usable  (new: {len(report['sfx'])})")
    print(f"effect tags available to the scene plan: {', '.join(assets.sfx_tags()) or 'none'}")
    if report["untagged"]:
        print()
        print(f"{len(report['untagged'])} effect(s) have no tag yet — edit {root / 'sfx' / 'manifest.yaml'}")
        print("and give each a tag from: " + ", ".join(assets.SFX_TAGS))
        for name in report["untagged"]:
            print(f"  - {name}")
    if report["unlicensed"]:
        print()
        print(f"{len(report['unlicensed'])} track(s) have no license yet and won't be used — edit "
              f"{root / 'music' / 'manifest.yaml'}:")
        print(f'set license (e.g. "{assets.YOUTUBE_LIBRARY_LICENSE}") and, if the license asks for it, credit')
        for name in report["unlicensed"]:
            print(f"  - {name}")
    return 0


# --------------------------------------------------------------------------- #
# run helpers
# --------------------------------------------------------------------------- #
def _log_to_file() -> None:
    """Every `ogh run` also logs to data/logs/run_<date>.log (kept 30 days), so a run that
    went wrong overnight can be read afterwards."""
    from loguru import logger

    folder = Path(get_settings().storage_base) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    logger.add(folder / "run_{time:YYYY-MM-DD}.log", level="INFO", retention="30 days", encoding="utf-8")


@contextlib.contextmanager
def _stay_awake():
    """Windows doesn't sleep while a run is going (a sleep mid-run froze one and dropped its
    network calls). The display may still turn off; closing the lid still sleeps."""
    if sys.platform != "win32":
        yield
        return
    import ctypes

    es_continuous, es_system_required = 0x80000000, 0x00000001
    ctypes.windll.kernel32.SetThreadExecutionState(es_continuous | es_system_required)
    try:
        yield
    finally:
        ctypes.windll.kernel32.SetThreadExecutionState(es_continuous)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ogh", description="PantherTellsHistory pipeline.")
    commands = parser.add_subparsers(dest="command", required=True)
    run_parser = commands.add_parser("run", help="advance everything as far as possible")
    run_parser.add_argument(
        "--discover", action="store_true", help="look for new topics even if some are waiting"
    )
    commands.add_parser("review", help="review packaged and failed videos")
    commands.add_parser("assets", help="write/update the music and sound-effect manifests")
    doctor_parser = commands.add_parser("doctor", help="green/red health check")
    doctor_parser.add_argument(
        "--live", action="store_true", help="also check the API keys and model IDs online"
    )
    args = parser.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):  # descriptions carry emoji; the Windows console is cp1252
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if args.command == "doctor":
        return doctor(live=args.live)
    if args.command == "assets":
        return assets_cmd()

    get_config()  # fail fast on a bad config.yaml, before touching anything
    Base.metadata.create_all(get_engine())
    if args.command == "run":
        _log_to_file()
        with _stay_awake():
            orchestrator.run(force_discover=args.discover)
    elif args.command == "review":
        review()
    return 0


if __name__ == "__main__":
    sys.exit(main())
