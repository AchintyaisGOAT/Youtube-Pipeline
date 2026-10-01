"""S7 (README §4.1 steps 12–15): metadata, thumbnail, Shorts, upload kit, and `ogh review`.
The LLM and Kokoro are faked; the end-to-end test runs the real FFmpeg on tiny videos."""

from __future__ import annotations

import json
import math
import shutil
from datetime import UTC, datetime
from types import SimpleNamespace

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from app import cli as review
from app.config import get_config
from app.db import Asset, Base, Scene, Short, Video, get_engine, get_sessionmaker
from app.ffmpeg import probe
from app.schedule import next_slot
from app.stages import metadata, narrate, package, shorts, thumbnail
from app.status import Status
from app.storage import output_dir, work_dir


@pytest.fixture
def session_factory(tmp_path):
    url = f"sqlite:///{tmp_path / 'media.db'}"
    Base.metadata.create_all(get_engine(url))
    return get_sessionmaker(url)


def _scene(idx, start, end, text="A sentence.", overlay=None):
    return SimpleNamespace(idx=idx, start_s=start, end_s=end, text=text, overlay=overlay)


# --------------------------------------------------------------------------- #
# metadata — pure helpers
# --------------------------------------------------------------------------- #
def test_chapters_open_at_zero_merge_short_ones_and_need_three():
    chapter = lambda text: {"kind": "chapter", "text": text}  # noqa: E731
    scenes = [_scene(0, 0, 6), _scene(1, 30, 36, overlay=chapter("THE CRIME")),
              _scene(2, 35, 40, overlay=chapter("TOO CLOSE")), _scene(3, 90, 96, overlay=chapter("THE TRIAL"))]
    assert metadata.chapters(scenes, 200) == [(0.0, "Intro"), (30, "The Crime"), (90, "The Trial")]
    assert metadata.chapters(scenes[:2], 200) == []  # only 2: YouTube would ignore them


def test_timestamp_formats():
    assert metadata.timestamp(0) == "0:00"
    assert metadata.timestamp(95.7) == "1:35"
    assert metadata.timestamp(3725) == "1:02:05"


def _asset(source, n, **kw):
    return SimpleNamespace(source=source, source_id=f"File:{n}.jpg", attribution=kw.get("who", f"Author {n}"),
                           license="public domain", rights_url=f"https://commons.wikimedia.org/wiki/File:{n}.jpg")


def test_description_has_hook_chapters_sources_credits_footer_and_hashtags():
    cfg = get_config()
    meta = {"hook": "An axe, two bodies.", "summary": "Fall River, 1892.", "topic_hashtag": "Lizzie Borden"}
    text = metadata.build_description(
        meta, config=cfg, chapter_list=[(0.0, "Intro"), (40, "The Crime"), (95, "The Trial")],
        articles=[{"title": "Lizzie Borden", "url": "https://en.wikipedia.org/wiki/Lizzie_Borden"}],
        assets=[_asset("wikimedia_commons", 1), _asset("gemini", 2)], music={"credit": "Song by Someone"})
    assert text.startswith("An axe, two bodies.")
    assert "sub_confirmation=1" in text
    assert "0:00 Intro\n0:40 The Crime\n1:35 The Trial" in text
    assert "- Lizzie Borden: https://en.wikipedia.org/wiki/Lizzie_Borden" in text
    assert "Author 1" in text and "1 AI-generated illustration" in text
    assert "Song by Someone" in text
    assert cfg.publish.description_footer.strip() in text
    assert text.endswith("#LizzieBorden #History #PantherTellsHistory")


def test_description_collapses_image_credits_past_youtubes_limit():
    many = [_asset("wikimedia_commons", n, who="A very long author name " * 3) for n in range(80)]
    text = metadata.build_description({"hook": "h", "summary": "s"}, config=get_config(), chapter_list=[],
                                      articles=[], assets=many, music=None)
    assert len(text) <= metadata.DESCRIPTION_LIMIT
    assert "Images: 80 from Wikimedia Commons" in text


