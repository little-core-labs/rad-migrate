"""The contract a publishing destination must satisfy."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import DestinationContent, ProcessingStatus, PublishTarget, VideoItem

#: Called with (bytes_sent, total_bytes) during an upload.
ProgressCallback = Callable[[int, int], None]


@runtime_checkable
class Destination(Protocol):
    """A platform we can publish videos to."""

    name: str

    def verify(self) -> str:
        """Confirm credentials work. Returns a human-readable identity."""

    def resolve_channel(self, name_or_id: str | None, *, create_as: str | None = None) -> str:
        """Return the channel id to publish into, creating it if asked and needed."""

    def list_content(self, channel_id: str) -> list[DestinationContent]:
        """Everything already published to this channel.

        This is what makes migrations idempotent without local state: the answer to
        "have we already moved this video?" comes from here, not from a log file.
        """

    def create_content(self, item: VideoItem, channel_id: str) -> PublishTarget:
        """Reserve a content record and get upload URLs for it."""

    def upload_asset(
        self,
        url: str,
        path: Path,
        *,
        on_progress: ProgressCallback | None = None,
    ) -> dict[str, object]:
        """Push one file to an ingestion URL."""

    def finalize(self, content_id: str, item: VideoItem) -> bool:
        """Tell the destination the upload is complete and it should publish."""

    def processing_status(self, content_id: str) -> ProcessingStatus:
        """Where the transcode has got to."""

    def create_playlist(self, channel_id: str, title: str, content_ids: list[str]) -> str | None:
        """Group published content, in the order given."""
