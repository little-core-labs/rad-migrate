"""The contract a content source must satisfy."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..models import SourceChannel, VideoItem


@dataclass(frozen=True)
class QualityProbe:
    """Result of asking the source what quality it will actually serve us.

    Run before a migration commits to hours of work. The failure this exists to
    prevent is the expensive one: a degraded session silently serving standard
    definition for an entire channel, discovered only after the fact.
    """

    max_height: int | None
    formats_seen: int = 0
    used_cookies: bool = False
    cookie_source: str | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.max_height is not None and self.error is None

    def meets(self, min_height: int) -> bool:
        return self.max_height is not None and self.max_height >= min_height

    def describe(self) -> str:
        if self.error:
            return f"could not probe: {self.error}"
        if self.max_height is None:
            return "no playable formats found"
        auth = f" (cookies: {self.cookie_source})" if self.used_cookies else " (no cookies)"
        return f"{self.max_height}p available{auth}"


@runtime_checkable
class Source(Protocol):
    """A platform we can pull videos from."""

    name: str

    def resolve_channel(self, url: str) -> SourceChannel:
        """Turn user input — a handle, a URL, a channel id — into a channel."""

    def discover(self, channel: SourceChannel) -> list[VideoItem]:
        """List every video on the channel, cheaply, without downloading."""

    def probe_quality(self, channel: SourceChannel | None = None) -> QualityProbe:
        """Report the best resolution obtainable right now."""

    def download(self, item: VideoItem, target_dir: Path) -> VideoItem:
        """Fetch one video and its sidecar assets. Mutates and returns ``item``."""
