"""Thin FFmpeg/FFprobe wrappers shared by assemble.py and shorts.py — kept in one place
so encoder selection and error handling stay consistent (WORK_MEDIA.md §5: "never call
FFmpeg... directly without going through your own thin wrapper"). Leading-underscore
module name: internal to Media's stages, not part of any cross-team contract.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.config import ChannelConfig

_encoder_cache: str | None = None


def probe(path: Path) -> dict:
    """Run ffprobe and return its parsed JSON — the sanity check DESIGN.md #12 wants
    after every render stage."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def run_ffmpeg(args: list[str]) -> None:
    result = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args], capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-4000:]}")


def pick_encoder() -> str:
    """Probe for h264_nvenc once per process, fall back to libx264 (config's
    `encoder: auto`, WORK_MEDIA.md §5)."""
    global _encoder_cache
    if _encoder_cache is None:
        result = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True)
        _encoder_cache = "h264_nvenc" if "h264_nvenc" in result.stdout else "libx264"
    return _encoder_cache


def video_codec_args(config: ChannelConfig) -> list[str]:
    encoder = pick_encoder() if config.video.encoder == "auto" else config.video.encoder
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20"]


def ffmpeg_escape_path(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filtergraph string (e.g. `ass=...`,
    `subtitles=...`) — `:` is a filter-option separator, so a Windows drive letter
    needs escaping or ffmpeg misparses the filter."""
    return path.as_posix().replace(":", "\\:")
