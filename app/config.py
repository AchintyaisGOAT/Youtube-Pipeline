"""Configuration.

* ``Settings``       — secrets + machine paths, from environment / ``.env`` (git-ignored).
* ``ChannelConfig``  — channel behaviour, from the committed ``config.yaml``.

``ChannelConfig`` *is* the schema: unknown keys are rejected, so config drift is caught
on the first run. Stage code calls ``get_config()``; nothing else reads config.yaml.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import schedule

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    """Secrets and machine-specific paths. Never logged, never stored in a DB row."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    gemini_api_key: str = ""
    groq_api_key: str = ""
    smithsonian_api_key: str = ""
    #: Must be a URL, not an email — Wikimedia's bot policy (tightened 2026-09-23)
    #: 403s any request whose User-Agent contact isn't a URL.
    wikimedia_contact: str = "https://example.org/"

    config_path: str = str(REPO_ROOT / "config.yaml")
    database_url: str = f"sqlite:///{(REPO_ROOT / 'data' / 'pipeline.db').as_posix()}"
    storage_base: str = str(REPO_ROOT / "data")
    assets_dir: str = str(REPO_ROOT / "assets")

    alert_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


# --------------------------------------------------------------------------- #
# channel config (config.yaml) — section refs are README.md sections
# --------------------------------------------------------------------------- #
class _Model(BaseModel):
    """Base for every config block: reject unknown keys so config drift is caught."""

    model_config = ConfigDict(extra="forbid")


class ChannelInfo(_Model):
    name: str = ""
    handle: str = ""
    audience: str = ""
    tone: str = ""
    language: str = "en-US"


class Topics(_Model):
    in_scope: list[str] = Field(default_factory=list)
    auto_veto: list[str] = Field(default_factory=list)


class Discovery(_Model):
    sources: list[Literal["trending", "on_this_day"]] = Field(
        default_factory=lambda: ["trending", "on_this_day"], min_length=1
    )
    max_candidates: int = Field(20, ge=1, le=200)
    #: Gate relevance (0–10) a topic needs to pass; weaker fits are vetoed.
    min_relevance: int = Field(6, ge=0, le=10)
    #: Passed topics older than this expire, so trending topics don't go stale and
    #: discovery (which waits for an empty pool) refreshes it.
    max_topic_age_days: int = Field(7, ge=1, le=365)


class Llm(_Model):
    """Model IDs per role (README §5). Fallbacks may be empty (= no fallback)."""

    writer: str = "gemini-3.8-flash"
    writer_fallback: str = "openai/gpt-oss-120b"
    checker: str = "gemini-3.5-flash-lite"
    worker: str = "openai/gpt-oss-120b"
    worker_fallback: str = "gemini-3.5-flash-lite"
    image: str = "gemini-3.1-flash-image"

    @model_validator(mode="after")
    def _checker_differs(self) -> Llm:
        if self.checker == self.writer:
            raise ValueError("llm.checker must be a different model from llm.writer")
        return self


class Research(_Model):
    linked_articles: int = Field(3, ge=0, le=10)


class LongForm(_Model):
    #: A range, not a fixed length — the script fits the story within it.
    seconds_min: int = Field(180, ge=60, le=3600)
    seconds_max: int = Field(480, ge=60, le=3600)
    resolution: tuple[int, int] = (1920, 1080)
    fps: int = Field(30, ge=24, le=60)

    @model_validator(mode="after")
    def _range_order(self) -> LongForm:
        if self.seconds_min > self.seconds_max:
            raise ValueError("seconds_min must be <= seconds_max")
        return self


class Shorts(_Model):
    per_video: int = Field(7, ge=0, le=20)
    max_seconds: int = Field(55, ge=5, le=180)
    resolution: tuple[int, int] = (1080, 1920)
    fps: int = Field(30, ge=24, le=60)


