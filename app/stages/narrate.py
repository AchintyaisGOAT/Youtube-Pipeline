"""Narration (README §4.1 step 9, §6.2): Kokoro reads every scene into one WAV under
work_dir(video_id), and each scene's exact start/end is taken from the audio itself.

- `[QUOTE]...[/QUOTE]` text is read in the quote voice (`voice.quotes`), the rest in
  `voice.primary`; the tags themselves are never spoken.
- A scene that ends a sentence is followed by `voice.paragraph_pause_ms` of silence; a
  scene cut mid-sentence (a long sentence split at a comma) gets only a short breath, so
  the sentence still flows.
- The pause after a scene belongs to that scene: scene i ends exactly where scene i+1
  starts, and the last one ends at the end of the audio. The pictures are cut on these
  times, so they cover every moment of sound — the old pipeline timed images from the
  first to the last *spoken word*, left the pauses out, and drifted ~0.4 s per sentence.

The Kokoro model files (~350 MB) are downloaded into data/models/kokoro/ on first use.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import numpy as np
import soundfile as sf
from loguru import logger
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.db import Scene, Video
from app.http import get_http_client
from app.status import Status
from app.storage import models_dir, work_dir

MODEL_BASE_URL = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_FILES = ("kokoro-v1.0.onnx", "voices-v1.0.bin")
SAMPLE_RATE = 24_000
#: Silence between a quote and the narration around it, and after a mid-sentence cut.
BREATH_S = 0.12

_QUOTE = re.compile(r"\[QUOTE\](.*?)\[/QUOTE\]", re.DOTALL)
_SENTENCE_END = re.compile(r"""[.!?]["'”’)\]]*$""")

_kokoro = None


def _download(name: str, target: Path) -> None:
    logger.info("downloading Kokoro model file {} (first run only)", name)
    tmp = target.with_name(target.name + ".part")
    with get_http_client().stream("GET", f"{MODEL_BASE_URL}/{name}", follow_redirects=True, timeout=600) as resp:
        resp.raise_for_status()
        with tmp.open("wb") as fh:
            for chunk in resp.iter_bytes(1 << 20):
                fh.write(chunk)
    tmp.replace(target)


def model_files() -> tuple[Path, Path]:
    folder = models_dir() / "kokoro"
    folder.mkdir(parents=True, exist_ok=True)
    paths = [folder / name for name in MODEL_FILES]
    for path in paths:
        if not path.exists():
            _download(path.name, path)
    return paths[0], paths[1]


def _kokoro_instance():
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro

        _kokoro = Kokoro(*(str(p) for p in model_files()))
    return _kokoro


def runs(text: str) -> list[tuple[str, bool]]:
    """(text, is_quote) pieces in order, tags removed, empty pieces dropped."""
    out: list[tuple[str, bool]] = []
    cursor = 0
    for match in _QUOTE.finditer(text):
        out.append((text[cursor : match.start()], False))
        out.append((match.group(1), True))
        cursor = match.end()
    out.append((text[cursor:], False))
    return [(t.strip(), q) for t, q in out if t.strip()]


def pause_after(text: str, paragraph_pause_ms: int) -> float:
    plain = _QUOTE.sub(lambda m: m.group(1), text).strip()
    return paragraph_pause_ms / 1000 if _SENTENCE_END.search(plain) else BREATH_S


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.IMAGES_READY:
        return

    scenes = list(session.execute(select(Scene).filter_by(video_id=video_id).order_by(Scene.idx)).scalars())
    if not scenes:
        raise ValueError(f"video {video_id}: no scenes to narrate")

    voice = get_config().voice
    kokoro = _kokoro_instance()
    breath = np.zeros(int(SAMPLE_RATE * BREATH_S), dtype=np.float32)

    chunks: list[np.ndarray] = []
    cursor = 0.0
    for scene in scenes:
        parts: list[np.ndarray] = []
        for text, is_quote in runs(scene.text):
            samples, rate = kokoro.create(text, voice=voice.quotes if is_quote else voice.primary, lang="en-us")
            if rate != SAMPLE_RATE:
                raise ValueError(f"Kokoro returned {rate} Hz audio, expected {SAMPLE_RATE}")
            if parts:
                parts.append(breath)
            parts.append(samples.astype(np.float32))
        if not parts:
            raise ValueError(f"video {video_id}: scene {scene.idx} has no text to read")
        parts.append(np.zeros(int(SAMPLE_RATE * pause_after(scene.text, voice.paragraph_pause_ms)), dtype=np.float32))
        audio = np.concatenate(parts)
        chunks.append(audio)
        scene.start_s = cursor
        cursor += len(audio) / SAMPLE_RATE
        scene.end_s = cursor

    out_path = work_dir(video_id) / "narration.wav"
    sf.write(out_path, np.concatenate(chunks), SAMPLE_RATE)
    video.duration_s = cursor
    video.status = Status.NARRATED
