"""run_pipeline.py's dispatch/outcome rules, plus the G1 pieces it relies on: retry-later
quota errors, the discovery schedule, UTC datetimes and the rank monthly cap.

Stage modules are swapped for fakes registered in `sys.modules`, so these exercise the
orchestrator's own logic, not any real stage.
"""

from __future__ import annotations

import sys
import types
import uuid
from datetime import UTC, datetime, timedelta

import pytest

import run_pipeline
from app import quota, schedule
from app.config import load_channel_config
from app.db import Base, Candidate, Channel, Heartbeat, Upload, Video, get_engine, get_sessionmaker
from app.pipeline import analytics, discover
from app.quota import QuotaExceeded, RetryLater
from app.status import Status


@pytest.fixture
def session_factory(tmp_path, monkeypatch):
    # get_engine(url) also becomes the process default the orchestrator's own sessions use.
    url = f"sqlite:///{tmp_path / 'orchestrator.db'}"
    Base.metadata.create_all(get_engine(url))
    monkeypatch.setattr(run_pipeline.notify, "alert", lambda message: None)
    return get_sessionmaker(url)


@pytest.fixture
def channel(session_factory):
    with session_factory() as session:
        cfg = load_channel_config("config.example.yaml")
        ch = Channel(handle="main", name=cfg.channel.name, config=cfg.model_dump(mode="json"))
        session.add(ch)
        session.commit()
        return ch.id


def _video(session_factory, channel, status=Status.RESEARCHING) -> Video:
    with session_factory() as session:
        video = Video(channel_id=channel, status=status, title="Topic")
        session.add(video)
        session.commit()
        return video


def _fake_stage(monkeypatch, name: str, fn) -> str:
    module = types.ModuleType(name)
    module.run = fn
    monkeypatch.setitem(sys.modules, name, module)
    return name


def _advance_to(target: Status):
    def run(session, video_id):
        session.get(Video, video_id).status = target

    return run


# --------------------------------------------------------------------------- #
# videos
# --------------------------------------------------------------------------- #
def test_video_walks_every_available_stage_in_one_run(session_factory, channel, monkeypatch):
    stages = {
        Status.RESEARCHING: _fake_stage(monkeypatch, "fake_research", _advance_to(Status.FACT_CHECKING)),
        Status.FACT_CHECKING: _fake_stage(monkeypatch, "fake_factcheck", _advance_to(Status.SCRIPTING)),
        Status.SCRIPTING: _fake_stage(monkeypatch, "fake_script", _advance_to(Status.AWAITING_REVIEW)),
    }
    monkeypatch.setattr(run_pipeline, "VIDEO_STAGES", stages)
    video = _video(session_factory, channel)

    run_pipeline._advance_videos()

    with session_factory() as session:
        assert session.get(Video, video.id).status == Status.AWAITING_REVIEW


def test_stage_that_does_not_advance_is_not_repeated(session_factory, channel, monkeypatch):
    calls = []
    stages = {Status.RESEARCHING: _fake_stage(monkeypatch, "fake_noop", lambda s, v: calls.append(v))}
    monkeypatch.setattr(run_pipeline, "VIDEO_STAGES", stages)
    _video(session_factory, channel)

    run_pipeline._advance_videos()

    assert len(calls) == 1


def test_retry_later_keeps_status_and_partial_work(session_factory, channel, monkeypatch):
    def run(session, video_id):
        video = session.get(Video, video_id)
        video.angle = "partial progress"
        video.status = Status.UPLOADING  # moved mid-flight, like upload.py does
        raise QuotaExceeded("youtube daily unit budget exhausted")

    monkeypatch.setattr(run_pipeline, "VIDEO_STAGES", {Status.APPROVED: _fake_stage(monkeypatch, "fake_up", run)})
    video = _video(session_factory, channel, Status.APPROVED)

    run_pipeline._advance_videos()

    with session_factory() as session:
        row = session.get(Video, video.id)
        assert row.status == Status.APPROVED  # parked, not failed, not stuck in UPLOADING
        assert row.angle == "partial progress"  # committed, so the next run resumes from it
        assert row.error is None


def test_real_error_fails_video_and_rolls_back(session_factory, channel, monkeypatch):
    def run(session, video_id):
        session.get(Video, video_id).angle = "should be rolled back"
        raise ValueError("boom")

    monkeypatch.setattr(run_pipeline, "VIDEO_STAGES", {Status.RESEARCHING: _fake_stage(monkeypatch, "fake_boom", run)})
    video = _video(session_factory, channel)

    run_pipeline._advance_videos()

    with session_factory() as session:
        row = session.get(Video, video.id)
        assert row.status == Status.FAILED
        assert "boom" in row.error
        assert row.angle is None


