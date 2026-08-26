"""Local media handling: ffmpeg discovery, probing, muxing, thumbnails, filenames."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .errors import PreflightError

# Characters Windows forbids in a path component, plus control characters.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
# Names Windows reserves regardless of extension.
_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
# Leave room for the longest file we write inside a video folder.
_MAX_COMPONENT = 120


def safe_filename(name: str, *, max_length: int = _MAX_COMPONENT) -> str:
    """Make ``name`` safe as a single path component on every platform.

    Unlike yt-dlp's default sanitiser, this substitutes ASCII rather than fullwidth
    lookalikes — the original pipeline produced folder names containing ``：`` and
    ``｜``, which then leaked into upload logs and were painful to match on.
    """
    name = unicodedata.normalize("NFC", name).strip()
    name = _ILLEGAL.sub("-", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    if not name:
        return "untitled"
    if name.split(".")[0].upper() in _RESERVED:
        name = f"_{name}"
    if len(name) > max_length:
        name = name[:max_length].rstrip(" .-")
    return name or "untitled"


# -- toolchain discovery ---------------------------------------------------


def find_ffmpeg(override: Path | None = None) -> Path | None:
    """Locate ffmpeg. Explicit override wins, then PATH.

    Deliberately has no hardcoded fallback paths — the script this replaces carried
    an absolute path into one developer's Downloads folder, which is exactly the kind
    of thing that cannot ship.
    """
    if override:
        candidate = Path(override)
        if candidate.is_dir():
            for exe in ("ffmpeg", "ffmpeg.exe"):
                if (candidate / exe).exists():
                    return candidate / exe
        elif candidate.exists():
            return candidate
        raise PreflightError(
            f"ffmpeg not found at the path you gave: {override}",
            hint="Pass --ffmpeg-location pointing at the binary or its containing directory.",
        )
    found = shutil.which("ffmpeg")
    return Path(found) if found else None


def find_ffprobe(ffmpeg: Path | None = None) -> Path | None:
    """Locate ffprobe, preferring the one beside a known ffmpeg."""
    if ffmpeg:
        sibling = ffmpeg.parent / ("ffprobe.exe" if ffmpeg.suffix == ".exe" else "ffprobe")
        if sibling.exists():
            return sibling
    found = shutil.which("ffprobe")
    return Path(found) if found else None


def find_node() -> Path | None:
    """Locate node. Required for YouTube's signature/n challenges — without a JS
    runtime, yt-dlp cannot produce HD URLs and silently falls back to low quality."""
    found = shutil.which("node")
    return Path(found) if found else None


def tool_version(exe: Path, *args: str) -> str | None:
    try:
        # S603: argument list, never a shell; exe comes from PATH lookup or explicit
        # user config, and the arguments are literals.
        result = subprocess.run(  # noqa: S603
            [str(exe), *(args or ("-version",))],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0] if output else None


def node_major_version(node: Path) -> int | None:
    raw = tool_version(node, "--version")
    if not raw:
        return None
    match = re.search(r"v?(\d+)", raw)
    return int(match.group(1)) if match else None


# -- probing ---------------------------------------------------------------


@dataclass(frozen=True)
class MediaInfo:
    width: int | None = None
    height: int | None = None
    duration: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None

    @property
    def has_video(self) -> bool:
        return self.video_codec is not None

    @property
    def has_audio(self) -> bool:
        return self.audio_codec is not None

    @property
    def is_landscape(self) -> bool | None:
        if self.width is None or self.height is None:
            return None
        return self.width > self.height


def probe(path: Path, ffprobe: Path | None = None) -> MediaInfo:
    """Read stream properties with ffprobe. Returns empty info if unavailable."""
    exe = ffprobe or find_ffprobe()
    if not exe or not path.exists():
        return MediaInfo()
    try:
        result = subprocess.run(  # noqa: S603 - argument list, no shell
            [
                str(exe),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,width,height:format=duration",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            return MediaInfo()
        data = json.loads(result.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return MediaInfo()

    width = height = None
    video_codec = audio_codec = None
    for stream in data.get("streams", []):
        kind = stream.get("codec_type")
        if kind == "video" and video_codec is None:
            video_codec = stream.get("codec_name")
            width = stream.get("width")
            height = stream.get("height")
        elif kind == "audio" and audio_codec is None:
            audio_codec = stream.get("codec_name")

    duration = None
    raw_duration = (data.get("format") or {}).get("duration")
    if raw_duration is not None:
        try:
            duration = float(raw_duration)
        except (TypeError, ValueError):
            duration = None

    return MediaInfo(
        width=width,
        height=height,
        duration=duration,
        video_codec=video_codec,
        audio_codec=audio_codec,
    )


# -- muxing ----------------------------------------------------------------


def mux_streams(
    folder: Path,
    output: Path,
    *,
    ffmpeg: Path | None = None,
    ffprobe: Path | None = None,
) -> Path | None:
    """Combine leftover split video/audio streams into one file.

    yt-dlp normally merges DASH streams itself, but an interrupted or ffmpeg-less run
    can leave them separate. Partners are found by *probing* each candidate rather
    than matching a hardcoded format id — the previous implementation looked
    specifically for ``video.f251.webm`` and so could never repair a folder whose
    audio arrived in any other format.
    """
    if output.exists() and output.stat().st_size > 0:
        return output
    if any(folder.glob("*.part")):
        return None  # a download is still in flight

    ffmpeg = ffmpeg or find_ffmpeg()
    if not ffmpeg:
        return None
    ffprobe = ffprobe or find_ffprobe(ffmpeg)

    video_only: list[tuple[Path, MediaInfo]] = []
    audio_only: list[tuple[Path, MediaInfo]] = []
    for candidate in sorted(folder.iterdir()):
        if not candidate.is_file() or candidate.suffix.lower() not in {
            ".mp4",
            ".m4a",
            ".webm",
            ".mkv",
            ".opus",
            ".m4v",
        }:
            continue
        if candidate == output or candidate.stat().st_size == 0:
            continue
        info = probe(candidate, ffprobe)
        if info.has_video and not info.has_audio:
            video_only.append((candidate, info))
        elif info.has_audio and not info.has_video:
            audio_only.append((candidate, info))

    if not video_only or not audio_only:
        return None

    # Highest-resolution video, largest audio.
    video_path = max(video_only, key=lambda pair: (pair[1].height or 0, pair[0].stat().st_size))[0]
    audio_path = max(audio_only, key=lambda pair: pair[0].stat().st_size)[0]

    result = subprocess.run(  # noqa: S603 - argument list, no shell
        [
            str(ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            "-y",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and output.exists() and output.stat().st_size > 0:
        return output
    output.unlink(missing_ok=True)
    return None


# -- thumbnails ------------------------------------------------------------

_THUMB_SOURCES = (".webp", ".jpg", ".jpeg", ".png")


def convert_thumbnail(folder: Path, output: Path, *, quality: int = 90) -> Path | None:
    """Produce a JPEG thumbnail from whatever image the download left behind.

    Destinations reliably accept JPEG; WebP is inconsistently supported.
    """
    if output.exists() and output.stat().st_size > 0:
        return output

    candidates = [
        path
        for suffix in _THUMB_SOURCES
        for path in sorted(folder.glob(f"*{suffix}"))
        if path != output and path.is_file() and path.stat().st_size > 0
    ]
    if not candidates:
        return None
    # Prefer the largest — YouTube writes several sizes.
    source = max(candidates, key=lambda p: p.stat().st_size)

    try:
        from PIL import Image

        with Image.open(source) as image:
            image.convert("RGB").save(output, "JPEG", quality=quality, optimize=True)
    except Exception:  # noqa: BLE001 - a bad thumbnail must never fail a migration
        return None
    return output if output.exists() and output.stat().st_size > 0 else None


def human_size(num_bytes: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num_bytes) < 1024:
            return f"{num_bytes:.1f} {unit}" if unit != "B" else f"{int(num_bytes)} B"
        num_bytes /= 1024
    return f"{num_bytes:.1f} PB"


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
