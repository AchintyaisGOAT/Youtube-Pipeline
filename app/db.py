"""Database: SQLAlchemy 2.0 models (README §8) and a small sync engine/session helper.

Single-file SQLite — no server, no migrations; the schema is created with
``Base.metadata.create_all()``. Status values are stored as plain strings — always assign
``Status`` / ``TopicStatus`` members (``app.status``), never bare strings.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    TypeDecorator,
    UniqueConstraint,
    Uuid,
    create_engine,
    event,
    func,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

from app.config import get_settings
from app.status import Status, TopicStatus


class UtcDateTime(TypeDecorator):
    """Timezone-aware UTC datetimes on SQLite, which has no timezone type: a
    `UtcDateTime` column there silently comes back *naive* (verified), so
    comparing it with `datetime.now(UTC)` raises TypeError. Stores naive UTC, always
    returns aware UTC; naive values passed in are assumed to already be UTC."""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# topics
# --------------------------------------------------------------------------- #
class Topic(Base, TimestampMixin):
    __tablename__ = "topic"
    #: One row per title across every source: a topic is never done twice.
    __table_args__ = (UniqueConstraint("title", name="uq_topic_title"),)

    id: Mapped[uuid.UUID] = _pk()
    source: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSON)
    score: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default=TopicStatus.CANDIDATE)
    decided_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


# --------------------------------------------------------------------------- #
# video + scenes + shorts
# --------------------------------------------------------------------------- #
class Video(Base, TimestampMixin):
    __tablename__ = "video"

    id: Mapped[uuid.UUID] = _pk()
    topic_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("topic.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, default=Status.SELECTED)

    title: Mapped[str | None] = mapped_column(String(300))
    script: Mapped[str | None] = mapped_column(Text)
    script_prompt_hash: Mapped[str | None] = mapped_column(String(64))
    research: Mapped[dict | None] = mapped_column(JSON)
    video_metadata: Mapped[dict | None] = mapped_column("metadata", JSON)
    duration_s: Mapped[float | None] = mapped_column(Float)
    thumbnail_uri: Mapped[str | None] = mapped_column(String(1024))
    error: Mapped[str | None] = mapped_column(Text)

    scenes: Mapped[list[Scene]] = relationship(
        back_populates="video", cascade="all, delete-orphan", order_by="Scene.idx"
    )
    shorts: Mapped[list[Short]] = relationship(
        back_populates="video", cascade="all, delete-orphan", order_by="Short.idx"
    )


class Scene(Base):
    __tablename__ = "scene"
    __table_args__ = (UniqueConstraint("video_id", "idx", name="uq_scene_video_idx"),)

    id: Mapped[uuid.UUID] = _pk()
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("video.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    image_query: Mapped[str | None] = mapped_column(String(400))
    image_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("asset.id", ondelete="SET NULL")
    )
    start_s: Mapped[float | None] = mapped_column(Float)
    end_s: Mapped[float | None] = mapped_column(Float)
    in_short_span: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: A sound-effect tag from assets/sfx/manifest.yaml played at the scene start, or None.
    sfx: Mapped[str | None] = mapped_column(String(40))
    #: On-screen text: {"kind": "label" | "number" | "chapter", "text": str}, or None.
    overlay: Mapped[dict | None] = mapped_column(JSON)

    video: Mapped[Video] = relationship(back_populates="scenes")


class Short(Base, TimestampMixin):
    __tablename__ = "short"
    __table_args__ = (UniqueConstraint("video_id", "idx", name="uq_short_video_idx"),)

    id: Mapped[uuid.UUID] = _pk()
    video_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("video.id", ondelete="CASCADE"))
    idx: Mapped[int] = mapped_column(Integer, nullable=False)
    start_s: Mapped[float] = mapped_column(Float, nullable=False)
    end_s: Mapped[float] = mapped_column(Float, nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    caption: Mapped[str | None] = mapped_column(Text)
    #: "pending" | "rendered" | "failed" — per-Short render state, not a video Status.
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="pending")

    video: Mapped[Video] = relationship(back_populates="shorts")


# --------------------------------------------------------------------------- #
# assets
# --------------------------------------------------------------------------- #
class Asset(Base):
    __tablename__ = "asset"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_asset_source"),)

    id: Mapped[uuid.UUID] = _pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # image | music
    source: Mapped[str] = mapped_column(String(60), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(200))
    license: Mapped[str | None] = mapped_column(String(60))
    rights_url: Mapped[str | None] = mapped_column(String(1024))
    attribution: Mapped[str | None] = mapped_column(Text)
    uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# render log + LLM usage + LLM cache
# --------------------------------------------------------------------------- #
class Render(Base):
    """Append-only log of FFmpeg render attempts (long-form stages and Shorts)."""

    __tablename__ = "render"

    id: Mapped[uuid.UUID] = _pk()
    video_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("video.id", ondelete="CASCADE"), index=True
    )
    short_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("short.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # longform | short
    stage: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), nullable=False)  # success | failed
    output_uri: Mapped[str | None] = mapped_column(String(1024))
    log: Mapped[str | None] = mapped_column(Text)
    tool_versions: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )


class LlmCall(Base):
    """One row per attempt to call an LLM/image provider (README §5, §8) — what the daily
    free-tier checks in app/quota.py count, and how you see which model did what."""

    __tablename__ = "llm_call"

    id: Mapped[uuid.UUID] = _pk()
    provider: Mapped[str] = mapped_column(String(20), nullable=False, index=True)  # gemini | groq
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    step: Mapped[str] = mapped_column(String(40), nullable=False)  # gate | script | image | ...
    #: ok | error | retry_later (provider rate-limited/overloaded) | over_budget (local
    #: daily cap hit — never reached the provider, so not counted as usage)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    response_tokens: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False, index=True
    )


class LlmCache(Base):
    """Cached text responses keyed by role + prompt + inputs (not model), so an answer
    the fallback model produced is reused too; `model` records which one it was."""

    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(role+prompt+inputs)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    response_tokens: Mapped[int | None] = mapped_column(Integer)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        UtcDateTime, server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(UtcDateTime)


# --------------------------------------------------------------------------- #
# engine / session helpers (sync)
# --------------------------------------------------------------------------- #
_engine: Engine | None = None


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
    """SQLite ignores FK constraints (our ondelete=CASCADE/SET NULL) unless told otherwise."""
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def get_engine(url: str | None = None) -> Engine:
    global _engine
    if _engine is None or url is not None:
        db_url = url or get_settings().database_url
        if db_url.startswith("sqlite:///") and db_url != "sqlite:///:memory:":
            Path(db_url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(db_url, pool_pre_ping=True, future=True)
    return _engine


def get_sessionmaker(url: str | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


@contextmanager
def session_scope(url: str | None = None) -> Iterator[Session]:
    session = get_sessionmaker(url)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
