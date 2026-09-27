"""Thin FFmpeg/FFprobe wrappers shared by the assemble and Shorts stages — kept in one
place so encoder selection and error handling stay consistent. FFmpeg is called directly
(README §3.1: no wrapper library).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.config import ChannelConfig, get_settings

_encoder_cache: str | None = None
_font_file_cache: str | None = None

#: Fallback when assets/fonts/ is empty. This Gyan.dev ffmpeg build's `drawtext` filter
#: hard-depends on fontconfig for family-name lookups (unset here, family-name mode
#: fails with "Fontconfig error: Cannot load default config file") -- passing an
#: explicit `fontfile=` bypasses fontconfig entirely and only needs FreeType, which
#: works. Arial ships on every Windows install, so it's a safe fallback, not a guess.
_SYSTEM_FONT_FALLBACK = r"C:\Windows\Fonts\arial.ttf"


def probe(path: Path) -> dict:
    """Run ffprobe and return its parsed JSON — the sanity check after every render step."""
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def run_ffmpeg(args: list[str], *, cwd: Path | None = None) -> None:
    result = subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr[-4000:]}")


def pick_encoder() -> str:
    """Probe for a *working* Intel Quick Sync encoder once per process, else libx264
    (config's `encoder: auto`, README §6.4).

    `ffmpeg -encoders` listing h264_qsv only proves the build supports it, not that this
    machine's GPU/driver does — so this tries a trivial real encode instead of trusting
    the capability list (the same lesson learned with NVENC on another machine: listed,
    then failed at first use).
    """
    global _encoder_cache
    if _encoder_cache is None:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
                "-c:v", "h264_qsv", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
        )
        _encoder_cache = "h264_qsv" if result.returncode == 0 else "libx264"
    return _encoder_cache


def video_codec_args(config: ChannelConfig) -> list[str]:
    """Always 8-bit 4:2:0. Verified live: without an explicit pixel format, libx264
    inherits whatever chroma subsampling the source JPEGs decode to -- one real render
    came out `yuvj422p` / High 4:2:2, which wouldn't open in Windows' own player. QSV
    takes `nv12` (its native 4:2:0 layout). `-color_range tv` matters too: archival JPEGs
    are full-range, and without it a QSV render came out tagged `yuvj420p` (verified);
    with it FFmpeg converts to the standard limited range players and YouTube expect."""
    encoder = pick_encoder() if config.video.encoder == "auto" else config.video.encoder
    if encoder == "h264_qsv":
        return ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "23", "-pix_fmt", "nv12",
                "-color_range", "tv"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-color_range", "tv"]


def drawtext_font_file() -> str:
    """A real font *file* path for `drawtext`'s `fontfile=` option -- never rely on
    `drawtext`'s family-name fontconfig lookup, which isn't configured on this ffmpeg
    build (see `_SYSTEM_FONT_FALLBACK`). Prefers assets/fonts/ so a real deployment picks
    up the channel's brand font; falls back to Arial only when that folder is empty."""
    global _font_file_cache
    if _font_file_cache is None:
        candidates = sorted(Path(get_settings().assets_dir).glob("fonts/*.ttf"))
        _font_file_cache = str(candidates[0]) if candidates else _SYSTEM_FONT_FALLBACK
    return _font_file_cache


