"""rad-migrate — move your YouTube channel to Rad TV in one command."""

from .auth import activate_plugins as _activate_plugins

__version__ = "0.1.0"

# yt-dlp discovers plugins from the ``yt_dlp_plugins`` namespace package on sys.path,
# and does so while importing its extractors. Putting our private plugin directory in
# place here — before anything imports yt_dlp — is what makes a locally installed
# proof-of-origin token provider visible.
_activate_plugins()

__all__ = ["__version__"]
