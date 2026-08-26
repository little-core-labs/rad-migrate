"""Rad TV destination.

Speaks JSON-RPC 2.0 to Rad's MCP endpoint over HTTPS.

**Structured first, scraped second.** Several tools currently answer with human-readable
prose, so the content id and ingestion URLs have to be recovered with regexes. That is
fragile, and the fix belongs on the API side. This client therefore always looks for a
structured payload first and only falls back to scraping — so the day the API returns
proper JSON, it starts using it with no change here, and the regexes become dead code.

Known upstream quirks handled here, learned in production:

* **Finalize is flaky, but often unnecessary.** When the video upload responds with
  ``autoFinalize: true`` the server publishes on its own, so a failing finalize call
  after that is cosmetic and must not fail the video.
* **Transient truncated responses** ("Response ended prematurely") create orphan
  content records with no asset. Retrying the *whole* create step would multiply those,
  so create is retried only when nothing was created.
* **Release dates must be in the past**, which :meth:`VideoItem.release_iso` enforces.
"""

from __future__ import annotations

import mimetypes
import re
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from types import TracebackType
from typing import IO, Any, cast

import httpx

from ..config import Settings
from ..errors import AuthError, DestinationError, TransientError
from ..models import DestinationContent, ProcessingStatus, PublishTarget, VideoItem
from ..redact import redact
from ..reporting import Reporter, null_reporter
from .sse import SSEParseError, ToolCallError, extract_tool_text, parse_response

ProgressCallback = Callable[[int, int], None]

_CONTENT_ID = re.compile(r"did:rad\.live:content/feature/[\w-]+")
_CHANNEL_ID = re.compile(r"did:rad\.live:channel/[\w-]+")
#: Playlists come back as `did:rad.live:content/playlist/<uuid>` from the create call
#: but as `did:rad.live:playlist/<uuid>` from the dashboard. Match either.
_PLAYLIST_ID = re.compile(r"did:rad\.live:(?:content/)?playlist/[\w-]+")
_INGESTION_URL = re.compile(r"https://[\w.-]*rad\.live/ingestion/[^\s\"'\\)<>]+")

#: Upstream messages that mean "try again", as opposed to "this will never work".
_TRANSIENT_SIGNS = (
    "response ended prematurely",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "connection reset",
    "too many requests",
)

_AUTH_SIGNS = ("unauthorized", "forbidden", "invalid api key", "authentication", "401", "403")

#: "No transcode job found" means not started yet, not broken.
_NO_JOB = re.compile(r"no transcode job|job not found|no job found", re.IGNORECASE)

_UPLOAD_CHUNK = 1024 * 1024


