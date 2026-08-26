"""Content sources. Only YouTube ships today; the protocol is in :mod:`.base`."""

from .base import Source
from .youtube import YouTubeSource

__all__ = ["Source", "YouTubeSource"]
