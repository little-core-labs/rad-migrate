"""Publishing destinations. Only Rad TV ships today; the protocol is in :mod:`.base`."""

from .base import Destination
from .radtv import RadTVDestination

__all__ = ["Destination", "RadTVDestination"]
