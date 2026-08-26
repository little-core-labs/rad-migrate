"""Domain models shared by sources, destinations, and the pipeline."""

from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ItemState(str, Enum):
    """Per-video position in the pipeline.

    Terminal-success is ``VERIFIED``. ``PUBLISHED`` means the destination accepted
    the bytes but transcoding has not finished, which is not the same thing — the
    original scripts conflated these and reported success for videos that later
    failed to transcode.
    """

    DISCOVERED = "discovered"
    SKIPPED = "skipped"
    ALREADY_PRESENT = "already_present"
    DOWNLOADED = "downloaded"
    PUBLISHED = "published"
    VERIFIED = "verified"
    FAILED_DOWNLOAD = "failed_download"
    FAILED_QUALITY = "failed_quality"
    FAILED_PUBLISH = "failed_publish"
    FAILED_PROCESSING = "failed_processing"

    @property
    def is_failure(self) -> bool:
        return self.value.startswith("failed_")

    @property
    def is_terminal_success(self) -> bool:
        return self is ItemState.VERIFIED

    @property
    def needs_download(self) -> bool:
        return self in _NEEDS_DOWNLOAD

    @property
    def needs_publish(self) -> bool:
        return self in _NEEDS_PUBLISH


_NEEDS_DOWNLOAD = frozenset(
    {ItemState.DISCOVERED, ItemState.FAILED_DOWNLOAD, ItemState.FAILED_QUALITY}
)
_NEEDS_PUBLISH = frozenset({ItemState.DOWNLOADED, ItemState.FAILED_PUBLISH})

#: States a `--retry` pass should reset and attempt again.
RETRYABLE = frozenset(
    {
        ItemState.FAILED_DOWNLOAD,
        ItemState.FAILED_QUALITY,
        ItemState.FAILED_PUBLISH,
        ItemState.FAILED_PROCESSING,
    }
)


def normalize_title(title: str) -> str:
    """Fold a title for comparison: case, whitespace, and punctuation.

    Titles round-trip through sanitising and platform-side trimming, so an exact
    string match misses records that are plainly the same video.
    """
    folded = unicodedata.normalize("NFKC", title or "").casefold()
    folded = re.sub(r"[^\w\s]", " ", folded)
    return re.sub(r"\s+", " ", folded).strip()


def _day_of(value: str | None) -> date | None:
    """Parse the date portion of an ISO-ish timestamp."""
    if not value or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


class SourceChannel(BaseModel):
    """A channel on the source platform."""

    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    title: str
    url: str
    description: str = ""
    video_count: int | None = None

    @property
    def slug(self) -> str:
        """Filesystem-safe directory name for this channel's media."""
        from .media import safe_filename

        return safe_filename(self.name or self.title or self.id)


