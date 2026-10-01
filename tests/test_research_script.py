"""README §4.1 steps 5–8: research -> script (written around the image inventory) ->
check -> scene plan. Wikipedia and the LLMs are faked; conftest.py blocks any real network
access. The picture stage has its own tests (test_picture.py)."""

from __future__ import annotations

import pytest

from app.config import get_config
from app.db import Asset, Base, Scene, Topic, Video, get_engine, get_sessionmaker
from app.quota import RetryLater
from app.stages import check, research, script, segment
from app.status import Status, TopicStatus

SCRIPT = (
    "Rome was not built in a day. "
    "[SHORT]In 44 BC, Julius Caesar was assassinated on the Ides of March by a group "
    "of senators who feared his growing power. [QUOTE]Et tu, Brute?[/QUOTE][/SHORT] "
    "The senate had hoped his death would restore the republic. "
    "[SHORT]Instead, it triggered over a decade of civil war that ended the republic "
    "for good.[/SHORT] "
    "Octavian would go on to become Rome's first emperor."
)
ARTICLES = {"articles": [{"title": "Assassination of Julius Caesar", "url": "u", "text": "Caesar died in 44 BC."}]}
#: The same story, written around three images (as the script stage now asks).
MARKED = (
    "[IMG 1] Rome was not built in a day. "
    "[SHORT][IMG 2] In 44 BC, Julius Caesar was assassinated on the Ides of March by a group "
    "of senators who feared his growing power. [QUOTE]Et tu, Brute?[/QUOTE][/SHORT] "
    "[IMG 3] The senate had hoped his death would restore the republic. "
    "[SHORT][IMG 1] Instead, it triggered over a decade of civil war that ended the republic "
    "for good.[/SHORT] "
    "[IMG 2] Octavian would go on to become Rome's first emperor."
)


def _inventory(session, count: int) -> list[dict]:
    """`count` stored images, as the picture stage leaves them in video.research["images"]."""
    items = []
    for n in range(1, count + 1):
        asset = Asset(kind="image", source="t", source_id=f"img{n}", uri=f"img{n}.jpg")
        session.add(asset)
        session.flush()
        items.append({"n": n, "asset_id": str(asset.id), "shows": f"Picture {n}", "title": f"T{n}", "source": "t"})
    return items


@pytest.fixture
def session_factory(tmp_path):
    url = f"sqlite:///{tmp_path / 's4.db'}"
    Base.metadata.create_all(get_engine(url))
    return get_sessionmaker(url)


def _video(session, status, **fields) -> Video:
    video = Video(status=status, title="Assassination of Julius Caesar", **fields)
    session.add(video)
    session.commit()
    return video


# --------------------------------------------------------------------------- #
# research
# --------------------------------------------------------------------------- #
def test_trim_cuts_the_reference_apparatus():
    text = "Intro.\n\n== Life ==\nStory.\n\n== See also ==\nOther pages.\n\n== References ==\n1."
    assert research._trim(text, 1000) == "Intro.\n\n== Life ==\nStory."


def test_rank_links_counts_surnames_and_skips_the_topics_own_title():
    text = "Aguinaldo fled. Aguinaldo returned. Emilio Aguinaldo surrendered. Manila fell. Lizzie"
    links = ["Emilio Aguinaldo", "Manila", "Lizzie (2018 film)", "Philippine–American War (film)"]
    assert research.rank_links("Philippine–American War", text, links) == ["Emilio Aguinaldo"]


def test_shortlist_drops_generic_and_prefiltered_pages():
    descriptions = {
        "Emilio Aguinaldo": {"description": "Filipino revolutionary leader (1869–1964)"},
        "Manila": {"description": "Capital city of the Philippines"},
        "Cello": {"description": "Bowed string instrument"},
        "Some 2019 film": {"description": "2019 film"},
    }
    ranked = ["Emilio Aguinaldo", "Manila", "Cello", "Some 2019 film"]
    # "Cello" isn't caught by the rules — that's what the LLM pick is for
    assert research.shortlist(ranked, descriptions) == ["Emilio Aguinaldo", "Cello"]