def test_thumbnail_hook_is_four_words_max_and_highlights_a_word_in_it():
    assert metadata.thumbnail_hook({"thumbnail_text": "she was acquitted", "thumbnail_highlight": "acquitted"},
                                   "x") == ("SHE WAS ACQUITTED", "ACQUITTED")
    text, word = metadata.thumbnail_hook({"thumbnail_text": "one two three four five", "thumbnail_highlight": "x"}, "")
    assert text == "ONE TWO THREE FOUR" and word in text.split()


def test_short_description_ends_with_the_footer_and_hashtags():
    text = metadata.short_description({"description": "A tease."}, config=get_config(), topic_hashtag="LizzieBorden")
    assert text.startswith("A tease.")
    assert text.endswith("#LizzieBorden #History #PantherTellsHistory #Shorts")


# --------------------------------------------------------------------------- #
# thumbnail
# --------------------------------------------------------------------------- #
def test_render_thumbnail_is_1280x720_with_or_without_a_usable_image(tmp_path):
    assert thumbnail.render_thumbnail("SHE WAS ACQUITTED", "ACQUITTED", None).size == thumbnail.THUMBNAIL_SIZE
    bad = tmp_path / "not_an_image.jpg"
    bad.write_text("not actually an image")
    assert thumbnail.render_thumbnail("A HOOK", "HOOK", bad).size == thumbnail.THUMBNAIL_SIZE


def test_thumbnail_text_fits_inside_the_frame():
    lines, font = thumbnail.layout("THE AXE THAT VANISHED".split(), thumbnail._font_file(), 1000)
    assert 1 <= len(lines) <= 2
    assert all(font.getlength(" ".join(line)) <= 1000 for line in lines)


# --------------------------------------------------------------------------- #
# shorts — planning
# --------------------------------------------------------------------------- #
def test_passage_over_the_limit_ends_at_the_last_full_sentence():
    scenes = [_scene(0, 10, 25, "One."), _scene(1, 25, 40, "Two, and"), _scene(2, 40, 52, "three."),
              _scene(3, 52, 70, "Four.")]
    short = SimpleNamespace(start_s=10, end_s=70)
    kept, length = shorts.passage(scenes, short, limit=50)
    assert [s.idx for s in kept] == [0, 1, 2] and length == 42
    kept, length = shorts.passage(scenes, short, limit=100)  # fits whole
    assert len(kept) == 4 and length == 60


def test_passage_with_no_sentence_end_in_reach_ends_at_a_scene_edge_not_mid_word():
    scenes = [_scene(0, 0, 30, "Long, and"), _scene(1, 30, 60, "longer.")]
    kept, length = shorts.passage(scenes, SimpleNamespace(start_s=0, end_s=60), limit=45)
    assert [s.idx for s in kept] == [0] and length == 30
    kept, length = shorts.passage([_scene(0, 0, 60, "One long run, and")], SimpleNamespace(start_s=0, end_s=60), 45)
    assert [s.idx for s in kept] == [0] and length == 45  # only a first scene over the limit is cut


def test_scene_box_leaves_room_for_the_zoom():
    assert shorts.BOX_W * shorts.ZOOM < 1 and shorts.BOX_H * shorts.ZOOM < 1


def test_publish_dates_follow_the_schedule_and_queue_after_taken_slots():
    cfg = get_config()
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)  # a Wednesday
    long_at, short_at = package.plan_dates(cfg, now, [], [], 3)
    assert (long_at.strftime("%a %H:%M"), long_at.day) == ("Fri 15:00", 2)
    assert [t.day for t in short_at] == [3, 4, 5] and short_at[0].hour == 12
    taken = next_slot(["daily 12:00"], datetime(2026, 10, 9, tzinfo=UTC), cfg.timezone)
    long2, shorts2 = package.plan_dates(cfg, now, [long_at], [taken], 1)
    assert long2.strftime("%a") == "Tue" and long2.day == 6
    assert shorts2[0].day == 10  # after the Shorts already queued


