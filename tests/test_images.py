"""S5 images stage (README §6.1): archive search parsing, and the per-scene fallback chain
specific query -> broad query -> AI illustration -> reuse. Archives, downloads and the
image model are faked; files go to a temp dir, never the real data/cache."""

from __future__ import annotations

import types

import pytest

from app.db import Asset, Base, Scene, Video, get_engine, get_sessionmaker
from app.quota import RetryLater
from app.stages import images
from app.status import Status


@pytest.fixture
def session_factory(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'images.db'}"
    Base.metadata.create_all(get_engine(url))
    monkeypatch.setattr(images, "cache_dir", lambda: tmp_path / "cache")
    monkeypatch.setattr(images, "_download", lambda url: f"bytes of {url}".encode())
    return get_sessionmaker(url)


def _candidate(source_id: str, title: str = "", source: str = "wikimedia_commons") -> images.Candidate:
    return images.Candidate(source=source, source_id=source_id, url=f"https://img/{source_id}.jpg",
                            license="Public domain", rights_url=f"https://page/{source_id}",
                            attribution=f"{title} — Unknown author", width=1600, height=1200)


@pytest.fixture
def archive(monkeypatch):
    """Fake Commons: {query: [source_ids]}; records every search made."""
    results: dict[str, list[str]] = {}
    searches: list[str] = []

    def search(query, *, exact=False):
        searches.append(query)
        return [_candidate(i, title=query) for i in results.get(query, [])]  # titled like the query

    monkeypatch.setitem(images.SOURCES, "wikimedia_commons", search)
    monkeypatch.setitem(images.SOURCES, "smithsonian", lambda query, *, exact=False: [])
    return results, searches


@pytest.fixture
def ai(monkeypatch):
    calls: list[str] = []

    def generate_image(session, prompt):
        calls.append(prompt)
        return b"png", "image/png"

    monkeypatch.setattr(images.llm, "generate_image", generate_image)
    return calls


def _video(session, queries: list[str]) -> Video:
    video = Video(status=Status.SEGMENTED, title="Lizzie Borden",
                  research={"articles": [{"title": "Lizzie Borden"}, {"title": "Melvin O. Adams"}]})
    session.add(video)
    session.flush()
    for idx, query in enumerate(queries):
        session.add(Scene(video_id=video.id, idx=idx, text=f"[QUOTE]Scene {idx}.[/QUOTE]", image_query=query))
    session.commit()
    return video


def _sources(session, video) -> list[str]:
    session.expire_all()
    return [session.get(Asset, s.image_asset_id).source_id for s in session.get(Video, video.id).scenes]


# --------------------------------------------------------------------------- #
# archive parsing
# --------------------------------------------------------------------------- #
def _fake_http(monkeypatch, payload):
    response = types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    monkeypatch.setattr(images, "get_http_client", lambda: types.SimpleNamespace(get=lambda *a, **k: response))


def _commons_page(index, title, license, width=1600, height=900, mime="image/jpeg"):
    meta = {"License": {"value": license}, "LicenseShortName": {"value": license.upper()},
            "Artist": {"value": '<a href="/u">Mathew <b>Brady</b></a>'}, "ObjectName": {"value": title}}
    return {"index": index, "title": f"File:{title}.jpg", "imageinfo": [{
        "width": width, "height": height, "mime": mime, "url": "https://orig", "thumburl": "https://thumb",
        "descriptionurl": f"https://commons/{title}", "extmetadata": meta}]}


def test_commons_keeps_public_domain_big_non_gif_images_in_rank_order(monkeypatch):
    _fake_http(monkeypatch, {"query": {"pages": [
        _commons_page(3, "Third PD", "pd"),
        _commons_page(1, "First CC0", "cc0"),
        _commons_page(2, "Share-alike", "cc-by-sa-3.0"),
        _commons_page(4, "Tiny", "pd", width=520, height=400),
        _commons_page(5, "Animated", "pd", mime="image/gif"),
    ]}})
    found = images.search_commons("Lizzie Borden")
    assert [c.source_id for c in found] == ["File:First CC0.jpg", "File:Third PD.jpg"]
    assert found[0].url == "https://thumb"
    assert found[0].attribution == "First CC0 — Mathew Brady"


