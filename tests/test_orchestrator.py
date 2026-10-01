"""app/orchestrator.py's dispatch/outcome rules (`ogh run`), plus the pieces it relies on:
discover-when-empty and UTC datetimes. LLM routing/quota tests live in test_llm.py.

Stage modules are swapped for fakes registered in `sys.modules`, so these exercise the
orchestrator's own logic, not any real stage.
"""

from __future__ import annotations

import sys
import types
from datetime import UTC, datetime

import pytest

from app import orchestrator
from app.cli import main as ogh
from app.db import Base, Topic, Video, get_engine, get_sessionmaker
from app.quota import QuotaExceeded
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




def test_rank_runs_from_the_orchestrator(session_factory):
    """Regression: rank takes no id; calling it with one used to reject every topic."""
    with session_factory() as session:
        session.add(Topic(source="s", title="A", status=TopicStatus.PASSED, raw={"images": 40}))
        session.commit()

    orchestrator._run_singleton(orchestrator.RANK_STAGE)

    with session_factory() as session:
        assert session.query(Video).one().status == Status.SELECTED
        assert session.query(Topic).one().status == TopicStatus.USED




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
# the `ogh` CLI
# --------------------------------------------------------------------------- #
def test_ogh_requires_a_command():
    with pytest.raises(SystemExit):
        ogh([])


def test_http_retries_only_transient_errors():
    import httpx

    from app.http import is_transient

    request = httpx.Request("GET", "https://example.org/")
    status = lambda code: httpx.HTTPStatusError("x", request=request, response=httpx.Response(code, request=request))  # noqa: E731
    assert is_transient(httpx.ConnectTimeout("t")) and is_transient(status(503)) and is_transient(status(429))
    assert not is_transient(status(404)) and not is_transient(status(403))