class Video(_Model):
    long_form: LongForm = Field(default_factory=LongForm)
    shorts: Shorts = Field(default_factory=Shorts)
    scene_seconds_min: float = Field(5.0, gt=0)
    scene_seconds_max: float = Field(7.0, gt=0)
    loudness_lufs: float = Field(-14.0, le=0)
    music_lufs: float = Field(-18.0, le=0)
    encoder: Literal["auto", "h264_qsv", "libx264"] = "auto"

    @model_validator(mode="after")
    def _scene_seconds_order(self) -> Video:
        if self.scene_seconds_min > self.scene_seconds_max:
            raise ValueError("scene_seconds_min must be <= scene_seconds_max")
        return self


class Images(_Model):
    sources: list[Literal["wikimedia_commons", "library_of_congress", "smithsonian"]] = Field(
        default_factory=lambda: ["wikimedia_commons", "library_of_congress", "smithsonian"]
    )
    #: README §6.1 step 3 — set false to never generate AI images.
    ai_fallback: bool = True
    #: Prepended to every AI illustration prompt; keeps AI scenes consistent with each
    #: other and close to the archival material around them. Never photorealistic.
    ai_style: str = (
        "Vintage historical illustration in the style of a period engraving or print, "
        "muted colors, fine linework, 16:9 widescreen, no text."
    )
    max_images: int = Field(80, ge=1, le=500)


class Voice(_Model):
    primary: str = "bm_george"
    quotes: str = "bf_emma"
    words_per_second: float = Field(2.6, gt=0.5, lt=6)
    paragraph_pause_ms: int = Field(400, ge=0, le=3000)


class Alignment(_Model):
    whisper_model: str = "small.en"
    mismatch_fallback_ratio: float = Field(0.15, ge=0, le=1)


class Subtitles(_Model):
    font_file: str = "assets/fonts/Inter-Regular.ttf"
    size: int = Field(34, ge=8, le=200)
    position: Literal["bottom-center", "bottom-left", "bottom-right", "center", "top-center"] = (
        "bottom-center"
    )
    highlight_color: str = "#FFD23F"
    karaoke: bool = True

    @field_validator("highlight_color")
    @classmethod
    def _hex_color(cls, v: str) -> str:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            raise ValueError("highlight_color must be #RRGGBB")
        return v


class Schedule(_Model):
    """Suggested Studio publish slots, written into each upload kit's checklist."""

    long_form: list[str] = Field(default_factory=list)
    shorts: list[str] = Field(default_factory=list)

    @field_validator("long_form", "shorts")
    @classmethod
    def _slots(cls, v: list[str]) -> list[str]:
        return [schedule.validate(slot) for slot in v]


class Publish(_Model):
    category: str = "Education"
    made_for_kids: bool = False
    schedule: Schedule = Field(default_factory=Schedule)


class Ops(_Model):
    min_free_gb: int = Field(15, ge=1, le=10_000)
    keep_output_days: int = Field(30, ge=1, le=3650)


class ChannelConfig(_Model):
    timezone: str = "America/New_York"
    channel: ChannelInfo = Field(default_factory=ChannelInfo)
    topics: Topics = Field(default_factory=Topics)
    discovery: Discovery = Field(default_factory=Discovery)
    llm: Llm = Field(default_factory=Llm)
    research: Research = Field(default_factory=Research)
    video: Video = Field(default_factory=Video)
    images: Images = Field(default_factory=Images)
    voice: Voice = Field(default_factory=Voice)
    alignment: Alignment = Field(default_factory=Alignment)
    subtitles: Subtitles = Field(default_factory=Subtitles)
    publish: Publish = Field(default_factory=Publish)
    ops: Ops = Field(default_factory=Ops)

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {v!r}") from exc
        return v


def load_config(path: str | Path) -> ChannelConfig:
    """Parse and fully validate a config.yaml. Raises on any problem."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return ChannelConfig.model_validate(data)


@lru_cache
def get_config() -> ChannelConfig:
    """The channel config every stage reads — loaded once per process."""
    return load_config(get_settings().config_path)
