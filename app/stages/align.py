"""Word timing (README §4.1 step 10): faster-whisper finds when each word is spoken, for
the highlighted-word subtitles; then the Shorts' cut points become `short` rows.

Scene start/end times are already exact (the narrate stage measured them from the audio);
Whisper only places words *inside* each scene. The displayed words are always the
script's own words (Whisper's spelling of names and numbers is never shown):
- if Whisper heard as many words in a scene as the script has, they're paired 1:1;
- otherwise the script's words are spread over the span Whisper heard speech in that
  scene, in proportion to their length.
Either way a mismatch stays inside one 5–7 s scene instead of drifting across the video.

Writes work_dir/words.json: `[{"scene": idx, "words": [{"text", "start", "end", "quote"}]}]`.
Runs on CPU (int8) — README §2: no CUDA on this machine. The model downloads into
data/models/whisper/ on first use.
"""

from __future__ import annotations

import json
import os
import re
import uuid

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import Scene, Short, Video
from app.stages.narrate import runs
from app.status import Status
from app.storage import atomic_write_text, models_dir, work_dir

_model = None
_WORD = re.compile(r"\S+")


def _whisper(model_name: str):
    global _model
    if _model is None:
        # The model downloader warns about Windows symlinks (the cache just uses a bit more
        # disk) and about not being logged in to Hugging Face (not needed for public models).
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        os.environ.setdefault("HF_HUB_VERBOSITY", "error")
        from faster_whisper import WhisperModel

        _model = WhisperModel(model_name, device="cpu", compute_type="int8",
                              download_root=str(models_dir() / "whisper"))
    return _model


def transcribe(audio_path, model_name: str) -> list[tuple[float, float]]:
    """(start, end) of every word Whisper hears, in order."""
    segments, _info = _whisper(model_name).transcribe(str(audio_path), word_timestamps=True, language="en")
    return [(w.start, w.end) for segment in segments for w in (segment.words or [])]


def script_words(text: str) -> list[tuple[str, bool]]:
    """(word, is_quote) for a scene's text, quote tags removed."""
    return [(word, is_quote) for piece, is_quote in runs(text) for word in _WORD.findall(piece)]


def time_words(words: list[tuple[str, bool]], scene_start: float, scene_end: float,
               heard: list[tuple[float, float]]) -> list[dict]:
    """Timestamps for one scene's script words, given the words Whisper heard in it."""
    if not words:
        return []
    if len(heard) == len(words):
        times = heard
    else:
        span_start, span_end = (heard[0][0], heard[-1][1]) if heard else (scene_start, scene_end)
        weights = [len(w) + 1 for w, _ in words]  # +1: short words still take some time
        total, cursor, times = sum(weights), span_start, []
        for weight in weights:
            step = (span_end - span_start) * weight / total
            times.append((cursor, cursor + step))
            cursor += step
    return [{"text": w, "start": round(s, 3), "end": round(e, 3), "quote": q}
            for (w, q), (s, e) in zip(words, times, strict=True)]


def _create_shorts(session: Session, video_id: uuid.UUID, scenes: list[Scene]) -> None:
    """One `short` row per run of consecutive in-short-span scenes. Delete-then-recreate
    so a re-run (e.g. after a send-back) can't hit the (video_id, idx) unique constraint."""
    session.execute(delete(Short).where(Short.video_id == video_id))
    idx, run_start, run_end = 0, None, None
    for scene in [*scenes, None]:  # sentinel flushes a trailing run
        if scene is not None and scene.in_short_span:
            run_start = scene.start_s if run_start is None else run_start
            run_end = scene.end_s
            continue
        if run_start is not None:
            session.add(Short(video_id=video_id, idx=idx, start_s=run_start, end_s=run_end))
            idx, run_start, run_end = idx + 1, None, None


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.NARRATED:
        return

    scenes = list(session.execute(select(Scene).filter_by(video_id=video_id).order_by(Scene.idx)).scalars())
    if not scenes or any(s.start_s is None or s.end_s is None for s in scenes):
        raise ValueError(f"video {video_id}: scenes have no narration timing")
    audio_path = work_dir(video_id) / "narration.wav"
    if not audio_path.exists():
        raise FileNotFoundError(f"video {video_id}: narration audio not found at {audio_path}")

    heard = transcribe(audio_path, get_config().alignment.whisper_model)
    timeline = []
    for scene in scenes:
        inside = [(s, e) for s, e in heard if scene.start_s <= (s + e) / 2 < scene.end_s]
        timeline.append({"scene": scene.idx,
                         "words": time_words(script_words(scene.text), scene.start_s, scene.end_s, inside)})
    atomic_write_text(work_dir(video_id) / "words.json", json.dumps(timeline))

    _create_shorts(session, video_id, scenes)
    video.status = Status.ALIGNED
