"""Script (README §4.1 step 6): the writer LLM turns the researched Wikipedia articles —
and only those — into narration written around the video's checked images.

Inline markup, parsed later by software and never spoken:
- `[IMG n]` — before every passage: image n of the inventory (app/stages/picture.py) is
  on screen while that passage is read. Every picture shown is something the narration
  is talking about at that moment.
- `[SHORT]...[/SHORT]` — the passages that become the Shorts (align -> `short` rows)
- `[QUOTE]...[/QUOTE]` — direct historical quotations, read in the quote voice

The length follows the images (`plan`): about `images.seconds_per_image` per passage, an
image used at most `images.max_uses` times, within the 3–8 minute range; one Short per
minute or so, 3–7. A topic with few images makes a short video instead of a padded one.

The draft is checked before it is kept (`problems`): markup, length, the number of Short
passages (never touching: the align stage would merge them), and the image marks (known
numbers, not overused, never the same image twice in a row, each passage 3–17 seconds,
none inside a quote). A draft that fails is sent back to the writer once more with what
was wrong (`ATTEMPTS` in all); still failing, the stage fails and `ogh review` can send it back.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections import Counter
from pathlib import Path

from sqlalchemy.orm import Session

from app import llm
from app.config import ChannelConfig, get_config
from app.db import Video
from app.status import Status

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "script.md"

_SHORT_OPEN, _SHORT_CLOSE = "[SHORT]", "[/SHORT]"
IMG = re.compile(r"\[IMG (\d+)\]")
_TAG = re.compile(r"\[/?(?:SHORT|QUOTE)\]|\[IMG \d+\]")
_TOUCHING_SHORTS = re.compile(r"\[/SHORT\]\s*\[SHORT\]")
_QUOTE_SPAN = re.compile(r"\[QUOTE\].*?\[/QUOTE\]", re.DOTALL)
#: Drafts per video: the first, plus one retry with the problems named.
ATTEMPTS = 2
#: How far outside the word range a draft may land (spoken pace varies a little).
LENGTH_SLACK = 0.1
#: An image passage must run 3–17 seconds (scenes split longer ones on the same image).
PASSAGE_SECONDS = (3, 17)
#: Passages beyond one per image: images may return, but not every one of them.
REUSE_SHARE = 0.3


def articles_block(research: dict) -> str:
    """The researched articles as one prompt block (shared with the check stage)."""
    return "\n\n".join(
        f"=== ARTICLE: {a['title']} ===\n{a['text']}" for a in (research or {}).get("articles", [])
    )


def images_block(research: dict) -> str:
    return "\n".join(f"[IMG {i['n']}] {i['shows']}" for i in (research or {}).get("images", []))


def word_count(script: str) -> int:
    return len(_TAG.sub(" ", script).split())


def plan(images: int, config: ChannelConfig) -> dict:
    """{"seconds", "words_min", "words_max", "shorts"} for a video with `images` images."""
    long_form, wps, cfg = config.video.long_form, config.voice.words_per_second, config.images
    passages = images * (1 + REUSE_SHARE * (cfg.max_uses > 1))
    seconds = min(max(passages * cfg.seconds_per_image, long_form.seconds_min), long_form.seconds_max)
    low, high = max(seconds * 0.85, long_form.seconds_min), min(seconds * 1.1, long_form.seconds_max)
    shorts = min(max(round(seconds / 60), 3), config.video.shorts.per_video)
    return {"seconds": round(seconds), "words_min": round(low * wps), "words_max": round(high * wps),
            "shorts": shorts}


def validate_markup(script: str) -> None:
    """[SHORT] and [QUOTE] tags balanced and not nested within their own kind; [IMG n] marks
    well formed and never inside a quote."""
    for tag in ("SHORT", "QUOTE"):
        depth = 0
        for match in re.finditer(rf"\[(/?){tag}\]", script):
            depth += -1 if match.group(1) else 1
            if depth not in (0, 1):
                raise ValueError(f"unbalanced or nested [{tag}] markup")
        if depth != 0:
            raise ValueError(f"unclosed [{tag}] markup")
    if _SHORT_OPEN not in script:
        raise ValueError("script has no [SHORT] spans")
    if re.search(r"\[IMG(?! \d+\])", script):
        raise ValueError("a malformed image mark (write it as [IMG 12])")
    if any(IMG.search(q) for q in _QUOTE_SPAN.findall(script)):
        raise ValueError("an [IMG n] mark inside a [QUOTE]; put it before the quote")


def marks(script: str) -> list[int]:
    return [int(n) for n in IMG.findall(script)]


def passages(script: str) -> list[tuple[int, str]]:
    """(image number, the narration it shows over), in order; text before the first mark
    comes back with image 0."""
    parts = IMG.split(script)
    out = [(0, parts[0])] if _TAG.sub("", parts[0]).strip() else []
    out += [(int(parts[i]), parts[i + 1]) for i in range(1, len(parts), 2)]
    return out


def problems(script: str, words_min: int, words_max: int, shorts: int, images: int = 0,
             max_uses: int = 2, wps: float = 2.6) -> list[str]:
    """What's wrong with a draft, in words the writer can act on; [] = fine. `images` = 0
    skips the image-mark checks."""
    found = []
    try:
        validate_markup(script)
    except ValueError as exc:
        found.append(str(exc))
    words = word_count(script)
    if words < words_min * (1 - LENGTH_SLACK):
        found.append(f"it is {words} words; it must be {words_min}–{words_max} words")
    elif words > words_max * (1 + LENGTH_SLACK):
        found.append(f"it is {words} words; it must be at most {words_max} words")
    spans = script.count(_SHORT_OPEN)
    if spans != shorts:
        found.append(f"it has {spans} [SHORT] passages; it must have exactly {shorts}")
    if _TOUCHING_SHORTS.search(script):
        found.append("two [SHORT] passages touch; leave narration between every two")
    if images:
        found += _mark_problems(script, images, max_uses, wps)
    return found


def _mark_problems(script: str, images: int, max_uses: int, wps: float) -> list[str]:
    found = []
    numbers = marks(script)
    if not numbers:
        return ["it has no [IMG n] marks; start every passage with the image it shows over"]
    parts = passages(script)
    if parts and parts[0][0] == 0:
        found.append("narration comes before the first [IMG n] mark; the script must start with one")
    unknown = sorted({n for n in numbers if not 1 <= n <= images})
    if unknown:
        found.append(f"[IMG {unknown[0]}] is not in the list (images are 1–{images})")
    overused = sorted(n for n, k in Counter(numbers).items() if k > max_uses)
    if overused:
        found.append(f"[IMG {overused[0]}] is used more than {max_uses} times")
    if any(a == b for a, b in zip(numbers, numbers[1:], strict=False)):
        found.append("the same [IMG n] mark appears twice in a row; merge those passages or pick another image")
    low, high = (round(s * wps) for s in PASSAGE_SECONDS)
    for n, text in parts:
        count = len(_TAG.sub(" ", text).split())
        if n and count < low:
            found.append(f"the passage after [IMG {n}] is only {count} words; give each image at least {low}")
            break
        if n and count > high:
            found.append(f"the passage after [IMG {n}] is {count} words; split it with another image mark "
                         f"(at most {high} words per image)")
            break
    return found


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.PICTURED:
        return
    research = video.research or {}
    if not research.get("articles"):
        raise ValueError(f"video {video_id}: no researched articles to write from")
    images = research.get("images") or []
    if not images:
        raise ValueError(f"video {video_id}: no images to write around (send it back to pictures)")

    config = get_config()
    wps = config.voice.words_per_second
    length = plan(len(images), config)
    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        topic=video.title,
        tone=config.channel.tone,
        articles=articles_block(research),
        images=images_block(research),
        image_count=len(images),
        max_uses=config.images.max_uses,
        passage_min=round(PASSAGE_SECONDS[0] * wps),
        passage_max=round(PASSAGE_SECONDS[1] * wps),
        words_per_second=wps,
        seconds=length["seconds"],
        words_min=length["words_min"],
        words_max=length["words_max"],
        shorts_count=length["shorts"],
    )

    request, issues = prompt, []
    for _ in range(ATTEMPTS):
        script_text = llm.generate(
            session, request, role="writer", step="script", inputs={"video_id": str(video_id)}, json_mode=False
        )
        if not isinstance(script_text, str):
            raise ValueError(f"video {video_id}: writer returned no script")
        script_text = script_text.strip()
        issues = problems(script_text, length["words_min"], length["words_max"], length["shorts"],
                          images=len(images), max_uses=config.images.max_uses, wps=wps)
        if not issues:
            break
        request = (f"{prompt}\n\nYour previous draft was rejected because " + "; ".join(issues)
                   + ". Write the whole script again, fixing that.")
    if issues:
        raise ValueError(f"video {video_id}: script still unusable after {ATTEMPTS} drafts: {'; '.join(issues)}")

    video.script = script_text
    video.script_prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    video.research = {**research, "plan": length}
    video.status = Status.SCRIPTED
