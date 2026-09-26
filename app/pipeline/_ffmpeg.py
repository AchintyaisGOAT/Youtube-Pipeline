"""Thin FFmpeg/FFprobe wrappers shared by assemble.py and shorts.py — kept in one place
so encoder selection and error handling stay consistent (WORK_MEDIA.md §5: "never call
FFmpeg... directly without going through your own thin wrapper"). Leading-underscore
module name: internal to Media's stages, not part of any cross-team contract.
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
#: works. Arial ships on every Windows install (DESIGN.md locks this project to
#: Windows), so it's a safe universal fallback, not a guess.
_SYSTEM_FONT_FALLBACK = r"C:\Windows\Fonts\arial.ttf"


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
    """Probe for a *working* h264_nvenc once per process, fall back to libx264
    (config's `encoder: auto`, WORK_MEDIA.md §5).

    Checking `ffmpeg -encoders` only proves the build was compiled with NVENC
    support — it says nothing about whether the installed NVIDIA driver actually
    supports the NVENC API version this FFmpeg build needs. Verified live: a real
    RTX 4060 with a driver one version-family behind (596.49, needs 610+) lists
    h264_nvenc as an available encoder and then fails at first use with "Driver does
    not support the required nvenc API version." So this actually tries a trivial
    real encode instead of trusting the capability list.
    """
    global _encoder_cache
    if _encoder_cache is None:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "color=black:s=64x64:d=0.1",
                "-c:v", "h264_nvenc", "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
        )
        _encoder_cache = "h264_nvenc" if result.returncode == 0 else "libx264"
    return _encoder_cache


def video_codec_args(config: ChannelConfig) -> list[str]:
    """`-pix_fmt yuv420p` is not optional here. Verified live: without it, libx264
    just inherits whatever chroma subsampling the source JPEGs decode to -- one
    real render came out `yuvj422p` / High 4:2:2 Profile, which played in ffprobe
    fine but wouldn't open in Windows' own video player. 4:2:0 is what every consumer
    player (and YouTube's own upload spec) actually expects."""
    encoder = pick_encoder() if config.video.encoder == "auto" else config.video.encoder
    if encoder == "h264_nvenc":
        return ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "23", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p"]


def drawtext_font_file() -> str:
    """A real font *file* path for `drawtext`'s `fontfile=` option -- never rely on
    `drawtext`'s family-name fontconfig lookup, which isn't configured on this ffmpeg
    build (see `_SYSTEM_FONT_FALLBACK`). Prefers assets/fonts/ (config.subtitles'
    font_file lives there) so a real deployment picks up the channel's actual brand
    font; falls back to Arial only when that folder is empty (e.g. this test)."""
    global _font_file_cache
    if _font_file_cache is None:
        candidates = sorted(Path(get_settings().assets_dir).glob("fonts/*.ttf"))
        _font_file_cache = str(candidates[0]) if candidates else _SYSTEM_FONT_FALLBACK
    return _font_file_cache