class RadTVDestination:
    """Client for Rad TV's MCP API."""

    name = "radtv"

    def __init__(
        self,
        settings: Settings,
        reporter: Reporter | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.reporter = reporter or null_reporter()
        self._token = settings.require_api_key()
        self._endpoint = f"{settings.api_url}/mcp"
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(120.0, connect=20.0, read=120.0),
            follow_redirects=True,
        )
        self._request_id = 0

    # -- lifecycle ---------------------------------------------------------

    def __enter__(self) -> RadTVDestination:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # -- transport ---------------------------------------------------------

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "rad-migrate",
        }

    def call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
        *,
        retries: int = 3,
        timeout: float | None = None,
    ) -> str:
        """Invoke an MCP tool and return its text result."""
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments or {}},
        }

        last: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = self._client.post(
                    self._endpoint,
                    headers=self._headers,
                    json=payload,
                    timeout=timeout or self._client.timeout,
                )
                if response.status_code in (401, 403):
                    raise AuthError(
                        f"Rad rejected the API key ({response.status_code}).",
                        hint="Check RAD_API_KEY. Keys are per-account.",
                    )
                response.raise_for_status()
                return extract_tool_text(parse_response(response.text))
            except AuthError:
                raise
            except (httpx.HTTPError, SSEParseError, ToolCallError) as exc:
                last = exc
                if not self._should_retry(exc) or attempt == retries:
                    break
                backoff = min(30.0, 2.0**attempt)
                self.reporter.detail(
                    f"{tool} failed ({_brief(exc)}); retrying in {backoff:.0f}s "
                    f"[{attempt}/{retries - 1}]"
                )
                time.sleep(backoff)

        message = f"{tool} failed: {_brief(last) if last else 'unknown error'}"
        if last is not None and self._should_retry(last):
            raise TransientError(message) from last
        raise DestinationError(message) from last

    @staticmethod
    def _should_retry(exc: Exception) -> bool:
        if isinstance(exc, httpx.TimeoutException | httpx.NetworkError | httpx.RemoteProtocolError):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code >= 500 or exc.response.status_code == 429
        text = str(exc).lower()
        if any(sign in text for sign in _AUTH_SIGNS):
            return False
        return any(sign in text for sign in _TRANSIENT_SIGNS)

    # -- identity ----------------------------------------------------------

    def identity(self) -> dict[str, Any]:
        """Everything ``verify_connection`` knows: user, channel, plan, permissions."""
        data = _maybe_json(self.call("verify_connection", retries=2))
        return dict(data) if isinstance(data, dict) else {}

    def verify(self) -> str:
        """Confirm the key works. Returns a human-readable identity."""
        data = self.identity()
        raw_user = data.get("user")
        raw_channel = data.get("channel")
        user: dict[str, Any] = raw_user if isinstance(raw_user, dict) else {}
        channel: dict[str, Any] = raw_channel if isinstance(raw_channel, dict) else {}
        username = user.get("username") or user.get("email") or ""
        channel_name = channel.get("name") or ""
        if username and channel_name:
            return f"{username} → {channel_name}"
        return str(username or channel_name or "connected")[:120]

    # -- channels ----------------------------------------------------------

    def resolve_channel(self, name_or_id: str | None, *, create_as: str | None = None) -> str:
        """Find the channel to publish into, creating one only if asked."""
        if name_or_id and _CHANNEL_ID.fullmatch(name_or_id.strip()):
            return name_or_id.strip()

        channels = self.list_channels()
        if name_or_id:
            wanted = name_or_id.strip().casefold()
            for channel_id, title in channels:
                if wanted in (channel_id.casefold(), title.casefold()):
                    return channel_id
            if create_as is None:
                raise DestinationError(
                    f"No Rad channel matching {name_or_id!r}.",
                    hint=(
                        "Available: "
                        + (", ".join(t for _, t in channels) if channels else "none")
                        + ". Pass --create-channel to make a new one."
                    ),
                )
        elif len(channels) == 1:
            return channels[0][0]
        elif len(channels) > 1 and create_as is None:
            raise DestinationError(
                "Your account has more than one channel, so rad-migrate cannot guess.",
                hint="Pass --channel with one of: " + ", ".join(t for _, t in channels),
            )

        if create_as:
            return self.create_channel(create_as)
        raise DestinationError(
            "No Rad channel available to publish into.",
            hint="Create one in Rad, or pass --create-channel to have rad-migrate make it.",
        )

    def list_channels(self) -> list[tuple[str, str]]:
        """(id, title) for the channel this key publishes to.

        Deliberately *not* the ``list_channels`` tool, which enumerates every channel
        on the platform rather than the ones this key owns. ``verify_connection``
        names the authenticated user's own channel, which is what we actually want.
        """
        data = self.identity()
        raw_channel = data.get("channel")
        channel: dict[str, Any] = raw_channel if isinstance(raw_channel, dict) else {}
        identifier = channel.get("id")
        if isinstance(identifier, str) and identifier.startswith("did:rad.live:channel/"):
            return [(identifier, str(channel.get("name") or identifier))]
        return []

    def create_channel(self, title: str) -> str:
        for tool in ("create_channel", "create_new_channel"):
            try:
                text = self.call(tool, {"title": title, "name": title}, retries=2)
            except DestinationError:
                continue
            match = _CHANNEL_ID.search(text)
            if match:
                self.reporter.success(f"Created Rad channel {title!r}")
                return match.group(0)
        raise DestinationError(
            f"Could not create a channel named {title!r}.",
            hint="Create it in the Rad web app, then re-run with --channel.",
        )

    # -- reconcile ---------------------------------------------------------

    #: channel_dashboard caps at 100 items, so beyond that the listing is partial.
    CONTENT_LIMIT = 100

    def list_content(self, channel_id: str) -> list[DestinationContent]:
        """Everything already on the channel — the basis for idempotent re-runs.

        ``channel_dashboard`` is the only listing tool available and it is capped at
        100 items, so on a larger catalogue this is incomplete. When the API says
        there is more, we say so loudly rather than silently under-reporting and
        letting a re-run republish.
        """
        args: dict[str, Any] = {"contentLimit": self.CONTENT_LIMIT}
        if channel_id:
            args["channel"] = channel_id
        try:
            text = self.call("channel_dashboard", args, retries=2)
        except DestinationError as exc:
            self.reporter.detail(f"content listing unavailable — {_brief(exc)}")
            return []

        data = _maybe_json(text)
        if isinstance(data, dict) and data.get("hasMoreContent"):
            self.reporter.warn(
                f"The Rad channel holds more than {self.CONTENT_LIMIT} items, which is "
                "the most the API will list.",
                hint=(
                    "Videos beyond that cannot be checked for duplicates and may be "
                    "republished. A paginated content-list endpoint would fix this."
                ),
            )
        return list(self._parse_content(text))

    @staticmethod
    def _parse_content(text: str) -> Iterator[DestinationContent]:
        data = _maybe_json(text)
        seen: set[str] = set()
        for record in _walk_records(data):
            identifier = _first_str(record, ("contentId", "content", "id", "did"))
            if not identifier or "did:rad.live:content" not in identifier:
                continue
            if identifier in seen:
                continue
            seen.add(identifier)
            yield DestinationContent(
                id=identifier,
                title=_first_str(record, ("title", "name")) or "",
                # Not currently returned by this API; kept so the match upgrades
                # automatically if provenance stamping is ever added.
                external_id=_first_str(record, ("externalId", "external_id", "sourceId")),
                source_url=_first_str(record, ("sourceUrl", "source_url", "originalUrl")),
                release_date=_first_str(
                    record, ("releaseDate", "release_date", "publishedAt", "released")
                ),
                status=_first_str(record, ("processingStatus", "status", "state")),
            )
        if not seen:
            for match in _CONTENT_ID.finditer(text):
                identifier = match.group(0)
                if identifier not in seen:
                    seen.add(identifier)
                    yield DestinationContent(id=identifier)

    # -- publish -----------------------------------------------------------

    def create_content(
        self, item: VideoItem, channel_id: str, *, publish: bool = True
    ) -> PublishTarget:
        """Reserve a content record and collect its ingestion URLs.

        ``publish=False`` creates it as a draft — useful for verifying the pipeline
        without exposing anything to a channel's followers.
        """
        if item.video_path is None or not item.video_path.exists():
            raise DestinationError(f"No local video file for {item.title!r}.")

        arguments: dict[str, Any] = {
            "title": item.title or item.source_id,
            "summary": _summary_for(item),
            "releaseDate": item.release_iso(),
            "filename": item.video_path.name,
            # Required. The API warns that uploads without a size may be corrupted.
            "size": item.video_path.stat().st_size,
            "publish": publish,
            "channel": channel_id,
            # Portrait video published as LANDSCAPE displays wrong, and the source is
            # the only place that knows the real aspect.
            "layout": "PORTRAIT" if item.is_landscape is False else "LANDSCAPE",
        }
        # NB: deliberately no `sourceUrl`. On this API that field means "download the
        # video from this URL yourself" and switches publish_video into server-side
        # fetch mode — it is not a provenance stamp. Passing the YouTube URL there
        # would make Rad re-download from YouTube and ignore the file we just fetched.
        if item.thumbnail_path and item.thumbnail_path.exists():
            arguments["thumbnailFilename"] = item.thumbnail_path.name
            arguments["thumbnailSize"] = item.thumbnail_path.stat().st_size

        # Not retried: a truncated response may still have created a record, and
        # retrying would leave orphans behind. The pipeline retries at item level.
        text = self.call("publish_video", arguments, retries=1)
        return self._parse_publish(text, want_thumbnail="thumbnailFilename" in arguments)

    @staticmethod
    def _parse_publish(text: str, *, want_thumbnail: bool) -> PublishTarget:
        data = _maybe_json(text)
        content_id: str | None = None
        video_url: str | None = None
        thumb_url: str | None = None

        # Structured form, if the API provides one.
        for record in _walk_records(data):
            content_id = content_id or _first_str(record, ("contentId", "content", "id"))
            ingestion = record.get("ingestion") if isinstance(record, dict) else None
            if isinstance(ingestion, dict):
                video = ingestion.get("video")
                thumb = ingestion.get("thumbnail")
                if isinstance(video, dict):
                    video_url = video_url or _first_str(video, ("url", "uploadUrl"))
                elif isinstance(video, str):
                    video_url = video_url or video
                if isinstance(thumb, dict):
                    thumb_url = thumb_url or _first_str(thumb, ("url", "uploadUrl"))
                elif isinstance(thumb, str):
                    thumb_url = thumb_url or thumb
            if isinstance(record, dict):
                video_url = video_url or _first_str(record, ("uploadUrl", "videoUploadUrl"))
                thumb_url = thumb_url or _first_str(record, ("thumbnailUploadUrl",))

        # Prose fallback. Convention: the video URL is emitted before the thumbnail's.
        if not content_id:
            match = _CONTENT_ID.search(text)
            content_id = match.group(0) if match else None
        if not video_url:
            urls = _INGESTION_URL.findall(text)
            if urls:
                video_url = urls[0]
                if want_thumbnail and len(urls) > 1:
                    thumb_url = thumb_url or urls[1]

        if not content_id or not video_url:
            raise DestinationError(
                "Could not read the content id and upload URL from Rad's response.",
                hint=f"Response began: {redact(text.strip()[:240])}",
            )
        return PublishTarget(
            content_id=content_id,
            video_upload_url=video_url,
            thumbnail_upload_url=thumb_url,
        )

    def upload_asset(
        self,
        url: str,
        path: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, Any]:
        """Send one file to an ingestion endpoint.

        The endpoint expects ``multipart/form-data`` with the file in a ``file``
        field — not a raw body. httpx builds the multipart envelope from a file
        object, so progress is reported by wrapping the reader rather than by
        generating the body ourselves.
        """
        size = path.stat().st_size
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"

        try:
            with path.open("rb") as handle:
                reader = _ProgressReader(handle, size, on_progress)
                response = self._client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._token}",
                        "User-Agent": "rad-migrate",
                    },
                    files={"file": (path.name, cast("IO[bytes]", reader), content_type)},
                    timeout=httpx.Timeout(3600.0, connect=30.0, write=3600.0, read=600.0),
                )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            body = redact(exc.response.text[:300])
            raise _classify_upload_error(exc.response.status_code, body) from exc
        except httpx.HTTPError as exc:
            raise TransientError(f"Upload of {path.name} failed: {_brief(exc)}") from exc

        parsed = _maybe_json(response.text)
        return parsed if isinstance(parsed, dict) else {"raw": response.text[:500]}

    def unpublish(self, content_id: str) -> bool:
        """Make content non-public again.

        This is as far as the API goes — there is no delete, so the record itself
        remains on the channel, unreleased and ACL-protected.
        """
        self.call("unpublish_content", {"id": content_id}, retries=2)
        return True

    def finalize(self, content_id: str, item: VideoItem) -> bool:
        """Mark the upload complete. Returns whether the server confirmed publishing."""
        text = self.call(
            "finalize_content_upload",
            {"contentId": content_id, "releaseDate": item.release_iso(), "publish": True},
            retries=2,
        )
        data = _maybe_json(text)
        if isinstance(data, dict):
            for record in _walk_records(data):
                if isinstance(record, dict) and "published" in record:
                    return bool(record["published"])
        return "publish" in text.lower()

    def processing_status(self, content_id: str) -> ProcessingStatus:
        """Where the transcode has got to.

        A missing job is reported as ``pending``, not raised. There is a real window
        after upload where the content exists but the transcode job has not been
        created yet, and drafts do not get a job at all until they are published —
        treating either as a failure would condemn a perfectly good video.
        """
        try:
            text = self.call("get_processing_status", {"contentId": content_id}, retries=2)
        except DestinationError as exc:
            if _NO_JOB.search(str(exc)):
                return ProcessingStatus(content_id=content_id, status="pending")
            raise
        data = _maybe_json(text)

        status = None
        job_id = None
        progress = None
        error = None
        for record in _walk_records(data):
            status = status or _first_str(record, ("status", "state"))
            job = record.get("job")
            if isinstance(job, dict):
                job_id = job_id or _stringify(job.get("id"))
                status = _first_str(job, ("status",)) or status
                raw_progress = job.get("progress")
                if isinstance(raw_progress, (int, float)):
                    progress = float(raw_progress)
            error = error or _first_str(record, ("error", "message"))

        if status is None:
            lowered = text.lower()
            if "complete" in lowered:
                status = "complete"
            elif "error" in lowered or "failed" in lowered:
                status = "failed"
            else:
                status = "processing"

        return ProcessingStatus(
            content_id=content_id,
            status=status,
            job_id=job_id,
            progress=progress,
            error=error,
        )

    def create_playlist(self, channel_id: str, title: str, content_ids: list[str]) -> str | None:
        if not content_ids:
            return None
        # Not retried: a retry on a truncated response creates a second playlist
        # rather than recovering the first.
        text = self.call(
            "create_playlist_with_items",
            {
                "title": title,
                "summary": f"{title} — migrated from YouTube by rad-migrate.",
                "contentIds": content_ids,
                "channel": channel_id,
            },
            retries=1,
        )
        data = _maybe_json(text)
        for record in _walk_records(data):
            identifier = _first_str(record, ("id", "playlistId"))
            if identifier and "playlist/" in identifier:
                return identifier
        match = _PLAYLIST_ID.search(text)
        if match:
            return match.group(0)
        # A playlist may well have been created even though we cannot name it, so
        # say so rather than reporting a clean failure the caller might retry.
        self.reporter.warn(
            "Created a playlist but could not read its id back from the response.",
            hint=f"Check the channel for a playlist titled {title!r}.",
        )
        return None


