"""S3 topic flow (README §4.1 steps 1–3): discover's two sources + free pre-filter, the
batched gate, and how the orchestrator handles gate failures. All offline: the Wikipedia
calls and the LLM are faked."""

from __future__ import annotations

import sys
import types
from datetime import UTC, date, datetime, timedelta

import pytest

from app import orchestrator
from app.db import Base, Topic, get_engine, get_sessionmaker
from app.quota import RetryLater
from app.stages import discover, gate, rank
from app.status import TopicStatus


@pytest.fixture
def session_factory(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'topics.db'}"
    Base.metadata.create_all(get_engine(url))  # also the orchestrator's default engine
    monkeypatch.setattr(orchestrator.notify, "alert", lambda message: None)
    return get_sessionmaker(url)


# --------------------------------------------------------------------------- #
# pre-filter + scoring
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("title", "description", "reason"),
    [
        ("Lizzie Borden", "American woman acquitted of murder (1860–1927)", None),
        ("Fall of Constantinople", "1453 capture of the Byzantine capital by the Ottomans", None),
        ("Printing press", "Device for applying pressure to an inked surface", None),
        ("Justin Verlander", "American baseball player (born 1983)", "living person"),
        ("Ted Kaczynski", "American domestic terrorist (1942–2023)", "dated 2000 or later"),
        ("The Paradise (2026 Indian film)", "2026 film by Jeethu Joseph", "dated 2000 or later"),
        ("Roblox", "Multiplayer game creation platform", "entertainment/sport"),
        ("Main Page", "Main page of the English Wikipedia", "meta page"),
        ("List of Roman emperors", "", "meta page"),
        ("Mercury", "Topics referred to by the same term (disambiguation)", "meta page"),
    ],
)
def test_prefilter(title, description, reason):
    assert discover.prefilter_reason(title, description) == reason


def test_anniversary_score_prefers_round_numbers():
    assert discover.anniversary_score(500) == 1.0
    assert discover.anniversary_score(150) == 0.7
    assert discover.anniversary_score(75) == 0.5
    assert discover.anniversary_score(130) == 0.3
    assert discover.anniversary_score(123) == 0.1


def test_trend_score_is_log_scaled():
    assert discover.trend_score(1) == 1.0
    assert discover.trend_score(10) == pytest.approx(2 / 3)
    assert discover.trend_score(1000) == 0.0
    assert discover.trend_score(0) == 0.0


def test_rank_score_adds_trend_and_relevance():
    assert rank.score(Topic(raw={"trend": 0.5, "relevance": 8})) == pytest.approx(1.3)
    assert rank.score(Topic(raw=None)) == 0.0


# --------------------------------------------------------------------------- #
# discover
# --------------------------------------------------------------------------- #
@pytest.fixture
def fake_wikipedia(monkeypatch):
    calls = {"pageviews": 0, "on_this_day": 0}

    def top_articles(day):
        calls["pageviews"] += 1
        return [
            {"article": "Main_Page", "rank": 1},
            {"article": "Lizzie_Borden", "rank": 4, "views": 900_000},
            {"article": "Roblox", "rank": 6},
            {"article": "Constantinople_1453", "rank": 9},  # a redirect
        ]

    def describe(titles):
        pages = {
            "Lizzie Borden": ("Lizzie Borden", "American woman acquitted of murder (1860–1927)"),
            "Roblox": ("Roblox", "Multiplayer game creation platform"),
            "Constantinople 1453": ("Fall of Constantinople", "1453 capture of the Byzantine capital"),
        }
        return {t: {"title": pages[t][0], "description": pages[t][1], "extract": "…"} for t in titles if t in pages}

    def on_this_day(day):
        calls["on_this_day"] += 1
        if day != date(2026, 9, 27):
            return []
        page = lambda title, desc: {"titles": {"normalized": title}, "description": desc, "extract": "…"}  # noqa: E731
        return [
            {"year": 2024, "text": "A hurricane.", "pages": [page("Hurricane Helene", "2024 hurricane")]},
            {"year": 1540, "text": "Jesuits approved.", "pages": [page("Society of Jesus", "Catholic religious order")]},
            {"year": 1453, "text": "Siege.", "pages": [page("Fall of Constantinople", "1453 capture")]},
        ]

    monkeypatch.setattr(discover, "_fetch_top_articles", top_articles)
    monkeypatch.setattr(discover.wikipedia, "describe_pages", describe)
    monkeypatch.setattr(discover, "_fetch_on_this_day", on_this_day)
    monkeypatch.setattr(discover, "_channel_today", lambda timezone: date(2026, 9, 27))
    return calls


def test_discover_merges_sources_prefilters_and_dedupes(session_factory, fake_wikipedia):
    with session_factory() as session:
        discover.run(session)
        session.commit()
        topics = {t.title: t for t in session.query(Topic)}

    # Roblox (pre-filter), Main Page (meta) and the 2024 hurricane (post-2000) are gone;
    # "Fall of Constantinople" is found by both sources but stored once, via the redirect.
    assert set(topics) == {"Society of Jesus", "Fall of Constantinople", "Lizzie Borden"}
    assert all(t.status == TopicStatus.CANDIDATE for t in topics.values())
    assert topics["Society of Jesus"].raw["years_ago"] == 486
    assert topics["Lizzie Borden"].raw["trend"] == pytest.approx(discover.trend_score(4))