def test_research_stores_main_and_llm_picked_linked_articles(session_factory, monkeypatch):
    pages = {
        "Philippine–American War": {"title": "Philippine–American War", "url": "w/PAW",
                                    "text": "Aguinaldo " * 5 + "Cello " * 3 + "\n== References ==\nx"},
        "Emilio Aguinaldo": {"title": "Emilio Aguinaldo", "url": "w/EA", "text": "Leader."},
    }
    monkeypatch.setattr(research.wikipedia, "article", lambda title: dict(pages[title]))
    monkeypatch.setattr(research.wikipedia, "links_and_sources",
                        lambda title: (["Emilio Aguinaldo", "Cello"], ["https://a", "https://a", "https://b"]))
    monkeypatch.setattr(research.wikipedia, "describe_pages", lambda titles: {
        "Emilio Aguinaldo": {"description": "Filipino revolutionary leader"},
        "Cello": {"description": "Bowed string instrument"},
    })
    monkeypatch.setattr(research.llm, "generate", lambda *a, **k: {"picks": [1]})  # skips Cello

    with session_factory() as session:
        topic = Topic(source="s", title="Philippine–American War", status=TopicStatus.USED)
        session.add(topic)
        session.flush()
        video = _video(session, Status.SELECTED, topic_id=topic.id)
        research.run(session, video.id)
        session.commit()

        assert video.status == Status.RESEARCHED
        assert [a["title"] for a in video.research["articles"]] == ["Philippine–American War", "Emilio Aguinaldo"]
        assert "References" not in video.research["articles"][0]["text"]
        assert video.research["sources"] == ["https://a", "https://b"]


def test_pick_linked_falls_back_to_most_mentioned_when_the_llm_errors(session_factory, monkeypatch):
    def broken(*a, **k):
        raise ValueError("bad json")

    monkeypatch.setattr(research.llm, "generate", broken)
    with session_factory() as session:
        main = {"title": "T", "text": "x"}
        assert research._pick_linked(session, None, main, ["A", "B", "C"], {}, 2) == ["A", "B"]


def test_pick_linked_waits_when_the_llm_is_only_rate_limited(session_factory, monkeypatch):
    def limited(*a, **k):
        raise RetryLater("429")

    monkeypatch.setattr(research.llm, "generate", limited)
    with session_factory() as session, pytest.raises(RetryLater):
        research._pick_linked(session, None, {"title": "T", "text": "x"}, ["A"], {}, 2)


# --------------------------------------------------------------------------- #
# script
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("no markup at all", "no \\[SHORT\\] spans"),
        ("[SHORT]open but never closed", "unclosed \\[SHORT\\]"),
        ("[SHORT]a [SHORT]nested[/SHORT][/SHORT]", "nested \\[SHORT\\]"),
        ("[SHORT]ok[/SHORT] [QUOTE]unclosed", "unclosed \\[QUOTE\\]"),
    ],
)
def test_validate_markup_rejects_broken_scripts(text, error):
    with pytest.raises(ValueError, match=error):
        script.validate_markup(text)


def test_script_is_written_around_the_images(session_factory, monkeypatch):
    seen = {}
    monkeypatch.setattr(script, "problems", lambda *a, **k: [])  # the guard has its own tests

    def fake_generate(session, prompt, **kwargs):
        seen.update(kwargs, prompt=prompt)
        return f"  {MARKED}\n"

    monkeypatch.setattr(script.llm, "generate", fake_generate)
    with session_factory() as session:
        video = _video(session, Status.PICTURED, research={**ARTICLES, "images": _inventory(session, 20)})
        script.run(session, video.id)
        session.commit()

        assert video.script == MARKED
        assert video.status == Status.SCRIPTED
        assert (seen["role"], seen["json_mode"]) == ("writer", False)
        assert "=== ARTICLE: Assassination of Julius Caesar ===" in seen["prompt"]
        assert "[IMG 20] Picture 20" in seen["prompt"]
        expected = script.plan(20, get_config())
        assert f"{expected['words_min']}–{expected['words_max']} words" in seen["prompt"]
        assert video.research["plan"] == expected


def test_length_and_shorts_follow_the_images():
    cfg = get_config()
    few, many, lots = script.plan(12, cfg), script.plan(30, cfg), script.plan(200, cfg)
    assert few["seconds"] == 180 and few["shorts"] == 3  # never under 3 minutes
    assert few["seconds"] < many["seconds"] < lots["seconds"] == 480  # grows with images, capped at 8
    assert lots["shorts"] == 7
    assert many["words_min"] < many["words_max"]


