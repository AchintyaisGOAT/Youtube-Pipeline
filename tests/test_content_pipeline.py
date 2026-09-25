from __future__ import annotations

import pytest

from app.config import ChannelConfig, load_channel_config
from app.db import Base, Candidate, Channel, Video, get_engine, get_sessionmaker
from app.pipeline import gate, images, rank, script, segment
from app.status import Status

FIXED_SCRIPT = (
    "Rome was not built in a day. "
    "[SHORT]In 44 BC, Julius Caesar was assassinated on the Ides of March by a group "
    "of senators who feared his growing power.[/SHORT] "
    "The senate had hoped his death would restore the republic. "
    "[SHORT]Instead, it triggered over a decade of civil war that ended the republic "
    "for good.[/SHORT] "
    "Octavian would go on to become Rome's first emperor."
)


@pytest.fixture
def session_factory(tmp_path):
    url = f"sqlite:///{tmp_path / 'content.db'}"
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
# segment.py — golden-file: [SHORT] spans -> in_short_span on the right segments
# --------------------------------------------------------------------------- #
def test_segment_marks_short_spans(session_factory, channel):
    with session_factory() as session:
        video = Video(channel_id=channel, status=Status.SEGMENTING, script=FIXED_SCRIPT)
        session.add(video)
        session.commit()

        segment.run(session, video.id)
        session.commit()

        rows = video.segments
        assert len(rows) >= 5
        assert video.status == Status.FETCHING_IMAGES

        short_texts = {s.text for s in rows if s.in_short_span}
        assert any("Ides of March" in t for t in short_texts)
        assert any("civil war" in t for t in short_texts)
        assert not any("Rome was not built" in t for t in short_texts)
        assert not any("Octavian" in t for t in short_texts)
        # every segment got some image query
        assert all(s.image_query for s in rows)


def test_segment_is_idempotent_when_not_in_expected_status(session_factory, channel):
    with session_factory() as session:
        video = Video(channel_id=channel, status=Status.RESEARCHING, script=FIXED_SCRIPT)
        session.add(video)
        session.commit()

        segment.run(session, video.id)  # wrong status -> no-op
        session.commit()

        assert video.segments == []
        assert video.status == Status.RESEARCHING


# --------------------------------------------------------------------------- #
# gate.py — two-tier blocklist against config.example.yaml's real lists
# --------------------------------------------------------------------------- #
def test_gate_vetoes_recent_event(session_factory, channel):
    with session_factory() as session:
        candidate = Candidate(
            channel_id=channel,
            source="wikimedia_pageviews",
            title="2024 Something That Just Happened",
            status=Status.CANDIDATE_NEW,
        )
        session.add(candidate)
        session.commit()

        gate.run(session, candidate.id)
        session.commit()

        assert candidate.status == Status.CANDIDATE_VETOED
        assert "last 10 years" in candidate.rationale.lower() or "10 years" in candidate.rationale


