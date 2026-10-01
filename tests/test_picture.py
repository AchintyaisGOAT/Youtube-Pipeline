"""README §4.1 steps 3 + 5, §6.1: the image archives (app/archives.py), the survey that
counts a topic's free images, and the picture stage that builds a video's image inventory.
Archives, downloads and the LLM are faked; conftest.py blocks any real network access."""

from __future__ import annotations

import io
import types
import uuid

import pytest
from PIL import Image

from app import archives
from app.config import get_config
from app.db import Asset, Base, Topic, Video, get_engine, get_sessionmaker
from app.quota import RetryLater
from app.stages import picture, survey
from app.status import Status, TopicStatus


@pytest.fixture
def session_factory(tmp_path):
    url = f"sqlite:///{tmp_path / 'pictures.db'}"
    Base.metadata.create_all(get_engine(url))
    return get_sessionmaker(url)


def _candidate(source_id: str, title: str = "", source: str = "wikimedia_commons", description: str = ""):
    return archives.Candidate(source=source, source_id=source_id, url=f"https://img/{source_id}",
                              license="Public domain", rights_url=f"https://page/{source_id}",
                              attribution=f"{title or source_id} — Unknown author", width=1600, height=1200,
                              description=description)


def _jpeg(color, size=(800, 600), stripe=None) -> bytes:
    image = Image.new("RGB", size, color)
    if stripe:  # a different picture, not just a different colour
        for x in range(0, size[0] // 2):
            for y in range(size[1]):
                image.putpixel((x, y), stripe)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# archive parsing
# --------------------------------------------------------------------------- #
def _fake_http(monkeypatch, payload):
    response = types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)
    monkeypatch.setattr(archives, "get_http_client", lambda: types.SimpleNamespace(get=lambda *a, **k: response))


def _commons_page(index, title, license, width=1600, height=900, mime="image/jpeg"):
    meta = {"License": {"value": license}, "LicenseShortName": {"value": license.upper()},
            "Artist": {"value": '<a href="/u">Mathew <b>Brady</b></a>'}, "ObjectName": {"value": title},
            "ImageDescription": {"value": 'Portrait <i>of</i> the actor date QS:P,+1865-00-'}}
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
    found = archives.search_commons("Lizzie Borden")
    assert [c.source_id for c in found] == ["File:First CC0.jpg", "File:Third PD.jpg"]
    assert found[0].url == "https://thumb"
    assert found[0].attribution == "First CC0 — Mathew Brady"
    assert found[0].description == "Portrait of the actor"  # HTML and Wikidata codes stripped


def test_smithsonian_is_skipped_without_a_key_and_keeps_cc0_high_res(monkeypatch):
    monkeypatch.setattr(archives, "get_settings", lambda: types.SimpleNamespace(smithsonian_api_key=""))
    assert archives.search_smithsonian("anything") == []

    monkeypatch.setattr(archives, "get_settings", lambda: types.SimpleNamespace(smithsonian_api_key="k"))
    media = lambda access, width: {"type": "Images", "usage": {"access": access}, "idsId": f"id-{access}",  # noqa: E731
                                   "content": "https://ids/x", "resources": [
                                       {"label": "High-resolution JPEG", "url": "https://hi", "width": width,
                                        "height": 900}]}
    _fake_http(monkeypatch, {"response": {"rows": [
        {"title": "Restricted", "unitCode": "NPG",
         "content": {"descriptiveNonRepeating": {"online_media": {"media": [media("Usage conditions apply", 1600)]}}}},
        {"title": "Court House", "unitCode": "NMAH",
         "content": {"descriptiveNonRepeating": {"online_media": {"media": [media("CC0", 1600)]}}}},
    ]}})
    found = archives.search_smithsonian("court house")
    assert [(c.source_id, c.license, c.attribution) for c in found] == [
        ("id-CC0", "CC0", "Court House — Smithsonian NMAH")]


