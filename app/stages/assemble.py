"""Assemble the long-form video (README §4.1 step 11, §6.3–6.4).

1. One clip per scene, exactly as long as the scene's narration (pauses included — the
   narrate stage measured them), with a slow pan or zoom. Motions rotate through six
   kinds, and an image shown again gets a different motion than last time, so reuse
   doesn't look repeated. Narrow/portrait images sit on a blurred, darkened copy of
   themselves instead of being cropped to 16:9 (which cut faces off period portraits).
   Clips are rendered in parallel.
2. One final pass: clips joined, subtitles burned in, narration + music bed mixed and
   loudness-normalised, encoded once.

Audio: narration normalised to `video.loudness_lufs`; a music track from
assets/music/manifest.yaml normalised to `video.music_lufs`, ducked while speech plays,
looped to length; final mix normalised again. No manifest/tracks = no music. The chosen
track is recorded in video.video_metadata["music"] for the credits.

Scene frame boundaries come from the cumulative scene times (round(start*fps) ..
round(end*fps)), so rounding never accumulates and the picture stays locked to the voice.
"""

from __future__ import annotations

import json
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml
from loguru import logger
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import ChannelConfig, get_config, get_settings
from app.db import Asset, Render, Scene, Video
from app.ffmpeg import pick_encoder, probe, run_ffmpeg, video_codec_args
from app.status import Status
from app.storage import atomic_write_text, output_dir, work_dir
from app.subtitles import build_subtitles, font_path

MOTIONS = ("zoom_in", "pan_right", "zoom_out", "pan_left", "pan_down", "pan_up")
#: How far a pan/zoom travels: 12% — slow enough to read as calm, not a whip.
ZOOM = 1.12
#: Images narrower than this aspect ratio get the blurred-background treatment.
FIT_BELOW_ASPECT = 1.4
SUPERSAMPLE = 2  # zoompan on a 2x canvas avoids the visible 1px jitter of slow zooms
RENDER_WORKERS = 4


# --------------------------------------------------------------------------- #
# planning (pure — unit-tested)
# --------------------------------------------------------------------------- #
def choose_motions(image_ids: list) -> list[str]:
    """Rotate through MOTIONS; an image seen before never repeats its last motion."""
    last_motion: dict = {}
    chosen = []
    for i, image in enumerate(image_ids):
        motion = MOTIONS[i % len(MOTIONS)]
        if last_motion.get(image) == motion:
            motion = MOTIONS[(i + 1) % len(MOTIONS)]
        last_motion[image] = motion
        chosen.append(motion)
    return chosen


def frame_counts(bounds: list[tuple[float, float]], fps: int) -> list[int]:
    return [max(1, round(end * fps) - round(start * fps)) for start, end in bounds]


def zoompan(motion: str, frames: int, size: tuple[int, int], fps: int) -> str:
    p = f"on/{max(1, frames - 1)}"
    center_x, center_y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    z, x, y = f"{ZOOM}", center_x, center_y
    if motion == "zoom_in":
        z = f"1+{ZOOM - 1:.3f}*{p}"
    elif motion == "zoom_out":
        z = f"{ZOOM}-{ZOOM - 1:.3f}*{p}"
    elif motion == "pan_right":
        x = f"(iw-iw/zoom)*{p}"
    elif motion == "pan_left":
        x = f"(iw-iw/zoom)*(1-{p})"
    elif motion == "pan_down":
        y = f"(ih-ih/zoom)*{p}"
    elif motion == "pan_up":
        y = f"(ih-ih/zoom)*(1-{p})"
    return f"zoompan=z='{z}':x='{x}':y='{y}':d={frames}:s={size[0]}x{size[1]}:fps={fps}"


def clip_filter(motion: str, frames: int, size: tuple[int, int], fps: int, image_aspect: float) -> str:
    cw, ch = size[0] * SUPERSAMPLE, size[1] * SUPERSAMPLE
    motion_filter = f"{zoompan(motion, frames, size, fps)},setsar=1"
    if image_aspect >= FIT_BELOW_ASPECT:
        return f"[0:v]scale={cw}:{ch}:force_original_aspect_ratio=increase,crop={cw}:{ch},setsar=1,{motion_filter}[v]"
    small_w, small_h = cw // 8, ch // 8  # blur at 1/8 size: same look, a fraction of the work
    return (
        f"[0:v]split[a][b];"
        f"[a]scale={small_w}:{small_h}:force_original_aspect_ratio=increase,crop={small_w}:{small_h},"
        f"boxblur=8:2,eq=brightness=-0.12,scale={cw}:{ch}[bg];"
        f"[b]scale=-2:{ch}[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1,{motion_filter}[v]"
    )


def audio_graph(config: ChannelConfig, with_music: bool) -> str:
    loud, music = config.video.loudness_lufs, config.video.music_lufs
    final = f"loudnorm=I={loud}:TP=-1.5:LRA=11,aresample=48000[a]"
    if not with_music:
        return f"[1:a]{final}"
    return (
        f"[1:a]loudnorm=I={loud}:TP=-1.5:LRA=11,asplit=2[n1][n2];"
        f"[2:a]loudnorm=I={music}:TP=-2:LRA=11[m];"
        f"[m][n1]sidechaincompress=threshold=0.03:ratio=6:attack=20:release=400[duck];"
        f"[n2][duck]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,{final}"
    )


