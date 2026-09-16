"""Database: SQLAlchemy 2.0 models for every table in DESIGN.md section 5, plus
``llm_cache`` (DESIGN section 8.2), and a small sync engine/session helper.

Single-file SQLite (see WORK_FOUNDATION.md §2) — no server, no Alembic; schema changes
go through ``Base.metadata.create_all()`` at startup, which is additive-only. Status
values are stored as plain strings — always assign ``Status`` members
(``app.status.Status``), never bare strings.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
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
from app.status import Status


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(Uuid, primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# channel
# --------------------------------------------------------------------------- #
class Channel(Base, TimestampMixin):
    __tablename__ = "channel"

    id: Mapped[uuid.UUID] = _pk()
    handle: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), default="")
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON, nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    videos: Mapped[list[Video]] = relationship(back_populates="channel")


# --------------------------------------------------------------------------- #
# discovery
# --------------------------------------------------------------------------- #
class Candidate(Base, TimestampMixin):
    __tablename__ = "candidate"
    __table_args__ = (
        UniqueConstraint("channel_id", "source", "title", name="uq_candidate_dedupe"),
    )

    id: Mapped[uuid.UUID] = _pk()
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channel.id", ondelete="CASCADE"), index=True
    )
    source: Mapped[str] = mapped_column(String(60), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict | None] = mapped_column(JSON)
    score: Mapped[float | None] = mapped_column(Float)
    rationale: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default=Status.CANDIDATE_NEW)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(60))


# --------------------------------------------------------------------------- #
# video + shorts + segments
# --------------------------------------------------------------------------- #
class Video(Base, TimestampMixin):
    __tablename__ = "video"

    id: Mapped[uuid.UUID] = _pk()
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channel.id", ondelete="CASCADE"), index=True
    )
    candidate_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("candidate.id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, default=Status.RESEARCHING)

    title: Mapped[str | None] = mapped_column(String(300))
    angle: Mapped[str | None] = mapped_column(String(400))
    script: Mapped[str | None] = mapped_column(Text)
    script_prompt_hash: Mapped[str | None] = mapped_column(String(64))
    research: Mapped[dict | None] = mapped_column(JSON)
    video_metadata: Mapped[dict | None] = mapped_column("metadata", JSON)
    duration_s: Mapped[float | None] = mapped_column(Float)
    thumbnail_uri: Mapped[str | None] = mapped_column(String(1024))
    error: Mapped[str | None] = mapped_column(Text)

    channel: Mapped[Channel] = relationship(back_populates="videos")
    segments: Mapped[list[Segment]] = relationship(
        back_populates="video", cascade="all, delete-orphan", order_by="Segment.idx"
    )
    shorts: Mapped[list[Short]] = relationship(
        back_populates="video", cascade="all, delete-orphan", order_by="Short.idx"
    )


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
    status: Mapped[str] = mapped_column(String(40), nullable=False, default=Status.CUTTING_SHORTS)
    youtube_id: Mapped[str | None] = mapped_column(String(32))

    video: Mapped[Video] = relationship(back_populates="shorts")


class Segment(Base):
    __tablename__ = "segment"
    __table_args__ = (UniqueConstraint("video_id", "idx", name="uq_segment_video_idx"),)

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

    video: Mapped[Video] = relationship(back_populates="segments")


# --------------------------------------------------------------------------- #
# assets
# --------------------------------------------------------------------------- #
class Asset(Base):
    __tablename__ = "asset"
    __table_args__ = (UniqueConstraint("source", "source_id", name="uq_asset_source"),)

    id: Mapped[uuid.UUID] = _pk()
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # image | music | sfx
    source: Mapped[str] = mapped_column(String(60), nullable=False)
    source_id: Mapped[str | None] = mapped_column(String(200))
    license: Mapped[str | None] = mapped_column(String(60))
    rights_url: Mapped[str | None] = mapped_column(String(1024))
    attribution: Mapped[str | None] = mapped_column(Text)
    uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    meta: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# renders + uploads + analytics
# --------------------------------------------------------------------------- #
class Render(Base):
    __tablename__ = "render"

    id: Mapped[uuid.UUID] = _pk()
    video_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("video.id", ondelete="CASCADE"), index=True
    )
    short_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("short.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # longform | short | stage
    stage: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    output_uri: Mapped[str | None] = mapped_column(String(1024))
    duration_s: Mapped[float | None] = mapped_column(Float)
    log: Mapped[str | None] = mapped_column(Text)
    tool_versions: Mapped[dict | None] = mapped_column(JSON)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Upload(Base, TimestampMixin):
    __tablename__ = "upload"

    id: Mapped[uuid.UUID] = _pk()
    target_type: Mapped[str] = mapped_column(String(16), nullable=False)  # video | short
    target_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    client_token: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False, unique=True, default=uuid.uuid4
    )
    status: Mapped[str] = mapped_column(String(40), nullable=False, default=Status.UPLOADING)
    youtube_id: Mapped[str | None] = mapped_column(String(32))
    visibility: Mapped[str | None] = mapped_column(String(16))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    quota_units: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)


class AnalyticsSnapshot(Base):
    __tablename__ = "analytics_snapshot"
    __table_args__ = (UniqueConstraint("youtube_id", "captured_at", name="uq_analytics_snapshot"),)

    id: Mapped[uuid.UUID] = _pk()
    youtube_id: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    views: Mapped[int | None] = mapped_column(Integer)
    watch_time_minutes: Mapped[float | None] = mapped_column(Float)
    avg_view_duration_s: Mapped[float | None] = mapped_column(Float)
    retention: Mapped[dict | None] = mapped_column(JSON)


# --------------------------------------------------------------------------- #
# ledgers + feedback + ops
# --------------------------------------------------------------------------- #
class ApiQuotaLedger(Base):
    __tablename__ = "api_quota_ledger"
    __table_args__ = (UniqueConstraint("api", "day", name="uq_quota_api_day"),)

    id: Mapped[uuid.UUID] = _pk()
    api: Mapped[str] = mapped_column(String(40), nullable=False)  # youtube | gemini | ...
    day: Mapped[date] = mapped_column(Date, nullable=False)
    units_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class TopicPerformance(Base, TimestampMixin):
    __tablename__ = "topic_performance"

    id: Mapped[uuid.UUID] = _pk()
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channel.id", ondelete="CASCADE"), index=True
    )
    topic_key: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    tag: Mapped[str | None] = mapped_column(String(80))
    length_bucket: Mapped[str | None] = mapped_column(String(20))
    thumb_style: Mapped[str | None] = mapped_column(String(40))
    videos: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_views: Mapped[float | None] = mapped_column(Float)
    avg_retention: Mapped[float | None] = mapped_column(Float)
    score: Mapped[float | None] = mapped_column(Float)


class Heartbeat(Base):
    __tablename__ = "heartbeat"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON)


class LlmCache(Base):
    __tablename__ = "llm_cache"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)  # sha256(model+prompt+inputs)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    response: Mapped[dict] = mapped_column(JSON, nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    response_tokens: Mapped[int | None] = mapped_column(Integer)
    hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