def test_openverse_and_europeana_keep_only_public_domain(monkeypatch):
    _fake_http(monkeypatch, {"results": [
        {"id": "a", "url": "https://x/a.jpg", "title": "Fall River mill", "creator": "MassDOT", "license": "pdm",
         "foreign_landing_url": "https://flickr/a", "width": 1600, "height": 900},
        {"id": "b", "url": "https://x/b.jpg", "title": "Tiny", "creator": "", "license": "cc0", "width": 300,
         "height": 200},
    ]})
    found = archives.search_openverse("Fall River")
    assert [(c.source_id, c.license, c.rights_url) for c in found] == [("a", "Public domain", "https://flickr/a")]

    monkeypatch.setattr(archives, "get_settings", lambda: types.SimpleNamespace(europeana_api_key="k"))
    _fake_http(monkeypatch, {"items": [
        {"id": "/1/pd", "title": ["Fall River Line"], "edmIsShownBy": ["https://e/1.jpg"], "dcCreator": ["Studio"],
         "rights": ["http://creativecommons.org/publicdomain/mark/1.0/"], "guid": "https://europeana/1?utm=x"},
        {"id": "/2/by", "title": ["Attribution only"], "edmIsShownBy": ["https://e/2.jpg"],
         "rights": ["http://creativecommons.org/licenses/by/3.0/"]},
    ]})
    found = archives.search_europeana("Fall River")
    assert [(c.source_id, c.rights_url) for c in found] == [("/1/pd", "https://europeana/1")]
    monkeypatch.setattr(archives, "get_settings", lambda: types.SimpleNamespace(europeana_api_key=""))
    assert archives.search_europeana("Fall River") == []  # no key: skipped


def test_museum_results_carry_title_date_and_artist(monkeypatch):
    _fake_http(monkeypatch, {"data": [{"id": 7, "title": "A Family Meal", "image_id": "abc", "date_display": "1890s",
                                       "artist_display": "Unknown", "thumbnail": {"width": 3000, "height": 2000}}]})
    (found,) = archives.search_artic("family meal")
    assert found.url.endswith("/abc/full/1686,/0/default.jpg") and found.title == "A Family Meal, 1890s"


# --------------------------------------------------------------------------- #
# filters
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("title", "query", "ok"),
    [
        ("Portrait of John Wilkes Booth", "John Wilkes Booth", True),
        ("Edwin Booth as Hamlet", "John Wilkes Booth", False),  # a name must appear whole
        ("VIEW OF THEATER FROM STAGE - Academy Building", "Sinking of the Titanic", False),
        ("Titanic sinking, engraving by Willy Stöwer", "Sinking of the Titanic", True),
        ("Crazy Quilt Parlor Throw, 1887", "Fall River", False),  # a museum's loose match
        ("Downtown Fall River 1890s", "Fall River", True),
    ],
)
def test_relevance(title, query, ok):
    assert picture.relevant(_candidate(title, title), query) is ok


def test_site_furniture_and_the_dead_are_unusable():
    assert not picture.usable(_candidate("File:Open Access logo.png", "Open Access logo"))
    assert not picture.usable(_candidate("File:Flag of Massachusetts.svg", "Flag of Massachusetts"))
    assert not picture.usable(_candidate("File:Andrew Borden slain body 1892.jpg", "Andrew Borden slain body"))
    assert picture.usable(_candidate("File:Lizzie Borden 1890.jpg", "Lizzie Borden 1890"))


# --------------------------------------------------------------------------- #
# gather + check
# --------------------------------------------------------------------------- #
@pytest.fixture
def archive(monkeypatch):
    """Fake archives: {title: [candidates]} for article images and for searches."""
    article, search, calls = {}, {}, []

    def fake_search(query, *, exact=False):
        calls.append(query)
        return search.get(query, [])

    monkeypatch.setattr(archives, "article_images", lambda title: article.get(title, []))
    monkeypatch.setitem(archives.SOURCES, "wikimedia_commons", fake_search)
    monkeypatch.setattr(get_config().images, "sources", ["wikimedia_commons"])
    return article, search, calls


