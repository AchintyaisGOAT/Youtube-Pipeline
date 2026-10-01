"""Shorts (README §4.1 steps 12–14, §6.4): metadata and thumbnail first, then each
`[SHORT]` passage re-rendered vertically from its scenes, ending on a card that sends the
viewer to the long-form video.

- **Nothing in a picture is lost.** Each image is shown whole, fitted inside a box a
  little narrower than the 1080-px frame, on a blurred, darkened copy of itself that
  fills the rest. A slow zoom (at most 4%) adds motion; the box margin means even the
  zoom never pushes an edge off screen. The blurred bands above and below carry the
  title caption and the subtitles, so neither covers the picture.
- **The ending** (~4 s): the thumbnail in the middle of the screen with a glowing frame
  in the highlight colour, "WATCH THE FULL STORY" above it, while the narrator says one
  line pointing to the linked video (the upload kit tells you to set it as the Short's
  Related video in Studio). The line rotates so the Shorts don't all end the same way.
- **Length:** passage + ending stay within `video.shorts.max_seconds`; a passage that
  would run over is cut at the last scene that ends a sentence.
- Sound: the passage's own narration, the long-form's music bed, loudness-normalised.

Writes output_dir/short_NN.mp4 + short_NN.srt (NN from 01); the package stage moves them
into the upload kit.
"""

from __future__ import annotations

import json
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pysubs2
import soundfile as sf
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import assets
from app.config import ChannelConfig, get_config
from app.db import Asset, Render, Scene, Short, Video
from app.ffmpeg import probe, run_ffmpeg, video_codec_args
from app.stages import narrate
from app.stages.assemble import audio_graph, frame_counts
from app.stages.metadata import write_metadata
from app.stages.thumbnail import write_thumbnail
from app.status import Status
from app.storage import output_dir, work_dir
from app.subtitles import _ass_color, build_subtitles, font_family, font_path

#: Spoken over the closing card; rotated by the Short's number.
OUTRO_LINES = (
    "Want the whole story? Tap the video linked below.",
    "That's only part of it. The full story is linked right below.",
    "There's a lot more to this one. Watch the full video, linked below.",
    "Want to know how it all ends? The full story is just below.",
)
OUTRO_TITLE = "WATCH THE FULL STORY"
ZOOM = 1.04
#: The image box, as a share of the frame: 0.925 * 1.04 zoom < 1, so edges stay on screen,
#: and even a tall portrait stays clear of the caption above and the subtitles below.
BOX_W, BOX_H = 0.925, 0.58
CENTER_Y = 0.5
#: Closing card: thumbnail width as a share of the frame, frame and glow in px.
CARD_W, CARD_BORDER, CARD_GLOW = 0.85, 12, 36
OUTRO_LEAD_S, OUTRO_TAIL_S = 0.25, 0.6
SUPERSAMPLE = 2
RENDER_WORKERS = 4


# --------------------------------------------------------------------------- #
# planning (pure — unit-tested)
# --------------------------------------------------------------------------- #
def passage(scenes: list[Scene], short: Short, limit: float) -> tuple[list[Scene], float]:
    """(the Short's scenes, its length). Over `limit`, it ends at the last scene that
    finishes a sentence and fits; failing that, at the last scene that fits (a scene edge,
    never mid-word); only a first scene longer than the limit is cut at the limit."""
    span = [s for s in scenes if s.start_s is not None and short.start_s - 0.01 <= s.start_s < short.end_s]
    if not span:
        return [], 0.0
    start = span[0].start_s
    if span[-1].end_s - start <= limit:
        return span, span[-1].end_s - start
    ends = [s for s in span if s.end_s - start <= limit and ends_sentence(s.text)]
    if ends:
        keep = span[: span.index(ends[-1]) + 1]
        return keep, keep[-1].end_s - start
    fits = [s for s in span if s.end_s - start <= limit]
    if fits:
        return fits, fits[-1].end_s - start
    return span[:1], limit


def ends_sentence(text: str) -> bool:
    return narrate.pause_after(text, 1000) == 1.0  # a full pause is only given after a sentence end


def _zoom(frames: int, zoom_in: bool) -> str:
    p = f"on/{max(1, frames - 1)}"
    return f"1+{ZOOM - 1:.3f}*{p}" if zoom_in else f"{ZOOM}-{ZOOM - 1:.3f}*{p}"


def _zoompan(frames: int, zoom_in: bool, size: tuple[int, int], fps: int) -> str:
    return (f"zoompan=z='{_zoom(frames, zoom_in)}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames}:s={size[0]}x{size[1]}:fps={fps},setsar=1")


def _blurred_background(label: str, cw: int, ch: int, blur: int = 8, darken: float = 0.18) -> str:
    small_w, small_h = cw // 8, ch // 8  # blur at 1/8 size: same look, a fraction of the work
    return (f"[{label}]scale={small_w}:{small_h}:force_original_aspect_ratio=increase,"
            f"crop={small_w}:{small_h},boxblur={blur}:2,eq=brightness=-{darken},scale={cw}:{ch},setsar=1")


