"""Pull YouTube Analytics API at 48h/7d post-publish for each published `upload` ->
`analytics_snapshot` rows; also rolls the result into `topic_performance` (DESIGN.md
#28). No id parameter — scans every published upload for a due snapshot each call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import AnalyticsSnapshot, TopicPerformance, Upload, Video
from app.status import Status

_SNAPSHOT_OFFSETS = (timedelta(hours=48), timedelta(days=7))


def _analytics_client():
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    settings = get_settings()
    credentials = Credentials(
        token=None,
        refresh_token=settings.youtube_refresh_token,
        client_id=settings.youtube_client_id,
        client_secret=settings.youtube_client_secret,
        token_uri="https://oauth2.googleapis.com/token",
    )
    return build("youtubeAnalytics", "v2", credentials=credentials)


def _due_offsets(published_at: datetime, already_captured: set[timedelta]) -> list[timedelta]:
    now = datetime.now(UTC)
    return [off for off in _SNAPSHOT_OFFSETS if off not in already_captured and now >= published_at + off]


def _capture_snapshot(session: Session, client, upload: Upload, offset: timedelta) -> None:
    captured_at = upload.published_at + offset
    response = (
        client.reports()
        .query(
            ids="channel==MINE",
            startDate=upload.published_at.date().isoformat(),
            endDate=captured_at.date().isoformat(),
            metrics="views,estimatedMinutesWatched,averageViewDuration",
            filters=f"video=={upload.youtube_id}",
        )
        .execute()
    )
    views, watch_minutes, avg_duration = (response.get("rows") or [[0, 0, 0]])[0]

    session.add(
        AnalyticsSnapshot(
            youtube_id=upload.youtube_id,
            captured_at=captured_at,
            views=views,
            watch_time_minutes=watch_minutes,
            avg_view_duration_s=avg_duration,
        )
    )
    _update_topic_performance(session, upload, views)


def _update_topic_performance(session: Session, upload: Upload, views: int) -> None:
    if upload.target_type != "video":
        return
    video = session.get(Video, upload.target_id)
    if video is None:
        return

    topic_key = video.title or str(video.id)
    row = session.execute(
        select(TopicPerformance).filter_by(channel_id=video.channel_id, topic_key=topic_key)
    ).scalar_one_or_none()
    if row is None:
        session.add(TopicPerformance(channel_id=video.channel_id, topic_key=topic_key, videos=1, avg_views=views))
        return

    total = row.videos + 1
    row.avg_views = ((row.avg_views or 0) * row.videos + views) / total
    row.videos = total


def run(session: Session) -> None:
    uploads = session.execute(
        select(Upload).filter_by(status=Status.PUBLISHED).where(Upload.published_at.is_not(None))
    ).scalars().all()
    if not uploads:
        return

    client = _analytics_client()
    for upload in uploads:
        already_captured = {
            timedelta(seconds=round((snap.captured_at - upload.published_at).total_seconds()))
            for snap in session.execute(select(AnalyticsSnapshot).filter_by(youtube_id=upload.youtube_id)).scalars()
        }
        for offset in _due_offsets(upload.published_at, already_captured):
            _capture_snapshot(session, client, upload, offset)