def test_smithsonian_is_skipped_without_a_key_and_keeps_cc0_high_res(monkeypatch):
    monkeypatch.setattr(images, "get_settings", lambda: types.SimpleNamespace(smithsonian_api_key=""))
    assert images.search_smithsonian("anything") == []

    monkeypatch.setattr(images, "get_settings", lambda: types.SimpleNamespace(smithsonian_api_key="k"))
    media = lambda access, width: {"type": "Images", "usage": {"access": access}, "idsId": f"id-{access}",  # noqa: E731
                                   "content": "https://ids/x", "resources": [
                                       {"label": "High-resolution JPEG", "url": "https://hi", "width": width, "height": 900}]}
    _fake_http(monkeypatch, {"response": {"rows": [
        {"title": "Restricted", "unitCode": "NPG", "content": {"descriptiveNonRepeating": {"online_media": {"media": [media("Usage conditions apply", 1600)]}}}},
        {"title": "Court House", "unitCode": "NMAH", "content": {"descriptiveNonRepeating": {"online_media": {"media": [media("CC0", 1600)]}}}},
    ]}})
    found = images.search_smithsonian("court house")
    assert [(c.source_id, c.license, c.attribution) for c in found] == [("id-CC0", "CC0", "Court House — Smithsonian NMAH")]


@pytest.mark.parametrize(
    ("title", "query", "exact", "ok"),
    [
        ("File:Andrew Borden 1890.jpg", "Andrew Borden on sofa", False, True),
        ("File:VIEW OF THEATER FROM STAGE - Academy Building.jpg", "Andrew Borden on sofa", False, False),
        ("File:Robert Borden 1917.jpg", "Lizzie Borden", True, False),  # exact: the whole phrase
        ("File:Richard Borden Mill 1968.jpg", "M. C. D. Borden", True, False),  # initials don't vanish
        ("File:Lizzie Borden trial.jpg", "Lizzie Borden", True, True),
        ("File:Manila 1899.jpg", "Manila", False, True),  # a one-word query needs its one word
    ],
)
def test_relevance(title, query, exact, ok):
    assert images.relevant(_candidate(title), query, exact=exact) is ok


def test_graphic_and_duplicate_results_are_dropped(session_factory, monkeypatch, ai):
    def search(query, *, exact=False):
        return [
            _candidate("File:Abby Borden dead body 1892.jpg", "Abby Borden"),
            _candidate("File:Abby Durfee Gray Borden slain body 1892.jpg", "Abby Borden"),
            _candidate("File:Borden Mill.tif", "Abby Borden mill"),
            _candidate("File:Borden Mill.jpg", "Abby Borden mill"),  # same scan as the .tif
        ]

    monkeypatch.setitem(images.SOURCES, "wikimedia_commons", search)
    monkeypatch.setitem(images.SOURCES, "smithsonian", lambda query, *, exact=False: [])
    with session_factory() as session:
        video = _video(session, ["Abby Borden", "Abby Borden"])
        images.run(session, video.id)
        session.commit()
        first, second = _sources(session, video)
        assert first == "File:Borden Mill.tif"
        assert second != "File:Borden Mill.jpg"  # not the same scan again (AI instead)
        assert "dead body" not in first + second


# --------------------------------------------------------------------------- #
# the fallback chain
# --------------------------------------------------------------------------- #
def test_specific_query_first_then_broad_and_never_the_same_image_twice(session_factory, archive, ai):
    results, searches = archive
    results.update({"Fall River 1892": ["house.jpg"], "Lizzie Borden": ["portrait.jpg", "house.jpg", "trial.jpg"]})
    with session_factory() as session:
        video = _video(session, ["Fall River 1892", "nothing matches", "Fall River 1892"])
        images.run(session, video.id)
        session.commit()

        assert _sources(session, video) == ["house.jpg", "portrait.jpg", "trial.jpg"]
        assert searches.count("Lizzie Borden") == 1  # cached across scenes
        assert ai == []
        assert session.get(Video, video.id).status == Status.IMAGES_READY
        commons = session.query(Asset).filter_by(source_id="house.jpg").one()
        assert (commons.license, commons.rights_url) == ("Public domain", "https://page/house.jpg")


