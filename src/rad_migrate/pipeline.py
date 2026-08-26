"""The migration orchestrator.

Stages run in order, each idempotent and independently resumable:

``preflight → discover → reconcile → download → publish → verify → playlist``

Two properties matter more than anything else here:

**Re-running is safe.** Reconcile asks the *destination* what it already holds, so a
second run publishes nothing twice even on a machine with no local state at all.

**State advances one item at a time and is written immediately.** A crash, a Ctrl-C, or
a killed process loses at most the video in flight. There is no batch commit and no
end-of-run bookkeeping step that can be skipped.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from .config import Settings
from .dest.radtv import RadTVDestination
from .errors import (
    DestinationError,
    QualityError,
    RateLimitError,
    RunCancelledError,
    SourceError,
    TransientError,
)
from .media import human_size
from .models import RETRYABLE, DestinationContent, ItemState, SourceChannel, VideoItem
from .reporting import Reporter, null_reporter
from .source.youtube import YouTubeSource
from .state import Store

#: How long to wait for a transcode before moving on. The video is still published;
#: we simply stop watching and report it as pending.
_VERIFY_TIMEOUT = 900.0
_VERIFY_INTERVAL = 10.0

_T = TypeVar("_T")


def _require(value: _T | None, message: str) -> _T:
    """Assert an internal invariant.

    A real check rather than ``assert``, which ``python -O`` strips — turning a
    contract violation into an obscure ``NoneType`` crash much further downstream.
    """
    if value is None:
        raise DestinationError(message)
    return value


@dataclass
class MigrationPlan:
    """What a run would do, before it does it."""

    channel: SourceChannel
    dest_channel: str | None
    total_discovered: int
    already_present: list[VideoItem] = field(default_factory=list)
    filtered: list[VideoItem] = field(default_factory=list)
    to_migrate: list[VideoItem] = field(default_factory=list)
    #: Videos excluded only by --newest/--limit, not by any content rule. Counted
    #: separately because "filtered" reads as "rejected" and these were merely not
    #: reached this run.
    deselected: int = 0
    reconcile_degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": {
                "id": self.channel.id,
                "name": self.channel.name,
                "title": self.channel.title,
                "url": self.channel.url,
            },
            "destination_channel": self.dest_channel,
            "discovered": self.total_discovered,
            "already_present": len(self.already_present),
            "filtered": len(self.filtered),
            "not_selected": self.deselected,
            "to_migrate": len(self.to_migrate),
            "reconcile_degraded": self.reconcile_degraded,
            "items": [i.summary() for i in self.to_migrate],
        }


@dataclass
class MigrationResult:
    """What a run actually did."""

    channel: SourceChannel
    dest_channel: str | None
    attempted: int = 0
    published: int = 0
    verified: int = 0
    skipped: int = 0
    failed: int = 0
    playlist_id: str | None = None
    items: list[VideoItem] = field(default_factory=list)
    cancelled: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel.name,
            "destination_channel": self.dest_channel,
            "attempted": self.attempted,
            "published": self.published,
            "verified": self.verified,
            "skipped": self.skipped,
            "failed": self.failed,
            "playlist": self.playlist_id,
            "cancelled": self.cancelled,
            "items": [i.summary() for i in self.items],
        }


class Pipeline:
    """Drives a migration from a source channel to a destination channel."""

    def __init__(
        self,
        settings: Settings,
        reporter: Reporter | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self.reporter = reporter or null_reporter()
        self.cancel_check = cancel_check or (lambda: False)
        settings.ensure_workdir()
        self.store = Store(settings.state_path)
        self.source = YouTubeSource(settings, self.reporter, self.cancel_check)
        self._deselected = 0

    # -- helpers -----------------------------------------------------------

    def _check_cancelled(self) -> None:
        if self.cancel_check():
            raise RunCancelledError("Migration cancelled.")

    def _save(self, item: VideoItem) -> None:
        self.store.upsert(item)

    # -- planning ----------------------------------------------------------

    def plan(self, url: str, dest: RadTVDestination | None = None) -> MigrationPlan:
        """Work out what would migrate, touching no media."""
        with self.reporter.phase("Discover"):
            channel = self.source.resolve_channel(url)
            self.reporter.info(f"channel: {channel.title} ({channel.name})")
            discovered = self.source.discover(channel)
            self.reporter.info(f"{len(discovered)} videos on the channel")

        # Merge with anything we already know, so prior progress is preserved.
        self.store.add_if_absent(discovered)
        known = {i.source_id: i for i in self.store.all_items(channel.id)}
        items = [self._refresh(known.get(d.source_id), d) for d in discovered]

        dest_channel: str | None = None
        existing: list[DestinationContent] = []
        degraded = False
        if dest is not None:
            with self.reporter.phase("Reconcile"):
                dest_channel = dest.resolve_channel(
                    self.settings.channel,
                    create_as=channel.title if self.settings.channel is None else None,
                )
                existing = dest.list_content(dest_channel)
                if existing:
                    self.reporter.info(f"{len(existing)} items already on the Rad channel")
                else:
                    degraded = True
                    self.reporter.warn(
                        "Could not list existing Rad content — falling back to local state.",
                        hint=(
                            "Re-runs may re-publish videos. This needs a "
                            "list-channel-content API tool."
                        ),
                    )

        self._deselected = 0
        already, filtered, todo = self._partition(items, existing, degraded)
        return MigrationPlan(
            channel=channel,
            dest_channel=dest_channel,
            total_discovered=len(discovered),
            already_present=already,
            filtered=filtered,
            to_migrate=todo,
            deselected=self._deselected,
            reconcile_degraded=degraded,
        )

    @staticmethod
    def _refresh(known: VideoItem | None, fresh: VideoItem) -> VideoItem:
        """Carry forward stored progress, but take volatile fields from discovery.

        Position in particular shifts every time the channel publishes something new,
        so a stored position goes stale and would misorder selection on later runs.
        """
        if known is None:
            return fresh
        known.position = fresh.position
        if fresh.title:
            known.title = fresh.title
        if fresh.duration is not None:
            known.duration = fresh.duration
        return known

    def _partition(
        self,
        items: list[VideoItem],
        existing: list[DestinationContent],
        degraded: bool,
    ) -> tuple[list[VideoItem], list[VideoItem], list[VideoItem]]:
        """Split into already-done, filtered-out, and to-be-migrated."""
        already: list[VideoItem] = []
        filtered: list[VideoItem] = []
        todo: list[VideoItem] = []

        for item in items:
            match = next((c for c in existing if c.matches(item)), None)
            if match is not None:
                if item.state is not ItemState.VERIFIED:
                    item.state = ItemState.ALREADY_PRESENT
                    item.dest_id = match.id
                    self._save(item)
                already.append(item)
                continue
            # Without a destination listing, fall back to what we published locally.
            if degraded and item.state in {
                ItemState.VERIFIED,
                ItemState.PUBLISHED,
                ItemState.ALREADY_PRESENT,
            }:
                already.append(item)
                continue
            if item.state is ItemState.SKIPPED:
                filtered.append(item)
                continue
            reason = self._filter_reason(item)
            if reason:
                item.state = ItemState.SKIPPED
                item.skip_reason = reason
                self._save(item)
                filtered.append(item)
                continue
            todo.append(item)

        # Selection is newest-first by recency_rank, which trusts channel position
        # rather than dates — flat listings carry no dates to sort by.
        todo.sort(key=lambda i: i.recency_rank)
        if self.settings.newest is not None:
            self._deselected += len(todo[self.settings.newest :])
            todo = todo[: self.settings.newest]
        if self.settings.limit is not None:
            self._deselected += len(todo[self.settings.limit :])
            todo = todo[: self.settings.limit]
        todo.sort(key=lambda i: i.sort_key)  # publish oldest first
        return already, filtered, todo

    def _filter_reason(self, item: VideoItem) -> str | None:
        """Selection rules evaluable before download. Dimensions are checked later."""
        settings = self.settings
        if item.duration is not None:
            if settings.min_duration is not None and item.duration < settings.min_duration:
                return f"shorter than {settings.min_duration}s"
            if settings.max_duration is not None and item.duration > settings.max_duration:
                return f"longer than {settings.max_duration}s"
        # Only applies once a date is known. Flat channel listings carry none, so for
        # most items this is a no-op here and the check runs again in the source's
        # match_filter, after extraction but before any bytes move.
        if settings.since:
            day = item.upload_day
            if day is not None and day.isoformat() < settings.since:
                return f"uploaded before {settings.since}"
        if settings.landscape_only and item.is_landscape is False:
            return "vertical video"
        return None

    # -- execution ---------------------------------------------------------

    def run(self, url: str, dest: RadTVDestination) -> MigrationResult:
        """Migrate a channel end to end."""
        plan = self.plan(url, dest)
        result = MigrationResult(channel=plan.channel, dest_channel=plan.dest_channel)
        result.skipped = len(plan.filtered)

        if not plan.to_migrate:
            self.reporter.success(
                f"Nothing to do — {len(plan.already_present)} already on Rad, "
                f"{len(plan.filtered)} filtered out."
            )
            result.items = plan.already_present
            return result

        self.reporter.step(
            f"{len(plan.to_migrate)} to migrate "
            f"({len(plan.already_present)} already there, {len(plan.filtered)} filtered)"
        )

        dest_channel = _require(plan.dest_channel, "No destination channel was resolved.")
        published_ids: list[tuple[VideoItem, str]] = []

        with self.reporter.phase("Migrate"):
            for index, item in enumerate(plan.to_migrate, start=1):
                try:
                    self._check_cancelled()
                except RunCancelledError:
                    result.cancelled = True
                    break

                label = f"[{index}/{len(plan.to_migrate)}] {item.title[:60] or item.source_id}"
                self.reporter.step(label)
                result.attempted += 1
                try:
                    self._migrate_one(item, dest, dest_channel)
                except RunCancelledError:
                    result.cancelled = True
                    break
                except Exception as exc:  # noqa: BLE001 - one bad video must not end the run
                    self._record_failure(item, exc)
                    result.failed += 1
                    self.reporter.error(f"{item.title[:60]}: {_brief(exc)}")
                    continue

                if item.state is ItemState.SKIPPED:
                    result.skipped += 1
                    self.reporter.detail(f"skipped — {item.skip_reason}")
                elif item.state in {ItemState.PUBLISHED, ItemState.VERIFIED}:
                    result.published += 1
                    if item.state is ItemState.VERIFIED:
                        result.verified += 1
                    if item.dest_id:
                        published_ids.append((item, item.dest_id))
                result.items.append(item)

        if self.settings.playlist and published_ids and not result.cancelled:
            result.playlist_id = self._build_playlist(dest, plan, published_ids)

        self._summarise(result)
        return result

    def _migrate_one(self, item: VideoItem, dest: RadTVDestination, channel_id: str) -> None:
        """Advance one video as far as it can go, saving after every transition."""
        item.attempts += 1

        if item.state.needs_download:
            self._download(item)
            if item.state is ItemState.SKIPPED:
                self._save(item)
                return

        self._check_cancelled()

        if item.state.needs_publish:
            self._publish(item, dest, channel_id)

        if item.state is ItemState.PUBLISHED and item.dest_id:
            self._verify(item, dest)

        if (
            self.settings.cleanup_after_publish
            and item.state is ItemState.VERIFIED
            and item.video_path
        ):
            self._cleanup(item)

    def _download(self, item: VideoItem) -> None:
        media_dir = self.settings.media_dir / item.channel_id
        media_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.source.download(item, media_dir)
        except RateLimitError:
            self.source.pacer.on_rate_limit()
            self.source.download(item, media_dir)  # one retry after backing off
        if item.state is ItemState.DOWNLOADED:
            size = item.video_path.stat().st_size if item.video_path else 0
            self.reporter.detail(
                f"downloaded {item.width or '?'}x{item.height or '?'} · {human_size(size)}"
            )
            self.source.pacer.on_success()
        self._save(item)

    def _publish(self, item: VideoItem, dest: RadTVDestination, channel_id: str) -> None:
        target = dest.create_content(item, channel_id)
        item.dest_id = target.content_id
        self._save(item)  # persist before uploading, so a crash leaves no orphan

        auto_finalize = self._upload_with_progress(dest, target.video_upload_url, item)

        if item.thumbnail_path and target.thumbnail_upload_url:
            try:
                dest.upload_asset(target.thumbnail_upload_url, item.thumbnail_path)
            except DestinationError as exc:
                # A missing thumbnail is cosmetic; never fail a video over it.
                self.reporter.detail(f"thumbnail upload failed: {_brief(exc)}")

        try:
            dest.finalize(target.content_id, item)
        except DestinationError as exc:
            if not auto_finalize:
                raise
            self.reporter.detail(
                f"finalize errored but the server reported autoFinalize — treating as "
                f"published ({_brief(exc)})"
            )

        item.state = ItemState.PUBLISHED
        item.last_error = None
        self._save(item)

    def _upload_with_progress(self, dest: RadTVDestination, url: str, item: VideoItem) -> bool:
        video_path = _require(item.video_path, f"No local video file for {item.title!r}.")
        total = video_path.stat().st_size
        last = 0.0

        def on_progress(sent: int, size: int) -> None:
            nonlocal last
            now = time.monotonic()
            if now - last < 2.0 and sent < size:
                return
            last = now
            self.reporter.event(
                "upload_progress",
                source_id=item.source_id,
                sent=sent,
                total=size,
                percent=round(100 * sent / size, 1) if size else None,
            )

        self.reporter.detail(f"uploading {human_size(total)}")
        response = dest.upload_asset(url, video_path, on_progress=on_progress)
        data = response.get("data") if isinstance(response.get("data"), dict) else response
        return bool(isinstance(data, dict) and data.get("autoFinalize"))

    def _verify(self, item: VideoItem, dest: RadTVDestination) -> None:
        """Watch the transcode to a terminal state.

        Publishing is not the same as playable — the original pipeline reported
        success on upload and only later discovered failed transcodes.
        """
        dest_id = _require(item.dest_id, f"No destination id recorded for {item.title!r}.")
        deadline = time.monotonic() + _VERIFY_TIMEOUT
        while time.monotonic() < deadline:
            self._check_cancelled()
            status = dest.processing_status(dest_id)
            item.dest_job_id = status.job_id or item.dest_job_id
            if status.is_complete:
                item.state = ItemState.VERIFIED
                item.last_error = None
                self._save(item)
                self.reporter.success(f"published and processed: {item.title[:60]}")
                return
            if status.is_failed:
                item.state = ItemState.FAILED_PROCESSING
                item.last_error = status.error or "transcode failed"
                self._save(item)
                self.reporter.error(
                    f"transcode failed for {item.title[:60]}"
                    + (f" (job {status.job_id})" if status.job_id else ""),
                    hint="Retry with: rad-migrate retry --failed",
                )
                return
            time.sleep(_VERIFY_INTERVAL)

        self.reporter.detail("still transcoding — leaving it to finish server-side")
        self._save(item)

    def _cleanup(self, item: VideoItem) -> None:
        import shutil

        if item.video_path is None:
            return
        folder = item.video_path.parent
        try:
            shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            return
        item.video_path = None
        item.thumbnail_path = None
        item.subtitle_path = None
        self._save(item)

    def _record_failure(self, item: VideoItem, exc: Exception) -> None:
        if isinstance(exc, QualityError):
            item.state = ItemState.FAILED_QUALITY
        elif isinstance(exc, SourceError):
            item.state = ItemState.FAILED_DOWNLOAD
        elif item.state.needs_publish or item.state is ItemState.DOWNLOADED:
            item.state = ItemState.FAILED_PUBLISH
        else:
            item.state = ItemState.FAILED_DOWNLOAD
        item.last_error = _brief(exc)
        self._save(item)

    def _build_playlist(
        self,
        dest: RadTVDestination,
        plan: MigrationPlan,
        published: list[tuple[VideoItem, str]],
    ) -> str | None:
        """Rebuild the channel's order on the destination.

        Sorted by the precise upload timestamp where available, so videos published
        on the same day keep their original relative order.
        """
        channel_id = _require(plan.dest_channel, "No destination channel was resolved.")
        ordered = sorted(published, key=lambda pair: pair[0].sort_key)
        title = self.settings.playlist or plan.channel.title
        try:
            playlist_id = dest.create_playlist(channel_id, title, [cid for _, cid in ordered])
        except DestinationError as exc:
            self.reporter.warn(f"Could not create the playlist: {_brief(exc)}")
            return None
        if playlist_id:
            self.reporter.success(f"playlist {title!r} created with {len(ordered)} videos")
        return playlist_id

    def _summarise(self, result: MigrationResult) -> None:
        parts = [f"{result.published} published"]
        if result.verified != result.published:
            parts.append(f"{result.verified} confirmed processed")
        if result.skipped:
            parts.append(f"{result.skipped} skipped")
        if result.failed:
            parts.append(f"{result.failed} failed")
        message = ", ".join(parts)
        if result.cancelled:
            self.reporter.warn(f"Cancelled — {message}. Re-run to resume.")
        elif result.failed:
            self.reporter.warn(f"Done with errors — {message}.")
            self.reporter.info("Retry the failures with: rad-migrate retry --failed")
        else:
            self.reporter.success(f"Done — {message}.")

    # -- retry -------------------------------------------------------------

    def reset_failures(self, channel_id: str) -> int:
        """Move failed items back to a state the next run will pick up."""
        return self.store.reset_states(channel_id, sorted(RETRYABLE, key=lambda s: s.value))


def _brief(exc: Exception) -> str:
    from .errors import RadMigrateError

    if isinstance(exc, RadMigrateError):
        return str(exc)[:300]
    if isinstance(exc, TransientError):
        return f"temporary: {exc}"[:300]
    text = str(exc).strip() or exc.__class__.__name__
    return text.splitlines()[0][:300]