def test_discover_waits_while_topics_are_waiting_unless_forced(session_factory, fake_wikipedia):
    with session_factory() as session:
        discover.run(session)
        session.commit()
        discover.run(session)  # candidates are waiting -> no fetch
        session.commit()
        assert fake_wikipedia["pageviews"] == 1

        discover.run(session, force=True)
        session.commit()
        assert fake_wikipedia["pageviews"] == 2
        assert session.query(Topic).count() == 3  # nothing new: every title already known


def test_stale_passed_topics_expire_and_let_discovery_run(session_factory, fake_wikipedia):
    with session_factory() as session:
        old = Topic(source="trending", title="Old news", status=TopicStatus.PASSED,
                    created_at=datetime.now(UTC) - timedelta(days=8))
        fresh = Topic(source="trending", title="Fresh", status=TopicStatus.PASSED)
        session.add_all([old, fresh])
        session.commit()

        discover.run(session)  # "Fresh" is still waiting -> no fetch, but "Old news" expires
        session.commit()
        assert fake_wikipedia["pageviews"] == 0
        assert old.status == TopicStatus.VETOED
        assert "expired" in old.rationale
        assert fresh.status == TopicStatus.PASSED


# --------------------------------------------------------------------------- #
# gate
# --------------------------------------------------------------------------- #
def _candidates(session, *titles) -> list[Topic]:
    topics = [Topic(source="trending", title=t, status=TopicStatus.CANDIDATE) for t in titles]
    session.add_all(topics)
    session.commit()
    return topics


def test_gate_judges_all_candidates_in_one_call(session_factory, monkeypatch):
    prompts = []

    def fake_generate(session, prompt, **kwargs):
        prompts.append(prompt)
        return {"results": [
            {"id": 1, "verdict": "pass", "relevance": 9, "reason": "great story"},
            {"id": "2", "verdict": "veto", "relevance": 0, "reason": "a film"},  # string id tolerated
            # no verdict for 3
            {"id": 4, "verdict": "pass", "relevance": 5, "reason": "weak fit"},
        ]}

    monkeypatch.setattr(gate.llm, "generate", fake_generate)
    with session_factory() as session:
        aqueducts, film, skipped, weak = _candidates(
            session, "Roman aqueducts", "Some film", "Skipped topic", "Limonene"
        )
        gate.run(session)
        session.commit()

        assert len(prompts) == 1
        assert (aqueducts.status, aqueducts.raw["relevance"]) == (TopicStatus.PASSED, 9)
        assert film.status == TopicStatus.VETOED
        assert skipped.status == TopicStatus.VETOED
        assert "no verdict" in skipped.rationale
        assert weak.status == TopicStatus.VETOED  # below discovery.min_relevance (6)
        assert "below 6" in weak.rationale


def test_gate_vetoes_recent_events_without_an_llm_call(session_factory, monkeypatch):
    def _fail(*a, **k):
        raise AssertionError("recency veto should short-circuit before any LLM call")

    monkeypatch.setattr(gate.llm, "generate", _fail)
    with session_factory() as session:
        (recent,) = _candidates(session, "2024 Something That Just Happened")
        gate.run(session)
        session.commit()

        assert recent.status == TopicStatus.VETOED
        assert "10 years" in recent.rationale


def test_gate_batches_large_pools(session_factory, monkeypatch):
    sizes = []

    def fake_generate(session, prompt, **kwargs):
        size = len(kwargs["inputs"]["topics"])
        sizes.append(size)
        return {"results": [{"id": i, "verdict": "pass", "relevance": 5} for i in range(1, size + 1)]}

    monkeypatch.setattr(gate.llm, "generate", fake_generate)
    with session_factory() as session:
        _candidates(session, *[f"Topic {i}" for i in range(gate.BATCH_SIZE + 5)])
        gate.run(session)
        session.commit()

    assert sizes == [gate.BATCH_SIZE, 5]


# --------------------------------------------------------------------------- #
# orchestrator + gate outcomes
# --------------------------------------------------------------------------- #
def _fake_gate(monkeypatch, fn):
    module = types.ModuleType("fake_gate")
    module.run = fn
    monkeypatch.setitem(sys.modules, "fake_gate", module)
    monkeypatch.setattr(orchestrator, "GATE_STAGE", "fake_gate")


def test_rate_limited_gate_leaves_candidates_for_next_run(session_factory, monkeypatch):
    def run(session):
        raise RetryLater("groq rate-limited")

    _fake_gate(monkeypatch, run)
    with session_factory() as session:
        _candidates(session, "A", "B")

    orchestrator._gate_topics()

    with session_factory() as session:
        assert {t.status for t in session.query(Topic)} == {TopicStatus.CANDIDATE}


def test_broken_gate_vetoes_candidates_so_discovery_is_not_blocked(session_factory, monkeypatch):
    def run(session):
        raise ValueError("every model returned garbage")

    _fake_gate(monkeypatch, run)
    with session_factory() as session:
        _candidates(session, "A", "B")

    orchestrator._gate_topics()

    with session_factory() as session:
        topics = session.query(Topic).all()
        assert {t.status for t in topics} == {TopicStatus.VETOED}
        assert all("garbage" in t.rationale for t in topics)
