"""Metadata (README §4.1 step 12, §7): one LLM call writes the packaging — 5 titles ranked
best first (the first is used; `ogh review` can switch), the description's hook and
summary, tags, the thumbnail hook and image, and each Short's title, caption and
description. Code, not the LLM, builds everything that must be exact: chapters from the
real scene timings, the sources, the image and music credits, the fixed footer.

Called by the Shorts stage (which needs the captions and thumbnail), not dispatched on its
own; the LLM answer is cached, so a re-run costs nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import llm
from app.config import ChannelConfig, get_config
from app.db import Asset, Scene, Short, Video

PROMPT_PATH = Path(__file__).resolve().parent.parent.parent / "prompts" / "metadata.md"
#: YouTube's description limit is 5000 characters; per-image credits give way first.
DESCRIPTION_LIMIT = 5000
#: YouTube only shows chapters when there are 3+, starting at 0:00, each at least 10 s.
MIN_CHAPTERS, MIN_CHAPTER_S = 3, 10.0
_MAX_IMAGE_CHOICES = 40
_QUOTE_TAG = re.compile(r"\[/?QUOTE\]")
_WIKIDATA = re.compile(r"\s*\blabel QS:\S*?(?:\"[^\"]*\")?(?=\s|$)|\s*\bQS:\S+")
_SOURCE_NAMES = {"wikimedia_commons": "Wikimedia Commons", "smithsonian": "Smithsonian Open Access",
                 "gemini": "AI-generated"}


# --------------------------------------------------------------------------- #
# pure helpers (unit-tested)
# --------------------------------------------------------------------------- #
def timestamp(seconds: float) -> str:
    total = int(seconds)
    h, m, s = total // 3600, total % 3600 // 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def chapters(scenes: list[Scene], duration: float) -> list[tuple[float, str]]:
    """(start, title) from the chapter cards, opening at 0:00. Chapters under 10 s are
    merged into the one before; fewer than 3 = none (YouTube would ignore them)."""
    marks = [(s.start_s, s.overlay["text"].strip().title()) for s in scenes
             if s.overlay and s.overlay.get("kind") == "chapter" and s.start_s is not None]
    if not marks or marks[0][0] >= MIN_CHAPTER_S:
        marks.insert(0, (0.0, "Intro"))
    marks[0] = (0.0, marks[0][1])
    kept: list[tuple[float, str]] = []
    for start, title in marks:
        if kept and start - kept[-1][0] < MIN_CHAPTER_S:
            continue
        kept.append((start, title))
    if kept and duration - kept[-1][0] < MIN_CHAPTER_S and len(kept) > 1:
        kept.pop()
    return kept if len(kept) >= MIN_CHAPTERS else []


def hashtag(text: str) -> str:
    return "#" + re.sub(r"[^0-9A-Za-z]", "", text)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:-–—") + "…"


def tidy_credit(attribution: str, fallback: str) -> tuple[str, str | None]:
    """(title, author or None) from an archive's "title — author" attribution. Commons
    metadata is messy: Wikidata codes (`label QS:Len,"..."`), doubled "Unknown author
    Unknown author or not provided", 250-character titles."""
    title, _, author = (attribution or fallback).partition(" — ")
    title = _WIKIDATA.sub("", title)
    title = re.sub(r"\.(jpe?g|png|tiff?|gif|svg|webp)$", "", title, flags=re.IGNORECASE).strip(" -_") or fallback
    author = _WIKIDATA.sub("", author).strip()
    if not author or re.search(r"\b(unknown|anonymous|not provided)\b", author, re.IGNORECASE):
        author = None
    return _shorten(title, 90), _shorten(author, 60) if author else None


