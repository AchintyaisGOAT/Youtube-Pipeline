"""S6 (README §4.1 steps 9–11): narration timing, word timing, subtitles, assembly.
Kokoro and Whisper are faked; the render test runs the real FFmpeg on a tiny video."""

from __future__ import annotations

import json
import math
import shutil
import uuid

import numpy as np
import pytest
import soundfile as sf
from PIL import Image

from app.config import get_config
from app.db import Asset, Base, Scene, Short, Video, get_engine, get_sessionmaker
from app.stages import align, assemble, narrate
from app.status import Status
from app.subtitles import build_subtitles, captions


@pytest.fixture
def session_factory(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 's6.db'}"
    Base.metadata.create_all(get_engine(url))
    work, out = tmp_path / "work", tmp_path / "output"
    work.mkdir()
    out.mkdir()
    for module in (narrate, align, assemble):
        monkeypatch.setattr(module, "work_dir", lambda video_id: work)
    monkeypatch.setattr(assemble, "output_dir", lambda video_id: out)
    return get_sessionmaker(url)


def _video(session, status, texts, **scene_fields) -> Video:
    video = Video(status=status, title="Lizzie Borden")
    session.add(video)
    session.flush()
    for idx, text in enumerate(texts):
        fields = {k: v[idx] for k, v in scene_fields.items()}
        session.add(Scene(video_id=video.id, idx=idx, text=text, **fields))
    session.commit()
    return video


# --------------------------------------------------------------------------- #
# narrate
# --------------------------------------------------------------------------- #
class FakeKokoro:
    """0.1 s of sound per word; records which voice read what."""

    def __init__(self):
        self.calls = []

    def create(self, text, voice, lang):
        self.calls.append((voice, text))
        return np.full(int(narrate.SAMPLE_RATE * 0.1 * len(text.split())), 0.1, dtype=np.float32), narrate.SAMPLE_RATE


def test_runs_split_quotes_and_strip_tags():
    assert narrate.runs("She cried: [QUOTE]Father's dead![/QUOTE] Then silence.") == [
        ("She cried:", False), ("Father's dead!", True), ("Then silence.", False)]


def test_pause_is_long_after_a_sentence_and_a_breath_mid_sentence():
    assert narrate.pause_after("It was over.", 400) == 0.4
    assert narrate.pause_after('[QUOTE]"Et tu, Brute?"[/QUOTE]', 400) == 0.4
    assert narrate.pause_after("In 44 BC, Julius Caesar was assassinated on the Ides of March", 400) == narrate.BREATH_S


def test_narrate_times_scenes_from_the_audio_with_no_gaps(session_factory, monkeypatch, tmp_path):
    kokoro = FakeKokoro()
    monkeypatch.setattr(narrate, "_kokoro_instance", lambda: kokoro)
    with session_factory() as session:
        video = _video(session, Status.IMAGES_READY, [
            "Four words in here,",                                  # mid-sentence: breath only
            "Lizzie called out: [QUOTE]Come quick![/QUOTE]",         # sentence end: 0.4 s
        ])
        narrate.run(session, video.id)
        session.commit()
        first, second = video.scenes

        assert (first.start_s, second.start_s) == (0.0, first.end_s)  # contiguous
        assert first.end_s == pytest.approx(0.4 + narrate.BREATH_S)
        assert second.end_s - second.start_s == pytest.approx(0.3 + narrate.BREATH_S + 0.2 + 0.4)
        assert video.duration_s == pytest.approx(second.end_s)
        assert ("bf_emma", "Come quick!") in kokoro.calls and ("bm_george", "Lizzie called out:") in kokoro.calls
        audio, rate = sf.read(tmp_path / "work" / "narration.wav")
        assert len(audio) / rate == pytest.approx(video.duration_s, abs=1e-3)
        assert video.status == Status.NARRATED


# --------------------------------------------------------------------------- #
# align
# --------------------------------------------------------------------------- #
def test_tokens_split_hyphens_and_dashes_and_drop_punctuation():
    assert align.tokens("thirty-two-year-old") == ["thirty", "two", "year", "old"]
    assert align.tokens("dollars—worth") == ["dollars", "worth"]
    assert align.tokens("Father's") == ["fathers"]
    assert align.tokens(".") == []


