from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

import review
from app.config import ChannelConfig, load_channel_config
from app.db import Base, Channel, Segment, Short, Upload, Video, get_engine, get_sessionmaker
from app.pipeline import align, shorts, thumbnail, upload
from app.pipeline._subtitles import build_subtitles
from app.status import Status


@pytest.fixture
def session_factory(tmp_path):
    url = f"sqlite:///{tmp_path / 'media.db'}"
    Base.metadata.create_all(get_engine(url))
    return get_sessionmaker(url)


@pytest.fixture
def channel(session_factory):
    with session_factory() as session:
        cfg = load_channel_config("config.example.yaml")
        ch = Channel(handle="main", name=cfg.channel.name, config=cfg.model_dump(mode="json"))
        session.add(ch)
        session.commit()
        return ch.id


# --------------------------------------------------------------------------- #
# align.py — forced alignment vs. proportional-timing fallback, and short merging
# --------------------------------------------------------------------------- #
def test_forced_alignment_assigns_segment_boundaries_from_recognized_words():
    segments = [
        SimpleNamespace(text="Rome was not built in a day.", start_s=None, end_s=None),
        SimpleNamespace(text="Caesar crossed the Rubicon.", start_s=None, end_s=None),
    ]
    # 7 words then 4 words, contiguous, matching the segments exactly
    recognized = [(0.0, 0.4), (0.4, 0.8), (0.8, 1.0), (1.0, 1.5), (1.5, 1.7), (1.7, 1.9), (1.9, 2.3),
                  (2.3, 2.6), (2.6, 3.0), (3.0, 3.3), (3.3, 3.8)]

    ok = align._forced_alignment(segments, recognized)

    assert ok is True
    assert segments[0].start_s == 0.0
    assert segments[0].end_s == 2.3
    assert segments[1].start_s == 2.3
    assert segments[1].end_s == 3.8


def test_forced_alignment_fails_when_too_few_recognized_words():
    segments = [SimpleNamespace(text="A fairly long sentence with many words in it.", start_s=None, end_s=None)]
    assert align._forced_alignment(segments, []) is False


def test_proportional_timing_splits_duration_by_word_count():
    segments = [
        SimpleNamespace(text="one two", start_s=None, end_s=None),
        SimpleNamespace(text="one two three four", start_s=None, end_s=None),
    ]
    align._proportional_timing(segments, duration_s=6.0)

    assert segments[0].start_s == pytest.approx(0.0)
    assert segments[0].end_s == pytest.approx(2.0)  # 2/6 of 6s
    assert segments[1].start_s == pytest.approx(2.0)
    assert segments[1].end_s == pytest.approx(6.0)  # 4/6 of 6s


def test_create_shorts_merges_contiguous_short_spans(session_factory, channel):
    with session_factory() as session:
        video = Video(channel_id=channel, status=Status.ALIGNING)
        session.add(video)
        session.commit()

        segs = [
            Segment(video_id=video.id, idx=0, text="a", start_s=0.0, end_s=1.0, in_short_span=False),
            Segment(video_id=video.id, idx=1, text="b", start_s=1.0, end_s=2.0, in_short_span=True),
            Segment(video_id=video.id, idx=2, text="c", start_s=2.0, end_s=3.0, in_short_span=True),
            Segment(video_id=video.id, idx=3, text="d", start_s=3.0, end_s=4.0, in_short_span=False),
            Segment(video_id=video.id, idx=4, text="e", start_s=4.0, end_s=5.0, in_short_span=True),
        ]
        session.add_all(segs)
        session.commit()

        align._create_shorts(session, video.id, segs)
        session.commit()

        result = session.query(Short).order_by(Short.idx).all()
        assert len(result) == 2
        assert (result[0].start_s, result[0].end_s) == (1.0, 3.0)
        assert (result[1].start_s, result[1].end_s) == (4.0, 5.0)


def test_align_is_idempotent_when_not_in_expected_status(session_factory, channel):
    with session_factory() as session:
        video = Video(channel_id=channel, status=Status.RESEARCHING)
        session.add(video)
        session.commit()
        align.run(session, video.id)  # wrong status -> no-op, must not raise
        assert video.status == Status.RESEARCHING


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
# _subtitles.py — golden fixture: deterministic SRT output from fixed segments
# --------------------------------------------------------------------------- #
GOLDEN_SRT = (
    "1\n00:00:00,000 --> 00:00:02,200\nRome was not built in a day.\n\n"
    "2\n00:00:02,200 --> 00:00:04,500\nCaesar crossed the Rubicon.\n\n"
)


def test_subtitles_golden_srt_fixture():
    segments = [
        SimpleNamespace(text="Rome was not built in a day.", start_s=0.0, end_s=2.2),
        SimpleNamespace(text="Caesar crossed the Rubicon.", start_s=2.2, end_s=4.5),
    ]
    subs = build_subtitles(segments, ChannelConfig())
    assert subs.to_string("srt") == GOLDEN_SRT


def test_subtitles_skip_unaligned_segments():
    segments = [SimpleNamespace(text="never aligned", start_s=None, end_s=None)]
    subs = build_subtitles(segments, ChannelConfig())
    assert len(subs.events) == 0