def image_credits(assets: list[Asset]) -> tuple[list[str], str]:
    """(one line per image, a one-line summary by source) — the summary stands in for the
    list when the description would run past YouTube's limit."""
    lines, counts = [], {}
    for asset in sorted(assets, key=lambda a: (a.source, a.source_id or "")):
        name = _SOURCE_NAMES.get(asset.source, asset.source)
        counts[name] = counts.get(name, 0) + 1
        if asset.source == "gemini":
            continue
        title, author = tidy_credit(asset.attribution or "", (asset.source_id or "Untitled").removeprefix("File:"))
        by = f" by {author}" if author else " (author unknown)"
        lines.append(f'- "{title}"{by}. {asset.license or "Public domain"}, {name}: {asset.rights_url or ""}'
                     .rstrip(": "))
    ai = counts.pop("AI-generated", 0)
    summary = ", ".join(f"{n} from {name}" for name, n in counts.items())
    if ai:
        lines.append(f"- {ai} AI-generated illustration{'s' if ai != 1 else ''}, used where no archive image existed")
        summary = f"{summary}, {ai} AI-generated" if summary else f"{ai} AI-generated"
    return lines, f"Images: {summary}. Archive images are public domain / CC0."


def music_credit(music: dict | None) -> str:
    """The background track, always named — with the credit its license asks for when it
    asks for one (it then already names the track, e.g. Kevin MacLeod's CC BY line)."""
    if not music or not (music.get("title") or music.get("credit")):
        return ""
    if music.get("credit"):
        return f"🎵 Music\n{music['credit'].strip()}"
    by = f" by {music['artist']}" if music.get("artist") else ""
    source = f" ({music['license']})" if music.get("license") else ""
    return f'🎵 Music\n"{music["title"]}"{by}{source}'


def build_description(meta: dict, *, config: ChannelConfig, chapter_list: list[tuple[float, str]],
                      articles: list[dict], assets: list[Asset], music: dict | None) -> str:
    handle = config.channel.handle.lstrip("@")
    head = [meta.get("hook", "").strip(), "", meta.get("summary", "").strip()]
    if handle:
        head += ["", f"▶ Subscribe for more true stories: https://www.youtube.com/@{handle}?sub_confirmation=1"]
    blocks = ["\n".join(head).strip()]
    if chapter_list:
        blocks.append("⏱ Chapters\n" + "\n".join(f"{timestamp(t)} {title}" for t, title in chapter_list))
    if articles:
        blocks.append("📚 Sources (Wikipedia)\n" + "\n".join(f"- {a['title']}: {a['url']}" for a in articles))
    credit_lines, credit_summary = image_credits(assets)
    credits_at = len(blocks)
    if credit_lines:
        blocks.append("🖼 Image credits\n" + "\n".join(credit_lines))
    music_block = music_credit(music)
    if music_block:
        blocks.append(music_block)
    tags = [hashtag(meta.get("topic_hashtag", ""))] if meta.get("topic_hashtag") else []
    tags += [hashtag(t) for t in config.publish.hashtags]
    tail = "\n\n".join(p for p in (config.publish.description_footer.strip(), " ".join(dict.fromkeys(tags))) if p)
    text = "\n\n".join([*blocks, tail]).strip()
    if len(text) > DESCRIPTION_LIMIT and credit_lines:  # the full list stays in the kit's credits.md
        blocks[credits_at] = "🖼 " + credit_summary
        text = "\n\n".join([*blocks, tail]).strip()
    return text[:DESCRIPTION_LIMIT]


def short_description(short_meta: dict, *, config: ChannelConfig, topic_hashtag: str) -> str:
    tags = [hashtag(t) for t in (topic_hashtag, *config.publish.hashtags, "Shorts") if t]
    parts = [short_meta.get("description", "").strip(), config.publish.shorts_footer.strip(),
             " ".join(dict.fromkeys(tags))]
    return "\n\n".join(p for p in parts if p)


def thumbnail_hook(meta: dict, fallback: str) -> tuple[str, str]:
    """(2–4 word hook in capitals, the word to colour)."""
    words = (meta.get("thumbnail_text") or fallback).upper().split()[:4]
    highlight = (meta.get("thumbnail_highlight") or "").upper().strip()
    if highlight not in words:
        highlight = max(words, key=len) if words else ""
    return " ".join(words), highlight


# --------------------------------------------------------------------------- #
# stage step
# --------------------------------------------------------------------------- #
def used_assets(session: Session, scenes: list[Scene]) -> list[Asset]:
    ids = {s.image_asset_id for s in scenes if s.image_asset_id}
    return list(session.execute(select(Asset).where(Asset.id.in_(ids))).scalars()) if ids else []


