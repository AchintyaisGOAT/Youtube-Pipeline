"""S6b (README §6.5): sound effects + on-screen text — the asset manifests, the rules the
scene plan's extras must pass, overlay rendering, and a real render with both."""

from __future__ import annotations

import json
import math
import shutil
import types

import numpy as np
import pytest
import soundfile as sf
import yaml
from PIL import Image

from app import assets
from app.config import get_config
from app.db import Asset, Base, Scene, Video, get_engine, get_sessionmaker
from app.stages import assemble, segment
from app.status import Status
from app.subtitles import build_subtitles


@pytest.fixture
def assets_dir(tmp_path, monkeypatch):
    root = tmp_path / "assets"
    monkeypatch.setattr(assets, "get_settings", lambda: types.SimpleNamespace(assets_dir=str(root)))
    return root


def _tone(path, seconds=1.0, freq=440.0, rate=24_000):
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, 0.3 * np.sin(2 * math.pi * freq * np.arange(int(rate * seconds)) / rate), rate)


# --------------------------------------------------------------------------- #
# manifests
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(("name", "tags"), [
    ("Whoosh Swish.mp3", ["whoosh"]),
    ("Big_Impact_Boom.wav", ["impact"]),
    ("Church Bell Distant.mp3", ["bell"]),
    ("Mystery Thing.mp3", []),
])
def test_guess_tags_from_file_names(name, tags):
    assert assets.guess_tags(name) == tags


def test_write_manifests_keeps_edits_adds_new_files_and_drops_missing(assets_dir):
    _tone(assets_dir / "sfx" / "Whoosh Swish.wav")
    _tone(assets_dir / "sfx" / "Mystery Thing.wav")
    _tone(assets_dir / "music" / "Calm Piano.wav")
    (assets_dir / "sfx" / "manifest.yaml").write_text(yaml.safe_dump({"effects": [
        {"file": "Whoosh Swish.wav", "tags": ["whoosh"], "title": "My title", "credit": "", "gain_db": -3},
        {"file": "Deleted.wav", "tags": ["impact"]},
    ]}), encoding="utf-8")

    report = assets.write_manifests()

    effects = yaml.safe_load((assets_dir / "sfx" / "manifest.yaml").read_text(encoding="utf-8"))["effects"]
    assert [e["file"] for e in effects] == ["Mystery Thing.wav", "Whoosh Swish.wav"]
    assert effects[1]["title"] == "My title" and effects[1]["gain_db"] == -3  # your edit survives
    assert report == {"music": ["Calm Piano.wav"], "sfx": ["Mystery Thing.wav"], "untagged": ["Mystery Thing.wav"],
                      "unlicensed": ["Calm Piano.wav"]}
    assert assets.sfx_tags() == ["whoosh"]  # untagged effects are unusable
    assert assets.pick_music(__import__("uuid").uuid4()) is None  # and so are unlicensed tracks
    music = assets_dir / "music" / "manifest.yaml"
    tracks = yaml.safe_load(music.read_text(encoding="utf-8"))["tracks"]
    tracks[0]["license"] = assets.YOUTUBE_LIBRARY_LICENSE
    music.write_text(yaml.safe_dump({"tracks": tracks}), encoding="utf-8")
    assert assets.pick_music(__import__("uuid").uuid4())["title"] == "Calm Piano"
    assert assets.pick_sfx("whoosh", 3)["path"].endswith("Whoosh Swish.wav")
    assert assets.pick_sfx("thunder", 0) is None


# --------------------------------------------------------------------------- #
# scene-plan extras
# --------------------------------------------------------------------------- #
TEXTS = [
    "On August 4, 1892, in Fall River, a scream tore through the house.",
    "Andrew Borden was dead on the sofa.",
    "His estate was worth three hundred thousand dollars.",
    "The trial began.",
    "Twelve men would decide.",
    "The jury took ninety minutes.",
    "Lizzie walked free.",
    "She never left Fall River.",
]
SOURCES = "Andrew Borden left an estate of $300,000. Lizzie Borden, Fall River, Massachusetts, 1892."


def _raw(**by_index):
    return [by_index.get(i, {}) for i in range(len(TEXTS))]


def test_sfx_budget_known_tags_and_never_twice_in_a_row():
    cfg = get_config()
    raw = _raw(**{str(i): {} for i in range(8)})
    raw = [{"sfx": "whoosh"}, {"sfx": "impact"}, {"sfx": "unknown"}, {"sfx": "impact"},
           {"sfx": "whoosh"}, {"sfx": "whoosh"}, {}, {}]
    sfx, _ = segment.clean_extras(raw, TEXTS, SOURCES, {"whoosh", "impact"}, cfg)
    assert sfx == ["whoosh", None, None, "impact", None, None, None, None]  # floor(8 * 0.3) = 2


