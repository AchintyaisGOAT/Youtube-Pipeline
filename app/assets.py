"""Your curated media in assets/ (README §6.3, §6.5): music tracks and sound effects, each
folder described by a manifest.yaml. `ogh assets` writes/updates both manifests from the
files you dropped in, guessing sound-effect tags from their names — you only fix guesses.

assets/music/manifest.yaml:
    tracks:
      - {file: Some Track.mp3, title: Some Track, artist: "", license: "...", credit: ""}
assets/sfx/manifest.yaml:
    effects:
      - {file: Whoosh Swish.mp3, tags: [whoosh], title: Whoosh Swish, credit: "", gain_db: 0}

`credit` is only needed for tracks/effects whose license asks for attribution; it goes
into the video description, which always names the track used.

A new track's title and artist come from the file's own tags. Its `license` starts empty
unless the tags identify it (Kevin MacLeod: CC BY 4.0, with the credit text his license
requires), and **a track with no license is never used**: a wrong default once labelled a
CC BY track "no attribution required" and a monetization-restricted one as safe.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import yaml
from loguru import logger

from app.config import get_settings
from app.ffmpeg import probe

AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".m4a", ".flac")
#: What to write for a YouTube Audio Library track marked "attribution not required".
YOUTUBE_LIBRARY_LICENSE = "YouTube Audio Library — no attribution required"

#: The sound-effect vocabulary, with the file-name words each tag is guessed from.
SFX_TAGS: dict[str, tuple[str, ...]] = {
    "whoosh": ("whoosh", "swish", "swoosh", "woosh", "swipe", "transition", "swoop"),
    "impact": ("impact", "hit", "boom", "thud", "slam", "punch", "drum"),
    "riser": ("riser", "rise", "build", "tension", "suspense", "drone"),
    "crowd": ("crowd", "applause", "cheer", "murmur", "audience", "chatter", "people"),
    "typewriter": ("typewriter", "typing"),
    "clock": ("clock", "tick", "ticking"),
    "thunder": ("thunder", "storm", "lightning"),
    "rain": ("rain",),
    "bell": ("bell", "chime", "church"),
    "door": ("door", "knock", "creak"),
    "footsteps": ("footstep", "footsteps", "steps", "walking"),
    "paper": ("paper", "page", "newspaper", "book"),
    "camera": ("camera", "shutter", "flash"),
    "gunshot": ("gun", "gunshot", "shot", "rifle", "cannon", "musket", "pistol"),
    "horse": ("horse", "hooves", "carriage", "gallop"),
    "fire": ("fire", "crackle", "flame", "burning"),
    "water": ("water", "waves", "sea", "ocean", "river"),
    "wind": ("wind", "howl", "gust"),
    "train": ("train", "steam", "locomotive"),
    "sword": ("sword", "blade", "clash", "metal"),
}
#: Effects that work as punctuation anywhere; every other tag is an ambient sound that
#: needs the scene to actually describe it (a live plan put a bell on "director of
#: textile mills" and thunder on a murder with no storm).
MUSICAL_SFX = {"whoosh", "impact", "riser"}
#: Words in a scene's narration that justify each ambient effect.
SFX_SCENE_WORDS: dict[str, tuple[str, ...]] = {
    "crowd": ("crowd", "crowds", "mob", "audience", "spectators", "people", "cheered", "gathered"),
    "typewriter": ("typewriter", "typed", "typing", "telegram", "telegraph"),
    "clock": ("clock", "clocks", "minutes", "hour", "o'clock", "midnight"),
    "thunder": ("thunder", "storm", "lightning"),
    "rain": ("rain", "raining", "downpour", "storm"),
    "bell": ("bell", "bells", "church", "chime", "rang"),
    "door": ("door", "doors", "knock", "knocked", "doorway"),
    "footsteps": ("footsteps", "steps", "stairs", "walked", "staircase", "paced"),
    "paper": ("paper", "papers", "letter", "letters", "newspaper", "newspapers", "note", "document", "page", "will"),
    "camera": ("camera", "photograph", "photographs", "photo", "photographed"),
    "gunshot": ("gun", "guns", "shot", "shots", "shoot", "fired", "rifle", "cannon", "musket", "pistol"),
    "horse": ("horse", "horses", "carriage", "cavalry", "rode", "gallop"),
    "fire": ("fire", "fires", "burn", "burned", "burning", "flames", "stove", "blaze"),
    "water": ("river", "sea", "ocean", "ship", "waves", "harbor", "harbour", "boat"),
    "wind": ("wind", "winds", "gale", "storm"),
    "train": ("train", "trains", "railway", "railroad", "locomotive"),
    "sword": ("sword", "swords", "blade", "blades", "duel"),
}
_WORD = re.compile(r"[a-z]+")


def sfx_fits(tag: str, scene_text: str) -> bool:
    """A musical effect fits anywhere; an ambient one only if the scene mentions it."""
    if tag in MUSICAL_SFX:
        return True
    words = set(re.findall(r"[a-z']+", scene_text.lower()))
    return bool(words & set(SFX_SCENE_WORDS.get(tag, ())))


def _folder(name: str) -> Path:
    return Path(get_settings().assets_dir) / name


def _load(folder: Path, key: str) -> list[dict]:
    manifest = folder / "manifest.yaml"
    if not manifest.exists():
        return []
    entries = (yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}).get(key) or []
    return [e for e in entries if isinstance(e, dict) and (folder / str(e.get("file", ""))).is_file()]


def music_tracks() -> list[dict]:
    """Tracks with a license filled in — the only ones a video may use."""
    return [t for t in _load(_folder("music"), "tracks") if str(t.get("license") or "").strip()]


def _file_tags(path: Path) -> dict[str, str]:
    try:
        tags = probe(path).get("format", {}).get("tags", {})
    except Exception:  # unreadable tags just mean no guess
        return {}
    return {k.lower(): str(v).strip() for k, v in tags.items()}


def new_track(path: Path) -> dict:
    """A manifest entry for a new music file, filled in from its tags where they're certain."""
    tags = _file_tags(path)
    title, artist = tags.get("title") or _title(path.name), tags.get("artist", "")
    entry = {"file": path.name, "title": title, "artist": artist, "license": "", "credit": ""}
    if artist.lower() == "kevin macleod":
        entry["license"] = "CC BY 4.0 (incompetech.com)"
        entry["credit"] = (f'"{title}" Kevin MacLeod (incompetech.com)\n'
                           "Licensed under Creative Commons: By Attribution 4.0 License\n"
                           "http://creativecommons.org/licenses/by/4.0/")
    return entry