def test_script_without_images_to_write_around_fails(session_factory):
    with session_factory() as session:
        video = _video(session, Status.PICTURED, research=ARTICLES)
        with pytest.raises(ValueError, match="no images"):
            script.run(session, video.id)


def test_script_rejects_output_without_short_spans(session_factory, monkeypatch):
    monkeypatch.setattr(script.llm, "generate", lambda *a, **k: "[IMG 1] a script with no shorts at all")
    with session_factory() as session:
        video = _video(session, Status.PICTURED, research={**ARTICLES, "images": _inventory(session, 20)})
        with pytest.raises(ValueError, match="no \\[SHORT\\] spans"):
            script.run(session, video.id)


# --------------------------------------------------------------------------- #
# check
# --------------------------------------------------------------------------- #
def test_check_applies_only_listed_changes_and_records_them(session_factory, monkeypatch):
    fix = {"original": "Octavian would go on to become Rome's first emperor.",
           "replacement": "Octavian would later rule as Augustus.", "reason": "material names him Augustus"}
    remove_quote = {"original": "[QUOTE]Et tu, Brute?[/QUOTE]", "replacement": "", "reason": "not quoted"}
    seen = {}

    def fake_generate(session, prompt, **kwargs):
        seen.update(kwargs)
        return {"changes": [fix, remove_quote, "junk"]}

    monkeypatch.setattr(check.llm, "generate", fake_generate)
    with session_factory() as session:
        video = _video(session, Status.SCRIPTED, research=ARTICLES, script=SCRIPT)
        check.run(session, video.id)
        session.commit()

        expected = SCRIPT.replace(fix["original"], fix["replacement"]).replace("[QUOTE]Et tu, Brute?[/QUOTE]", "")
        assert video.status == Status.CHECKED
        assert video.script == expected.replace("  ", " ").strip()
        applied = [c["original"] for c in video.research["check"]["applied"]]
        assert applied == [fix["original"], remove_quote["original"]]
        assert video.research["articles"] == ARTICLES["articles"]
        assert seen["role"] == "checker"


def test_check_never_adds_drops_or_moves_an_image_mark():
    changes = [
        {"original": "[IMG 3] The senate had hoped his death would restore the republic.", "replacement": "",
         "reason": "r"},  # removes a mark with the sentence
        {"original": "[IMG 2] Octavian would go on to become Rome's first emperor.",
         "replacement": "[IMG 3] Octavian would later rule as Augustus.", "reason": "r"},  # swaps the picture
        {"original": "Octavian would go on to become Rome's first emperor.",
         "replacement": "Octavian would later rule as Augustus.", "reason": "r"},  # words only: fine
    ]
    checked, applied, rejected = check.apply_changes(MARKED, changes)
    assert [r["why"] for r in rejected] == ["would add, drop or move an [IMG n] image mark"] * 2
    assert len(applied) == 1 and "[IMG 2] Octavian would later rule as Augustus." in checked


def test_check_rejects_unfindable_noop_and_markup_breaking_changes():
    changes = [
        {"original": "A sentence the writer never wrote.", "replacement": "x", "reason": "r"},
        {"original": "Rome was not built in a day.", "replacement": "Rome was not built in a day.", "reason": "r"},
        {"original": "for good.[/SHORT]", "replacement": "for good.", "reason": "r"},  # drops a closing tag
    ]
    checked, applied, rejected = check.apply_changes(SCRIPT, changes)

    assert checked == SCRIPT
    assert applied == []
    assert [r["why"].split(":")[0] for r in rejected] == [
        "original sentence not found in the script", "no actual change", "would break markup",
    ]


def test_check_with_no_changes_keeps_the_script(session_factory, monkeypatch):
    monkeypatch.setattr(check.llm, "generate", lambda *a, **k: {"changes": []})
    with session_factory() as session:
        video = _video(session, Status.SCRIPTED, research=ARTICLES, script=SCRIPT)
        check.run(session, video.id)
        assert video.script == SCRIPT
        assert video.research["check"]["applied"] == []


