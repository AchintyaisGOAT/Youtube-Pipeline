"""S4 (README §4.1 steps 4–7): research -> script -> check -> scene plan. Wikipedia and
the LLMs are faked; conftest.py blocks any real network access."""

from __future__ import annotations

import pytest

from app.db import Base, Scene, Topic, Video, get_engine, get_sessionmaker
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


def test_script_writes_from_articles_with_the_writer_role(session_factory, monkeypatch):
    seen = {}

    def fake_generate(session, prompt, **kwargs):
        seen.update(kwargs, prompt=prompt)
        return f"  {SCRIPT}\n"

    monkeypatch.setattr(script.llm, "generate", fake_generate)
    with session_factory() as session:
        video = _video(session, Status.RESEARCHED, research=ARTICLES)
        script.run(session, video.id)
        session.commit()

        assert video.script == SCRIPT
        assert video.status == Status.SCRIPTED
        assert (seen["role"], seen["json_mode"]) == ("writer", False)
        assert "=== ARTICLE: Assassination of Julius Caesar ===" in seen["prompt"]
        assert "468–1248 words" in seen["prompt"]  # 180–480 s at 2.6 words/s


def test_script_rejects_output_without_short_spans(session_factory, monkeypatch):
    monkeypatch.setattr(script.llm, "generate", lambda *a, **k: "a script with no shorts at all")
    with session_factory() as session:
        video = _video(session, Status.RESEARCHED, research=ARTICLES)
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
        assert [c["original"] for c in video.research["check"]["applied"]] == [fix["original"], remove_quote["original"]]
        assert video.research["articles"] == ARTICLES["articles"]
        assert seen["role"] == "checker"


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
    texts = [t for t, _ in scenes]

    assert " ".join(texts).replace("[QUOTE]", "").replace("[/QUOTE]", "").split() == \
        SCRIPT.replace("[SHORT]", " ").replace("[/SHORT]", " ").replace("[QUOTE]", "").replace("[/QUOTE]", "").split()
    assert any(t == "[QUOTE]Et tu, Brute?[/QUOTE]" or "[QUOTE]Et tu, Brute?[/QUOTE]" in t for t in texts)
    shorts = [t for t, in_short in scenes if in_short]
    assert any("Ides of March" in t for t in shorts)
    assert not any("Rome was not built" in t for t, in_short in scenes if in_short)
    assert all(5 <= segment._words(t) <= segment.SCENE_STRETCH * 12 for t in texts)  # >= min, <= stretch


def test_long_sentences_split_at_clauses():
    sentence = ("The emperor marched north, burned the bridges behind him, crossed the frozen river, "
                "and surprised the enemy camp at dawn.")
    scenes = segment.plan_scenes(sentence, min_words=3, max_words=8)
    assert len(scenes) > 1
    assert all(3 <= segment._words(t) <= segment.SCENE_STRETCH * 8 for t, _ in scenes)


def test_unpunctuated_long_sentence_is_cut_near_the_middle():
    sentence = " ".join(f"w{i}" for i in range(30)) + "."
    sizes = [segment._words(t) for t, _ in segment.plan_scenes(sentence, min_words=5, max_words=12)]
    assert sum(sizes) == 30 and max(sizes) <= 12 and min(sizes) >= 5


def test_segment_run_stores_scenes_with_llm_queries(session_factory, monkeypatch):
    def fake_generate(session, prompt, **kwargs):
        count = len(prompt.split("Scenes:\n")[1].split("\n\n")[0].splitlines())
        return {"scenes": [{"id": i, "query": f"query {i}"} for i in range(1, count)]}  # last one missing

    monkeypatch.setattr(segment.llm, "generate", fake_generate)
    with session_factory() as session:
        video = _video(session, Status.CHECKED, script=SCRIPT)
        segment.run(session, video.id)
        session.commit()

        rows = video.scenes
        assert video.status == Status.SEGMENTED
        assert rows[0].image_query == "query 1"
        assert rows[-1].image_query and not rows[-1].image_query.startswith("query")  # heuristic fill
        assert any(r.in_short_span for r in rows) and not rows[0].in_short_span


def test_segment_rerun_replaces_scenes(session_factory, monkeypatch):
    monkeypatch.setattr(segment.llm, "generate", lambda *a, **k: {"scenes": []})
    with session_factory() as session:
        video = _video(session, Status.CHECKED, script=SCRIPT)
        segment.run(session, video.id)
        session.commit()
        first = session.query(Scene).count()

        video.status = Status.CHECKED  # sent back
        session.commit()
        segment.run(session, video.id)
        session.commit()
        assert session.query(Scene).count() == first
