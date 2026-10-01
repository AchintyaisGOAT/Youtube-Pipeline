"""Upload kit (README §4.1 step 15, §7): the video's files moved (not copied) into
`data/output/<publish date>_<slug>/` under their upload names, plus `upload.md` —
everything to paste into YouTube Studio, in order — and `credits.md`, the full source and
image-credit list. Then the video waits for `ogh review`.

Publish dates: the long-form gets the next free `publish.schedule.long_form` slot after
every other video's; its Shorts follow one per `shorts` slot, starting after the
long-form is out (a Short's "watch the full video" link needs it public) and after the
Shorts already scheduled. Re-running keeps the dates and folder it chose the first time.
"""

from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import schedule
from app.config import ChannelConfig, get_config
from app.db import Asset, Scene, Short, Video
from app.stages.metadata import music_credit
from app.status import Status
from app.storage import atomic_write_text, output_dir, output_root

#: rendered name -> name in the kit
_RENAMES = {"final.mp4": "long.mp4", "final.srt": "long.srt"}


def slug(text: str, limit: int = 60) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].rstrip("-") or "video"


def kit_dir(video: Video) -> Path | None:
    """The video's upload-kit folder, once the package stage has made it."""
    path = (video.video_metadata or {}).get("kit_dir")
    return Path(path) if path else None


def video_file(video: Video, name: str) -> Path:
    """Where one of the video's files is now: in its kit if packaged, else its render folder."""
    kit = kit_dir(video)
    return kit / _RENAMES.get(name, name) if kit else output_dir(video.id) / name


def plan_dates(config: ChannelConfig, now: datetime, taken_long: list[datetime], taken_shorts: list[datetime],
               n_shorts: int) -> tuple[datetime, list[datetime]]:
    """(long-form slot, one slot per Short)."""
    sched, tz = config.publish.schedule, config.timezone
    long_at = schedule.next_slot(sched.long_form, max([now, *taken_long]), tz)
    shorts, cursor = [], max([long_at, *taken_shorts])
    for _ in range(n_shorts):
        cursor = schedule.next_slot(sched.shorts, cursor, tz)
        shorts.append(cursor)
    return long_at, shorts


def _when(at: datetime) -> str:
    return f"{at:%a %d %b %Y, %H:%M} ({at.tzname()})"


def upload_md(video: Video, shorts: list[Short], config: ChannelConfig, long_at: datetime,
              short_at: list[datetime], ai_images: int) -> str:
    meta = video.video_metadata or {}
    others = [t for t in meta.get("title_candidates", []) if t != video.title]
    synthetic = ("**No** is normally right: YouTube asks for disclosure only when content looks "
                 "*realistic* (a real person saying something they didn't, realistic footage of "
                 "events). This video's narrator is an AI voice that imitates no real person"
                 + (f", and {ai_images} scene(s) use AI-generated period-style illustrations."
                    if ai_images else ".")
                 + " Choose **Yes** if any AI image looks like a real photograph.")
    lines = [
        f"# {video.title}", "",
        f"Long-form: **{_when(long_at)}** · Shorts: daily from **{_when(short_at[0])}**" if short_at
        else f"Long-form: **{_when(long_at)}**", "",
        "Upload the long-form first: each Short links to it.", "",
        "## 1. Long-form video", "",
        "1. YouTube Studio → **Create → Upload videos** → `long.mp4`",
        "2. **Title**:", "", "```", video.title or "", "```", "",
    ]
    if others:
        lines += ["   Other options (switch in `ogh review` if you prefer one):", ""]
        lines += [f"   - {t}" for t in others] + [""]
    lines += [
        "3. **Description**:", "", "```", meta.get("description", ""), "```", "",
        "4. **Thumbnail**: upload `thumbnail.png`",
        f"5. **Audience**: {'Yes' if config.publish.made_for_kids else 'No'}, it's "
        f"{'' if config.publish.made_for_kids else 'not '}made for kids",
        "6. **Show more**:",
        f"   - **Altered content**: {synthetic}",
        "   - **Tags**:", "", "     ```", "     " + ", ".join(meta.get("tags", [])), "     ```", "",
        f"   - **Category**: {config.publish.category}",
        "7. **Subtitles** (Video elements → Add subtitles → Upload file → With timing): `long.srt`, English",
        "8. **Visibility** → Schedule → " + _when(long_at), "",
        "## 2. Shorts", "",
        "For each one: **Create → Upload videos**, then the fields below. Same audience and "
        "altered-content answers as above.", "",
    ]
    for n, (short, entry) in enumerate(zip(shorts, meta.get("shorts", []), strict=False)):
        name = f"short_{n + 1:02d}"
        lines += [
            f"### Short {n + 1}: {_when(short_at[n]) if n < len(short_at) else ''}", "",
            f"- File: `{name}.mp4` · subtitles: `{name}.srt`",
            "- **Title**:", "", "  ```", f"  {short.title}", "  ```",
            "- **Description**:", "", "  ```", *[f"  {line}" for line in entry["description"].splitlines()], "  ```",
            f"- **Related video**: choose \"{video.title}\" (the ending tells viewers to tap it)",
            f"- **Visibility** → Schedule → {_when(short_at[n])}" if n < len(short_at) else "", "",
        ]
    lines += ["## 3. When everything is scheduled", "", "Run `uv run ogh review` and mark the video uploaded.", ""]
    return "\n".join(lines)