def sound_effects() -> list[dict]:
    """Effects with at least one known tag."""
    return [e for e in _load(_folder("sfx"), "effects") if set(e.get("tags") or []) & SFX_TAGS.keys()]


def sfx_tags() -> list[str]:
    """Tags the library can actually play — the only ones the scene plan may use."""
    return sorted({t for e in sound_effects() for t in e.get("tags", []) if t in SFX_TAGS})


def pick_music(video_id: uuid.UUID) -> dict | None:
    """A track chosen by video id (so a re-render keeps it), with its full path. None = no music."""
    tracks = music_tracks()
    if not tracks:
        return None
    track = dict(tracks[video_id.int % len(tracks)])
    track["path"] = str(_folder("music") / track["file"])
    return track


def pick_sfx(tag: str, salt: int) -> dict | None:
    """An effect with `tag`, varied by `salt` (e.g. the scene index) so repeats differ."""
    options = [e for e in sound_effects() if tag in (e.get("tags") or [])]
    if not options:
        return None
    effect = dict(options[salt % len(options)])
    effect["path"] = str(_folder("sfx") / effect["file"])
    return effect


def guess_tags(file_name: str) -> list[str]:
    words = set(_WORD.findall(Path(file_name).stem.lower()))
    return [tag for tag, keys in SFX_TAGS.items() if words & set(keys)]


def _title(file_name: str) -> str:
    return re.sub(r"[_\s]+", " ", Path(file_name).stem).strip()


def write_manifests() -> dict[str, list[str]]:
    """Create/update assets/music and assets/sfx manifests from the files present. Existing
    entries (your edits) are kept; new files are added; entries whose file is gone are
    dropped. Returns {"music": new files, "sfx": new files, "untagged": effects needing tags,
    "unlicensed": tracks needing a license}."""
    report: dict[str, list[str]] = {"music": [], "sfx": [], "untagged": [], "unlicensed": []}
    for name, key in (("music", "tracks"), ("sfx", "effects")):
        folder = _folder(name)
        folder.mkdir(parents=True, exist_ok=True)
        manifest = folder / "manifest.yaml"
        existing = {}
        if manifest.exists():
            for entry in (yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}).get(key) or []:
                if isinstance(entry, dict) and entry.get("file"):
                    existing[entry["file"]] = entry
        entries = []
        for path in sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_EXTENSIONS):
            entry = existing.get(path.name)
            if entry is None:
                report[name].append(path.name)
                entry = (new_track(path) if name == "music" else
                         {"file": path.name, "tags": guess_tags(path.name), "title": _title(path.name),
                          "credit": "", "gain_db": 0})
            if name == "sfx" and not entry.get("tags"):
                report["untagged"].append(path.name)
            if name == "music" and not str(entry.get("license") or "").strip():
                report["unlicensed"].append(path.name)
            entries.append(entry)
        manifest.write_text(yaml.safe_dump({key: entries}, sort_keys=False, allow_unicode=True), encoding="utf-8")
        logger.info("{}: {} entries", manifest, len(entries))
    return report
