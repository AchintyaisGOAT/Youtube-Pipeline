"""FFmpeg: for each `short` row, cut its [start_s, end_s] span out of the already-
assembled long-form render, crop to vertical, and burn its own caption (not a plain
center-crop — DESIGN.md #37, near-identical Shorts risk repetitious-content flags).
Hard-gates against config's max_seconds by trimming the tail rather than the head, so
the hook stays intact.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ChannelConfig, get_channel_config
from app.db import Render, Segment, Short, Video
from app.pipeline._ffmpeg import drawtext_font_file, probe, run_ffmpeg, video_codec_args
from app.status import Status
from app.storage import output_dir


def _caption_for(short: Short, segments: list[Segment]) -> str:
    if short.caption:
        return short.caption
    covering = [
        s.text
        for s in segments
        if s.start_s is not None and s.end_s is not None and s.start_s >= short.start_s and s.end_s <= short.end_s
    ]
    return covering[0][:80] if covering else f"Short {short.idx + 1}"


def _trim_to_max_seconds(start_s: float, end_s: float, max_seconds: float) -> float:
    return min(end_s, start_s + max_seconds)


def _escape_drawtext(text: str) -> str:
    return text.replace("\\", "\\\\").replace("'", "’").replace(":", "\\:")


def _crop_and_caption(
    source: Path, start_s: float, end_s: float, caption: str, config: ChannelConfig, out_path: Path
) -> None:
    resolution = config.video.shorts.resolution
    codec_args = video_codec_args(config)
    # `fontfile=` needs a bare filename, not an absolute path -- a Windows drive-letter
    # colon inside a filtergraph option value isn't reliably parsed by this ffmpeg
    # build (see assemble.py's _burn_subtitles for the same issue with `ass=filename=`).
    # Running with cwd set to the font's own directory sidesteps it the same way.
    font_path = Path(drawtext_font_file())
    vf = (
        f"crop=ih*{resolution[0]}/{resolution[1]}:ih,"
        f"scale={resolution[0]}:{resolution[1]},"
        f"drawtext=text='{_escape_drawtext(caption)}':fontfile={font_path.name}:"
        "fontcolor=white:fontsize=48:borderw=3:bordercolor=black:x=(w-text_w)/2:y=h*0.08"
    )
    run_ffmpeg(
        [
            "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}", "-i", str(source),
            "-vf", vf, "-r", str(config.video.shorts.fps), *codec_args,
            "-c:a", "aac", "-b:a", "192k",
            str(out_path),
        ],
        cwd=font_path.parent,
    )
    probe(out_path)


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.CUTTING_SHORTS:
        return

    final_path = output_dir(video_id) / "final.mp4"
    if not final_path.exists():
        raise FileNotFoundError(f"video {video_id}: no final render at {final_path}")

    config = get_channel_config(session, video.channel_id)
    segments = list(
        session.execute(select(Segment).filter_by(video_id=video_id).order_by(Segment.idx)).scalars()
    )
    shorts = list(session.execute(select(Short).filter_by(video_id=video_id).order_by(Short.idx)).scalars())

    for short in shorts:
        if short.start_s is None or short.end_s is None:
            continue

        end_s = _trim_to_max_seconds(short.start_s, short.end_s, config.video.shorts.max_seconds)
        caption = _caption_for(short, segments)
        out_path = output_dir(video_id) / f"short_{short.idx}.mp4"

        try:
            _crop_and_caption(final_path, short.start_s, end_s, caption, config, out_path)
        except Exception as exc:
            session.add(Render(short_id=short.id, kind="short", stage="crop", status="failed", log=str(exc)))
            short.status = Status.FAILED
            continue

        short.end_s = end_s
        short.caption = caption
        short.title = short.title or caption[:80]
        # "rendered" is deliberately not an app.status.Status member: nothing dispatches
        # on short.status (the orchestrator only reads video.status), and none of the
        # shared lifecycle values actually mean "this short finished cropping" — same
        # reasoning as Render.status below.
        short.status = "rendered"
        session.add(
            Render(short_id=short.id, kind="short", stage="crop", status="success", output_uri=str(out_path))
        )

    video.status = Status.GENERATING_METADATA