# -- helpers ---------------------------------------------------------------


class _ProgressReader:
    """File wrapper that reports progress as httpx reads the multipart body."""

    def __init__(self, handle: IO[bytes], size: int, on_progress: ProgressCallback | None) -> None:
        self._handle = handle
        self._size = size
        self._on_progress = on_progress
        self._sent = 0

    def read(self, amount: int = -1) -> bytes:
        chunk: bytes = self._handle.read(amount)
        if chunk:
            self._sent += len(chunk)
            if self._on_progress:
                self._on_progress(min(self._sent, self._size), self._size)
        return chunk

    def seek(self, offset: int, whence: int = 0) -> int:
        # httpx rewinds the stream when it retries; progress must rewind with it.
        position: int = self._handle.seek(offset, whence)
        self._sent = position
        return position

    def tell(self) -> int:
        position: int = self._handle.tell()
        return position


def _summary_for(item: VideoItem) -> str:
    """Rad summaries are shorter than YouTube descriptions; trim on a boundary."""
    text = (item.description or "").strip()
    if len(text) <= 4000:
        return text
    cut = text[:4000]
    boundary = max(cut.rfind("\n"), cut.rfind(". "))
    return (cut[:boundary] if boundary > 2000 else cut).rstrip() + "…"


def _maybe_json(text: str) -> Any:
    import json

    stripped = (text or "").strip()
    if not stripped:
        return None
    if stripped.startswith(("{", "[")):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    # Some tools wrap JSON in prose; recover the first embedded object.
    start = stripped.find("{")
    if start != -1:
        try:
            value, _ = json.JSONDecoder().raw_decode(stripped[start:])
            return value
        except json.JSONDecodeError:
            pass
    return None