def test_slug():
    assert package.slug("Lizzie Borden: The Axe Murders!") == "lizzie-borden-the-axe-murders"


# --------------------------------------------------------------------------- #
# end to end: shorts -> package, real FFmpeg
# --------------------------------------------------------------------------- #
class FakeKokoro:
    def create(self, text, voice, lang):
        return np.full(int(narrate.SAMPLE_RATE * 0.05 * len(text.split())), 0.1, dtype=np.float32), narrate.SAMPLE_RATE


LLM_ANSWER = {
    "titles": ["Best title", "Second title", "3", "4", "5"],
    "hook": "Hook.", "summary": "Summary.", "tags": ["history", "axe"], "topic_hashtag": "LizzieBorden",
    "thumbnail_text": "SHE WAS ACQUITTED", "thumbnail_highlight": "ACQUITTED", "thumbnail_scene": 1,
    "shorts": [{"title": "Short one", "caption": "THE AXE", "description": "Tease."}],
}


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg not installed")
def test_shorts_then_package_build_the_upload_kit(session_factory, monkeypatch, tmp_path):
    cfg = get_config()
    monkeypatch.setattr(cfg.video.shorts, "resolution", (180, 320))
    monkeypatch.setattr(cfg.video.shorts, "fps", 10)
    monkeypatch.setattr(cfg.video, "encoder", "libx264")
    monkeypatch.setattr(shorts, "RENDER_WORKERS", 2)
    monkeypatch.setattr(narrate, "_kokoro_instance", lambda: FakeKokoro())
    monkeypatch.setattr(shorts.assets, "pick_music", lambda video_id: None)
    prompts = []
    monkeypatch.setattr(metadata.llm, "generate", lambda session, prompt, **kw: prompts.append(prompt) or LLM_ANSWER)

    Image.new("RGB", (800, 450), (180, 40, 40)).save(tmp_path / "wide.jpg")
    Image.new("RGB", (450, 800), (40, 40, 180)).save(tmp_path / "tall.jpg")
    rate, duration = narrate.SAMPLE_RATE, 3.0
    with session_factory() as session:
        wide = Asset(kind="image", source="wikimedia_commons", source_id="wide", uri=str(tmp_path / "wide.jpg"),
                     license="public domain", meta={"width": 800, "height": 450})
        tall = Asset(kind="image", source="wikimedia_commons", source_id="tall", uri=str(tmp_path / "tall.jpg"),
                     license="public domain", meta={"width": 450, "height": 800})
        session.add_all([wide, tall])
        video = Video(status=Status.ASSEMBLED, title="Lizzie Borden", script="Red. Blue. Green.",
                      duration_s=duration, research={"articles": [{"title": "Lizzie Borden", "url": "u"}]})
        session.add(video)
        session.flush()
        for idx, (text, start, end, image) in enumerate([("Red.", 0.0, 1.0, wide), ("Blue.", 1.0, 2.0, tall),
                                                          ("Green.", 2.0, 3.0, wide)]):
            session.add(Scene(video_id=video.id, idx=idx, text=text, start_s=start, end_s=end,
                              image_asset_id=image.id, in_short_span=idx < 2))
        session.add(Short(video_id=video.id, idx=0, start_s=0.0, end_s=2.0))
        session.commit()

        work = work_dir(video.id)
        t = np.arange(int(rate * duration)) / rate
        sf.write(work / "narration.wav", (0.3 * np.sin(2 * math.pi * 220 * t)).astype(np.float32), rate)
        (work / "words.json").write_text(json.dumps([
            {"scene": i, "words": [{"text": w, "start": i + 0.1, "end": i + 0.6, "quote": False}]}
            for i, w in enumerate(["Red.", "Blue.", "Green."])]))
        (output_dir(video.id) / "final.mp4").write_bytes(b"long")  # assemble's output, not re-rendered here
        (output_dir(video.id) / "final.srt").write_text("1\n")

        shorts.run(session, video.id)
        session.commit()
        assert video.status == Status.SHORTS_READY and video.title == "Best title"
        assert "Short 1: Red. Blue." in prompts[0]
        short_file = output_dir(video.id) / "short_01.mp4"
        info = probe(short_file)
        streams = {s["codec_type"]: s for s in info["streams"]}
        outro = 0.05 * len(shorts.OUTRO_LINES[0].split()) + shorts.OUTRO_LEAD_S + shorts.OUTRO_TAIL_S
        assert float(info["format"]["duration"]) == pytest.approx(2.0 + outro, abs=0.25)
        assert (streams["video"]["width"], streams["video"]["height"]) == (180, 320)
        assert shorts.OUTRO_LINES[0] in (output_dir(video.id) / "short_01.srt").read_text(encoding="utf-8")

        package.run(session, video.id)
        session.commit()
        kit = package.kit_dir(video)
        assert video.status == Status.PACKAGED
        assert kit.name.endswith("_lizzie-borden")
        assert {p.name for p in kit.iterdir()} >= {"long.mp4", "long.srt", "short_01.mp4", "short_01.srt",
                                                  "thumbnail.png", "upload.md", "credits.md"}
        assert not output_dir(video.id).exists() or not any(output_dir(video.id).iterdir())
        upload = (kit / "upload.md").read_text(encoding="utf-8")
        assert "Best title" in upload and "Related video" in upload and "#LizzieBorden" in upload
        first_dates = video.video_metadata["publish"]

        video.status = Status.SHORTS_READY  # a re-run keeps its folder and dates
        package.run(session, video.id)
        assert package.kit_dir(video) == kit and video.video_metadata["publish"] == first_dates