def test_check_refuses_to_gut_the_script(session_factory, monkeypatch):
    sentences = ["Rome was not built in a day.", "The senate had hoped his death would restore the republic.",
                 "Octavian would go on to become Rome's first emperor."]
    changes = [{"original": s, "replacement": "", "reason": "r"} for s in sentences]
    changes.append({"original": "[QUOTE]Et tu, Brute?[/QUOTE]", "replacement": "", "reason": "r"})
    monkeypatch.setattr(check.llm, "generate", lambda *a, **k: {"changes": changes})
    with session_factory() as session:
        video = _video(session, Status.SCRIPTED, research=ARTICLES, script=SCRIPT)
        with pytest.raises(ValueError, match="cut the script"):
            check.run(session, video.id)


# --------------------------------------------------------------------------- #
# scene plan
# --------------------------------------------------------------------------- #
def test_plan_scenes_respects_short_edges_quotes_and_budget():
    scenes = segment.plan_scenes(SCRIPT, min_words=5, max_words=12)
    texts = [t for t, _, _ in scenes]

    assert " ".join(texts).replace("[QUOTE]", "").replace("[/QUOTE]", "").split() == \
        SCRIPT.replace("[SHORT]", " ").replace("[/SHORT]", " ").replace("[QUOTE]", "").replace("[/QUOTE]", "").split()
    assert any(t == "[QUOTE]Et tu, Brute?[/QUOTE]" or "[QUOTE]Et tu, Brute?[/QUOTE]" in t for t in texts)
    shorts = [t for t, in_short, _ in scenes if in_short]
    assert any("Ides of March" in t for t in shorts)
    assert not any("Rome was not built" in t for t, in_short, _ in scenes if in_short)
    assert all(5 <= segment._words(t) <= segment.SCENE_STRETCH * 12 for t in texts)  # >= min, <= stretch


def test_long_sentences_split_at_clauses():
    sentence = ("The emperor marched north, burned the bridges behind him, crossed the frozen river, "
                "and surprised the enemy camp at dawn.")
    scenes = segment.plan_scenes(sentence, min_words=3, max_words=8)
    assert len(scenes) > 1
    assert all(3 <= segment._words(t) <= segment.SCENE_STRETCH * 8 for t, _, _ in scenes)


def test_unpunctuated_long_sentence_is_cut_near_the_middle():
    sentence = " ".join(f"w{i}" for i in range(30)) + "."
    sizes = [segment._words(t) for t, _, _ in segment.plan_scenes(sentence, min_words=5, max_words=12)]
    assert sum(sizes) == 30 and max(sizes) <= 12 and min(sizes) >= 5


def test_plan_scenes_cuts_at_image_marks_and_never_speaks_them():
    scenes = segment.plan_scenes(MARKED, min_words=5, max_words=12)
    assert [n for _, _, n in scenes][0] == 1 and {n for _, _, n in scenes} == {1, 2, 3}
    assert not any("[IMG" in t for t, _, _ in scenes)
    by_image = {}
    for text, _, n in scenes:
        by_image.setdefault(n, []).append(text)
    assert any("Ides of March" in t for t in by_image[2]) and any("Octavian" in t for t in by_image[2])
    assert any("senate" in t for t in by_image[3])


def test_segment_run_gives_each_scene_its_passages_image(session_factory, monkeypatch):
    monkeypatch.setattr(segment.llm, "generate", lambda *a, **k: {"scenes": []})
    with session_factory() as session:
        images = _inventory(session, 3)
        video = _video(session, Status.CHECKED, script=MARKED, research={**ARTICLES, "images": images})
        segment.run(session, video.id)
        session.commit()

        rows = video.scenes
        assert video.status == Status.SEGMENTED
        assert str(rows[0].image_asset_id) == images[0]["asset_id"] and rows[0].image_query == "Picture 1"
        assert any(r.in_short_span for r in rows) and not rows[0].in_short_span
        assert all(r.image_asset_id is not None for r in rows)


def test_segment_refuses_a_mark_outside_the_inventory(session_factory, monkeypatch):
    monkeypatch.setattr(segment.llm, "generate", lambda *a, **k: {"scenes": []})
    with session_factory() as session:
        video = _video(session, Status.CHECKED, script=MARKED, research={**ARTICLES, "images": _inventory(session, 2)})
        with pytest.raises(ValueError, match=r"image\(s\) \[3\]"):
            segment.run(session, video.id)


