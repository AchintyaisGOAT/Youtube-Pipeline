"""Kokoro TTS: narrate the video into one audio file under work_dir(video_id).

Reads the clean, ordered `segment.text` rows Content produced — never `video.script`
directly, which still has the raw `[SHORT]...[/SHORT]` markup in it and would get
spoken verbatim as words if fed to the TTS engine. Sets `video.duration_s` from the
actual rendered audio, not an estimate.
"""

from __future__ import annotations

import uuid

import numpy as np
import soundfile as sf
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_channel_config
from app.db import Segment, Video
from app.status import Status
from app.storage import work_dir

_kokoro = None
_FALLBACK_SAMPLE_RATE = 24000


def _kokoro_instance():
    global _kokoro
    if _kokoro is None:
        from kokoro_onnx import Kokoro

        _kokoro = Kokoro("kokoro-v1.0.onnx", "voices-v1.0.bin")
    return _kokoro


def _narration_sentences(session: Session, video_id: uuid.UUID) -> list[str]:
    return list(
        session.execute(select(Segment.text).filter_by(video_id=video_id).order_by(Segment.idx)).scalars()
    )


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.SYNTHESIZING_VOICE:
        return

    sentences = _narration_sentences(session, video_id)
    if not sentences:
        raise ValueError(f"video {video_id}: no segments to narrate")

    config = get_channel_config(session, video.channel_id)
    kokoro = _kokoro_instance()

    chunks: list[np.ndarray] = []
    pause: np.ndarray | None = None
    sample_rate = _FALLBACK_SAMPLE_RATE
    for sentence in sentences:
        samples, sample_rate = kokoro.create(sentence, voice=config.voice.primary, speed=1.0, lang="en-us")
        chunks.append(samples)
        if pause is None:
            pause = np.zeros(int(sample_rate * config.voice.paragraph_pause_ms / 1000), dtype=samples.dtype)
        chunks.append(pause)

    audio = np.concatenate(chunks[:-1])  # drop the trailing pause after the last sentence
    out_path = work_dir(video_id) / "narration.wav"
    sf.write(out_path, audio, sample_rate)

    video.duration_s = len(audio) / sample_rate
    video.status = Status.ALIGNING