def test_subtitles_ass_includes_karaoke_tags_when_enabled():
    segments = [SimpleNamespace(text="one two three", start_s=0.0, end_s=3.0)]
    config = ChannelConfig()
    assert config.subtitles.karaoke is True
    subs = build_subtitles(segments, config)
    assert "\\k" in subs.events[0].text


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
# upload.py — dedup-on-retry (DESIGN.md #16), no real YouTube API needed
# --------------------------------------------------------------------------- #
class _FakeYouTubeClient:
    """Mimics the two call chains upload.py uses: .search().list().execute() and
    .videos().insert().execute()."""

    def __init__(self, search_result: dict | None = None):
        self._search_result = search_result or {"items": []}
        self.insert_calls: list[dict] = []

    def search(self):
        return SimpleNamespace(list=lambda **kw: SimpleNamespace(execute=lambda: self._search_result))

    def videos(self):
        def insert(**kwargs):
            self.insert_calls.append(kwargs)
            return SimpleNamespace(execute=lambda: {"id": "brand-new-video-id"})

        return SimpleNamespace(insert=insert)


def test_upload_one_skips_api_call_when_already_uploaded(session_factory, channel):
    with session_factory() as session:
        video = Video(channel_id=channel, status=Status.APPROVED)
        session.add(video)
        session.commit()
        session.add(Upload(target_type="video", target_id=video.id, youtube_id="already-there"))
        session.commit()

        client = _FakeYouTubeClient()
        result = upload._upload_one(
            session, client,
            target_type="video", target_id=video.id, file_path="unused",
            title="t", description="d", tags=[], visibility="private",
        )

        assert result == "already-there"
        assert client.insert_calls == []  # never even tried to call the API


def test_upload_one_dedups_via_search_after_a_simulated_crash(session_factory, channel, tmp_path):
    fake_video_file = tmp_path / "final.mp4"
    fake_video_file.write_bytes(b"not a real mp4, just needs to exist on disk")

    with session_factory() as session:
        video = Video(channel_id=channel, status=Status.APPROVED)
        session.add(video)
        session.commit()
        # simulate: the Upload row was inserted and committed, then the process crashed
        # *before* the API call ever happened -- so youtube_id is still null.
        pending = Upload(target_type="video", target_id=video.id, status=Status.UPLOADING)
        session.add(pending)
        session.commit()

        # the "crash" here is: search finds nothing (nothing was ever actually
        # uploaded), so retrying must fall through to a real upload call.
        client = _FakeYouTubeClient(search_result={"items": []})
        result = upload._upload_one(
            session, client,
            target_type="video", target_id=video.id, file_path=fake_video_file,
            title="t", description="d", tags=[], visibility="private",
        )

        assert result == "brand-new-video-id"
        assert len(client.insert_calls) == 1


def test_upload_one_finds_prior_success_via_token_search_instead_of_reuploading(session_factory, channel, monkeypatch):
    with session_factory() as session:
        video = Video(channel_id=channel, status=Status.APPROVED)
        session.add(video)
        session.commit()
        pending = Upload(target_type="video", target_id=video.id, status=Status.UPLOADING)
        session.add(pending)
        session.commit()

        # this time the upload DID go through before the crash -- search finds it.
        client = _FakeYouTubeClient(search_result={"items": [{"id": {"videoId": "found-via-search"}}]})
        result = upload._upload_one(
            session, client,
            target_type="video", target_id=video.id, file_path="unused",
            title="t", description="d", tags=[], visibility="private",
        )

        assert result == "found-via-search"
        assert client.insert_calls == []  # dedup worked -- no second upload


# --------------------------------------------------------------------------- #
# review.py — CLI decision loop persists a status change (no real player launch)
# --------------------------------------------------------------------------- #
def test_review_one_approve_persists_status(session_factory, channel, monkeypatch):
    monkeypatch.setattr(review, "_open_in_default_player", lambda path: None)
    with session_factory() as session:
        video = Video(channel_id=channel, status=Status.AWAITING_REVIEW, title="T", video_metadata={})
        session.add(video)
        session.commit()

        responses = iter(["a"])
        review._review_one(session, video, input_fn=lambda prompt="": next(responses))
        session.commit()

        assert video.status == Status.APPROVED


def test_review_one_reject_records_reason(session_factory, channel, monkeypatch):
    monkeypatch.setattr(review, "_open_in_default_player", lambda path: None)
    with session_factory() as session:
        video = Video(channel_id=channel, status=Status.AWAITING_REVIEW, title="T", video_metadata={})
        session.add(video)
        session.commit()

        responses = iter(["r", "factually wrong"])
        review._review_one(session, video, input_fn=lambda prompt="": next(responses))
        session.commit()

        assert video.status == Status.REJECTED
        assert video.error == "factually wrong"


def test_review_one_skip_leaves_status_unchanged(session_factory, channel, monkeypatch):
    monkeypatch.setattr(review, "_open_in_default_player", lambda path: None)
    with session_factory() as session:
        video = Video(channel_id=channel, status=Status.AWAITING_REVIEW, title="T", video_metadata={})
        session.add(video)
        session.commit()

        responses = iter(["s"])
        review._review_one(session, video, input_fn=lambda prompt="": next(responses))

        assert video.status == Status.AWAITING_REVIEW
