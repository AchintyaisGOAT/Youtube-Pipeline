"""app/orchestrator.py's dispatch/outcome rules (`ogh run`), plus the pieces it relies on:
retry-later quota errors, discover-when-empty, and UTC datetimes.

Stage modules are swapped for fakes registered in `sys.modules`, so these exercise the
orchestrator's own logic, not any real stage.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime

import pytest

from app import orchestrator, quota
from app.cli import main as ogh
from app.db import Base, Topic, Video, get_engine, get_sessionmaker
from app.quota import QuotaExceeded, RetryLater
from app.stages import discover
from app.status import Status, TopicStatus


@pytest.fixture
def session_factory(tmp_path, monkeypatch):
    # get_engine(url) also becomes the process default the orchestrator's own sessions use.
    url = f"sqlite:///{tmp_path / 'orchestrator.db'}"
    Base.metadata.create_all(get_engine(url))
    monkeypatch.setattr(orchestrator.notify, "alert", lambda message: None)
    return get_sessionmaker(url)


def _video(session_factory, status=Status.SELECTED) -> Video:
    with session_factory() as session:
        video = Video(status=status, title="Topic")
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
def test_video_walks_every_available_stage_in_one_run(session_factory, monkeypatch):
    stages = {
        Status.SELECTED: _fake_stage(monkeypatch, "fake_research", _advance_to(Status.RESEARCHED)),
        Status.RESEARCHED: _fake_stage(monkeypatch, "fake_script", _advance_to(Status.SCRIPTED)),
        Status.SCRIPTED: _fake_stage(monkeypatch, "fake_package", _advance_to(Status.PACKAGED)),
    }
    monkeypatch.setattr(orchestrator, "VIDEO_STAGES", stages)
    video = _video(session_factory)

    orchestrator._advance_videos()

    with session_factory() as session:
        assert session.get(Video, video.id).status == Status.PACKAGED


def test_missing_stage_module_parks_the_video(session_factory, monkeypatch):
    monkeypatch.setattr(orchestrator, "VIDEO_STAGES", {Status.SCRIPTED: "app.stages.not_built_yet"})
    video = _video(session_factory, Status.SCRIPTED)

    orchestrator._advance_videos()

    with session_factory() as session:
        assert session.get(Video, video.id).status == Status.SCRIPTED


def test_stage_that_does_not_advance_is_not_repeated(session_factory, monkeypatch):
    calls = []
    stages = {Status.SELECTED: _fake_stage(monkeypatch, "fake_noop", lambda s, v: calls.append(v))}
    monkeypatch.setattr(orchestrator, "VIDEO_STAGES", stages)
    _video(session_factory)

    orchestrator._advance_videos()

    assert len(calls) == 1


def test_retry_later_keeps_status_and_partial_work(session_factory, monkeypatch):
    def run(session, video_id):
        video = session.get(Video, video_id)
        video.script = "partial progress"
        video.status = Status.SCRIPTED  # moved mid-flight, then hit a limit
        raise QuotaExceeded("gemini daily unit budget exhausted")

    monkeypatch.setattr(orchestrator, "VIDEO_STAGES", {Status.RESEARCHED: _fake_stage(monkeypatch, "fake_q", run)})
    video = _video(session_factory, Status.RESEARCHED)

    orchestrator._advance_videos()

    with session_factory() as session:
        row = session.get(Video, video.id)
        assert row.status == Status.RESEARCHED  # parked, not failed, not half-moved
        assert row.script == "partial progress"  # committed, so the next run resumes from it
        assert row.error is None


def test_real_error_fails_video_and_rolls_back(session_factory, monkeypatch):
    def run(session, video_id):
        session.get(Video, video_id).script = "should be rolled back"
        raise ValueError("boom")

    monkeypatch.setattr(orchestrator, "VIDEO_STAGES", {Status.SELECTED: _fake_stage(monkeypatch, "fake_boom", run)})
    video = _video(session_factory)

    orchestrator._advance_videos()

    with session_factory() as session:
        row = session.get(Video, video.id)
        assert row.status == Status.FAILED
        assert "boom" in row.error
        assert row.script is None


# --------------------------------------------------------------------------- #
# topics: gate + rank + discover
# --------------------------------------------------------------------------- #
def test_gate_stops_after_retry_later(session_factory, monkeypatch):
    calls = []

    def run(session, topic_id):
        calls.append(topic_id)
        raise RetryLater("groq rate-limited")

    monkeypatch.setattr(orchestrator, "GATE_STAGE", _fake_stage(monkeypatch, "fake_gate", run))
    with session_factory() as session:
        session.add_all([Topic(source="s", title=t, status=TopicStatus.CANDIDATE) for t in "AB"])
        session.commit()

    orchestrator._gate_topics()

    assert len(calls) == 1
    with session_factory() as session:
        assert {t.status for t in session.query(Topic)} == {TopicStatus.CANDIDATE}


def test_gate_error_vetoes_topic_with_reason(session_factory, monkeypatch):
    def run(session, topic_id):
        raise ValueError("bad json")

    monkeypatch.setattr(orchestrator, "GATE_STAGE", _fake_stage(monkeypatch, "fake_gate_err", run))
    with session_factory() as session:
        session.add(Topic(source="s", title="A", status=TopicStatus.CANDIDATE))
        session.commit()

    orchestrator._gate_topics()

    with session_factory() as session:
        topic = session.query(Topic).one()
        assert topic.status == TopicStatus.VETOED
        assert "bad json" in topic.rationale


def test_rank_runs_from_the_orchestrator(session_factory):
    """Regression: rank takes no id; calling it with one used to reject every topic."""
    with session_factory() as session:
        session.add(Topic(source="s", title="A", status=TopicStatus.PASSED))
        session.commit()

    orchestrator._run_singleton(orchestrator.RANK_STAGE)

    with session_factory() as session:
        assert session.query(Video).one().status == Status.SELECTED
        assert session.query(Topic).one().status == TopicStatus.USED


def test_discover_only_fetches_when_no_topic_is_waiting(session_factory, monkeypatch):
    fetched = []
    monkeypatch.setattr(
        discover, "_fetch_top_articles", lambda day: fetched.append(day) or [{"article": "Rome", "rank": 1}]
    )

    with session_factory() as session:
        discover.run(session)  # empty pool -> fetch
        session.commit()
        discover.run(session)  # "Rome" is now a waiting candidate -> skip
        session.commit()
        discover.run(session, force=True)  # forced -> fetch (Rome already known, not duplicated)
        session.commit()

        assert len(fetched) == 2
        assert session.query(Topic).count() == 1


# --------------------------------------------------------------------------- #
# UTC datetimes
# --------------------------------------------------------------------------- #
def test_datetimes_round_trip_as_aware_utc(session_factory):
    decided = datetime.now(UTC)
    with session_factory() as session:
        topic = Topic(source="s", title="A", decided_at=decided)
        session.add(topic)
        session.commit()
        topic_id = topic.id

    with session_factory() as session:
        loaded = session.get(Topic, topic_id)
        assert loaded.decided_at.tzinfo is not None
        assert loaded.created_at.tzinfo is not None
        assert abs((loaded.decided_at - decided).total_seconds()) < 1


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


# --------------------------------------------------------------------------- #
# the `ogh` CLI
# --------------------------------------------------------------------------- #
def test_ogh_requires_a_command():
    with pytest.raises(SystemExit):
        ogh([])