def test_segment_rerun_replaces_scenes(session_factory, monkeypatch):
    monkeypatch.setattr(segment.llm, "generate", lambda *a, **k: {"scenes": []})
    with session_factory() as session:
        video = _video(session, Status.CHECKED, script=MARKED, research={**ARTICLES, "images": _inventory(session, 3)})
        segment.run(session, video.id)
        session.commit()
        first = session.query(Scene).count()

        video.status = Status.CHECKED  # sent back
        session.commit()
        segment.run(session, video.id)
        session.commit()
        assert session.query(Scene).count() == first


# --------------------------------------------------------------------------- #
# script guards (bundle D)
# --------------------------------------------------------------------------- #
def _draft(words: int, shorts: int, touching: bool = False) -> str:
    body = ["word"] * words
    spans = [f"[SHORT]{' '.join(['short'] * 5)}.[/SHORT]" for _ in range(shorts)]
    glue = " " if touching else " between. "
    return " ".join(body) + " " + glue.join(spans)


def test_problems_catch_length_short_count_and_touching_spans():
    assert script.problems(_draft(600, 7), 468, 1248, 7) == []
    assert "it is 141 words" in script.problems(_draft(100, 7), 468, 1248, 7)[0]  # ~1 minute: rejected
    assert "exactly 7" in script.problems(_draft(600, 5), 468, 1248, 7)[0]
    assert "touch" in script.problems(_draft(600, 7, touching=True), 468, 1248, 7)[0]
    assert script.problems(_draft(440, 7), 468, 1248, 7) == []  # within the 10% slack


def _marked(words: int, shorts: int, images: int = 20) -> str:
    """A well-formed draft: 20-word passages cycling through the images, then the Shorts."""
    passages, k = [], 0
    for _ in range(max(1, words // 20)):
        passages.append(f"[IMG {k % images + 1}] " + " ".join(["word"] * 20) + ".")
        k += 1
    spans = [f"[SHORT][IMG {(k + i) % images + 1}] " + " ".join(["short"] * 10) + ".[/SHORT]" for i in range(shorts)]
    return " ".join(passages) + " " + " between. ".join(spans)


def test_image_mark_problems_are_named():
    ok = _marked(560, 4)
    assert script.problems(ok, 400, 700, 4, images=20) == []
    assert "before the first" in script.problems("Words first. " + ok, 400, 700, 4, images=20)[0]
    assert "not in the list" in script.problems(ok.replace("[IMG 3]", "[IMG 99]"), 400, 700, 4, images=20)[0]
    assert "twice in a row" in " ".join(script.problems(ok.replace("[IMG 2]", "[IMG 1]"), 400, 700, 4, images=20))
    assert "more than 2 times" in script.problems(_marked(560, 4, images=5), 400, 700, 4, images=5)[0]
    too_long = "[IMG 20] " + " ".join(["word"] * 60) + ". " + ok
    assert "split it" in " ".join(script.problems(too_long, 400, 800, 4, images=20))
    inside = ok.replace("[IMG 1] word", "[QUOTE]quoted [IMG 1] word[/QUOTE]", 1)
    assert "inside a [QUOTE]" in script.problems(inside, 400, 800, 4, images=20)[0]


def test_a_bad_draft_is_retried_once_with_the_problem_named(session_factory, monkeypatch):
    length = script.plan(20, get_config())
    good = _marked((length["words_min"] + length["words_max"]) // 2 - 40, length["shorts"])
    drafts, prompts = iter([_marked(100, length["shorts"]), good]), []
    monkeypatch.setattr(script.llm, "generate",
                        lambda session, prompt, **kw: prompts.append(prompt) or next(drafts))
    with session_factory() as session:
        video = _video(session, Status.PICTURED, research={**ARTICLES, "images": _inventory(session, 20)})
        script.run(session, video.id)
        assert video.status == Status.SCRIPTED and video.script == good
    assert "previous draft was rejected because it is" in prompts[1]


def test_a_script_still_wrong_after_the_retry_fails_the_stage(session_factory, monkeypatch):
    monkeypatch.setattr(script.llm, "generate", lambda session, prompt, **kw: _draft(100, 2))
    with session_factory() as session:
        video = _video(session, Status.PICTURED, research={**ARTICLES, "images": _inventory(session, 20)})
        with pytest.raises(ValueError, match="after 2 drafts"):
            script.run(session, video.id)
