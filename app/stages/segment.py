"""Scene plan (README §4.1 step 8): the checked script -> `scene` rows of roughly
`video.scene_seconds_min`–`scene_seconds_max` of narration, each showing the image its
passage was written over.

The *text* split is deterministic Python, never an LLM, so the narration can't be
altered between the check stage and the voice:
- The script is cut at every `[IMG n]` mark and `[SHORT]` edge first; scenes never cross
  one. Every scene of a passage shows that passage's image (a passage longer than one
  scene shows it again with a different camera motion), and every scene in a Short span
  gets `in_short_span` (the contract the Shorts stage reads). The marks are not spoken.
- `[QUOTE]...[/QUOTE]` blocks stay whole inside one scene, tags kept for the voice stage.
- Sentences are grouped up to the scene word budget (seconds × words/second). A scene
  never closes below the minimum (no image flashes for under a second); a sentence well
  over budget is split into balanced pieces at commas/semicolons/dashes.

The worker LLM then plans, in one call, each scene's optional sound-effect cue (only tags
the local library has) and optional on-screen text (a place/date label, a number callout
or a chapter card). `clean_extras` enforces the limits and rejects on-screen text the
narration/sources don't back up. If that call fails outright, there are no extras.
"""

from __future__ import annotations

import math
import re
import uuid
from pathlib import Path

from loguru import logger
from sqlalchemy.orm import Session

from app import assets, llm
from app.config import get_config
from app.db import Scene, Topic, Video
from app.quota import RetryLater
from app.status import Status

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "segment.md"

_CUTS = re.compile(r"\[(/?)SHORT\]|\[IMG (\d+)\]")
_QUOTE = re.compile(r"\[QUOTE\].*?\[/QUOTE\]", re.DOTALL)
_TAGS = re.compile(r"\[/?QUOTE\]")
#: Words a cut must not come right after ("the Ides of | March").
_GLUE_WORDS = {"a", "an", "the", "of", "to", "in", "on", "at", "by", "for", "from", "with", "and", "or",
               "but", "as", "his", "her", "their", "its", "was", "were", "is", "had", "has"}
#: How far past the max a scene may run rather than be cut: 1.25 x 7 s ~ 9 s.
SCENE_STRETCH = 1.25
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
def _words(text: str) -> int:
    return len(_TAGS.sub(" ", text).split())


def _merge(pieces: list[str], min_words: float, max_words: float) -> list[str]:
    """Greedy grouping within `max_words`, except that a chunk below `min_words` keeps
    growing (no image flashes for under a second) — but never past `SCENE_STRETCH` x max. A
    leftover below `min_words` is folded into the chunk before it when that still fits."""
    hard_max = SCENE_STRETCH * max_words
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current} {piece}".strip()
        too_long = _words(candidate) > (max_words if _words(current) >= min_words else hard_max)
        if current and too_long:
            chunks.append(current)
            current = piece
        else:
            current = candidate
    if current:
        if chunks and _words(current) < min_words and _words(f"{chunks[-1]} {current}") <= hard_max:
            chunks[-1] = f"{chunks[-1]} {current}"
        else:
            chunks.append(current)
    return chunks


def _clean_cut(words: list[str], b: int) -> bool:
    """True if cutting before words[b] splits neither a name ("Julius | Caesar", "Ides |
    of March") nor a phrase that dangles a small word ("the | Ides")."""
    if words[b][0].isupper() or words[b - 1].lower() in _GLUE_WORDS:
        return False
    in_name = words[b - 1][0].isupper() and words[b].lower() in _GLUE_WORDS
    return not (in_name and b + 1 < len(words) and words[b + 1][0].isupper())


def _split_long(sentence: str, min_words: float, max_words: float) -> list[str]:
    """A sentence over `SCENE_STRETCH` x max is cut into ceil(words / max) near-equal pieces:
    each cut goes at the clause break (comma, semicolon, colon, dash) nearest the ideal
    position; failing that, at the nearest word gap that doesn't split a name ("Ides of |
    March") or dangle a small word ("of", "the"); failing that, exactly at it."""
    words = sentence.split()
    n = len(words)
    if n <= SCENE_STRETCH * max_words:
        return [sentence]
    k = math.ceil(n / max_words)
    breaks = [i for i in range(1, n) if words[i - 1][-1] in ",;:" or words[i].startswith(("—", "–"))]
    cuts: list[int] = []
    for j in range(1, k):
        target = round(n * j / k)
        prev = cuts[-1] if cuts else 0
        near = [b for b in breaks if b - prev >= min_words / 2 and n - b >= min_words / 2
                and abs(b - target) <= max_words / 3]
        if not near:  # no clause break close by: nearest word gap that doesn't split a name/phrase
            reach = int(max_words / 3)
            near = [b for b in range(max(prev + 1, target - reach), min(n, target + reach + 1))
                    if _clean_cut(words, b)]
        cut = min(near, key=lambda b: abs(b - target)) if near else target
        if cut > prev:
            cuts.append(cut)
    bounds = [0, *cuts, n]
    return [" ".join(words[a:b]) for a, b in zip(bounds, bounds[1:], strict=False)]


