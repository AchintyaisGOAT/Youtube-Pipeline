from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.db import Base, Scene, Topic, Video, get_engine, get_sessionmaker
from app.status import Status, TopicStatus


@pytest.fixture
def session_factory(tmp_path):
    url = f"sqlite:///{tmp_path / 'db.db'}"
    Base.metadata.create_all(get_engine(url))
    return get_sessionmaker(url)


def test_create_all_makes_the_readme_tables(session_factory):
    assert set(Base.metadata.tables) >= {"topic", "video", "scene", "short", "asset", "llm_cache"}


def test_topic_roundtrip_defaults_to_candidate(session_factory):
    with session_factory() as session:
        topic = Topic(source="trending", title="Test Topic")
        session.add(topic)
        session.commit()

        assert session.get(Topic, topic.id).status == TopicStatus.CANDIDATE


def test_topic_titles_are_unique_across_sources(session_factory):
    """README §4.1: a topic is never done twice, whichever source found it."""
    with session_factory() as session:
        session.add_all([Topic(source="trending", title="Rome"), Topic(source="on_this_day", title="Rome")])
        with pytest.raises(IntegrityError):
            session.commit()


def test_deleting_a_video_cascades_to_its_scenes(session_factory):
    with session_factory() as session:
        video = Video(status=Status.SEGMENTED)
        session.add(video)
        session.flush()
        session.add(Scene(video_id=video.id, idx=0, text="Rome burns."))
        session.commit()

        session.delete(video)
        session.commit()

        assert session.query(Scene).count() == 0
