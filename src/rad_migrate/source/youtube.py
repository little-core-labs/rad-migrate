"""YouTube source, built on yt-dlp's Python API.

Most of what is non-obvious here was learned the hard way and is preserved with its
reasoning intact:

* **A JS runtime is mandatory for HD.** YouTube's signature/n challenges must be
  solved to get HD URLs. Without node, yt-dlp silently degrades to low quality rather
  than failing, so :mod:`rad_migrate.doctor` checks for it up front.
* **The throttle is on the player API, not on bytes.** yt-dlp queries several player
  clients per video by default, so a handful of videos can exhaust a session's budget
  while barely downloading anything. When we detect throttling we collapse to a single
  client, which multiplies how many videos a session survives.
* **HLS often works when DASH does not.** From a flagged IP, YouTube will 403 the DASH
  HD streams but still serve HD over ``m3u8_native``. The HD selectors therefore keep
  an HLS rung on the ladder.
* **Fail closed on quality.** Under ``hd-strict`` a video that cannot be had in HD is
  *failed*, not saved in standard definition. Failures are never recorded as done, so
  the next run — with a fresher session — retries them automatically.

Option names are validated against the installed yt-dlp at startup. An earlier script
passed four option names that yt-dlp does not recognise (``sleep_interval_min`` and
friends); yt-dlp ignores unknown keys silently, so its advertised rate limiting never
ran and nobody noticed. :func:`validate_ydl_opts` makes that class of bug loud.
"""

from __future__ import annotations

import random
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yt_dlp
from yt_dlp.utils import DownloadError, ExtractorError

from .. import potoken
from ..config import QualityStrategy, Settings
from ..errors import QualityError, RateLimitError, RunCancelledError, SourceError
from ..media import find_ffmpeg, find_node, probe, safe_filename
from ..models import ItemState, SourceChannel, VideoItem
from ..reporting import Reporter, null_reporter
from .base import QualityProbe

#: Substrings that mean "YouTube is throttling or challenging us", as opposed to an
#: ordinary per-video failure like a private or removed video.
_RATE_LIMIT_SIGNS = (
    "sign in to confirm",
    "confirm you're not a bot",
    "rate-limited",
    "too many requests",
    "http error 429",
    "this helps protect our community",
    "unable to download api page",
    "failed to extract any player response",
)

#: Errors that are this video's problem and will never succeed on retry.
_PERMANENT_SIGNS = (
    "private video",
    "video unavailable",
    "removed by the uploader",
    "account associated with this video has been terminated",
    "this video has been removed",
    "members-only",
    "join this channel",
    "video is age restricted",
    "requested format is not available",
)

#: A single player client makes roughly one player-API call per video instead of
#: several. Used once we detect throttling.
_ECONOMY_PLAYER_CLIENTS = ["web_safari"]

_HANDLE = re.compile(r"^@[\w.-]{2,}$")
_CHANNEL_ID = re.compile(r"^UC[\w-]{20,}$")


# -- option validation -----------------------------------------------------


def _known_ydl_opts() -> set[str]:
    try:
        return set(yt_dlp.parse_options([]).ydl_opts)
    except Exception:  # noqa: BLE001 - defensive; yt-dlp internals may move
        return set()


def validate_ydl_opts(opts: dict[str, Any], reporter: Reporter | None = None) -> list[str]:
    """Warn about option names the installed yt-dlp will silently ignore."""
    known = _known_ydl_opts()
    if not known:
        return []
    # Keys we set that are legitimately absent from yt-dlp's defaults dict.
    allowed_extra = {"progress_hooks", "postprocessor_hooks", "match_filter", "paths", "outtmpl"}
    unknown = sorted(k for k in opts if k not in known and k not in allowed_extra)
    if unknown and reporter:
        reporter.warn(
            f"yt-dlp does not recognise these options and will ignore them: {', '.join(unknown)}",
            hint="This is a rad-migrate bug — please report it with your yt-dlp version.",
        )
    return unknown


# -- URL handling ----------------------------------------------------------