def test_alignment_survives_the_cases_that_broke_the_old_count_check():
    words = ["Just", "before", "eleven-ten", "on", "August", "4,", "1892,", "maid", "Bridget", "."]
    heard = [(" Just", 0.0, 0.2), (" before", 0.2, 0.5), (" eleven", 0.5, 0.8), ("-ten", 0.8, 1.0),
             (" on", 1.0, 1.1), (" August", 1.1, 1.5), (" 4,", 1.5, 1.7), (" 1892,", 1.8, 2.4),
             (" made", 2.5, 2.7), (" Bridget", 2.7, 3.1)]  # "maid" misheard as "made"
    times = align.align_words(words, heard)
    assert times[2] == (0.5, 1.0)  # both halves of eleven-ten
    assert times[6] == (1.8, 2.4)
    assert times[7] is None and times[9] is None  # misheard word, lone full stop

    filled = align.fill_gaps(words, times, 0.0, 3.5)
    assert filled[7][0] >= 2.4 and filled[7][1] <= 2.7  # squeezed between its matched neighbours
    assert all(a[1] <= b[0] + 1e-9 for a, b in zip(filled, filled[1:], strict=False))  # in order, no overlap


def test_fill_gaps_uses_the_scene_bounds_when_nothing_matched():
    filled = align.fill_gaps(["Rome", "fell."], [None, None], 3.0, 5.0)
    assert filled[0][0] == 3.0 and filled[-1][1] == pytest.approx(5.0)


def test_align_writes_words_and_creates_shorts(session_factory, monkeypatch, tmp_path):
    (tmp_path / "work" / "narration.wav").write_bytes(b"")
    heard = [(" Rome", 0.1, 0.5), (" burned.", 0.5, 0.9), (" Et", 1.1, 1.4), (" tu?", 1.4, 1.8), (" The", 2.1, 2.3),
             (" end.", 2.3, 2.6)]
    monkeypatch.setattr(align, "transcribe", lambda path, model: heard)
    with session_factory() as session:
        video = _video(session, Status.NARRATED, ["Rome burned.", "[QUOTE]Et tu?[/QUOTE]", "The end."],
                       start_s=[0.0, 1.0, 2.0], end_s=[1.0, 2.0, 3.0], in_short_span=[False, True, True])
        align.run(session, video.id)
        session.commit()

        timeline = json.loads((tmp_path / "work" / "words.json").read_text())
        assert [w["text"] for w in timeline[1]["words"]] == ["Et", "tu?"]
        assert timeline[1]["words"][0] == {"text": "Et", "start": 1.1, "end": 1.4, "quote": True}
        assert [(s.start_s, s.end_s) for s in session.query(Short)] == [(1.0, 3.0)]
        assert video.status == Status.ALIGNED


# --------------------------------------------------------------------------- #
# subtitles
# --------------------------------------------------------------------------- #
def _timeline():
    words = ["Lizzie", "Borden", "took", "an", "axe.", "She", "said", "Maggie,", "come", "quick!"]
    quote = [False] * 7 + [True] * 3
    return [{"scene": 0, "words": [{"text": w, "start": i * 0.5, "end": i * 0.5 + 0.4, "quote": q}
                                   for i, (w, q) in enumerate(zip(words, quote, strict=True))]}]


def test_captions_break_at_sentences_quotes_and_size():
    assert [[w["text"] for w in c] for c in captions(_timeline(), 4)] == [
        ["Lizzie", "Borden", "took", "an"], ["axe."], ["She", "said"], ["Maggie,", "come", "quick!"]]


def test_ass_is_in_real_pixels_highlights_each_word_and_italicises_quotes():
    ass, srt = build_subtitles(_timeline(), get_config(), resolution=(1920, 1080), size=64)
    assert (ass.info["PlayResX"], ass.info["PlayResY"]) == ("1920", "1080")
    assert ass.styles["Default"].fontsize == 64
    assert len(ass.events) == 10 and len(srt.events) == 4  # one event per word / per caption
    assert "{\\c&H3FD2FF&}Borden" in ass.events[1].text  # #FFD23F as ASS BGR
    assert "{\\i1}" in ass.events[-1].text and "{\\i1}" not in ass.events[0].text
    assert ass.events[0].end == ass.events[1].start  # the caption stays up while words advance


def test_window_cuts_and_shifts_to_zero():
    ass, srt = build_subtitles(_timeline(), get_config(), resolution=(1080, 1920), size=84, window=(2.5, 5.0))
    assert srt.events[0].start == 0 and srt.events[0].plaintext.startswith("She")
    assert srt.events[-1].end <= 2500


