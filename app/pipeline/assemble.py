"""FFmpeg assembly: segment images (Ken Burns) + narration + ducked music bed + burned
subtitles -> the final long-form file. Staged per DESIGN.md #12 (slideshow ->
+narration -> +music(ducked) -> +subs -> final); each stage writes under
work_dir(video_id), gets an ffprobe sanity check, and is logged as a `render` row.
"""

from __future__ import annotations

import random
import uuid
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ChannelConfig, get_channel_config, get_settings
from app.db import Asset, Render, Segment, Video
from app.pipeline._ffmpeg import ffmpeg_escape_path, pick_encoder, probe, run_ffmpeg, video_codec_args
from app.pipeline._subtitles import build_subtitles
from app.status import Status
from app.storage import atomic_write_text, output_dir, work_dir


def _record_render(
    session: Session,
    video_id: uuid.UUID,
    stage: str,
    status: str,
    output_uri: Path | None = None,
    log: str | None = None,
) -> None:
    """`status` here is deliberately not an `app.status.Status` member — Render rows are
    an append-only per-attempt audit log, not a state-machine column the orchestrator
    dispatches on, and none of this pipeline's lifecycle statuses ("published",
    "researching", ...) actually describe "did this ffmpeg stage succeed"."""
    session.add(
        Render(
            video_id=video_id,
            kind="longform",
            stage=stage,
            status=status,
            output_uri=str(output_uri) if output_uri else None,
            log=log,
            tool_versions={"encoder": pick_encoder()},
        )
    )


def _build_slideshow(
    config: ChannelConfig, segments: list[Segment], assets_by_id: dict[uuid.UUID, Asset], work_path: Path
) -> Path:
    resolution = config.video.long_form.resolution
    fps = config.video.long_form.fps
    codec_args = video_codec_args(config)

    clip_paths: list[Path] = []
    last_image: Path | None = None
    for i, segment in enumerate(segments):
        duration = max(0.1, (segment.end_s or 0.0) - (segment.start_s or 0.0))
        image_path = None
        if segment.image_asset_id is not None:
            asset = assets_by_id.get(segment.image_asset_id)
            if asset is not None and asset.uri and Path(asset.uri).exists():
                image_path = Path(asset.uri)
        image_path = image_path or last_image
        if image_path is None:
            continue  # no image available yet at all — catches up once the first one appears
        last_image = image_path

        clip_path = work_path / f"clip_{i:04d}.mp4"
        zoom_in = i % 2 == 0  # DESIGN.md #13 — alternate direction for minimal but real visual variety
        zoom_expr = "min(zoom+0.0012,1.15)" if zoom_in else "if(eq(on,1),1.15,max(zoom-0.0012,1.0))"
        num_frames = max(1, round(duration * fps))
        vf = (
            f"scale={resolution[0] * 2}:{resolution[1] * 2}:force_original_aspect_ratio=increase,"
            f"crop={resolution[0] * 2}:{resolution[1] * 2},"
            f"zoompan=z='{zoom_expr}':d={num_frames}:s={resolution[0]}x{resolution[1]}:fps={fps},"
            "setsar=1"
        )
        run_ffmpeg(
            [
                "-loop", "1", "-t", f"{duration:.3f}", "-i", str(image_path),
                "-vf", vf, "-r", str(fps), *codec_args, "-an",
                str(clip_path),
            ]
        )
        probe(clip_path)
        clip_paths.append(clip_path)

    if not clip_paths:
        raise RuntimeError("no segment images available to build a slideshow")

    concat_list = work_path / "concat.txt"
    atomic_write_text(concat_list, "".join(f"file '{p.as_posix()}'\n" for p in clip_paths))

    slideshow_path = work_path / "slideshow.mp4"
    run_ffmpeg(["-f", "concat", "-safe", "0", "-i", str(concat_list), "-c", "copy", str(slideshow_path)])
    probe(slideshow_path)
    return slideshow_path


def _mux_narration(video_path: Path, narration_path: Path, out_path: Path) -> None:
    run_ffmpeg(
        [
            "-i", str(video_path), "-i", str(narration_path),
            "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(out_path),
        ]
    )
    probe(out_path)


def _pick_music_track(assets_dir: str) -> Path | None:
    music_dir = Path(assets_dir) / "music"
    tracks = sorted(music_dir.glob("*.mp3")) + sorted(music_dir.glob("*.wav"))
    return random.choice(tracks) if tracks else None


def _add_ducked_music(video_path: Path, music_path: Path, config: ChannelConfig, out_path: Path) -> None:
    pre_gain = config.video.music_bed_lufs - config.video.loudness_lufs
    filt = (
        f"[1:a]volume={pre_gain}dB[music];"
        "[music][0:a]sidechaincompress=threshold=0.05:ratio=8:attack=20:release=250[ducked];"
        "[0:a][ducked]amix=inputs=2:duration=first:dropout_transition=0[premix];"
        f"[premix]loudnorm=I={config.video.loudness_lufs}:TP=-1.5:LRA=11[aout]"
    )
    run_ffmpeg(
        [
            "-i", str(video_path), "-stream_loop", "-1", "-i", str(music_path),
            "-filter_complex", filt,
            "-map", "0:v:0", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            str(out_path),
        ]
    )
    probe(out_path)


def _burn_subtitles(video_path: Path, ass_path: Path, config: ChannelConfig, out_path: Path) -> None:
    codec_args = video_codec_args(config)
    run_ffmpeg(
        [
            "-i", str(video_path),
            "-vf", f"ass={ffmpeg_escape_path(ass_path)}",
            *codec_args, "-c:a", "copy",
            str(out_path),
        ]
    )
    probe(out_path)


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.ASSEMBLING:
        return

    segments = list(
        session.execute(select(Segment).filter_by(video_id=video_id).order_by(Segment.idx)).scalars()
    )
    if not segments:
        raise ValueError(f"video {video_id}: no segments to assemble")

    config = get_channel_config(session, video.channel_id)
    settings = get_settings()
    asset_ids = {s.image_asset_id for s in segments if s.image_asset_id is not None}
    assets_by_id = {
        a.id: a for a in session.execute(select(Asset).filter(Asset.id.in_(asset_ids))).scalars()
    }

    wp = work_dir(video_id)
    narration_path = wp / "narration.wav"
    if not narration_path.exists():
        raise FileNotFoundError(f"video {video_id}: narration audio not found at {narration_path}")

    stage = "slideshow"
    try:
        slideshow = _build_slideshow(config, segments, assets_by_id, wp)
        _record_render(session, video_id, stage, "success", slideshow)

        stage = "narration"
        with_narration = wp / "with_narration.mp4"
        _mux_narration(slideshow, narration_path, with_narration)
        _record_render(session, video_id, stage, "success", with_narration)

        stage = "music"
        music_track = _pick_music_track(settings.assets_dir)
        current = with_narration
        if music_track is not None:
            current = wp / "with_music.mp4"
            _add_ducked_music(with_narration, music_track, config, current)
            _record_render(session, video_id, stage, "success", current)

        stage = "subtitles"
        subs = build_subtitles(segments, config)
        ass_path = wp / "subtitles.ass"
        subs.save(str(ass_path))
        subs.save(str(output_dir(video_id) / "subtitles.srt"))

        stage = "final"
        final_path = output_dir(video_id) / "final.mp4"
        _burn_subtitles(current, ass_path, config, final_path)
        _record_render(session, video_id, stage, "success", final_path)
    except Exception as exc:
        _record_render(session, video_id, stage, "failed", log=str(exc))
        raise

    video.status = Status.CUTTING_SHORTS
