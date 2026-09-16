from __future__ import annotations

from app.config import ChannelConfig, get_channel_config
from app.db import Base, Candidate, Channel, get_engine, get_sessionmaker
from app.status import Status


def test_create_all_makes_every_table(tmp_path):
    engine = get_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    Base.metadata.create_all(engine)
    assert len(Base.metadata.tables) == 13


def test_candidate_roundtrip(tmp_path):
    url = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    Base.metadata.create_all(get_engine(url))

    with get_sessionmaker(url)() as session:
        channel = Channel(handle="test", config={"config_version": 1})
        session.add(channel)
        session.commit()

        candidate = Candidate(
            channel_id=channel.id,
            source="wikimedia",
            title="Test Topic",
            status=Status.CANDIDATE_NEW,
        )
        session.add(candidate)
        session.commit()

        fetched = session.get(Candidate, candidate.id)
        assert fetched.status == Status.CANDIDATE_NEW
        assert fetched.channel_id == channel.id


def test_foreign_key_cascade_deletes_candidate(tmp_path):
    url = f"sqlite:///{tmp_path / 'cascade.db'}"
    Base.metadata.create_all(get_engine(url))

    with get_sessionmaker(url)() as session:
        channel = Channel(handle="test", config={"config_version": 1})
        session.add(channel)
        session.commit()
        session.add(Candidate(channel_id=channel.id, source="s", title="t"))
        session.commit()

        session.delete(channel)
        session.commit()

        assert session.query(Candidate).count() == 0


def test_get_channel_config_reads_active_channel(tmp_path):
    url = f"sqlite:///{tmp_path / 'config.db'}"
    Base.metadata.create_all(get_engine(url))

    cfg = ChannelConfig()
    with get_sessionmaker(url)() as session:
        session.add(Channel(handle="main", config=cfg.model_dump(mode="json")))
        session.commit()

        loaded = get_channel_config(session)
        assert loaded.timezone == cfg.timezone
