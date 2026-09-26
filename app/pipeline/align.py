"""faster-whisper word-level alignment (DESIGN.md #14): transcribe the narration
Kokoro produced, match recognized words against the known segment text to time each
segment, then merge contiguous `in_short_span` segments into `short` rows with their
cut points.

If Whisper's recognized word count is too far off the known script's word count for
forced (positional) alignment to be trustworthy, fall back to distributing
`video.duration_s` proportionally by each segment's word count instead of trusting raw
Whisper timestamps.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import get_channel_config
from app.db import Segment, Short, Video
from app.status import Status
from app.storage import work_dir

_model = None
_model_is_cuda = False
_WORD = re.compile(r"[A-Za-z0-9']+")


def _load_model(model_name: str, device: str, compute_type: str):
    from faster_whisper import WhisperModel

    return WhisperModel(model_name, device=device, compute_type=compute_type)


def _ensure_model(model_name: str) -> None:
    global _model, _model_is_cuda
    if _model is None:
        try:
            _model = _load_model(model_name, "cuda", "float16")
            _model_is_cuda = True
        except Exception:
            _model = _load_model(model_name, "cpu", "int8")
            _model_is_cuda = False


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _run_transcribe(model, audio_path: Path, prompt: str) -> list:
    segments, _info = model.transcribe(
        str(audio_path), word_timestamps=True, initial_prompt=prompt[:800], language="en"
    )
    return list(segments)  # force the generator now so a lazy CUDA error surfaces here, not later


def _transcribe_words(audio_path: Path, prompt: str, model_name: str) -> list[tuple[float, float]]:
    """Flat (start, end) per recognized word, in order.

    CTranslate2 apparently loads its CUDA runtime libraries lazily on first actual
    inference, not at WhisperModel construction -- verified live that a `device="cuda"`
    model constructs successfully on a machine missing cublas64_12.dll, then fails only
    once `.transcribe()` is actually iterated. Constructing on CUDA is therefore not
    proof CUDA works; only a real transcribe call is. Falls back to a fresh CPU model
    and retries once if the CUDA path fails here."""
    global _model, _model_is_cuda
    _ensure_model(model_name)
    try:
        segments = _run_transcribe(_model, audio_path, prompt)
    except Exception:
        if not _model_is_cuda:
            raise
        _model = _load_model(model_name, "cpu", "int8")
        _model_is_cuda = False
        segments = _run_transcribe(_model, audio_path, prompt)

    out: list[tuple[float, float]] = []
    for seg in segments:
        for word in seg.words or []:
            out.append((word.start, word.end))
    return out


def _forced_alignment(segments: list[Segment], recognized: list[tuple[float, float]]) -> bool:
    """Consume `recognized` words positionally per segment's expected word count.
    Returns False (caller should fall back) if there aren't even enough recognized
    words to cover every segment."""
    cursor = 0
    for segment in segments:
        n = max(1, len(_words(segment.text)))
        chunk = recognized[cursor : cursor + n]
        if not chunk:
            return False
        segment.start_s = chunk[0][0]
        segment.end_s = chunk[-1][1]
        cursor += n
    return True


def _proportional_timing(segments: list[Segment], duration_s: float) -> None:
    counts = [max(1, len(_words(s.text))) for s in segments]
    total = sum(counts)
    cursor = 0.0
    for segment, count in zip(segments, counts, strict=True):
        span = duration_s * (count / total)
        segment.start_s = cursor
        segment.end_s = cursor + span
        cursor += span


def _create_shorts(session: Session, video_id: uuid.UUID, segments: list[Segment]) -> None:
    """Idempotent: a stage must be safe to re-run (WORK_MEDIA.md §5) -- verified live
    that re-running align.py on a video that already has Short rows (e.g. re-processing
    after an upstream fix, same as any other retry) hit a UNIQUE constraint on
    (video_id, idx) trying to insert a second set alongside the first. Delete-then-
    recreate rather than skip-if-exists, since re-alignment can legitimately produce
    different timings than the previous pass."""
    session.execute(delete(Short).where(Short.video_id == video_id))

    run_start: float | None = None
    run_end: float | None = None
    idx = 0
    for segment in [*segments, None]:  # sentinel flushes a trailing run
        in_span = segment is not None and segment.in_short_span
        if in_span:
            if run_start is None:
                run_start = segment.start_s
            run_end = segment.end_s
            continue
        if run_start is not None:
            session.add(Short(video_id=video_id, idx=idx, start_s=run_start, end_s=run_end))
            idx += 1
            run_start = run_end = None


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.ALIGNING:
        return

    segments = list(
        session.execute(select(Segment).filter_by(video_id=video_id).order_by(Segment.idx)).scalars()
    )
    if not segments:
        raise ValueError(f"video {video_id}: no segments to align")

    config = get_channel_config(session, video.channel_id)
    audio_path = work_dir(video_id) / "narration.wav"
    if not audio_path.exists():
        raise FileNotFoundError(f"video {video_id}: narration audio not found at {audio_path}")

    full_text = " ".join(s.text for s in segments)
    recognized = _transcribe_words(audio_path, full_text, config.alignment.whisper_model)

    expected_count = sum(len(_words(s.text)) for s in segments)
    mismatch_ratio = abs(expected_count - len(recognized)) / max(1, expected_count)

    aligned = mismatch_ratio <= config.alignment.mismatch_fallback_ratio and _forced_alignment(
        segments, recognized
    )
    if not aligned:
        if not video.duration_s:
            raise ValueError(f"video {video_id}: no duration_s available for proportional-timing fallback")
        _proportional_timing(segments, video.duration_s)

    _create_shorts(session, video_id, segments)
    video.status = Status.ASSEMBLING
