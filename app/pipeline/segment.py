"""Split `video.script` into `segment` rows: sentence-level narration chunks, a naive
image-search query per segment, and `in_short_span` from the script's `[SHORT]` markup.

Pure text parsing — no Gemini call, no external API. `[SHORT]...[/SHORT]` is internal
markup that script.py writes and only this module reads; `in_short_span` (the boolean
column, not the raw markup) is the actual contract with Media.
"""

from __future__ import annotations

import re
import uuid

from sqlalchemy.orm import Session

from app.db import Segment, Video
from app.status import Status

_SHORT_SPAN = re.compile(r"\[SHORT\](.*?)\[/SHORT\]", re.DOTALL)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
#: A run of capitalized words — a decent proxy for "the name/place/event this sentence
#: is about", which makes a much better image-search query than the full sentence.
_PROPER_NOUN_RUN = re.compile(r"(?:[A-Z][a-zA-Z'.-]*\s*){1,4}")
#: Common sentence-initial words that end up capitalized without being a real proper
#: noun (verified live: "The weapon was likely..." produced the query "The", which
#: matched a random church photo on Wikimedia Commons).
_SENTENCE_INITIAL_STOPWORDS = {
    "the", "a", "an", "it", "this", "that", "these", "those", "while", "instead",
    "however", "although", "when", "where", "who", "what", "why", "how", "but",
    "and", "yet", "so", "if", "then", "there", "here", "some", "many", "most",
    "few", "each", "every",
}


def _image_query(sentence: str) -> str:
    candidates = [m.strip() for m in _PROPER_NOUN_RUN.findall(sentence) if len(m.strip()) > 2]
    # A multi-word run ("Andrew Borden") is almost always a real name/place; a lone
    # capitalized word is just as likely to be sentence-initial capitalization.
    multi_word = [c for c in candidates if " " in c]
    if multi_word:
        return max(multi_word, key=len)
    single_word = [c for c in candidates if c.lower() not in _SENTENCE_INITIAL_STOPWORDS]
    if single_word:
        return max(single_word, key=len)
    return sentence[:120]


def _split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text.strip()) if s.strip()]


def _sentences_with_span_flag(script: str) -> list[tuple[str, bool]]:
    """Strip [SHORT] markers, returning (sentence, in_short_span) in original order."""
    out: list[tuple[str, bool]] = []
    cursor = 0
    for match in _SHORT_SPAN.finditer(script):
        out.extend((s, False) for s in _split_sentences(script[cursor : match.start()]))
        out.extend((s, True) for s in _split_sentences(match.group(1)))
        cursor = match.end()
    out.extend((s, False) for s in _split_sentences(script[cursor:]))
    return out


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.SEGMENTING:
        return
    if not video.script:
        raise ValueError(f"video {video_id}: no script to segment")

    for idx, (text, in_short_span) in enumerate(_sentences_with_span_flag(video.script)):
        session.add(
            Segment(
                video_id=video_id,
                idx=idx,
                text=text,
                image_query=_image_query(text),
                in_short_span=in_short_span,
            )
        )

    video.status = Status.FETCHING_IMAGES
