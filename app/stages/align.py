"""Word timing (README §4.1 step 10): faster-whisper finds when each word is spoken, for
the highlighted-word subtitles; then the Shorts' cut points become `short` rows.

Scene start/end times are already exact (the narrate stage measured them from the audio).
The displayed words are always the script's own words — Whisper's spelling of names and
numbers is never shown — so the script is *aligned* to what Whisper heard:

1. Both sides are normalised into comparable tokens: lowercase, punctuation dropped, and
   script words split at hyphens/dashes ("thirty-two-year-old" -> thirty, two, year, old;
   "dollars—worth" -> dollars, worth), since Whisper hears those as separate words.
2. The two token sequences are matched in order (difflib), across the whole narration.
3. A script word takes its time from its matched tokens; an unmatched word is spread
   between the nearest matched neighbours by length; every word is kept inside its scene.

A live run showed why: requiring equal word counts per scene sent 42% of the words to a
proportional guess (any hyphenated word or number broke the count), and the highlight
visibly ran ahead of or behind the voice. Matching tokens keeps each word on its real time.

Writes work_dir/words.json: `[{"scene": idx, "words": [{"text", "start", "end", "quote"}]}]`.
Runs on CPU (int8) — README §2: no CUDA on this machine. The model downloads into
data/models/whisper/ on first use.
"""

from __future__ import annotations

import difflib
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
_SPLIT = re.compile(r"[-‐‑‒–—/]+")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")


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


def transcribe(audio_path, model_name: str) -> list[tuple[str, float, float]]:
    """(word, start, end) for every word Whisper hears, in order."""
    segments, _info = _whisper(model_name).transcribe(str(audio_path), word_timestamps=True, language="en")
    return [(w.word, w.start, w.end) for segment in segments for w in (segment.words or [])]


def tokens(word: str) -> list[str]:
    """Comparable tokens for one word: split at hyphens/dashes, lowercase, alphanumerics only."""
    return [t for t in (_NON_ALNUM.sub("", part.lower()) for part in _SPLIT.split(word)) if t]


def script_words(text: str) -> list[tuple[str, bool]]:
    """(word, is_quote) for a scene's text, quote tags removed."""
    return [(word, is_quote) for piece, is_quote in runs(text) for word in _WORD.findall(piece)]


def align_words(words: list[str], heard: list[tuple[str, float, float]]) -> list[tuple[float, float] | None]:
    """(start, end) for each script word from the heard words it matches, or None."""
    script_tokens, owner = [], []
    for i, word in enumerate(words):
        for token in tokens(word):
            script_tokens.append(token)
            owner.append(i)
    heard_tokens, heard_times = [], []
    for word, start, end in heard:
        for token in tokens(word):
            heard_tokens.append(token)
            heard_times.append((start, end))

    times: list[tuple[float, float] | None] = [None] * len(words)
    matcher = difflib.SequenceMatcher(None, script_tokens, heard_tokens, autojunk=False)
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            i = owner[block.a + k]
            start, end = heard_times[block.b + k]
            times[i] = (start, end) if times[i] is None else (min(times[i][0], start), max(times[i][1], end))
    return times


def fill_gaps(words: list[str], times: list[tuple[float, float] | None], start: float,
              end: float) -> list[tuple[float, float]]:
    """Spread unmatched words between their matched neighbours (or the given bounds) by
    length; keep everything inside [start, end] and in order."""
    filled: list[tuple[float, float]] = []
    i = 0
    while i < len(words):
        if times[i] is not None:
            s, e = times[i]
            s = max(s, filled[-1][1] if filled else start, start)
            filled.append((min(s, end), min(max(e, s), end)))
            i += 1
            continue
        j = i
        while j < len(words) and times[j] is None:
            j += 1
        left = filled[-1][1] if filled else start
        right = times[j][0] if j < len(words) else end
        right = max(left, min(right, end))
        weights = [len(w) + 1 for w in words[i:j]]
        cursor, total = left, sum(weights)
        for weight in weights:
            step = (right - left) * weight / total
            filled.append((cursor, cursor + step))
            cursor += step
        i = j
    return filled


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


def timeline_for(scenes: list[Scene], heard: list[tuple[str, float, float]]) -> list[dict]:
    per_scene = [script_words(scene.text) for scene in scenes]
    flat = [word for words in per_scene for word, _ in words]
    matched = align_words(flat, heard)

    timeline, cursor = [], 0
    for scene, words in zip(scenes, per_scene, strict=True):
        n = len(words)
        # a match outside its own scene (a repeated phrase) is wrong for this word — drop it
        own = [t if t is not None and scene.start_s - 0.25 <= t[0] < scene.end_s else None
               for t in matched[cursor : cursor + n]]
        times = fill_gaps([w for w, _ in words], own, scene.start_s, scene.end_s)
        timeline.append({"scene": scene.idx, "words": [
            {"text": w, "start": round(s, 3), "end": round(e, 3), "quote": q}
            for (w, q), (s, e) in zip(words, times, strict=True)
        ]})
        cursor += n
    return timeline


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
    atomic_write_text(work_dir(video_id) / "words.json", json.dumps(timeline_for(scenes, heard)))

    _create_shorts(session, video_id, scenes)
    video.status = Status.ALIGNED