# --------------------------------------------------------------------------- #
# candidates: gate + rank
# --------------------------------------------------------------------------- #
def test_gate_stops_after_retry_later(session_factory, channel, monkeypatch):
    calls = []

    def run(session, candidate_id):
        calls.append(candidate_id)
        raise RetryLater("gemini rate-limited")

    monkeypatch.setattr(run_pipeline, "GATE_STAGE", _fake_stage(monkeypatch, "fake_gate", run))
    with session_factory() as session:
        session.add_all(
            [Candidate(channel_id=channel, source="s", title=t, status=Status.CANDIDATE_NEW) for t in "AB"]
        )
        session.commit()

    run_pipeline._gate_candidates()

    assert len(calls) == 1
    with session_factory() as session:
        assert {c.status for c in session.query(Candidate)} == {Status.CANDIDATE_NEW}


def test_rank_runs_from_the_orchestrator(session_factory, channel):
    """Regression: the orchestrator used to call rank.run(session, candidate_id), which
    raised TypeError and rejected every approved candidate."""
    with session_factory() as session:
        session.add(Candidate(channel_id=channel, source="s", title="A", status=Status.CANDIDATE_APPROVED))
        session.commit()

    run_pipeline._run_singleton(run_pipeline.RANK_STAGE)

    with session_factory() as session:
        assert session.query(Video).count() == 1
        assert session.query(Candidate).one().status == Status.CANDIDATE_APPROVED


def test_rank_respects_monthly_target(session_factory, channel):
    with session_factory() as session:
        ch = session.get(Channel, channel)
        ch.config = {**ch.config, "discovery": {**ch.config["discovery"], "target_long_form_per_month": 1}}
        session.add(Video(channel_id=channel, status=Status.PUBLISHED))
        session.add(Candidate(channel_id=channel, source="s", title="A", status=Status.CANDIDATE_APPROVED))
        session.commit()

    run_pipeline._run_singleton(run_pipeline.RANK_STAGE)

    with session_factory() as session:
        assert session.query(Video).count() == 1  # quota for the month already used


# --------------------------------------------------------------------------- #
# discovery schedule
# --------------------------------------------------------------------------- #
def test_schedule_last_occurrence_weekly_and_daily():
    wed_noon = datetime(2026, 9, 30, 16, 0, tzinfo=UTC)  # Wed 12:00 in New York (EDT)
    last_mon = schedule.last_occurrence("weekly Mon 06:00", wed_noon, "America/New_York")
    assert (last_mon.weekday(), last_mon.hour, last_mon.day) == (0, 6, 28)
    assert schedule.last_occurrence("daily 13:00", wed_noon, "America/New_York").day == 29
    with pytest.raises(ValueError):
        schedule.validate("every monday")


def test_discover_only_fetches_when_due(session_factory, channel, monkeypatch):
    fetched = []
    monkeypatch.setattr(discover, "_fetch_top_articles", lambda day: fetched.append(day) or [{"article": "Rome"}])

    with session_factory() as session:
        discover.run(session)  # never run before -> due
        session.commit()
        discover.run(session)  # just ran -> not due again until next Monday 06:00
        session.commit()
        discover.run(session, force=True)
        session.commit()

        assert len(fetched) == 2
        assert session.get(Heartbeat, discover.HEARTBEAT_KEY).at.tzinfo is not None


# --------------------------------------------------------------------------- #
# UTC datetimes + analytics
# --------------------------------------------------------------------------- #
def test_datetimes_round_trip_as_aware_utc_and_analytics_can_compare(session_factory):
    published = datetime.now(UTC) - timedelta(days=3)
    with session_factory() as session:
        upload = Upload(
            target_type="video", target_id=uuid.uuid4(), youtube_id="abc",
            status=Status.PUBLISHED, published_at=published,
        )
        session.add(upload)
        session.commit()
        upload_id = upload.id

    with session_factory() as session:
        loaded = session.get(Upload, upload_id).published_at
        assert loaded.tzinfo is not None
        # used to raise TypeError: can't compare offset-naive and offset-aware datetimes
        assert analytics._due_offsets(loaded, set()) == [timedelta(hours=48)]


# --------------------------------------------------------------------------- #
# quota + llm retry-later
# --------------------------------------------------------------------------- #
def test_quota_exceeded_is_retry_later_and_requests_are_counted(session_factory, monkeypatch):
    assert issubclass(QuotaExceeded, RetryLater)
    monkeypatch.setitem(quota.DAILY_LIMITS, "gemini", {"units": 2})
    with session_factory() as session:
        quota.check_and_increment(session, "gemini", units=1, tokens=10)
        quota.check_and_increment(session, "gemini", units=1, tokens=10)
        with pytest.raises(QuotaExceeded):
            quota.check_and_increment(session, "gemini", units=1)


def test_llm_turns_exhausted_rate_limit_into_retry_later(monkeypatch):
    import tenacity
    from google.genai import errors as genai_errors

    from app import llm

    class FakeModels:
        def generate_content(self, **kwargs):
            raise genai_errors.ClientError(429, {"error": {"message": "RESOURCE_EXHAUSTED"}})

    monkeypatch.setattr(llm, "_client_instance", lambda: types.SimpleNamespace(models=FakeModels()))
    monkeypatch.setattr(tenacity, "wait_exponential", lambda **kwargs: tenacity.wait_none())

    with pytest.raises(RetryLater):
        llm._generate_with_retry("some-model", "prompt", None)
