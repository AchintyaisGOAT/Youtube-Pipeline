from __future__ import annotations

from pathlib import Path

import pytest

from app.db import Asset, Base, Scene, Topic, Video, get_engine, get_sessionmaker
from app.stages import images, rank, script, segment
from app.status import Status, TopicStatus

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




# --------------------------------------------------------------------------- #
# segment.py — golden-file: [SHORT] spans -> in_short_span on the right segments
# --------------------------------------------------------------------------- #
def test_segment_marks_short_spans(session_factory):
    with session_factory() as session:
        video = Video(status=Status.CHECKED, script=FIXED_SCRIPT)
        session.add(video)
        session.commit()

        segment.run(session, video.id)
        session.commit()

        rows = video.scenes
        assert len(rows) >= 5
        assert video.status == Status.SEGMENTED

        short_texts = {s.text for s in rows if s.in_short_span}
        assert any("Ides of March" in t for t in short_texts)
        assert any("civil war" in t for t in short_texts)
        assert not any("Rome was not built" in t for t in short_texts)
        assert not any("Octavian" in t for t in short_texts)
        # every segment got some image query
        assert all(s.image_query for s in rows)


def test_segment_is_idempotent_when_not_in_expected_status(session_factory):
    with session_factory() as session:
        video = Video(status=Status.SELECTED, script=FIXED_SCRIPT)
        session.add(video)
        session.commit()

        segment.run(session, video.id)  # wrong status -> no-op
        session.commit()

        assert video.scenes == []
        assert video.status == Status.SELECTED


# --------------------------------------------------------------------------- #
# rank.py — best passed topic, one video in progress at a time
# --------------------------------------------------------------------------- #
def test_rank_picks_best_topic_and_waits_while_a_video_is_in_progress(session_factory):
    with session_factory() as session:
        weak = Topic(source="s", title="A", status=TopicStatus.PASSED, raw={"rank": 5, "relevance": 5})
        strong = Topic(source="s", title="B", status=TopicStatus.PASSED, raw={"rank": 1, "relevance": 9})
        session.add_all([weak, strong])
        session.commit()

        rank.run(session)
        session.commit()
        videos = session.query(Video).all()
        assert [v.topic_id for v in videos] == [strong.id]
        assert strong.status == TopicStatus.USED

        rank.run(session)  # the first video is still in progress -> nothing new starts
        session.commit()
        assert session.query(Video).count() == 1

        videos[0].status = Status.APPROVED  # handed over to you -> next one may start
        session.commit()
        rank.run(session)
        session.commit()
        assert {v.topic_id for v in session.query(Video)} == {strong.id, weak.id}


# --------------------------------------------------------------------------- #
# script.py — enforces the [SHORT] contract without needing a live Gemini call
# --------------------------------------------------------------------------- #
def test_script_rejects_output_with_no_short_spans(session_factory, monkeypatch):
    monkeypatch.setattr(script.llm, "generate", lambda *a, **k: "a script with no shorts at all")

    with session_factory() as session:
        video = Video(
            status=Status.RESEARCHED,
            research={"claims": [{"text": "x", "sources": ["y"]}]},
        )
        session.add(video)
        session.commit()

        with pytest.raises(ValueError, match="no \\[SHORT\\] spans"):
            script.run(session, video.id)


def test_script_accepts_output_with_short_spans(session_factory, monkeypatch):
    monkeypatch.setattr(script.llm, "generate", lambda *a, **k: FIXED_SCRIPT)

    with session_factory() as session:
        video = Video(
            status=Status.RESEARCHED,
            research={"claims": [{"text": "x", "sources": ["y"]}]},
        )
        session.add(video)
        session.commit()

        script.run(session, video.id)
        session.commit()

        assert video.script == FIXED_SCRIPT
        assert video.status == Status.SCRIPTED
        assert video.script_prompt_hash


# --------------------------------------------------------------------------- #
# images.py — AI-generated illustrations, no live Gemini call needed
# --------------------------------------------------------------------------- #
def test_prompt_hash_is_deterministic_and_sensitive_to_content():
    a = images._prompt_hash("draw a cat")
    b = images._prompt_hash("draw a cat")
    c = images._prompt_hash("draw a dog")
    assert a == b
    assert a != c


def test_images_run_generates_and_dedups_by_prompt(session_factory, monkeypatch):
    calls = []

    def fake_generate_image(session, prompt):
        calls.append(prompt)
        return b"fake-jpeg-bytes", "image/jpeg"

    monkeypatch.setattr(images.llm, "generate_image", fake_generate_image)

    with session_factory() as session:
        video = Video(status=Status.SEGMENTED)
        session.add(video)
        session.commit()

        # two segments with IDENTICAL text -> identical prompt -> only one generation call
        session.add_all(
            [
                Scene(video_id=video.id, idx=0, text="Rome burns.", in_short_span=False),
                Scene(video_id=video.id, idx=1, text="Rome burns.", in_short_span=False),
            ]
        )
        session.commit()

        images.run(session, video.id)
        session.commit()

        assert len(calls) == 1  # deduped by prompt hash, not re-generated per segment
        assert video.status == Status.IMAGES_READY

        segs = session.query(Scene).order_by(Scene.idx).all()
        assert segs[0].image_asset_id == segs[1].image_asset_id  # same asset, reused

        asset = session.get(Asset, segs[0].image_asset_id)
        assert asset.source == "gemini"
        assert asset.license == "ai-generated"
        assert Path(asset.uri).exists()
