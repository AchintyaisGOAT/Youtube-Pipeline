"""Upload the long-form video, then each rendered Short, to YouTube.

DESIGN.md #16 (double-upload on retry): an `upload` row + `client_token` is written
*before* the API call, and the token is embedded in the video's description. On any
retry, the channel is searched for that token first — a crash between "upload
succeeded" and "our own status update committed" must not re-upload. (YouTube's search
index can lag by minutes, so an immediate retry has a narrow window where this can't
yet find a video that did in fact just go up — a real gap, not a solved one; a stronger
guarantee would need the raw API response persisted synchronously with the row insert.)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_channel_config, get_settings
from app.db import Short, Upload, Video
from app.quota import check_and_increment
from app.status import Status
from app.storage import output_dir

#: Rough YouTube Data API v3 cost of one resumable videos.insert call.
_UPLOAD_QUOTA_UNITS = 1600


def _youtube_client():
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
    return build("youtube", "v3", credentials=credentials)


def _find_existing_upload(client, client_token: uuid.UUID) -> str | None:
    response = (
        client.search()
        .list(part="snippet", forMine=True, q=str(client_token), type="video", maxResults=1)
        .execute()
    )
    items = response.get("items", [])
    return items[0]["id"]["videoId"] if items else None


def _upload_one(
    session: Session,
    client,
    *,
    target_type: str,
    target_id: uuid.UUID,
    file_path: Path,
    title: str,
    description: str,
    tags: list[str],
    visibility: str,
) -> str:
    upload_row = session.execute(
        select(Upload).filter_by(target_type=target_type, target_id=target_id)
    ).scalar_one_or_none()
    if upload_row is not None and upload_row.youtube_id:
        return upload_row.youtube_id  # already uploaded, nothing to do

    if upload_row is None:
        upload_row = Upload(target_type=target_type, target_id=target_id, status=Status.UPLOADING, visibility=visibility)
        session.add(upload_row)
        session.flush()  # need client_token populated before we can embed/search it

    found = _find_existing_upload(client, upload_row.client_token)
    if found:
        upload_row.youtube_id = found
        upload_row.status = Status.PUBLISHED
        upload_row.published_at = datetime.now(UTC)
        return found

    check_and_increment(session, "youtube", units=_UPLOAD_QUOTA_UNITS)

    from googleapiclient.http import MediaFileUpload

    tagged_description = f"{description}\n\n<!-- upload-token:{upload_row.client_token} -->"
    body = {
        "snippet": {"title": title[:100], "description": tagged_description[:5000], "tags": tags[:500], "categoryId": "27"},
        "status": {
            "privacyStatus": visibility,
            "selfDeclaredMadeForKids": False,
            "containsSyntheticMedia": True,  # DESIGN.md #2 — AI voice must be disclosed
        },
    }
    media = MediaFileUpload(str(file_path), chunksize=-1, resumable=True, mimetype="video/mp4")
    response = client.videos().insert(part="snippet,status", body=body, media_body=media, notifySubscribers=False).execute()

    upload_row.youtube_id = response["id"]
    upload_row.status = Status.PUBLISHED
    upload_row.published_at = datetime.now(UTC)
    upload_row.quota_units = _UPLOAD_QUOTA_UNITS
    return upload_row.youtube_id


def run(session: Session, video_id: uuid.UUID) -> None:
    video = session.get(Video, video_id)
    if video is None or Status(video.status) != Status.APPROVED:
        return

    long_form_path = output_dir(video_id) / "final.mp4"
    if not long_form_path.exists():
        raise FileNotFoundError(f"video {video_id}: no final render at {long_form_path}")

    config = get_channel_config(session, video.channel_id)
    client = _youtube_client()
    metadata = video.video_metadata or {}
    video.status = Status.UPLOADING

    _upload_one(
        session,
        client,
        target_type="video",
        target_id=video.id,
        file_path=long_form_path,
        title=video.title or "Untitled",
        description=metadata.get("description", ""),
        tags=metadata.get("tags", []),
        visibility=config.publish.api_visibility,
    )

    shorts = session.execute(select(Short).filter_by(video_id=video_id).order_by(Short.idx)).scalars().all()
    for short in shorts:
        short_path = output_dir(video_id) / f"short_{short.idx}.mp4"
        if not short_path.exists():
            continue  # shorts.py skipped this one (e.g. no timing) — nothing to upload

        short.youtube_id = _upload_one(
            session,
            client,
            target_type="short",
            target_id=short.id,
            file_path=short_path,
            title=short.title or f"{video.title} — Short {short.idx + 1}",
            description=short.caption or "",
            tags=metadata.get("tags", []),
            visibility=config.publish.api_visibility,
        )
        short.status = Status.PUBLISHED

    video.status = Status.PUBLISHED