def scene_filter(frames: int, size: tuple[int, int], fps: int, zoom_in: bool) -> str:
    """The whole image, fitted in the box, on a blurred copy of itself."""
    cw, ch = size[0] * SUPERSAMPLE, size[1] * SUPERSAMPLE
    bw, bh = round(cw * BOX_W) // 2 * 2, round(ch * BOX_H) // 2 * 2
    return (f"[0:v]split[a][b];{_blurred_background('a', cw, ch)}[bg];"
            f"[b]scale={bw}:{bh}:force_original_aspect_ratio=decrease,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:{round(ch * CENTER_Y)}-h/2,{_zoompan(frames, zoom_in, size, fps)}[v]")


def outro_filter(frames: int, size: tuple[int, int], fps: int, color: str) -> str:
    """The thumbnail as a framed card with a soft glow, on a blurred copy of itself."""
    cw, ch = size[0] * SUPERSAMPLE, size[1] * SUPERSAMPLE
    tw = round(cw * CARD_W) // 2 * 2
    th = round(tw * 9 / 16) // 2 * 2
    border, glow = CARD_BORDER * SUPERSAMPLE, CARD_GLOW * SUPERSAMPLE
    gw, gh = tw + 2 * (border + glow), th + 2 * (border + glow)
    cy = round(ch * CENTER_Y)
    hexcolor = "0x" + color.lstrip("#")
    return (f"[0:v]split[a][b];{_blurred_background('a', cw, ch, blur=20, darken=0.35)},"
            f"drawbox=x={(cw - gw) // 2}:y={cy - gh // 2}:w={gw}:h={gh}:color={hexcolor}@0.85:t=fill,"
            f"boxblur={glow}:2[bg];"
            f"[b]scale={tw}:{th},setsar=1,pad=iw+{2 * border}:ih+{2 * border}:{border}:{border}:color={hexcolor}[card];"
            f"[bg][card]overlay=(W-w)/2:{cy}-h/2,{_zoompan(frames, True, size, fps)}[v]")


def add_title_events(ass: pysubs2.SSAFile, config: ChannelConfig, size: tuple[int, int], caption: str,
                     passage_end: float, total: float, outro_line: str) -> None:
    """The caption above the picture for the passage; the ending's title and spoken line."""
    width, height = size
    family = font_family(font_path(config))
    highlight, _ = _ass_color(config.subtitles.highlight_color)
    ass.styles["Title"] = pysubs2.SSAStyle(
        fontname=family, fontsize=round(config.subtitles.shorts_size * 0.95), bold=True,
        primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0),
        borderstyle=1, outline=6, shadow=0, alignment=pysubs2.Alignment(8),
        marginl=round(width * 0.07), marginr=round(width * 0.07), marginv=round(height * 0.085))
    ass.styles["OutroTitle"] = ass.styles["Title"].copy()
    ass.styles["OutroTitle"].primarycolor = highlight
    ass.styles["OutroTitle"].marginv = round(height * 0.16)
    clean = caption.replace("{", "").replace("}", "").replace("\\", "")
    ass.events.append(pysubs2.SSAEvent(start=0, end=pysubs2.make_time(s=passage_end), style="Title",
                                       text=f"{{\\fad(250,200)}}{clean}"))
    ass.events.append(pysubs2.SSAEvent(start=pysubs2.make_time(s=passage_end), end=pysubs2.make_time(s=total),
                                       style="OutroTitle", text=f"{{\\fad(200,0)}}{OUTRO_TITLE}"))
    ass.events.append(pysubs2.SSAEvent(start=pysubs2.make_time(s=passage_end + OUTRO_LEAD_S),
                                       end=pysubs2.make_time(s=total), text=outro_line))


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def _outro_audio(work: Path, n: int, voice: str) -> np.ndarray:
    """The spoken ending, read once per line and cached in the work dir."""
    cached = work / f"outro_{n}.wav"
    if cached.exists():
        return sf.read(cached, dtype="float32")[0]
    samples, rate = narrate._kokoro_instance().create(OUTRO_LINES[n], voice=voice, lang="en-us")
    if rate != narrate.SAMPLE_RATE:
        raise ValueError(f"Kokoro returned {rate} Hz audio, expected {narrate.SAMPLE_RATE}")
    sf.write(cached, samples.astype(np.float32), rate)
    return samples.astype(np.float32)


def _render_still(image: Path, out: Path, vf: str, frames: int, config: ChannelConfig) -> None:
    run_ffmpeg(["-i", str(image), "-filter_complex", vf, "-map", "[v]", "-frames:v", str(frames),
                "-r", str(config.video.shorts.fps), *video_codec_args(config), "-an", str(out)])


