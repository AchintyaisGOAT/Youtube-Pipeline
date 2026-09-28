from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import cli as review
from app.db import Base, Video, get_engine, get_sessionmaker
from app.stages import shorts, thumbnail
from app.status import Status


@pytest.fixture
def session_factory(tmp_path):
    url = f"sqlite:///{tmp_path / 'media.db'}"
    Base.metadata.create_all(get_engine(url))
    return get_sessionmaker(url)




# --------------------------------------------------------------------------- #
# shorts.py — max_seconds hard gate and per-short caption
# --------------------------------------------------------------------------- #
def test_trim_to_max_seconds_caps_the_tail_not_the_head():
    assert shorts._trim_to_max_seconds(10.0, 70.0, max_seconds=55.0) == 65.0
    assert shorts._trim_to_max_seconds(10.0, 40.0, max_seconds=55.0) == 40.0  # already under, untouched


def test_caption_for_uses_existing_caption_or_falls_back_to_covered_segment_text():
    short = SimpleNamespace(caption="Custom caption", idx=0, start_s=0.0, end_s=1.0)
    assert shorts._caption_for(short, []) == "Custom caption"

    short2 = SimpleNamespace(caption=None, idx=0, start_s=1.0, end_s=2.0)
    segs = [SimpleNamespace(text="The covering sentence.", start_s=1.0, end_s=2.0)]
    assert shorts._caption_for(short2, segs) == "The covering sentence."

    short3 = SimpleNamespace(caption=None, idx=2, start_s=9.0, end_s=10.0)
    assert shorts._caption_for(short3, []) == "Short 3"


# --------------------------------------------------------------------------- #
# thumbnail.py — pure compositing, no DB/FFmpeg needed
# --------------------------------------------------------------------------- #
def test_headline_truncates_to_four_words():
    assert thumbnail._headline("The Fall of the Roman Empire: A Complete History") == "THE FALL OF THE"


def test_render_thumbnail_produces_correct_size_without_a_subject_image():
    img = thumbnail.render_thumbnail("A Title", None)
    assert img.size == thumbnail.THUMBNAIL_SIZE


def test_render_thumbnail_survives_a_corrupt_subject_image(tmp_path):
    bad = tmp_path / "not_an_image.jpg"
    bad.write_text("not actually an image")
    img = thumbnail.render_thumbnail("A Title", bad)
    assert img.size == thumbnail.THUMBNAIL_SIZE  # falls back to the plain background


# --------------------------------------------------------------------------- #
# ogh review — CLI decision loop persists a status change (no real player launch)
# --------------------------------------------------------------------------- #
def test_review_one_approve_persists_status(session_factory, monkeypatch):
    monkeypatch.setattr(review, "_open_in_default_player", lambda path: None)
    with session_factory() as session:
        video = Video(status=Status.PACKAGED, title="T", video_metadata={})
        session.add(video)
        session.commit()

        responses = iter(["a"])
        review._review_one(session, video, input_fn=lambda prompt="": next(responses))
        session.commit()

        assert video.status == Status.APPROVED


def test_review_one_reject_records_reason(session_factory, monkeypatch):
    monkeypatch.setattr(review, "_open_in_default_player", lambda path: None)
    with session_factory() as session:
        video = Video(status=Status.PACKAGED, title="T", video_metadata={})
        session.add(video)
        session.commit()

        responses = iter(["r", "factually wrong"])
        review._review_one(session, video, input_fn=lambda prompt="": next(responses))
        session.commit()

        assert video.status == Status.REJECTED
        assert video.error == "factually wrong"


def test_review_one_skip_leaves_status_unchanged(session_factory, monkeypatch):
    monkeypatch.setattr(review, "_open_in_default_player", lambda path: None)
    with session_factory() as session:
        video = Video(status=Status.PACKAGED, title="T", video_metadata={})
        session.add(video)
        session.commit()

        responses = iter(["s"])
        review._review_one(session, video, input_fn=lambda prompt="": next(responses))

        assert video.status == Status.PACKAGED