def test_gather_takes_article_images_then_relevant_search_results(archive):
    article, search, _ = archive
    article["Assassination of Abraham Lincoln"] = [
        _candidate("File:Ford's Theatre 1865.jpg", "Ford's Theatre 1865"),
        _candidate("File:Seal of the President.svg", "Seal of the President"),
    ]
    search["Assassination of Abraham Lincoln"] = [
        _candidate("File:Lincoln assassination lithograph.jpg", "The assassination of Abraham Lincoln"),
        _candidate("File:Ford's Theatre 1865.jpg", "Ford's Theatre 1865"),  # already found
        _candidate("File:Garden party.jpg", "Garden party"),  # not about the query
    ]
    found = picture.gather(["Assassination of Abraham Lincoln"], ["wikimedia_commons"])
    assert [c.source_id for c in found] == ["File:Ford's Theatre 1865.jpg", "File:Lincoln assassination lithograph.jpg"]


def test_vet_keeps_only_what_the_check_keeps_with_what_it_shows(session_factory, monkeypatch):
    pool = [_candidate(f"f{n}", f"Image {n}") for n in range(3)]
    seen = {}

    def fake_generate(session, prompt, **kwargs):
        seen["prompt"] = prompt
        return {"images": [{"id": "i2", "keep": True, "shows": "Ford's Theatre, 1865"}, {"id": "i1", "keep": False},
                           {"id": "i0", "keep": True}, {"id": "i2", "keep": True}, {"id": "i9", "keep": True}]}

    monkeypatch.setattr(picture.llm, "generate", fake_generate)
    with session_factory() as session:
        kept = picture.vet(session, "Lincoln", pool, key="t")
    assert [(c.source_id, shows) for c, shows in kept] == [("f2", "Ford's Theatre, 1865"), ("f0", "Image 0")]
    assert "i1: Image 1" in seen["prompt"]


