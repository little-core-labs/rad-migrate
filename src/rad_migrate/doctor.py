"""Preflight checks.

The failure this module exists to prevent is the expensive one: discovering six hours
into a migration that every file came down at 360p because a JS runtime was missing or
the session was degraded. Everything here runs in seconds and answers, concretely,
"will this work, and if not, what is the one command that fixes it?"
"""

from __future__ import annotations

import platform
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from . import auth
from .config import Settings
from .media import find_ffmpeg, find_ffprobe, find_node, node_major_version, tool_version
from .models import SourceChannel

#: yt-dlp needs a modern JS runtime to solve YouTube's n-challenge. Older node works
#: for some videos and mysteriously fails others, which is worse than not having it.
MIN_NODE_MAJOR = 20
RECOMMENDED_NODE_MAJOR = 22


class Level(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class Check:
    name: str
    level: Level
    detail: str
    hint: str | None = None
    command: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "level": self.level.value,
            "detail": self.detail,
            "hint": self.hint,
            "command": self.command,
        }


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.level is Level.FAIL]

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.level is Level.WARN]

    @property
    def ok(self) -> bool:
        return not self.failures

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "checks": [c.to_dict() for c in self.checks],
            "failures": len(self.failures),
            "warnings": len(self.warnings),
        }


# -- individual checks -----------------------------------------------------


def check_python() -> Check:
    version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return Check("python", Level.OK, version)


def check_node() -> Check:
    node = find_node()
    if node is None:
        return Check(
            "node",
            Level.FAIL,
            "not found",
            hint=(
                "YouTube requires a JavaScript runtime to hand over HD streams. "
                "Without node, downloads silently fall back to low quality."
            ),
            command="Install Node.js 22+ from https://nodejs.org",
        )
    major = node_major_version(node)
    if major is None:
        return Check("node", Level.WARN, f"found at {node}, version unknown")
    if major < MIN_NODE_MAJOR:
        return Check(
            "node",
            Level.FAIL,
            f"v{major} is too old",
            hint=f"Node {RECOMMENDED_NODE_MAJOR}+ is needed for reliable HD.",
            command="Install Node.js 22+ from https://nodejs.org",
        )
    level = Level.OK if major >= RECOMMENDED_NODE_MAJOR else Level.WARN
    return Check("node", level, f"v{major}")


def check_ffmpeg(settings: Settings) -> list[Check]:
    checks: list[Check] = []
    ffmpeg = find_ffmpeg(settings.ffmpeg_location)
    if ffmpeg is None:
        checks.append(
            Check(
                "ffmpeg",
                Level.FAIL,
                "not found",
                hint="Needed to merge YouTube's separate video and audio streams.",
                command=_ffmpeg_install_command(),
            )
        )
    else:
        raw = tool_version(ffmpeg) or ""
        version = raw.split(" ")[2] if raw.startswith("ffmpeg version") else "present"
        checks.append(Check("ffmpeg", Level.OK, version))

    if find_ffprobe(ffmpeg) is None:
        checks.append(
            Check(
                "ffprobe",
                Level.WARN,
                "not found",
                hint=(
                    "Without ffprobe, rad-migrate cannot verify that a download really "
                    "is HD — the quality guard is skipped."
                ),
                command=_ffmpeg_install_command(),
            )
        )
    return checks


def _ffmpeg_install_command() -> str:
    # platform.system() rather than sys.platform: mypy narrows sys.platform to the
    # host it is checking on and marks the other branches unreachable.
    system = platform.system()
    if system == "Windows":
        return "winget install Gyan.FFmpeg"
    if system == "Darwin":
        return "brew install ffmpeg"
    return "sudo apt install ffmpeg   # or your distro's equivalent"


def check_ytdlp() -> Check:
    try:
        import yt_dlp

        version = yt_dlp.version.__version__
    except Exception as exc:  # noqa: BLE001 - report rather than crash the doctor
        return Check("yt-dlp", Level.FAIL, f"not importable: {exc}")
    return Check("yt-dlp", Level.OK, version)


def check_potoken() -> Check:
    status = auth.potoken_status()
    if status.available:
        return Check("proof-of-origin token", Level.OK, status.describe())
    return Check(
        "proof-of-origin token",
        Level.WARN,
        status.describe(),
        hint=(
            "Not needed unless YouTube caps your quality. If it does, this fixes it "
            "without signing in to anything."
        ),
        command="rad-migrate setup-potoken",
    )