def test_overlays_must_be_grounded_and_are_capped(monkeypatch):
    cfg = get_config()
    raw = [
        {"overlay": {"kind": "label", "text": "Fall River · 1892"}},        # in the narration
        {"overlay": {"kind": "label", "text": "Boston · 1893"}},            # invented place and year
        {"overlay": {"kind": "number", "text": "$300,000"}},                # stated in the sources
        {"overlay": {"kind": "chapter", "text": "The Trial"}},              # chapters are free wording
        {"overlay": {"kind": "number", "text": "12 JURORS"}},               # not budget-able: cap reached
        {"overlay": {"kind": "number", "text": "90 MINUTES"}},              # no "90" anywhere -> rejected
        {"overlay": {"kind": "chapter", "text": "Chapter 7 of the story"}},  # invents a number
        {"overlay": {"kind": "shout", "text": "?"}},                        # unknown kind
    ]
    _, overlays = segment.clean_extras(raw, TEXTS, SOURCES, set(), cfg)
    assert overlays[0] == {"kind": "label", "text": "FALL RIVER · 1892"}
    assert overlays[1] is None
    assert overlays[2] == {"kind": "number", "text": "$300,000"}
    assert overlays[3] == {"kind": "chapter", "text": "THE TRIAL"}
    assert overlays[4:] == [None, None, None, None]  # floor(8 * 0.25) = 2 labels/numbers used


def test_ambient_effects_need_the_scene_to_mention_them():
    cfg = get_config()
    texts = ["Andrew Borden directed textile mills.", "The church bell rang at noon.",
             "Another axe murder shook the town.", "Then came the verdict.", "", "", "", "", "", ""]
    raw = [{"sfx": "bell"}, {"sfx": "bell"}, {"sfx": "thunder"}, {"sfx": "impact"}] + [{}] * 6
    sfx, _ = segment.clean_extras(raw, texts, "", {"bell", "thunder", "impact"}, cfg)
    assert sfx[:4] == [None, "bell", None, "impact"]


def test_labels_must_name_a_place_or_date():
    ok = {"kind": "label", "text": "FALL RIVER"}
    assert segment.overlay_is_grounded(ok, "They reached Fall River at dawn.", "")
    front_door = {"kind": "label", "text": "FRONT DOOR"}
    assert not segment.overlay_is_grounded(front_door, "He found the front door locked.", "")
    assert segment.overlay_is_grounded({"kind": "label", "text": "AUGUST 1892"}, "In August 1892 it began.", "")


def test_chapter_cards_get_a_whoosh_and_numbers_an_impact():
    cfg = get_config()
    raw = [{"overlay": {"kind": "chapter", "text": "The Morning"}}, {}, {},
           {"overlay": {"kind": "number", "text": "$300,000"}}, {}, {}, {}, {}]
    sfx, _ = segment.clean_extras(raw, TEXTS, SOURCES, {"whoosh", "impact"}, cfg)
    assert sfx[0] == "whoosh" and sfx[3] == "impact"


def test_extras_are_off_when_disabled(monkeypatch):
    cfg = get_config()
    monkeypatch.setattr(cfg.effects, "sfx", False)
    monkeypatch.setattr(cfg.effects, "overlays", False)
    raw = [{"sfx": "whoosh", "overlay": {"kind": "chapter", "text": "Start"}}] + [{}] * 7
    assert segment.clean_extras(raw, TEXTS, SOURCES, {"whoosh"}, cfg) == ([None] * 8, [None] * 8)


# --------------------------------------------------------------------------- #
# rendering pieces
# --------------------------------------------------------------------------- #
def test_overlays_render_in_their_own_styles_and_respect_a_window():
    timeline = [{"scene": 0, "words": [{"text": "Hello.", "start": 0.1, "end": 0.5, "quote": False}]}]
    overlays = [{"kind": "label", "text": "FALL RIVER · 1892", "start": 0.25, "end": 3.0},
                {"kind": "number", "text": "$300,000", "start": 5.0, "end": 7.0}]
    ass, srt = build_subtitles(timeline, get_config(), resolution=(1920, 1080), size=64, overlays=overlays)
    styles = {e.style for e in ass.events}
    assert {"Label", "Number", "Default"} <= styles and "Chapter" in ass.styles
    assert "\\fad(" in next(e.text for e in ass.events if e.style == "Label")
    assert len(srt.events) == 1  # overlays never go into the SRT captions

    shorts_ass, _ = build_subtitles(timeline, get_config(), resolution=(1080, 1920), size=84,
                                    overlays=overlays, window=(4.0, 9.0))
    number = next(e for e in shorts_ass.events if e.style == "Number")
    assert number.start == 1000  # shifted into the window