def image_choices(scenes: list[Scene], assets: dict) -> list[tuple[Scene, Asset]]:
    """Thumbnail candidates: each image's first scene, archive images before AI ones,
    wide and large before small."""
    seen, out = set(), []
    for scene in scenes:
        asset = assets.get(scene.image_asset_id)
        if asset is None or asset.id in seen:
            continue
        seen.add(asset.id)
        out.append((scene, asset))

    def rank(pair):
        meta = pair[1].meta or {}
        w, h = meta.get("width") or 0, meta.get("height") or 1
        return (pair[1].source == "gemini", not (w / h >= 1.2), -w * h)

    return sorted(out, key=rank)[:_MAX_IMAGE_CHOICES]


def _shorts_block(scenes: list[Scene], shorts: list[Short]) -> str:
    if not shorts:
        return "(none)"
    lines = []
    for n, short in enumerate(shorts, 1):
        text = " ".join(s.text for s in scenes if s.start_s is not None
                        and short.start_s - 0.01 <= s.start_s < short.end_s)
        lines.append(f"Short {n}: {_QUOTE_TAG.sub('', text)}")
    return "\n\n".join(lines)


def _images_block(choices: list[tuple[Scene, Asset]]) -> str:
    lines = []
    for scene, asset in choices:
        meta = asset.meta or {}
        size = f", {meta['width']}x{meta['height']}" if meta.get("width") else ""
        text = _QUOTE_TAG.sub("", scene.text)[:110]
        lines.append(f"scene {scene.idx}: {text} ({_SOURCE_NAMES.get(asset.source, asset.source)}{size})")
    return "\n".join(lines) or "(none)"


def write_metadata(session: Session, video: Video) -> None:
    config = get_config()
    scenes = list(session.execute(select(Scene).filter_by(video_id=video.id).order_by(Scene.idx)).scalars())
    shorts = list(session.execute(select(Short).filter_by(video_id=video.id).order_by(Short.idx)).scalars())
    assets = used_assets(session, scenes)
    choices = image_choices(scenes, {a.id: a for a in assets})
    topic = (video.video_metadata or {}).get("topic") or video.title or ""

    prompt = PROMPT_PATH.read_text(encoding="utf-8").format(
        channel=config.channel.name, audience=config.channel.audience, tone=config.channel.tone,
        topic=topic, script=re.sub(r"\[/?SHORT\]", "", video.script or ""),
        shorts=_shorts_block(scenes, shorts), images=_images_block(choices),
    )
    result = llm.generate(session, prompt, role="writer", step="metadata", inputs={"video_id": str(video.id)})
    titles = [t.strip() for t in result.get("titles", []) if isinstance(t, str) and t.strip()]
    if not titles:
        raise ValueError(f"video {video.id}: metadata returned no titles")

    choice_ids = {scene.idx: asset.id for scene, asset in choices}
    picked = result.get("thumbnail_scene")
    thumbnail_asset = choice_ids.get(picked) if isinstance(picked, int) else None
    if thumbnail_asset is None and choices:
        thumbnail_asset = choices[0][1].id
    text, highlight = thumbnail_hook(result, titles[0])

    written = [s for s in result.get("shorts", []) if isinstance(s, dict)]
    short_meta = []
    for n, short in enumerate(shorts):
        entry = written[n] if n < len(written) else {}
        short.title = (entry.get("title") or f"{titles[0]} (part {n + 1})")[:100]
        short.caption = (entry.get("caption") or text).upper()[:80]
        short_meta.append({"title": short.title, "caption": short.caption, "description": short_description(
            entry, config=config, topic_hashtag=result.get("topic_hashtag", ""))})

    metadata = dict(video.video_metadata or {})
    metadata.update({
        "topic": topic,
        "title_candidates": titles,
        "tags": [t for t in result.get("tags", []) if isinstance(t, str)],
        "description": build_description(
            result, config=config, chapter_list=chapters(scenes, video.duration_s or 0.0),
            articles=(video.research or {}).get("articles", []), assets=assets, music=metadata.get("music")),
        "thumbnail": {"text": text, "highlight": highlight,
                      "asset_id": str(thumbnail_asset) if thumbnail_asset else None},
        "shorts": short_meta,
        "image_credits": image_credits(assets)[0],
    })
    video.title = titles[0]
    video.video_metadata = metadata