def test_vet_drops_a_batch_whose_check_failed_but_waits_on_a_limit(session_factory, monkeypatch):
    monkeypatch.setattr(picture.llm, "generate", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad json")))
    with session_factory() as session:
        assert picture.vet(session, "x", [_candidate("a")], key="t") == []
        monkeypatch.setattr(picture.llm, "generate", lambda *a, **k: (_ for _ in ()).throw(RetryLater("429")))
        with pytest.raises(RetryLater):
            picture.vet(session, "x", [_candidate("a")], key="t")


# --------------------------------------------------------------------------- #
# the picture stage
# --------------------------------------------------------------------------- #
@pytest.fixture
def downloads(monkeypatch):
    files: dict[str, bytes] = {}
    monkeypatch.setattr(picture, "_download", lambda url: files[url.removeprefix("https://img/")])
    return files


def _pictured_video(session, inventory=None) -> Video:
    topic = Topic(source="catalog", title="Assassination of Abraham Lincoln", status=TopicStatus.USED,
                  raw={"inventory": inventory or []})
    session.add(topic)
    session.flush()
    articles = [{"title": "Assassination of Abraham Lincoln"}, {"title": "John Wilkes Booth"}]
    video = Video(topic_id=topic.id, title=topic.title, status=Status.RESEARCHED, research={"articles": articles})
    session.add(video)
    session.commit()
    return video


def test_picture_builds_a_numbered_inventory_without_duplicates(session_factory, archive, downloads, monkeypatch):
    article, _, _ = archive
    monkeypatch.setattr(get_config().images, "min_per_video", 2)
    surveyed = [picture.to_dict(_candidate("theatre"), "Ford's Theatre, 1865"),
                picture.to_dict(_candidate("theatre-scan2"), "Ford's Theatre again")]  # same picture, 2nd scan
    article["John Wilkes Booth"] = [_candidate("booth", "John Wilkes Booth portrait"),
                                    _candidate("tiny", "John Wilkes Booth thumbnail")]
    downloads.update({"theatre": _jpeg("white", stripe=(0, 0, 0)), "theatre-scan2": _jpeg("white", stripe=(5, 5, 5)),
                      "booth": _jpeg("black"), "tiny": _jpeg("gray", size=(300, 200))})
    monkeypatch.setattr(picture.llm, "generate", lambda session, prompt, **kw: {
        "images": [{"id": "i0", "keep": True, "shows": "Portrait of John Wilkes Booth"},
                   {"id": "i1", "keep": True, "shows": "Booth, small"}]})
    with session_factory() as session:
        video = _pictured_video(session, surveyed)
        picture.run(session, video.id)
        session.commit()
        items = video.research["images"]
        assert [(i["n"], i["shows"]) for i in items] == [(1, "Ford's Theatre, 1865"),
                                                          (2, "Portrait of John Wilkes Booth")]
        assert video.status == Status.PICTURED
        assert session.get(Asset, uuid.UUID(items[1]["asset_id"])).meta == {"width": 800, "height": 600}


def test_picture_fails_when_too_few_images_survive(session_factory, archive, downloads, monkeypatch):
    monkeypatch.setattr(picture.llm, "generate", lambda *a, **k: {"images": []})
    with session_factory() as session:
        video = _pictured_video(session)
        with pytest.raises(ValueError, match="only 0 usable images"):
            picture.run(session, video.id)


def test_similar_pictures_hash_close_and_different_ones_far():
    same = [picture.average_hash(_jpeg("white", stripe=c)) for c in ((0, 0, 0), (8, 8, 8))]
    other = picture.average_hash(_jpeg("black", stripe=(255, 255, 255), size=(600, 800)))
    assert bin(same[0] ^ same[1]).count("1") <= 6
    assert bin(same[0] ^ other).count("1") > 6


# --------------------------------------------------------------------------- #
# survey
# --------------------------------------------------------------------------- #
def test_survey_counts_quickly_then_fully_and_vetoes_the_thin(session_factory, monkeypatch):
    cfg = get_config().discovery
    monkeypatch.setattr(cfg, "min_images", 3)
    monkeypatch.setattr(cfg, "quick_min_images", 3)
    monkeypatch.setattr(survey, "linked_titles", lambda title, n: [])
    monkeypatch.setattr(survey, "quick_count", lambda title: {"Rich": 40, "Mid": 12, "Thin": 1}[title])
    monkeypatch.setattr(survey, "_monthly_views", lambda title: 5000)
    gathered = []
    monkeypatch.setattr(survey, "linked_titles", lambda title, n: [f"{title} person"])
    monkeypatch.setattr(picture, "gather", lambda titles, sources: gathered.append(titles) or [
        _candidate(f"{titles[0]}{n}") for n in range(5)])
    kept_counts = {"Rich": 5, "Mid": 2}
    monkeypatch.setattr(picture, "vet", lambda session, subject, found, key: [
        (c, f"shows {c.source_id}") for c in found[: kept_counts[subject]]])
    with session_factory() as session:
        topics = {t: Topic(source="catalog", title=t, status=TopicStatus.PASSED) for t in ("Rich", "Mid", "Thin")}
        session.add_all(topics.values())
        session.commit()
        survey.run(session)
        session.commit()

    assert topics["Thin"].status == TopicStatus.VETOED and "quick count" in topics["Thin"].rationale
    assert topics["Rich"].status == TopicStatus.PASSED
    assert topics["Rich"].raw["images"] == 5 and topics["Rich"].raw["views"] == 5000
    assert len(topics["Rich"].raw["inventory"]) == 5
    assert topics["Mid"].status == TopicStatus.VETOED and "after checking" in topics["Mid"].rationale
    assert ["Rich", "Rich person"] in gathered  # the topic's people and places are searched too


def test_survey_skips_the_full_pass_while_a_batch_is_ready(session_factory, monkeypatch):
    cfg = get_config().discovery
    monkeypatch.setattr(cfg, "batch_size", 1)
    monkeypatch.setattr(survey, "quick_count", lambda title: 50)
    monkeypatch.setattr(picture, "gather", lambda *a: pytest.fail("the full survey should wait"))
    with session_factory() as session:
        session.add_all([Topic(source="s", title="Ready", status=TopicStatus.PASSED,
                               raw={"quick_images": 50, "images": 40}),
                         Topic(source="s", title="Unsurveyed", status=TopicStatus.PASSED)])
        session.commit()
        survey.run(session)


def test_interest_is_log_scaled():
    assert survey.interest(0) == 0.0
    assert survey.interest(999) == pytest.approx(0.5, abs=0.01)
    assert survey.interest(10**7) == 1.0
