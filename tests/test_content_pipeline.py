from __future__ import annotations

from pathlib import Path

import pytest

from app.db import Asset, Base, Scene, Topic, Video, get_engine, get_sessionmaker
from app.stages import images, rank
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