class VideoItem(BaseModel):
    """One video, tracked from discovery through verified publication."""

    model_config = ConfigDict(validate_assignment=True)

    # -- identity (from the source) ---------------------------------------
    source: str = "youtube"
    source_id: str
    channel_id: str
    url: str = ""
    title: str = ""
    description: str = ""
    upload_date: str | None = None  # YYYYMMDD, as the source reports it
    timestamp: int | None = None  # epoch seconds; disambiguates same-day uploads
    duration: int | None = None
    width: int | None = None
    height: int | None = None
    tags: list[str] = Field(default_factory=list)
    #: Index in the channel listing, 0 = newest. YouTube's flat listing returns no
    #: dates at all, so this is the only ordering signal available before download.
    position: int | None = None

    # -- local artefacts ---------------------------------------------------
    #: Persisted rather than recomputed: ``folder_name()`` depends on the upload date,
    #: which is unknown at discovery and known after download. Recomputing it on a
    #: resumed run would point at a different directory than the one already used.
    folder: Path | None = None
    video_path: Path | None = None
    thumbnail_path: Path | None = None
    subtitle_path: Path | None = None

    # -- destination -------------------------------------------------------
    dest_id: str | None = None
    dest_job_id: str | None = None

    # -- bookkeeping -------------------------------------------------------
    state: ItemState = ItemState.DISCOVERED
    attempts: int = 0
    last_error: str | None = None
    skip_reason: str | None = None
    updated_at: datetime | None = None

    # -- derived -----------------------------------------------------------

    @property
    def is_landscape(self) -> bool | None:
        """None when dimensions are unknown, rather than guessing."""
        if self.width is None or self.height is None:
            return None
        return self.width > self.height

    @property
    def sort_key(self) -> tuple[int, int, str]:
        """Oldest-first ordering, for rebuilding a playlist in channel order.

        Two videos uploaded the same day share an ``upload_date``; ``timestamp``
        breaks the tie. By the time a playlist is built every item has been
        downloaded and so has a real timestamp, which keeps this consistent.
        """
        if self.timestamp is not None:
            return (0, self.timestamp, self.title)
        if self.upload_date and self.upload_date.isdigit():
            return (1, int(self.upload_date), self.title)
        if self.position is not None:
            # Channel listings run newest-first, so a higher index is older.
            return (2, -self.position, self.title)
        return (3, 0, self.title)

    @property
    def recency_rank(self) -> tuple[int, float]:
        """Newest-first ordering, for selecting which videos to migrate.

        Deliberately separate from :attr:`sort_key`: selection happens *before*
        download, when no video has a date and position is the only truth. Mixing
        the two would let a handful of already-downloaded items with real dates
        outrank everything else and make ``--newest`` return the wrong videos.
        """
        if self.position is not None:
            return (0, float(self.position))
        if self.timestamp is not None:
            return (1, -float(self.timestamp))
        if self.upload_date and self.upload_date.isdigit():
            return (1, -float(self.upload_date))
        return (2, 0.0)

    @property
    def upload_day(self) -> date | None:
        if not self.upload_date or len(self.upload_date) != 8 or not self.upload_date.isdigit():
            return None
        try:
            return date(
                int(self.upload_date[0:4]), int(self.upload_date[4:6]), int(self.upload_date[6:8])
            )
        except ValueError:
            return None

    @property
    def display_date(self) -> str:
        day = self.upload_day
        return day.isoformat() if day else "unknown-date"

    def release_datetime(self) -> datetime:
        """Release timestamp for the destination, clamped into the past.

        Rad rejects a release date of today or later, and YouTube's ``upload_date``
        is frequently today for a fresh video, so clamp to yesterday rather than
        failing the publish.
        """
        day = self.upload_day
        now = datetime.now(timezone.utc)
        if day is None:
            return now - timedelta(days=1)
        moment = datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=timezone.utc)
        if moment >= now:
            return now - timedelta(days=1)
        return moment

    def release_iso(self) -> str:
        return self.release_datetime().strftime("%Y-%m-%dT%H:%M:%SZ")

    def folder_name(self) -> str:
        """Stable, human-readable, filesystem-safe directory name."""
        from .media import safe_filename

        return safe_filename(f"{self.display_date} - {self.title} [{self.source_id}]")

    def summary(self) -> dict[str, Any]:
        """Compact representation for --json output and MCP tool results."""
        return {
            "source_id": self.source_id,
            "title": self.title,
            "upload_date": self.display_date,
            "duration": self.duration,
            "resolution": (f"{self.width}x{self.height}" if self.width and self.height else None),
            "state": self.state.value,
            "dest_id": self.dest_id,
            "error": self.last_error,
            "skip_reason": self.skip_reason,
        }


class DestinationContent(BaseModel):
    """A content record that already exists at the destination."""

    model_config = ConfigDict(frozen=True)

    id: str
    title: str = ""
    external_id: str | None = None
    source_url: str | None = None
    release_date: str | None = None
    status: str | None = None

    def matches_source_id(self, source_id: str) -> bool:
        """Whether this record is stamped with the given source video id."""
        if self.external_id and self.external_id == source_id:
            return True
        return bool(self.source_url and source_id in self.source_url)

    def matches(self, item: VideoItem) -> bool:
        """Whether this record represents ``item``.

        Prefers a stamped id, which is exact. Rad's API exposes no such field today,
        so the working fallback is title plus release date — the release date is
        derived deterministically from the source upload date, which makes the pair
        a strong key. Title alone is deliberately not enough: channels legitimately
        reuse titles across years.
        """
        if self.matches_source_id(item.source_id):
            return True
        if not self.title or not normalize_title(self.title):
            return False
        if normalize_title(self.title) != normalize_title(item.title):
            return False
        # Same title. Require the dates to agree when both are known.
        their_day = _day_of(self.release_date)
        our_day = item.upload_day
        if their_day is None or our_day is None:
            return True
        return their_day == our_day


class PublishTarget(BaseModel):
    """What the destination hands back when it accepts a new content record."""

    model_config = ConfigDict(frozen=True)

    content_id: str
    video_upload_url: str
    thumbnail_upload_url: str | None = None


class ProcessingStatus(BaseModel):
    """Where the destination's transcode has got to."""

    model_config = ConfigDict(frozen=True)

    content_id: str
    status: str
    job_id: str | None = None
    progress: float | None = None
    error: str | None = None

    @property
    def is_complete(self) -> bool:
        return self.status.lower() in {"complete", "completed", "ready", "published"}

    @property
    def is_failed(self) -> bool:
        return self.status.lower() in {"failed", "error", "errored"}

    @property
    def is_pending(self) -> bool:
        """No job exists yet — either just uploaded, or still an unpublished draft."""
        return self.status.lower() == "pending"

    @property
    def is_terminal(self) -> bool:
        return self.is_complete or self.is_failed