def test_gate_does_not_call_llm_for_deterministic_recency_veto(session_factory, channel, monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("recency veto should short-circuit before any LLM call")

    monkeypatch.setattr(gate.llm, "generate", _fail)
    with session_factory() as session:
        candidate = Candidate(
            channel_id=channel, source="s", title="2024 Something", status=Status.CANDIDATE_NEW
        )
        session.add(candidate)
        session.commit()
        gate.run(session, candidate.id)  # would raise via _fail if it reached the LLM
        session.commit()
        assert candidate.status == Status.CANDIDATE_VETOED


def test_gate_approves_via_sensitivity_check(session_factory, channel, monkeypatch):
    monkeypatch.setattr(gate.llm, "generate", lambda *a, **k: {"verdict": "pass", "reason": "benign"})
    with session_factory() as session:
        candidate = Candidate(
            channel_id=channel,
            source="wikimedia_pageviews",
            title="Roman aqueducts",
            summary="How Roman engineers built long-distance water supply systems.",
            status=Status.CANDIDATE_NEW,
        )
        session.add(candidate)
        session.commit()

        gate.run(session, candidate.id)
        session.commit()

        assert candidate.status == Status.CANDIDATE_APPROVED


def test_gate_routes_to_manual_review_via_sensitivity_check(session_factory, channel, monkeypatch):
    monkeypatch.setattr(
        gate.llm, "generate", lambda *a, **k: {"verdict": "manual_review", "reason": "religion is the subject"}
    )
    with session_factory() as session:
        candidate = Candidate(
            channel_id=channel,
            source="wikimedia_pageviews",
            title="Religion in the Roman Empire",
            status=Status.CANDIDATE_NEW,
        )
        session.add(candidate)
        session.commit()

        gate.run(session, candidate.id)
        session.commit()

        assert candidate.status == Status.CANDIDATE_MANUAL_REVIEW


def test_gate_defaults_uncertain_verdict_to_manual_review(session_factory, channel, monkeypatch):
    monkeypatch.setattr(gate.llm, "generate", lambda *a, **k: {"verdict": "not-a-real-verdict"})
    with session_factory() as session:
        candidate = Candidate(
            channel_id=channel, source="s", title="Something ambiguous", status=Status.CANDIDATE_NEW
        )
        session.add(candidate)
        session.commit()

        gate.run(session, candidate.id)
        session.commit()

        assert candidate.status == Status.CANDIDATE_MANUAL_REVIEW


# --------------------------------------------------------------------------- #
# rank.py — promotes the best candidate, never the same one twice
# --------------------------------------------------------------------------- #
def test_rank_promotes_only_once(session_factory, channel):
    with session_factory() as session:
        c1 = Candidate(channel_id=channel, source="s", title="A", status=Status.CANDIDATE_APPROVED, raw={"rank": 5})
        c2 = Candidate(channel_id=channel, source="s", title="B", status=Status.CANDIDATE_APPROVED, raw={"rank": 1})
        session.add_all([c1, c2])
        session.commit()

        rank.run(session)
        session.commit()

        videos = session.query(Video).all()
        assert len(videos) == 1
        assert videos[0].candidate_id == c2.id  # rank 1 beats rank 5

        rank.run(session)  # run again — c2 is already promoted, so this picks up c1
        session.commit()

        videos2 = session.query(Video).all()
        assert len(videos2) == 2
        assert {v.candidate_id for v in videos2} == {c1.id, c2.id}


# --------------------------------------------------------------------------- #
# script.py — enforces the [SHORT] contract without needing a live Gemini call
# --------------------------------------------------------------------------- #
def test_script_rejects_output_with_no_short_spans(session_factory, channel, monkeypatch):
    monkeypatch.setattr(script.llm, "generate", lambda *a, **k: "a script with no shorts at all")

    with session_factory() as session:
        video = Video(
            channel_id=channel,
            status=Status.SCRIPTING,
            research={"claims": [{"text": "x", "sources": ["y"]}]},
        )
        session.add(video)
        session.commit()

        with pytest.raises(ValueError, match="no \\[SHORT\\] spans"):
            script.run(session, video.id)


def test_script_accepts_output_with_short_spans(session_factory, channel, monkeypatch):
    monkeypatch.setattr(script.llm, "generate", lambda *a, **k: FIXED_SCRIPT)

    with session_factory() as session:
        video = Video(
            channel_id=channel,
            status=Status.SCRIPTING,
            research={"claims": [{"text": "x", "sources": ["y"]}]},
        )
        session.add(video)
        session.commit()

        script.run(session, video.id)
        session.commit()

        assert video.script == FIXED_SCRIPT
        assert video.status == Status.SEGMENTING
        assert video.script_prompt_hash


# --------------------------------------------------------------------------- #
# images.py — license filtering, pure functions (no network)
# --------------------------------------------------------------------------- #
def test_commons_license_accepts_public_domain():
    page = {
        "pageid": 1,
        "title": "File:Test.jpg",
        "imageinfo": [
            {
                "url": "https://example.org/test.jpg",
                "descriptionurl": "https://commons.wikimedia.org/wiki/File:Test.jpg",
                "extmetadata": {
                    "LicenseShortName": {"value": "Public domain"},
                    "LicenseUrl": {"value": "https://example.org/pd"},
                },
            }
        ],
    }
    result = images._commons_license(page)
    assert result == ("Public domain", "https://example.org/pd")


def test_commons_license_rejects_cc_by():
    page = {
        "imageinfo": [{"extmetadata": {"LicenseShortName": {"value": "CC BY-SA 4.0"}}}],
    }
    assert images._commons_license(page) is None


def test_loc_license_requires_explicit_rights_statement():
    assert images._loc_license({"rights": "No known restrictions on publication."}) is not None
    assert images._loc_license({"rights": "Rights not evaluated."}) is None