def test_ai_illustration_when_the_archives_have_nothing(session_factory, archive, ai):
    with session_factory() as session:
        video = _video(session, ["q"])
        images.run(session, video.id)
        session.commit()

        (prompt,) = ai
        assert "Vintage historical illustration" in prompt
        assert "about: Lizzie Borden" in prompt and "[QUOTE]" not in prompt
        asset = session.get(Asset, session.get(Video, video.id).scenes[0].image_asset_id)
        assert (asset.source, asset.license) == ("gemini", "ai-generated")


def test_reuse_longest_ago_when_ai_quota_is_spent(session_factory, archive, monkeypatch):
    results, _ = archive
    results.update({"alpha": ["a.jpg"], "bravo": ["b.jpg"]})

    def spent(session, prompt):
        raise RetryLater("image quota spent")

    monkeypatch.setattr(images.llm, "generate_image", spent)
    with session_factory() as session:
        video = _video(session, ["alpha", "bravo", "zulu", "zulu"])
        monkeypatch.setattr(images.get_config().images, "sources", ["wikimedia_commons"])
        results["Lizzie Borden"] = []  # broad queries find nothing new either
        images.run(session, video.id)
        session.commit()

        # scene 2 reuses the image shown longest ago (a), scene 3 then b — never the previous one
        assert _sources(session, video) == ["a.jpg", "b.jpg", "a.jpg", "b.jpg"]


def test_ai_illustrations_are_capped_per_video(session_factory, archive, ai, monkeypatch):
    monkeypatch.setattr(images.get_config().images, "ai_max_per_video", 2)
    with session_factory() as session:
        video = _video(session, ["zulu0", "zulu1", "zulu2", "zulu3"])
        images.run(session, video.id)
        session.commit()
        assert len(ai) == 2
        ids = [s.image_asset_id for s in session.get(Video, video.id).scenes]
        assert len(set(ids)) == 2 and ids[2] == ids[0] and ids[3] == ids[1]  # then reuse


def test_max_images_caps_unique_images(session_factory, archive, ai, monkeypatch):
    results, _ = archive
    results.update({f"query{i}": [f"{i}.jpg"] for i in range(4)})
    monkeypatch.setattr(images.get_config().images, "max_images", 2)
    with session_factory() as session:
        video = _video(session, ["query0", "query1", "query2", "query3"])
        images.run(session, video.id)
        session.commit()
        assert _sources(session, video) == ["0.jpg", "1.jpg", "0.jpg", "1.jpg"]


def test_a_broken_archive_is_skipped_and_downloads_are_not_repeated(session_factory, archive, ai, monkeypatch):
    results, _ = archive
    results["query"] = ["one.jpg"]
    calls = []

    def broken(query, *, exact=False):
        calls.append(query)
        raise ConnectionError("smithsonian down")

    monkeypatch.setitem(images.SOURCES, "smithsonian", broken)
    monkeypatch.setattr(images.get_config().images, "sources", ["smithsonian", "wikimedia_commons"])
    downloads = []
    monkeypatch.setattr(images, "_download", lambda url: downloads.append(url) or b"x")
    with session_factory() as session:
        first = _video(session, ["query"])
        images.run(session, first.id)
        second = _video(session, ["query"])  # a later video wanting the same file
        images.run(session, second.id)
        session.commit()

        assert calls == ["query", "query"]  # tried once per run, then skipped for that run
        assert len(downloads) == 1  # the second video reused the cached asset
        assert _sources(session, second) == ["one.jpg"]


def test_waits_when_nothing_can_be_placed_yet(session_factory, archive, monkeypatch):
    def spent(session, prompt):
        raise RetryLater("image quota spent")

    monkeypatch.setattr(images.llm, "generate_image", spent)
    with session_factory() as session:
        video = _video(session, ["q"])
        with pytest.raises(RetryLater):
            images.run(session, video.id)


def test_fails_when_nothing_found_and_ai_is_off(session_factory, archive, monkeypatch):
    monkeypatch.setattr(images.get_config().images, "ai_fallback", False)
    with session_factory() as session:
        video = _video(session, ["q"])
        with pytest.raises(ValueError, match="AI fallback is off"):
            images.run(session, video.id)