def _walk_records(data: Any, _depth: int = 0) -> Iterator[dict[str, Any]]:
    """Yield every dict in a nested structure, outermost first."""
    if _depth > 8:
        return
    if isinstance(data, dict):
        yield data
        for value in data.values():
            if isinstance(value, (dict, list)):
                yield from _walk_records(value, _depth + 1)
    elif isinstance(data, list):
        for value in data:
            if isinstance(value, (dict, list)):
                yield from _walk_records(value, _depth + 1)


def _first_str(record: Any, keys: tuple[str, ...]) -> str | None:
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        text = _stringify(value)
        if text:
            return text
    return None


def _stringify(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _classify_upload_error(status_code: int, body: str) -> DestinationError:
    if status_code in (401, 403):
        return AuthError(f"Rad rejected the upload ({status_code}): {body}")
    if status_code == 404 or "not found in database" in body.lower():
        # Seen in production: the ingestion key goes missing between create and upload.
        return TransientError(
            f"Rad could not find the upload target ({status_code}): {body}",
        )
    if status_code >= 500 or status_code == 429:
        return TransientError(f"Rad upload failed ({status_code}): {body}")
    return DestinationError(f"Rad upload failed ({status_code}): {body}")


def _brief(exc: Exception | None) -> str:
    if exc is None:
        return "unknown error"
    text = redact(str(exc)).strip()
    return (text.splitlines()[0] if text else exc.__class__.__name__)[:300]