def check_cookies(settings: Settings) -> Check:
    if settings.cookies_file:
        exists = settings.cookies_file.exists()
        return Check(
            "cookies",
            Level.OK if exists else Level.FAIL,
            f"file {settings.cookies_file}" + ("" if exists else " (missing)"),
        )
    if settings.cookies_from_browser:
        name = settings.cookies_from_browser.split(":", 1)[0].lower()
        blocked = [b for b in auth.detect_browsers() if b.name == name and not b.readable]
        if blocked:
            return Check(
                "cookies",
                Level.FAIL,
                f"{name}: {blocked[0].note}",
                hint="Use Firefox, or export a cookies.txt and pass --cookies-file.",
            )
        return Check("cookies", Level.OK, f"from {settings.cookies_from_browser}")

    usable = [b.name for b in auth.detect_browsers() if b.usable]
    detail = "not using cookies"
    hint = "Most channels do not need them. rad-migrate will say so if yours does."
    if usable:
        hint += f" Available if needed: {', '.join(usable)}."
    return Check("cookies", Level.OK, detail, hint=hint)


def check_disk(settings: Settings) -> Check:
    target = settings.workdir
    probe = target
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        usage = shutil.disk_usage(probe)
    except OSError as exc:
        return Check("disk space", Level.WARN, f"could not determine: {exc}")

    free_gb = usage.free / 1_073_741_824
    detail = f"{free_gb:.1f} GB free at {target}"
    if free_gb < 5:
        return Check(
            "disk space",
            Level.FAIL,
            detail,
            hint="A channel migration can need tens of gigabytes.",
            command="Free up space, or point --workdir at a bigger drive.",
        )
    if free_gb < 25:
        return Check(
            "disk space",
            Level.WARN,
            detail,
            hint="Consider --cleanup-after-publish to delete local files once published.",
        )
    return Check("disk space", Level.OK, detail)


def check_destination(settings: Settings) -> Check:
    """Verify the Rad credentials before anything expensive happens."""
    if settings.api_key is None:
        return Check(
            "rad auth",
            Level.FAIL,
            "no API key",
            hint="rad-migrate cannot publish without one.",
            command="export RAD_API_KEY=...   (or put it in .env)",
        )
    from .dest.radtv import RadTVDestination

    try:
        with RadTVDestination(settings) as dest:
            identity = dest.verify()
    except Exception as exc:  # noqa: BLE001 - surface as a check, not a traceback
        return Check("rad auth", Level.FAIL, str(exc)[:200])
    return Check("rad auth", Level.OK, identity)


def check_quality(settings: Settings, channel: SourceChannel | None) -> Check:
    """Ask YouTube what it will actually serve, and say what to do if it is not enough."""
    from .source.youtube import YouTubeSource

    source = YouTubeSource(settings)
    probe = source.probe_quality(channel)
    minimum = settings.effective_min_height

    if not probe.ok:
        return Check(
            "quality probe",
            Level.WARN,
            probe.describe(),
            hint="Could not confirm available quality; the migration may still work.",
        )
    if probe.meets(minimum):
        return Check("quality probe", Level.OK, probe.describe())

    advice = auth.recommend(
        current_height=probe.max_height,
        min_height=minimum,
        using_cookies=settings.cookie_source.value in {"browser", "file"},
    )
    return Check(
        "quality probe",
        Level.FAIL,
        f"{probe.describe()} — below the {minimum}p minimum",
        hint=advice.headline if advice else None,
        command=advice.command if advice else None,
    )


# -- orchestration ---------------------------------------------------------


def run(
    settings: Settings,
    *,
    channel: SourceChannel | None = None,
    check_dest: bool = True,
    check_source_quality: bool = False,
) -> Report:
    """Run the checks. Network-touching checks are opt-in so ``doctor`` stays fast."""
    report = Report()
    report.add(check_python())
    report.add(check_ytdlp())
    report.add(check_node())
    for check in check_ffmpeg(settings):
        report.add(check)
    report.add(check_potoken())
    report.add(check_cookies(settings))
    report.add(check_disk(settings))
    if check_dest:
        report.add(check_destination(settings))
    if check_source_quality:
        report.add(check_quality(settings, channel))
    return report