def test_audio_graph_mixes_cues_after_narration():
    graph = assemble.audio_graph(get_config(), False, [(2, 1.3, 2.0, -8.0), (3, 5.0, 1.0, -5.0)])
    assert "adelay=1300:all=1" in graph and "adelay=5000:all=1" in graph
    assert "[n2][s0][s1]amix=inputs=3:duration=first" in graph
    assert assemble.audio_graph(get_config(), False, []).startswith("[1:a]loudnorm")


def test_sfx_cue_puts_a_whoosh_just_before_the_cut():
    assert assemble.sfx_cue(10.0, 16.0, "whoosh") == (pytest.approx(9.8), pytest.approx(6.0))
    assert assemble.sfx_cue(10.0, 12.0, "impact") == (10.0, pytest.approx(2.3))


# --------------------------------------------------------------------------- #
# real render with an effect and an overlay
# --------------------------------------------------------------------------- #
@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="FFmpeg not installed")
def test_render_with_sound_effect_and_overlay(tmp_path, monkeypatch, assets_dir):
    url = f"sqlite:///{tmp_path / 'fx.db'}"
    Base.metadata.create_all(get_engine(url))
    work, out = tmp_path / "work", tmp_path / "out"
    work.mkdir()
    out.mkdir()
    monkeypatch.setattr(assemble, "work_dir", lambda video_id: work)
    monkeypatch.setattr(assemble, "output_dir", lambda video_id: out)
    cfg = get_config()
    monkeypatch.setattr(cfg.video.long_form, "resolution", (320, 180))
    monkeypatch.setattr(cfg.video.long_form, "fps", 10)
    monkeypatch.setattr(cfg.video, "encoder", "libx264")

    _tone(assets_dir / "sfx" / "Impact Boom.wav", seconds=0.5, freq=90)
    assets.write_manifests()
    rate, duration = 24_000, 2.0
    t = np.arange(int(rate * duration)) / rate
    sf.write(work / "narration.wav", 0.2 * np.sin(2 * math.pi * 300 * t), rate)  # a steady 300 Hz "voice"
    (work / "words.json").write_text(json.dumps([{"scene": 0, "words": []}, {"scene": 1, "words": []}]))
    Image.new("RGB", (800, 450), (90, 60, 30)).save(tmp_path / "img.jpg")

    with get_sessionmaker(url)() as session:
        image = Asset(kind="image", source="t", source_id="img", uri=str(tmp_path / "img.jpg"))
        session.add(image)
        session.flush()
        video = Video(status=Status.ALIGNED, title="T", duration_s=duration)
        session.add(video)
        session.flush()
        session.add_all([
            Scene(video_id=video.id, idx=0, text="a", start_s=0.0, end_s=1.0, image_asset_id=image.id,
                  overlay={"kind": "chapter", "text": "THE START"}),
            Scene(video_id=video.id, idx=1, text="b", start_s=1.0, end_s=2.0, image_asset_id=image.id, sfx="impact"),
        ])
        session.commit()

        assemble.run(session, video.id)
        session.commit()

        ffmpeg_audio = tmp_path / "audio.wav"
        import subprocess
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(out / "final.mp4"), "-ac", "1", "-ar", "8000",
                        str(ffmpeg_audio)], check=True)
        audio, audio_rate = sf.read(ffmpeg_audio)

        def band(start_s, end_s):  # energy around the effect's 90 Hz, clear of the 300 Hz voice
            chunk = audio[int(start_s * audio_rate):int(end_s * audio_rate)]
            spectrum = np.abs(np.fft.rfft(chunk))
            freqs = np.fft.rfftfreq(len(chunk), 1 / audio_rate)
            return spectrum[(freqs > 70) & (freqs < 110)].sum()

        assert band(1.05, 1.4) > 20 * band(0.4, 0.75)  # the effect sounds when scene 2 starts, not before
        assert video.video_metadata["sfx"][0]["file"] == "Impact Boom.wav"
        ass_text = (work / "subtitles.ass").read_text(encoding="utf-8")
        assert "THE START" in ass_text and "Chapter" in ass_text