# --------------------------------------------------------------------------- #
# music
# --------------------------------------------------------------------------- #
def pick_music(video_id: uuid.UUID) -> dict | None:
    """A track from assets/music/manifest.yaml (file, title, artist, license, credit),
    chosen by video id so a re-render keeps the same one. None = no music."""
    folder = Path(get_settings().assets_dir) / "music"
    manifest = folder / "manifest.yaml"
    if not manifest.exists():
        return None
    tracks = [t for t in (yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}).get("tracks", [])
              if isinstance(t, dict) and (folder / str(t.get("file", ""))).is_file()]
    if not tracks:
        logger.warning("assets/music/manifest.yaml lists no existing files — rendering without music")
        return None
    track = dict(tracks[video_id.int % len(tracks)])
    track["path"] = str(folder / track["file"])
    return track


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _record(session: Session, video_id: uuid.UUID, stage: str, status: str, output: Path | None = None,
            log: str | None = None) -> None:
    session.add(Render(video_id=video_id, kind="longform", stage=stage, status=status,
                       output_uri=str(output) if output else None, log=log,
                       tool_versions={"encoder": pick_encoder()}))


def _render_clip(image: Path, out: Path, vf: str, frames: int, config: ChannelConfig) -> None:
    run_ffmpeg(["-i", str(image), "-filter_complex", vf, "-map", "[v]", "-frames:v", str(frames),
                "-r", str(config.video.long_form.fps), *video_codec_args(config), "-an", str(out)])


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.ALIGNED:
        return

    config = get_config()
    scenes = list(session.execute(select(Scene).filter_by(video_id=video_id).order_by(Scene.idx)).scalars())
    if not scenes or any(s.image_asset_id is None or s.start_s is None for s in scenes):
        raise ValueError(f"video {video_id}: scenes are missing images or timing")
    wp = work_dir(video_id)
    narration, words = wp / "narration.wav", wp / "words.json"
    for needed in (narration, words):
        if not needed.exists():
            raise FileNotFoundError(f"video {video_id}: {needed.name} not found in {wp}")

    assets = {a.id: a for a in session.execute(
        select(Asset).where(Asset.id.in_({s.image_asset_id for s in scenes}))).scalars()}
    size, fps = config.video.long_form.resolution, config.video.long_form.fps
    motions = choose_motions([s.image_asset_id for s in scenes])
    frames = frame_counts([(s.start_s, s.end_s) for s in scenes], fps)

    stage = "clips"
    try:
        jobs = []
        for scene, motion, n in zip(scenes, motions, frames, strict=True):
            image = Path(assets[scene.image_asset_id].uri)
            with Image.open(image) as im:
                aspect = im.width / im.height
            jobs.append((image, wp / f"clip_{scene.idx:04d}.mp4", clip_filter(motion, n, size, fps, aspect), n))
        with ThreadPoolExecutor(max_workers=RENDER_WORKERS) as pool:
            list(pool.map(lambda job: _render_clip(*job, config), jobs))
        atomic_write_text(wp / "concat.txt", "".join(f"file '{out.name}'\n" for _, out, _, _ in jobs))
        _record(session, video_id, stage, "success", log=f"{len(jobs)} clips")

        stage = "final"
        ass, srt = build_subtitles(json.loads(words.read_text(encoding="utf-8")), config,
                                   resolution=size, size=config.subtitles.size)
        ass.save(str(wp / "subtitles.ass"))
        srt.save(str(output_dir(video_id) / "final.srt"))
        font = font_path(config)
        shutil.copy2(font, wp / font.name)  # libass finds it via fontsdir=. (cwd), no drive-letter colon

        music = pick_music(video_id)
        final = output_dir(video_id) / "final.mp4"
        # Filter strings must not contain Windows paths ("C:" breaks option parsing — seen
        # live), so ffmpeg runs inside the work dir and filters use bare file names.
        run_ffmpeg([
            "-f", "concat", "-safe", "0", "-i", "concat.txt", "-i", "narration.wav",
            *(["-stream_loop", "-1", "-i", music["path"]] if music else []),
            "-filter_complex", f"[0:v]ass=filename=subtitles.ass:fontsdir=.[v];{audio_graph(config, bool(music))}",
            "-map", "[v]", "-map", "[a]", *video_codec_args(config), "-c:a", "aac", "-b:a", "192k",
            "-t", f"{video.duration_s:.3f}", "-movflags", "+faststart", str(final),
        ], cwd=wp)
        rendered = float(probe(final)["format"]["duration"])
        if abs(rendered - video.duration_s) > 0.5:
            raise RuntimeError(f"final render is {rendered:.2f}s, narration is {video.duration_s:.2f}s")
        _record(session, video_id, stage, "success", final)
    except Exception as exc:
        _record(session, video_id, stage, "failed", log=str(exc)[-4000:])
        raise

    if music:
        video.video_metadata = {**(video.video_metadata or {}),
                                "music": {k: music.get(k) for k in ("file", "title", "artist", "license", "credit")}}
    video.status = Status.ASSEMBLED