def _units(text: str, min_words: float, max_words: float) -> list[str]:
    """Sentences (quotes kept whole), with over-long sentences split into pieces."""
    cursor = 0
    pieces: list[str] = []
    for match in _QUOTE.finditer(text):
        pieces += _SENTENCE_SPLIT.split(text[cursor : match.start()])
        pieces.append(match.group(0))
        cursor = match.end()
    pieces += _SENTENCE_SPLIT.split(text[cursor:])

    units: list[str] = []
    for piece in (p.strip() for p in pieces):
        if piece:
            units += [piece] if piece.startswith("[QUOTE]") else _split_long(piece, min_words, max_words)
    return units


def plan_scenes(script: str, min_words: float, max_words: float) -> list[tuple[str, bool, int]]:
    """(scene text, in_short_span, image number) in narration order, [SHORT] tags and
    [IMG n] marks removed. Text before the first mark takes the first mark's image."""
    parts: list[tuple[str, bool, int]] = []
    cursor, in_short, image = 0, False, 0
    for match in _CUTS.finditer(script):
        parts.append((script[cursor : match.start()], in_short, image))
        if match.group(2) is not None:
            image = int(match.group(2))
        else:
            in_short = not match.group(1)
        cursor = match.end()
    parts.append((script[cursor:], in_short, image))
    first = next((n for _, _, n in parts if n), 0)
    out: list[tuple[str, bool, int]] = []
    for text, short, n in parts:
        if text.strip():
            out += [(scene, short, n or first)
                    for scene in _merge(_units(text, min_words, max_words), min_words, max_words)]
    return out


_DIGITS = re.compile(r"\d[\d,.]*")
_LETTERS = re.compile(r"[A-Za-z]{3,}")
_OVERLAY_KINDS = ("label", "number", "chapter")


def _digits(text: str) -> set[str]:
    return {d.replace(",", "").rstrip(".") for d in _DIGITS.findall(text)}


def overlay_is_grounded(overlay: dict, scene_text: str, sources: str) -> bool:
    """On-screen text may not state anything the video can't back up: every number in it
    must appear in the scene's narration or the researched articles, and every word of a
    label (a place/name) in the narration, the articles or the video title. Chapter titles
    are free wording but may not introduce numbers."""
    text = overlay["text"]
    known_digits = _digits(scene_text) | _digits(sources)
    if not _digits(text) <= known_digits:
        return False
    if overlay["kind"] == "label":
        haystack = f"{scene_text} {sources}"
        words = _LETTERS.findall(text)
        if not all(word.lower() in haystack.lower() for word in words):
            return False
        # a label names a place/date: it needs a number or a proper name as the narration
        # capitalises it ("FRONT DOOR" from "the front door" is not a label)
        proper = any(re.search(rf"\b{re.escape(w.capitalize())}\b", haystack) for w in words)
        return bool(_digits(text)) or proper
    return True