def credits_md(video: Video) -> str:
    meta, research = video.video_metadata or {}, video.research or {}
    lines = [f"# Credits: {video.title}", "", "## Wikipedia articles (the script's only sources)", ""]
    lines += [f"- [{a['title']}]({a['url']})" for a in research.get("articles", [])]
    if research.get("sources"):
        lines += ["", "## Their references", ""] + [f"- {url}" for url in research["sources"]]
    lines += ["", "## Images", ""] + (meta.get("image_credits") or ["- (none)"])
    music = music_credit(meta.get("music"))
    if music:
        lines += ["", "## Music", "", music.removeprefix("🎵 Music\n")]
    return "\n".join(lines) + "\n"


def _taken(session: Session, video_id: uuid.UUID) -> tuple[list[datetime], list[datetime]]:
    others = session.execute(select(Video).where(Video.id != video_id, Video.status.in_(
        [Status.PACKAGED, Status.APPROVED, Status.UPLOADED]))).scalars()
    long_at, shorts_at = [], []
    for other in others:
        publish = (other.video_metadata or {}).get("publish") or {}
        if publish.get("long"):
            long_at.append(datetime.fromisoformat(publish["long"]))
        shorts_at += [datetime.fromisoformat(t) for t in publish.get("shorts", [])]
    return long_at, shorts_at


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.SHORTS_READY:
        return

    config = get_config()
    shorts = list(session.execute(select(Short).filter_by(video_id=video_id).order_by(Short.idx)).scalars())
    meta = dict(video.video_metadata or {})
    publish = meta.get("publish")
    if publish:  # a re-run keeps the dates it chose
        long_at = datetime.fromisoformat(publish["long"])
        short_at = [datetime.fromisoformat(t) for t in publish["shorts"]]
    else:
        long_at, short_at = plan_dates(config, datetime.now(UTC), *_taken(session, video_id), len(shorts))

    name = slug(meta.get("topic") or video.title or str(video_id))
    kit = kit_dir(video) or output_root() / f"{long_at:%Y-%m-%d}_{name}"
    kit.mkdir(parents=True, exist_ok=True)
    rendered = output_dir(video_id)
    for path in rendered.iterdir():
        os.replace(path, kit / _RENAMES.get(path.name, path.name))
    rendered.rmdir()

    meta.update({"kit_dir": str(kit), "publish": {"long": long_at.isoformat(),
                                                  "shorts": [t.isoformat() for t in short_at]}})
    video.video_metadata = meta
    video.thumbnail_uri = str(kit / "thumbnail.png")
    write_notes(session, video)
    video.status = Status.PACKAGED


def write_notes(session: Session, video: Video) -> None:
    """(Re)write the kit's upload.md and credits.md from the video as it is now — also used
    by `ogh review` after switching the title."""
    kit, publish = kit_dir(video), (video.video_metadata or {}).get("publish") or {}
    if kit is None or not publish:
        raise ValueError(f"video {video.id} has no upload kit yet")
    shorts = list(session.execute(select(Short).filter_by(video_id=video.id).order_by(Short.idx)).scalars())
    scene_assets = select(Scene.image_asset_id).filter_by(video_id=video.id)
    ai_images = len(set(session.execute(select(Asset.id).where(
        Asset.id.in_(scene_assets), Asset.source == "gemini")).scalars()))
    long_at = datetime.fromisoformat(publish["long"])
    short_at = [datetime.fromisoformat(t) for t in publish["shorts"]]
    atomic_write_text(kit / "upload.md", upload_md(video, shorts, get_config(), long_at, short_at, ai_images))
    atomic_write_text(kit / "credits.md", credits_md(video))