# --------------------------------------------------------------------------- #
# ogh review — CLI decision loop persists a status change (no real player launch)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("answers", "status", "error"), [
    (["a"], Status.APPROVED, None),
    (["r", "factually wrong"], Status.REJECTED, "factually wrong"),
    (["s"], Status.PACKAGED, None),
])
def test_review_one_records_the_decision(session_factory, monkeypatch, answers, status, error):
    monkeypatch.setattr(review, "_open_in_default_player", lambda path: None)
    with session_factory() as session:
        video = Video(status=Status.PACKAGED, title="T", video_metadata={})
        session.add(video)
        session.commit()

        responses = iter(answers)
        review._review_one(session, video, input_fn=lambda prompt="": next(responses))
        session.commit()
        assert video.status == status and video.error == error


# --------------------------------------------------------------------------- #
# credits
# --------------------------------------------------------------------------- #
def test_tidy_credit_drops_wikidata_codes_and_unknown_authors():
    assert metadata.tidy_credit('Sir George Robinson label QS:Len,"Sir George Robinson" — George Romney', "x") == (
        "Sir George Robinson", "George Romney")
    assert metadata.tidy_credit("Lizzie Borden 1890 — Unknown author Unknown author or not provided", "x") == (
        "Lizzie Borden 1890", None)
    title, _ = metadata.tidy_credit("A " + "very long title " * 20 + "— Someone", "x")
    assert len(title) <= 91 and title.endswith("…")


def test_music_credit_always_names_the_track():
    assert metadata.music_credit(None) == ""
    assert metadata.music_credit({"title": "Calm Tide", "artist": "Aakash Gandhi", "license": "YouTube Audio Library",
                                  "credit": ""}) == '🎵 Music\n"Calm Tide" by Aakash Gandhi (YouTube Audio Library)'
    credit = ('"Lightless Dawn" Kevin MacLeod (incompetech.com)\n'
              "Licensed under Creative Commons: By Attribution 4.0 License")
    assert metadata.music_credit({"title": "Lightless Dawn", "credit": credit}) == f"🎵 Music\n{credit}"