def clean_extras(raw: list[dict], texts: list[str], sources: str, allowed_sfx: set[str], config) -> tuple[list, list]:
    """(sfx per scene, overlay per scene) after enforcing the rules the prompt asks for:
    known sfx tags only, ambient ones only where the scene mentions them (assets.sfx_fits),
    at most `sfx_max_share` of scenes and never two in a row;
    overlays grounded in the narration/sources, at most `overlay_max_share` of scenes for
    labels/numbers and `max_chapters` chapter cards. Chapter cards then get a whoosh and
    number callouts an impact, where the budget and spacing allow."""
    effects = config.effects
    n = len(texts)
    sfx: list[str | None] = [None] * n
    overlays: list[dict | None] = [None] * n
    sfx_budget = math.floor(n * effects.sfx_max_share) if effects.sfx else 0
    overlay_budget = math.floor(n * effects.overlay_max_share) if effects.overlays else 0
    chapters_left = effects.max_chapters if effects.overlays else 0

    for i, entry in enumerate(raw[:n]):
        tag = entry.get("sfx")
        if (sfx_budget and isinstance(tag, str) and tag in allowed_sfx and (i == 0 or sfx[i - 1] is None)
                and assets.sfx_fits(tag, _TAGS.sub("", texts[i]))):
            sfx[i] = tag
            sfx_budget -= 1
        overlay = entry.get("overlay")
        if not (isinstance(overlay, dict) and overlay.get("kind") in _OVERLAY_KINDS):
            continue
        text = " ".join(str(overlay.get("text") or "").split())[:48]
        candidate = {"kind": overlay["kind"], "text": text.upper() if overlay["kind"] != "number" else text}
        if not text or not overlay_is_grounded(candidate, _TAGS.sub("", texts[i]), sources):
            continue
        if candidate["kind"] == "chapter":
            if chapters_left and len(text.split()) <= 6:
                overlays[i] = candidate
                chapters_left -= 1
        elif overlay_budget:
            overlays[i] = candidate
            overlay_budget -= 1

    # A steady base layer of accents, whatever the LLM picked: a whoosh on every chapter
    # card, an impact on every number callout (a live plan with strict rules used just 2
    # effects in 71 scenes). Same budget, never next to another effect.
    accent = {"chapter": "whoosh", "number": "impact"}
    for i, overlay in enumerate(overlays):
        tag = accent.get((overlay or {}).get("kind"))
        neighbours = sfx[max(0, i - 1) : i + 2]
        if tag in allowed_sfx and sfx_budget and not any(neighbours):
            sfx[i] = tag
            sfx_budget -= 1
    return sfx, overlays


def _plan_extras(session: Session, video: Video, texts: list[str]) -> tuple[list, list]:
    """(sfx, overlay) per scene from one worker call. If the call fails outright: none."""
    config = get_config()
    topic = session.get(Topic, video.topic_id) if video.topic_id else None
    summary = " ".join(((topic.summary if topic else "") or "").split())[:300]
    allowed_sfx = set(assets.sfx_tags()) if config.effects.sfx else set()
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        title=video.title,
        summary=summary or "no summary",
        sfx_tags=", ".join(sorted(allowed_sfx)) or "none — always use null",
        max_chapters=config.effects.max_chapters if config.effects.overlays else 0,
        scenes="\n".join(f"{i}. {_TAGS.sub('', t)}" for i, t in enumerate(texts, start=1)),
    )
    try:
        result = llm.generate(session, prompt, role="worker", step="segment", inputs={"video_id": str(video.id)})
    except RetryLater:
        raise
    except Exception as exc:
        logger.warning("scene plan: LLM failed, no sound effects or on-screen text: {}", exc)
        return [None] * len(texts), [None] * len(texts)

    by_id: dict[int, dict] = {}
    for entry in result.get("scenes", []):
        try:
            by_id[int(entry.get("id"))] = entry
        except (TypeError, ValueError, AttributeError):
            continue
    raw = [by_id.get(i, {}) for i in range(1, len(texts) + 1)]
    sources = " ".join(a.get("text", "") for a in (video.research or {}).get("articles", []))
    return clean_extras(raw, texts, f"{sources} {video.title or ''}", allowed_sfx, config)


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.CHECKED:
        return
    if not video.script:
        raise ValueError(f"video {video_id}: no script to segment")

    images = {item["n"]: item for item in (video.research or {}).get("images", [])}
    if not images:
        raise ValueError(f"video {video_id}: no image inventory (send it back to pictures)")
    config = get_config()
    wps = config.voice.words_per_second
    planned = plan_scenes(video.script, config.video.scene_seconds_min * wps, config.video.scene_seconds_max * wps)
    missing = sorted({n for _, _, n in planned if n not in images})
    if missing:
        raise ValueError(f"video {video_id}: the script marks image(s) {missing} that aren't in the inventory")
    sfx, overlays = _plan_extras(session, video, [text for text, _, _ in planned])

    for scene in list(video.scenes):  # a re-run replans from scratch
        session.delete(scene)
    session.flush()
    for idx, ((text, in_short, n), cue, overlay) in enumerate(zip(planned, sfx, overlays, strict=True)):
        session.add(Scene(video_id=video_id, idx=idx, text=text, image_query=images[n]["shows"][:400],
                          image_asset_id=uuid.UUID(images[n]["asset_id"]), in_short_span=in_short,
                          sfx=cue, overlay=overlay))
    session.flush()
    session.expire(video, ["scenes"])  # the list read above for the delete is stale now

    video.status = Status.SEGMENTED
