"""Configuration.

Two things live here:

* ``Settings``       — secrets + machine paths, read from environment / ``.env``.
* ``ChannelConfig``  — channel behaviour, read from ``config.yaml``.

``ChannelConfig`` *is* the schema. ``scripts/load_config.py`` validates ``config.yaml``,
writes it into the ``channel`` DB row, and dumps ``config.schema.json`` for editor
validation. Runtime code reads channel config from the DB, never the file.
"""

from __future__ import annotations

import json
import re
import uuid
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_SCHEMA_PATH = REPO_ROOT / "config.schema.json"


class Settings(BaseSettings):
    """Secrets and machine-specific paths. Never logged, never stored in a DB row."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    gemini_api_key: str = ""
    youtube_client_id: str = ""
    youtube_client_secret: str = ""
    youtube_refresh_token: str = ""
    youtube_channel_id: str = ""
    smithsonian_api_key: str = ""
    #: Must be a URL, not an email — Wikimedia's bot policy (tightened 2026-09-23)
    #: 403s any upload.wikimedia.org request whose User-Agent contact isn't a URL.
    wikimedia_contact: str = "https://example.org/"

    database_url: str = f"sqlite:///{(REPO_ROOT / 'data' / 'pipeline.db').as_posix()}"

    storage_base: str = str(REPO_ROOT / "data")
    assets_dir: str = str(REPO_ROOT / "assets")

    alert_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


# --------------------------------------------------------------------------- #
# channel config (config.yaml)
# --------------------------------------------------------------------------- #
class _Model(BaseModel):
    """Base for every config block: reject unknown keys so config drift is caught."""

    model_config = ConfigDict(extra="forbid")


class ChannelInfo(_Model):
    name: str = ""
    handle: str = ""
    description: str = ""
    audience: str = ""
    tone: str = ""
    language: str = "en-US"


class Topics(_Model):
    in_scope: list[str] = Field(default_factory=list)
    auto_veto: list[str] = Field(default_factory=list)
    manual_review: list[str] = Field(default_factory=list)


class Discovery(_Model):
    primary: str = "wikimedia_pageviews"
    also: list[str] = Field(default_factory=list)
    run: str = "weekly Mon 06:00"
    target_long_form_per_month: int = Field(6, ge=1, le=100)


class LongForm(_Model):
    #: A range, not a fixed length -- script.py aims within it based on how much the
    #: topic naturally supports, rather than padding/cutting every video to one length.
    target_seconds_min: int = Field(180, ge=60, le=3600)
    target_seconds_max: int = Field(480, ge=60, le=3600)
    resolution: tuple[int, int] = (1920, 1080)
    fps: int = Field(30, ge=24, le=60)

    @model_validator(mode="after")
    def _range_order(self) -> LongForm:
        if self.target_seconds_min > self.target_seconds_max:
            raise ValueError("target_seconds_min must be <= target_seconds_max")
        return self


class Shorts(_Model):
    per_long_form: int = Field(7, ge=0, le=20)
    max_seconds: int = Field(55, ge=5, le=180)
    resolution: tuple[int, int] = (1080, 1920)
    fps: int = Field(30, ge=24, le=60)


class Video(_Model):
    long_form: LongForm = Field(default_factory=LongForm)
    shorts: Shorts = Field(default_factory=Shorts)
    loudness_lufs: float = Field(-14.0, le=0)
    music_bed_lufs: float = Field(-18.0, le=0)
    image_seconds_min: float = Field(2.5, gt=0)
    image_seconds_max: float = Field(3.5, gt=0)
    encoder: str = "auto"
    render_concurrency: int = Field(1, ge=1, le=8)

    @field_validator("encoder")
    @classmethod
    def _encoder(cls, v: str) -> str:
        allowed = {"auto", "h264_nvenc", "libx264"}
        if v not in allowed:
            raise ValueError(f"encoder must be one of {sorted(allowed)}")
        return v

    @model_validator(mode="after")
    def _image_seconds_order(self) -> Video:
        if self.image_seconds_min > self.image_seconds_max:
            raise ValueError("image_seconds_min must be <= image_seconds_max")
        return self


class Voice(_Model):
    primary: str = "bm_george"
    secondary_for_quotes: str = ""
    words_per_second: float = Field(2.6, gt=0.5, lt=6)
    paragraph_pause_ms: int = Field(400, ge=0, le=3000)


class Subtitles(_Model):
    font_file: str = "assets/fonts/Inter-Regular.ttf"
    size: int = Field(34, ge=8, le=200)
    position: str = "bottom-center"
    max_lines: int = Field(2, ge=1, le=4)
    highlight_color: str = "#FFD23F"
    karaoke: bool = True
    upload_sidecar_srt: bool = True

    @field_validator("position")
    @classmethod
    def _position(cls, v: str) -> str:
        allowed = {"bottom-center", "bottom-left", "bottom-right", "center", "top-center"}
        if v not in allowed:
            raise ValueError(f"position must be one of {sorted(allowed)}")
        return v

    @field_validator("highlight_color")
    @classmethod
    def _hex_color(cls, v: str) -> str:
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", v):
            raise ValueError("highlight_color must be #RRGGBB")
        return v


class Alignment(_Model):
    whisper_model: str = "small.en"
    mismatch_fallback_ratio: float = Field(0.15, ge=0, le=1)


class Images(_Model):
    #: Segment visuals are AI-generated illustrations (Gemini image models), not
    #: archival-photo search -- this string is prepended to every generation prompt,
    #: so it's what keeps a whole video's illustrations visually consistent.
    art_style: str = (
        "Flat 2D cartoon illustration, bold clean outlines, vibrant saturated colors, "
        "simple shapes, fun educational animated-explainer style, 16:9 widescreen."
    )
    per_video_max: int = Field(60, ge=1, le=500)


class Schedule(_Model):
    long_form: list[str] = Field(default_factory=list)
    shorts: list[str] = Field(default_factory=list)


class Publish(_Model):
    category: str = "Education"
    made_for_kids: bool = False
    synthetic_content_disclosure: bool = True
    api_visibility: str = "private"
    playlist_id: str = ""
    schedule: Schedule = Field(default_factory=Schedule)

    @field_validator("api_visibility")
    @classmethod
    def _visibility(cls, v: str) -> str:
        allowed = {"private", "unlisted", "public"}
        if v not in allowed:
            raise ValueError(f"api_visibility must be one of {sorted(allowed)}")
        return v


class Ops(_Model):
    media_retention_days: int = Field(30, ge=1, le=3650)
    delete_intermediates_on_success: bool = True
    min_free_gb_before_render: int = Field(15, ge=1, le=10_000)
    alert_on_budget_pct: int = Field(80, ge=1, le=100)
    backup_target: str = ""


class ModelPins(_Model):
    kokoro_revision: str = ""
    whisper_revision: str = ""
    ffmpeg_version: str = ""


class ChannelConfig(_Model):
    config_version: int = Field(1, ge=1)
    timezone: str = "America/New_York"
    channel: ChannelInfo = Field(default_factory=ChannelInfo)
    topics: Topics = Field(default_factory=Topics)
    discovery: Discovery = Field(default_factory=Discovery)
    video: Video = Field(default_factory=Video)
    voice: Voice = Field(default_factory=Voice)
    subtitles: Subtitles = Field(default_factory=Subtitles)
    alignment: Alignment = Field(default_factory=Alignment)
    images: Images = Field(default_factory=Images)
    publish: Publish = Field(default_factory=Publish)
    ops: Ops = Field(default_factory=Ops)
    models: ModelPins = Field(default_factory=ModelPins)
    test_mode: bool = True

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {v!r}") from exc
        return v


def load_channel_config(path: str | Path) -> ChannelConfig:
    """Parse and fully validate a config.yaml. Raises on any problem."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"config file not found: {p}")
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return ChannelConfig.model_validate(data)


def dump_config_schema(path: str | Path = CONFIG_SCHEMA_PATH) -> Path:
    """Write the JSON Schema for config.yaml (editor autocomplete/validation)."""
    p = Path(path)
    p.write_text(json.dumps(ChannelConfig.model_json_schema(), indent=2) + "\n", encoding="utf-8")
    return p


def get_channel_config(session: Session, channel_id: uuid.UUID | None = None) -> ChannelConfig:
    """Read channel behaviour config from the DB. Stage code must use this — never re-read
    config.yaml directly (that file only feeds ``scripts/load_config.py``).
    """
    from sqlalchemy import select

    from app.db import Channel  # deferred: app.db imports this module, so avoid a cycle

    stmt = select(Channel)
    stmt = stmt.filter_by(id=channel_id) if channel_id is not None else stmt.order_by(Channel.created_at)
    channel = session.execute(stmt).scalars().first()
    if channel is None:
        raise RuntimeError("no channel configured — run scripts/load_config.py first")
    return ChannelConfig.model_validate(channel.config)