def _render_short(video: Video, short: Short, scenes: list[Scene], images: dict,
                  timeline: list[dict], narration: np.ndarray, config: ChannelConfig, music: dict | None) -> Path:
    shorts_cfg, rate = config.video.shorts, narrate.SAMPLE_RATE
    size, fps = shorts_cfg.resolution, shorts_cfg.fps
    work, name = work_dir(video.id), f"short_{short.idx + 1:02d}"
    line_n = short.idx % len(OUTRO_LINES)
    outro_voice = _outro_audio(work, line_n, config.voice.primary)
    outro_s = OUTRO_LEAD_S + len(outro_voice) / rate + OUTRO_TAIL_S

    kept, length = passage(scenes, short, shorts_cfg.max_seconds - outro_s)
    if not kept:
        raise ValueError(f"short {short.idx}: no scenes in its span")
    start = kept[0].start_s

    # audio: the passage's narration, then the spoken ending
    a, b = round(start * rate), round(start * rate) + round(length * rate)
    audio = np.concatenate([narration[a:b], np.zeros(round(OUTRO_LEAD_S * rate), np.float32), outro_voice,
                            np.zeros(round(OUTRO_TAIL_S * rate), np.float32)])
    sf.write(work / f"{name}.wav", audio, rate)
    total = len(audio) / rate

    # pictures: one clip per scene, then the ending card
    bounds = [(s.start_s - start, min(s.end_s - start, length)) for s in kept]
    frames = frame_counts(bounds, fps)
    outro_frames = max(1, round(total * fps) - sum(frames))
    jobs = [(Path(images[s.image_asset_id].uri), work / f"{name}_{i:03d}.mp4",
             scene_filter(n, size, fps, zoom_in=(s.idx % 2 == 0)), n)
            for i, (s, n) in enumerate(zip(kept, frames, strict=True))]
    jobs.append((Path(video.thumbnail_uri), work / f"{name}_end.mp4",
                 outro_filter(outro_frames, size, fps, config.subtitles.highlight_color), outro_frames))
    with ThreadPoolExecutor(max_workers=RENDER_WORKERS) as pool:
        list(pool.map(lambda job: _render_still(*job, config), jobs))
    (work / f"{name}_concat.txt").write_text("".join(f"file '{out.name}'\n" for _, out, _, _ in jobs), encoding="utf-8")

    # subtitles: the passage's words, the caption above, the ending's lines
    ass, srt = build_subtitles(timeline, config, resolution=size, size=config.subtitles.shorts_size,
                               window=(start, start + length))
    add_title_events(ass, config, size, short.caption or "", length, total, OUTRO_LINES[line_n])
    srt.events.append(pysubs2.SSAEvent(start=pysubs2.make_time(s=length + OUTRO_LEAD_S),
                                       end=pysubs2.make_time(s=total), text=OUTRO_LINES[line_n]))
    ass.save(str(work / f"{name}.ass"))
    out_dir = output_dir(video.id)
    srt.save(str(out_dir / f"{name}.srt"))
    font = font_path(config)
    shutil.copy2(font, work / font.name)  # libass finds it via fontsdir=. (cwd), no drive-letter colon

    inputs = ["-f", "concat", "-safe", "0", "-i", f"{name}_concat.txt", "-i", f"{name}.wav"]
    if music:
        inputs += ["-stream_loop", "-1", "-i", music["path"]]
    out = out_dir / f"{name}.mp4"
    run_ffmpeg([
        *inputs, "-filter_complex", f"[0:v]ass=filename={name}.ass:fontsdir=.[v];{audio_graph(config, bool(music))}",
        "-map", "[v]", "-map", "[a]", *video_codec_args(config), "-c:a", "aac", "-b:a", "192k",
        "-t", f"{total:.3f}", "-movflags", "+faststart", str(out),
    ], cwd=work)
    rendered = float(probe(out)["format"]["duration"])
    if abs(rendered - total) > 0.5:
        raise RuntimeError(f"short {short.idx} rendered {rendered:.2f}s, expected {total:.2f}s")
    short.start_s, short.end_s = start, start + length
    return out


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.ASSEMBLED:
        return

    config = get_config()
    write_metadata(session, video)  # the captions and the thumbnail hook come from here
    write_thumbnail(session, video)

    scenes = list(session.execute(select(Scene).filter_by(video_id=video_id).order_by(Scene.idx)).scalars())
    shorts = list(session.execute(select(Short).filter_by(video_id=video_id).order_by(Short.idx)).scalars())
    work = work_dir(video_id)
    timeline = json.loads((work / "words.json").read_text(encoding="utf-8"))
    narration, rate = sf.read(work / "narration.wav", dtype="float32")
    if rate != narrate.SAMPLE_RATE:
        raise ValueError(f"narration is {rate} Hz, expected {narrate.SAMPLE_RATE}")
    images = {a.id: a for a in session.execute(
        select(Asset).where(Asset.id.in_({s.image_asset_id for s in scenes}))).scalars()}
    music = assets.pick_music(video_id)

    for short in shorts:  # a failure fails the video, with the error saved (orchestrator)
        out = _render_short(video, short, scenes, images, timeline, narration, config, music)
        short.status = "rendered"  # per-Short state, not a video Status (see db.Short)
        session.add(Render(video_id=video_id, short_id=short.id, kind="short", stage="render",
                           status="success", output_uri=str(out)))
    video.status = Status.SHORTS_READY
