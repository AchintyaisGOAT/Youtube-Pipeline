"""app/sendback.py + `ogh review`'s send-back (bundle B): a video goes back to the stage to
redo, with exactly the later stages' output cleared."""

from __future__ import annotations

import pytest

from app import cli, sendback
from app.db import Asset, Base, Scene, Short, Video, get_engine, get_sessionmaker
from app.status import Status
from app.storage import output_dir, output_root, work_dir


@pytest.fixture
def packaged(tmp_path):
    """A packaged video: 3 scenes with images and timing, a Short, its work files and kit."""
    url = f"sqlite:///{tmp_path / 'sb.db'}"
    Base.metadata.create_all(get_engine(url))
    session = get_sessionmaker(url)()
    image = Asset(kind="image", source="t", source_id="a", uri="a.jpg")
    session.add(image)
    session.flush()
    kit = output_root() / "2026-10-02_lizzie-borden"
    kit.mkdir(parents=True)
    for name in ("long.mp4", "long.srt", "short_01.mp4", "upload.md"):
        (kit / name).write_text(name)
    video = Video(status=Status.PACKAGED, title="Best title", script="s", duration_s=12.0,
                  research={"articles": [], "images": [{"n": 1}], "plan": {"seconds": 180}},
                  thumbnail_uri=str(kit / "thumbnail.png"),
                  video_metadata={"topic": "Lizzie Borden", "kit_dir": str(kit), "publish": {"long": "x"},
                                  "description": "d", "music": {"title": "Lightless Dawn"}})
    session.add(video)
    session.flush()
    for idx in range(3):
        session.add(Scene(video_id=video.id, idx=idx, text=f"Scene {idx}.", image_asset_id=image.id,
                          start_s=idx * 4.0, end_s=idx * 4.0 + 4))
    session.add(Short(video_id=video.id, idx=0, start_s=0, end_s=8))
    (work_dir(video.id) / "narration.wav").write_text("wav")
    (work_dir(video.id) / "words.json").write_text("[]")
    session.commit()
    yield session, video, kit
    session.close()


def _scenes(session, video):
    session.expire_all()
    return list(session.get(Video, video.id).scenes)


def test_redo_pictures_clears_the_inventory_script_and_everything_after(packaged):
    session, video, kit = packaged
    assert sendback.send_back(session, video, "pictures") == Status.RESEARCHED
    session.commit()
    assert video.research == {"articles": []}  # research kept; images + length plan gone
    assert video.script is None  # its [IMG n] marks numbered the old inventory
    assert _scenes(session, video) == []
    assert not work_dir(video.id, create=False).exists()
    assert not kit.exists() and video.video_metadata.get("kit_dir") is None
    assert video.title == "Lizzie Borden"  # the topic, until metadata picks a title again
    assert session.query(Short).count() == 0


def test_redo_narration_keeps_scenes_and_their_images(packaged):
    session, video, kit = packaged
    assert sendback.send_back(session, video, "narration") == Status.SEGMENTED
    session.commit()
    assert [s.image_asset_id is not None for s in _scenes(session, video)] == [True, True, True]
    assert all(s.start_s is None for s in _scenes(session, video))
    assert not work_dir(video.id, create=False).joinpath("narration.wav").exists()
    assert video.research["images"] == [{"n": 1}] and not kit.exists()


def test_redo_shorts_keeps_the_long_form_for_the_kit(packaged):
    session, video, kit = packaged
    sendback.send_back(session, video, "shorts")
    session.commit()
    assert (output_dir(video.id) / "final.mp4").read_text() == "long.mp4"
    assert not kit.exists()
    assert video.video_metadata["music"] == {"title": "Lightless Dawn"}  # the render's credits stay
    assert "description" not in video.video_metadata
    assert (work_dir(video.id) / "words.json").exists()
    assert [s.image_asset_id is not None for s in _scenes(session, video)] == [True, True, True]