# --------------------------------------------------------------------------- #
# assemble — planning
# --------------------------------------------------------------------------- #
def test_motions_rotate_and_a_reused_image_changes_motion():
    a, *others = [uuid.uuid4() for _ in range(6)]
    motions = assemble.choose_motions([a, *others[:5], a])
    assert motions[:6] == list(assemble.MOTIONS)
    assert motions[6] != motions[0]  # `a` returns at the slot of its own last motion -> changed


def test_frame_counts_never_drift():
    bounds = [(i * 1.013, (i + 1) * 1.013) for i in range(300)]
    assert sum(assemble.frame_counts(bounds, 30)) == round(300 * 1.013 * 30)


def test_portrait_images_get_a_blurred_background():
    assert "boxblur" in assemble.clip_filter("zoom_in", 10, (1920, 1080), 30, 0.75)
    assert "boxblur" not in assemble.clip_filter("zoom_in", 10, (1920, 1080), 30, 1.5)


def test_pick_music_reads_the_manifest(tmp_path, monkeypatch):
    music = tmp_path / "assets" / "music"
    music.mkdir(parents=True)
    (music / "calm.mp3").write_bytes(b"x")
    (music / "manifest.yaml").write_text(
        "tracks:\n  - {file: calm.mp3, title: Calm, artist: Someone, license: no attribution required}\n"
        "  - {file: missing.mp3, title: Gone}\n", encoding="utf-8")
    monkeypatch.setattr(assemble, "get_settings", lambda: type("S", (), {"assets_dir": str(tmp_path / "assets")}))
    track = assemble.pick_music(uuid.uuid4())
    assert track["title"] == "Calm" and track["path"].endswith("calm.mp3")


# --------------------------------------------------------------------------- #
# assemble — real FFmpeg render of a tiny video
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg not installed")
@pytest.mark.parametrize("with_music", [False, True])
def test_assemble_renders_a_synced_video(session_factory, monkeypatch, tmp_path, with_music):
    cfg = get_config()
    monkeypatch.setattr(cfg.video.long_form, "resolution", (320, 180))
    monkeypatch.setattr(cfg.video.long_form, "fps", 10)
    monkeypatch.setattr(cfg.video, "encoder", "libx264")
    monkeypatch.setattr(assemble, "RENDER_WORKERS", 2)
    work = tmp_path / "work"

    Image.new("RGB", (800, 450), (180, 40, 40)).save(tmp_path / "wide.jpg")
    Image.new("RGB", (450, 800), (40, 40, 180)).save(tmp_path / "tall.jpg")
    rate, duration = 24_000, 2.2
    t = np.arange(int(rate * duration)) / rate
    sf.write(work / "narration.wav", 0.3 * np.sin(2 * math.pi * 220 * t), rate)
    (work / "words.json").write_text(json.dumps([
        {"scene": 0, "words": [{"text": "Red", "start": 0.1, "end": 0.5, "quote": False}]},
        {"scene": 1, "words": [{"text": "blue.", "start": 1.2, "end": 1.8, "quote": False}]},
    ]))
    music = None
    if with_music:
        sf.write(tmp_path / "music.wav", 0.2 * np.sin(2 * math.pi * 440 * np.arange(rate) / rate), rate)
        music = {"file": "music.wav", "title": "Tone", "artist": "Test", "license": "cc0", "credit": "",
                 "path": str(tmp_path / "music.wav")}
    monkeypatch.setattr(assemble, "pick_music", lambda video_id: music)

    with session_factory() as session:
        wide = Asset(kind="image", source="t", source_id="wide", uri=str(tmp_path / "wide.jpg"))
        tall = Asset(kind="image", source="t", source_id="tall", uri=str(tmp_path / "tall.jpg"))
        session.add_all([wide, tall])
        session.flush()
        video = _video(session, Status.ALIGNED, ["Red", "blue."], start_s=[0.0, 1.0], end_s=[1.0, duration],
                       image_asset_id=[wide.id, tall.id])
        video.duration_s = duration
        session.commit()

        assemble.run(session, video.id)
        session.commit()

        final = tmp_path / "output" / "final.mp4"
        info = assemble.probe(final)
        streams = {s["codec_type"]: s for s in info["streams"]}
        assert float(info["format"]["duration"]) == pytest.approx(duration, abs=0.2)
        assert (streams["video"]["width"], streams["video"]["height"]) == (320, 180)
        assert streams["video"]["pix_fmt"] == "yuv420p"
        assert (tmp_path / "output" / "final.srt").read_text().count("-->") == 2
        assert video.status == Status.ASSEMBLED
        assert (video.video_metadata or {}).get("music", {}).get("title") == ("Tone" if with_music else None)