def normalize_channel_url(raw: str) -> str:
    """Accept the many ways people refer to a channel; return a videos-tab URL.

    Handles ``@handle``, a bare handle, ``youtube.com/@handle``, ``/c/Name``,
    ``/user/Name``, ``/channel/UC...``, and any of those with an existing tab suffix.
    """
    value = raw.strip().rstrip("/")
    if not value:
        raise SourceError("No channel given.", hint="Pass a channel URL or @handle.")

    if _HANDLE.match(value):
        return f"https://www.youtube.com/{value}/videos"
    if _CHANNEL_ID.match(value):
        return f"https://www.youtube.com/channel/{value}/videos"
    if not value.startswith(("http://", "https://")):
        if "/" not in value and not value.startswith("@"):
            return f"https://www.youtube.com/@{value}/videos"
        value = f"https://{value}"

    # Strip any existing tab so we always land on /videos.
    value = re.sub(r"/(videos|shorts|streams|featured|playlists|community)$", "", value)
    if "/watch" in value or "list=" in value:
        raise SourceError(
            "That looks like a video or playlist URL, not a channel.",
            hint="Pass the channel itself, e.g. https://youtube.com/@yourchannel",
        )
    return f"{value}/videos"


# -- pacing ----------------------------------------------------------------


class Pacer:
    """Adaptive rate-limit handling.

    Starts out of the way and only becomes cautious once YouTube pushes back:
    exponential backoff on throttling, capped, with an optional fixed break between
    batches. The predecessor slept 60–75 minutes on every rate-limit hit with no cap
    and no way to interrupt it; this backs off proportionally and stays responsive to
    Ctrl-C.
    """

    def __init__(
        self,
        settings: Settings,
        reporter: Reporter,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.reporter = reporter
        self.cancel_check = cancel_check
        self.throttled = False
        self.consecutive_limits = 0
        self.completed = 0

    @property
    def player_clients(self) -> list[str] | None:
        """Collapse to one player client once we know we are being throttled."""
        return _ECONOMY_PLAYER_CLIENTS if self.throttled else None

    def sleep(self, seconds: float, reason: str) -> None:
        """Interruptible sleep with progress, so a long backoff is not a black box."""
        if seconds <= 0:
            return
        self.reporter.detail(f"{reason} — waiting {_duration(seconds)}")
        self.reporter.event("waiting", seconds=round(seconds), reason=reason)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.cancel_check and self.cancel_check():
                raise RunCancelledError("Cancelled while waiting out a rate limit.")
            time.sleep(min(1.0, deadline - time.monotonic()))

    def on_rate_limit(self) -> None:
        """Back off after YouTube throttles us."""
        self.throttled = True
        self.consecutive_limits += 1
        base = min(
            self.settings.max_backoff_seconds,
            120 * (2 ** (self.consecutive_limits - 1)),
        )
        wait = base * random.uniform(0.8, 1.2)  # noqa: S311 - jitter, not crypto
        wait = min(wait, self.settings.max_backoff_seconds)
        if self.consecutive_limits == 1:
            self.reporter.warn(
                "YouTube is throttling this session; backing off and reducing "
                "player-API calls per video."
            )
        self.sleep(wait, f"rate limited ({self.consecutive_limits}x)")

    def on_success(self) -> None:
        self.completed += 1
        self.consecutive_limits = 0
        every = self.settings.break_every
        if every and self.completed % every == 0:
            self.sleep(self.settings.break_seconds, f"scheduled break after {self.completed}")


def _duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def classify_error(message: str) -> type[SourceError]:
    lowered = message.lower()
    if any(sign in lowered for sign in _RATE_LIMIT_SIGNS):
        return RateLimitError
    return SourceError


def is_permanent(message: str) -> bool:
    lowered = message.lower()
    return any(sign in lowered for sign in _PERMANENT_SIGNS)


# -- format selection ------------------------------------------------------


def build_format(strategy: QualityStrategy, min_height: int) -> str:
    """Compose a yt-dlp format selector.

    ``hd-strict`` has no rung below ``min_height`` on purpose: if HD cannot be had,
    the download fails and gets retried later rather than quietly saving SD.
    """
    mp4_pair = "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
    any_pair = "bestvideo+bestaudio"

    if strategy is QualityStrategy.BEST:
        return f"{mp4_pair}/{any_pair}/best"

    hd = (
        f"bestvideo[height>={min_height}][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo[height>={min_height}]+bestaudio/"
        # HLS rung: from a flagged IP the DASH HD streams 403 but m3u8 still serves HD.
        f"best[protocol=m3u8_native][height>={min_height}]/"
        f"best[height>={min_height}]"
    )
    if strategy is QualityStrategy.HD_STRICT:
        return hd
    return f"{hd}/{mp4_pair}/{any_pair}/best"


# -- the source ------------------------------------------------------------


class YouTubeSource:
    """Pulls channels and videos from YouTube."""

    name = "youtube"

    def __init__(
        self,
        settings: Settings,
        reporter: Reporter | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.reporter = reporter or null_reporter()
        self.pacer = Pacer(settings, self.reporter, cancel_check)
        self._validated = False

    # -- option construction ----------------------------------------------

    def _cookie_opts(self) -> dict[str, Any]:
        settings = self.settings
        if settings.cookies_file:
            return {"cookiefile": str(settings.cookies_file)}
        if settings.cookies_from_browser:
            spec = settings.cookies_from_browser
            browser, _, profile = spec.partition(":")
            # yt-dlp wants (browser, profile, keyring, container)
            return {"cookiesfrombrowser": (browser.lower(), profile or None, None, None)}
        return {}

    def _base_opts(self, **overrides: Any) -> dict[str, Any]:
        ffmpeg = find_ffmpeg(self.settings.ffmpeg_location)
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "consoletitle": False,
            # Nothing from yt-dlp may reach stdout: under `rad-migrate mcp` that stream
            # is the JSON-RPC transport, and one stray line corrupts the protocol.
            "logtostderr": True,
            "ignoreerrors": False,  # we handle failures per item, with state
            "retries": 3,
            "fragment_retries": 10,
            "socket_timeout": 60,
            # Pace requests, not bytes: the budget being conserved is player-API calls.
            "sleep_interval_requests": 2,
            "sleep_interval": 3,
            "max_sleep_interval": 10,
        }
        if ffmpeg:
            opts["ffmpeg_location"] = str(ffmpeg)
        # A JS runtime is what makes HD URLs resolvable at all.
        if find_node():
            opts["js_runtimes"] = {"node": {"path": None}}
        opts.update(self._cookie_opts())

        extractor_args: dict[str, dict[str, list[str]]] = {}
        clients = self.pacer.player_clients
        if clients:
            extractor_args["youtube"] = {"player_client": clients}
        # Point the PO token provider straight at our binary. It would also find it
        # via PATH, but being explicit survives a PATH that gets rewritten under us.
        binary = potoken.binary_path()
        if binary.exists():
            extractor_args["youtubepot-bgutilcli"] = {"cli_path": [str(binary)]}
        if extractor_args:
            opts["extractor_args"] = extractor_args

        opts.update(overrides)
        if not self._validated:
            validate_ydl_opts(opts, self.reporter)
            self._validated = True
        return opts

    # -- channel -----------------------------------------------------------

    def resolve_channel(self, url: str) -> SourceChannel:
        target = normalize_channel_url(url)
        opts = self._base_opts(extract_flat=True, skip_download=True, playlist_items="1")
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=False)
        except (DownloadError, ExtractorError) as exc:
            message = str(exc)
            raise classify_error(message)(
                f"Could not read that channel: {message}",
                hint=_channel_hint(message),
            ) from exc
        if not info:
            raise SourceError(f"No channel found at {target}")

        channel_id = info.get("channel_id") or info.get("uploader_id") or info.get("id") or ""
        name = (
            info.get("uploader_id")
            or info.get("channel")
            or info.get("uploader")
            or info.get("title")
            or channel_id
        )
        return SourceChannel(
            id=channel_id or safe_filename(str(name)),
            name=str(name).lstrip("@"),
            title=str(info.get("channel") or info.get("title") or name),
            url=target,
            description=info.get("description") or "",
            video_count=info.get("playlist_count"),
        )

    def discover(self, channel: SourceChannel) -> list[VideoItem]:
        """Enumerate the channel cheaply — one flat listing, no per-video calls."""
        opts = self._base_opts(extract_flat="in_playlist", skip_download=True)
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(channel.url, download=False)
        except (DownloadError, ExtractorError) as exc:
            message = str(exc)
            raise classify_error(message)(
                f"Could not list videos for {channel.name}: {message}",
                hint=_channel_hint(message),
            ) from exc

        entries = (info or {}).get("entries") or []
        items: list[VideoItem] = []
        position = 0
        for entry in entries:
            if not entry or not entry.get("id"):
                continue
            duration = entry.get("duration")
            items.append(
                VideoItem(
                    source=self.name,
                    source_id=str(entry["id"]),
                    channel_id=channel.id,
                    url=entry.get("url") or f"https://www.youtube.com/watch?v={entry['id']}",
                    title=str(entry.get("title") or "").strip(),
                    description=entry.get("description") or "",
                    duration=int(duration) if isinstance(duration, (int, float)) else None,
                    # Flat listings carry no dates — YouTube returns timestamp=None
                    # for every entry — so position is what ordering relies on.
                    timestamp=entry.get("timestamp"),
                    upload_date=entry.get("upload_date"),
                    width=entry.get("width"),
                    height=entry.get("height"),
                    position=position,
                )
            )
            position += 1
        return items

    # -- quality probe -----------------------------------------------------

    def probe_quality(self, channel: SourceChannel | None = None) -> QualityProbe:
        """Ask what resolution we can actually get, before committing to a run.

        Probes a real video from the target channel when one is available, because
        availability is per-video as well as per-session.
        """
        target: str | None = None
        if channel is not None:
            try:
                opts = self._base_opts(
                    extract_flat="in_playlist", skip_download=True, playlist_items="1"
                )
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(channel.url, download=False)
                entries = (info or {}).get("entries") or []
                if entries and entries[0].get("id"):
                    target = f"https://www.youtube.com/watch?v={entries[0]['id']}"
            except Exception:  # noqa: BLE001 - probing must never be fatal
                target = None

        source_desc = None
        if self.settings.cookies_file:
            source_desc = str(self.settings.cookies_file)
        elif self.settings.cookies_from_browser:
            source_desc = self.settings.cookies_from_browser

        if target is None:
            return QualityProbe(
                max_height=None,
                error="no video available to probe",
                used_cookies=bool(source_desc),
                cookie_source=source_desc,
            )

        try:
            opts = self._base_opts(skip_download=True)
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(target, download=False)
        except (DownloadError, ExtractorError) as exc:
            return QualityProbe(
                max_height=None,
                error=str(exc).strip().splitlines()[0] if str(exc) else "extraction failed",
                used_cookies=bool(source_desc),
                cookie_source=source_desc,
            )

        formats = (info or {}).get("formats") or []
        heights = [f.get("height") for f in formats if isinstance(f.get("height"), int)]
        return QualityProbe(
            max_height=max(heights) if heights else None,
            formats_seen=len(formats),
            used_cookies=bool(source_desc),
            cookie_source=source_desc,
        )

    # -- download ----------------------------------------------------------

    def _match_filter(self, info: dict[str, Any], *, incomplete: bool = False) -> str | None:
        """Reject videos before any bytes move.

        Runs after extraction but before download, so filtering costs no bandwidth
        and no extra player-API calls.
        """
        settings = self.settings
        duration = info.get("duration")
        if isinstance(duration, (int, float)):
            if settings.min_duration is not None and duration < settings.min_duration:
                return f"shorter than {settings.min_duration}s"
            if settings.max_duration is not None and duration > settings.max_duration:
                return f"longer than {settings.max_duration}s"
        if settings.landscape_only:
            width, height = info.get("width"), info.get("height")
            if isinstance(width, int) and isinstance(height, int) and width <= height:
                return "vertical video"
        # Date filtering has to happen here rather than at discovery: YouTube's flat
        # channel listing returns no upload dates, so this is the first point at which
        # a real date exists — and it still costs no bandwidth.
        if settings.since:
            upload_date = info.get("upload_date")
            if isinstance(upload_date, str) and len(upload_date) == 8:
                iso = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
                if iso < settings.since:
                    return f"uploaded before {settings.since}"
        return None

    def download(self, item: VideoItem, target_dir: Path) -> VideoItem:
        """Fetch one video plus thumbnail, subtitles, and metadata."""
        # Reuse the folder from a previous attempt. The name embeds the upload date,
        # which is unknown until the first successful extraction, so recomputing it
        # after a partial download would abandon the bytes already on disk.
        folder = Path(item.folder) if item.folder else target_dir / item.folder_name()
        folder.mkdir(parents=True, exist_ok=True)
        item.folder = folder

        opts = self._base_opts(
            # Templating the folder into outtmpl would break on any title containing
            # '%'; `paths.home` sidesteps the template language entirely.
            paths={"home": str(folder)},
            outtmpl={"default": "video.%(ext)s"},
            format=build_format(self.settings.quality, self.settings.effective_min_height),
            merge_output_format="mp4",
            writeinfojson=True,
            writethumbnail=True,
            writesubtitles=True,
            writeautomaticsub=True,
            subtitleslangs=["en.*"],
            subtitlesformat="vtt/best",
            match_filter=self._match_filter,
            overwrites=False,
            continuedl=True,
        )

        url = item.url or f"https://www.youtube.com/watch?v={item.source_id}"
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except (DownloadError, ExtractorError) as exc:
            message = str(exc).strip()
            if is_permanent(message):
                item.state = ItemState.SKIPPED
                item.skip_reason = _tidy(message)
                return item
            error_cls = classify_error(message)
            if error_cls is RateLimitError:
                raise RateLimitError(_tidy(message)) from exc
            raise SourceError(_tidy(message)) from exc

        if info is None:
            # match_filter rejected it.
            item.state = ItemState.SKIPPED
            item.skip_reason = self._match_filter({}) or "filtered out"
            return item

        reason = self._match_filter(info)
        if reason:
            item.state = ItemState.SKIPPED
            item.skip_reason = reason
            return item

        self._absorb_metadata(item, info)
        video_path = self._locate_video(info, folder)
        if video_path is None:
            raise SourceError("Download finished but no video file was produced.")

        self._enforce_quality(item, video_path)
        item.video_path = video_path
        item.thumbnail_path = self._prepare_thumbnail(folder)
        item.subtitle_path = next(iter(sorted(folder.glob("video*.vtt"))), None)
        self._name_folder_now_that_the_date_is_known(item, target_dir)
        item.state = ItemState.DOWNLOADED
        item.last_error = None
        return item

    @staticmethod
    def _name_folder_now_that_the_date_is_known(item: VideoItem, target_dir: Path) -> None:
        """Rename the download folder once the upload date is available.

        The folder has to be created before extraction, when the date is still
        unknown, so it starts out prefixed ``unknown-date``. Renaming here gives the
        date-sorted layout the naming scheme exists for. Purely cosmetic: if the
        rename fails — a file still open, a name collision — the existing folder is
        kept and the recorded paths stay valid.
        """
        if item.folder is None or item.upload_day is None:
            return
        current = Path(item.folder)
        desired = target_dir / item.folder_name()
        if current == desired or desired.exists():
            return
        try:
            current.rename(desired)
        except OSError:
            return

        item.folder = desired
        for attr in ("video_path", "thumbnail_path", "subtitle_path"):
            path = getattr(item, attr)
            if path is not None:
                setattr(item, attr, desired / Path(path).name)

    # -- download helpers --------------------------------------------------

    @staticmethod
    def _absorb_metadata(item: VideoItem, info: dict[str, Any]) -> None:
        item.title = str(info.get("title") or item.title).strip()
        item.description = info.get("description") or item.description
        item.upload_date = info.get("upload_date") or item.upload_date
        item.timestamp = info.get("timestamp") or item.timestamp
        duration = info.get("duration")
        if isinstance(duration, (int, float)):
            item.duration = int(duration)
        tags = info.get("tags")
        if isinstance(tags, list):
            item.tags = [str(t) for t in tags][:50]

    @staticmethod
    def _locate_video(info: dict[str, Any], folder: Path) -> Path | None:
        for requested in info.get("requested_downloads") or []:
            raw = requested.get("filepath") or requested.get("_filename")
            if raw:
                candidate = Path(raw)
                if candidate.exists() and candidate.stat().st_size > 0:
                    return candidate
        media = [
            path
            for path in folder.glob("video.*")
            if path.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}
            and path.is_file()
            and path.stat().st_size > 0
        ]
        return max(media, key=lambda p: p.stat().st_size) if media else None

    def _enforce_quality(self, item: VideoItem, video_path: Path) -> None:
        """Verify what actually landed on disk, not what we asked for.

        The format selector expresses intent; this checks reality. A file below the
        bar is deleted and the item failed, so it is retried on a later, healthier
        session instead of being published in the wrong resolution.
        """
        info = probe(video_path)
        if info.width:
            item.width = info.width
        if info.height:
            item.height = info.height

        minimum = self.settings.effective_min_height
        if minimum and info.height is not None and info.height < minimum:
            video_path.unlink(missing_ok=True)
            item.video_path = None
            raise QualityError(
                f"Got {info.height}p but need at least {minimum}p — discarded, will retry.",
                hint=(
                    "Usually a degraded YouTube session. Try --cookies-from-browser "
                    "chrome, or wait and re-run; rad-migrate picks up where it left off."
                ),
            )

    @staticmethod
    def _prepare_thumbnail(folder: Path) -> Path | None:
        from ..media import convert_thumbnail

        return convert_thumbnail(folder, folder / "thumbnail.jpg")


def _tidy(message: str) -> str:
    """Collapse yt-dlp's multi-line errors to one useful line."""
    cleaned = re.sub(r"^ERROR:\s*", "", message.strip(), flags=re.IGNORECASE)
    first = cleaned.splitlines()[0] if cleaned else "unknown error"
    return first[:400]


def _channel_hint(message: str) -> str | None:
    lowered = message.lower()
    if any(sign in lowered for sign in _RATE_LIMIT_SIGNS):
        return (
            "YouTube wants to verify this session. Pass --cookies-from-browser chrome "
            "(or firefox/edge) so rad-migrate can use your signed-in session."
        )
    if "not found" in lowered or "does not exist" in lowered:
        return "Check the handle. It should look like @yourchannel."
    return None