def test_redo_kit_only_rewrites_the_upload_notes(packaged):
    session, video, kit = packaged
    assert sendback.send_back(session, video, "kit") == Status.SHORTS_READY
    assert kit.exists() and video.video_metadata["kit_dir"] == str(kit)


def test_redo_scenes_replans_from_scratch(packaged):
    session, video, _ = packaged
    sendback.send_back(session, video, "scenes")
    session.commit()
    assert _scenes(session, video) == [] and video.status == Status.CHECKED


def test_bad_requests_are_refused(packaged):
    session, video, _ = packaged
    with pytest.raises(ValueError):
        sendback.send_back(session, video, "nonsense")
    with pytest.raises(ValueError):
        sendback.send_back(session, video, "images")  # gone: images come from the script's marks
    assert video.status == Status.PACKAGED


def test_review_menu_sends_back_to_the_chosen_stage(packaged, monkeypatch):
    session, video, _ = packaged
    monkeypatch.setattr(cli, "_open_in_default_player", lambda path: None)
    answers = iter(["b", str(list(sendback.REDO).index("pictures") + 1)])
    cli._review_one(session, video, input_fn=lambda prompt="": next(answers))
    session.commit()
    assert video.status == Status.RESEARCHED


def test_review_offers_failed_videos_a_send_back(packaged):
    session, video, _ = packaged
    video.status, video.error = Status.FAILED, "ValueError('checker cut the script')"
    answers = iter(["b", str(list(sendback.REDO).index("check") + 1)])
    cli._review_one(session, video, input_fn=lambda prompt="": next(answers))
    assert video.status == Status.SCRIPTED and video.error is None


# --------------------------------------------------------------------------- #
# S8 (bundle F): approve / title / uploaded, cleanup
# --------------------------------------------------------------------------- #
def test_approving_deletes_the_work_files(packaged, monkeypatch):
    session, video, _ = packaged
    monkeypatch.setattr(cli, "_open_in_default_player", lambda path: None)
    cli._review_one(session, video, input_fn=lambda prompt="": "a")
    assert video.status == Status.APPROVED and not work_dir(video.id, create=False).exists()


def test_switching_the_title_rewrites_upload_md(packaged, monkeypatch):
    session, video, kit = packaged
    monkeypatch.setattr(cli, "_open_in_default_player", lambda path: None)
    video.video_metadata = {**video.video_metadata, "title_candidates": ["Best title", "Other title"],
                            "publish": {"long": "2026-10-02T15:00:00-04:00", "shorts": ["2026-10-03T12:00:00-04:00"]}}
    answers = iter(["t", "2", "s"])
    cli._review_one(session, video, input_fn=lambda prompt="": next(answers))
    assert video.title == "Other title"
    assert (kit / "upload.md").read_text(encoding="utf-8").startswith("# Other title")


def test_an_approved_video_can_be_marked_uploaded(packaged):
    session, video, _ = packaged
    video.status = Status.APPROVED
    cli._review_one(session, video, input_fn=lambda prompt="": "u")
    assert video.status == Status.UPLOADED


def test_cleanup_keeps_what_is_needed_and_removes_the_rest(packaged, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from app import cleanup

    session, video, kit = packaged
    stray = output_root() / "7b0a3c2e-0000-4000-8000-000000000000"  # render folder of a deleted video
    stray.mkdir()
    old_image = Asset(kind="image", source="t", source_id="old", uri=str(output_root() / "old.jpg"))
    session.add(old_image)
    session.commit()

    now = datetime.now(UTC)
    cleanup.run(session, now=now)  # packaged: work + kit stay; stray render folder goes
    assert work_dir(video.id, create=False).exists() and kit.exists() and not stray.exists()

    video.status = Status.UPLOADED
    session.commit()
    cleanup.run(session, now=now)  # uploaded today: work goes, kit stays for keep_output_days
    assert not work_dir(video.id, create=False).exists() and kit.exists()

    later = now + timedelta(days=400)
    removed = cleanup.run(session, now=later)
    assert not kit.exists() and removed["images"] == 1  # the unused image, not the one scenes use
    assert session.query(Asset).count() == 1
