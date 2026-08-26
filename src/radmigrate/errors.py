"""Exception hierarchy.

Every error carries a ``hint`` where there is a concrete next action, because the
failure modes of this tool are overwhelmingly environmental (stale cookies, missing
node, flaky upstream) rather than programming errors. The CLI prints the hint; the
MCP server passes it back to the agent.
"""

from __future__ import annotations

from .redact import redact


class RadMigrateError(Exception):
    """Base class. All expected failures derive from this."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return redact(self.message)

    def render(self) -> str:
        text = redact(self.message)
        if self.hint:
            text = f"{text}\n\n  {redact(self.hint)}"
        return text


class ConfigError(RadMigrateError):
    """Missing or invalid configuration."""

    exit_code = 2


class PreflightError(RadMigrateError):
    """The environment cannot support a migration."""

    exit_code = 3


class SourceError(RadMigrateError):
    """Something went wrong talking to the source platform."""

    exit_code = 4


class QualityError(SourceError):
    """A download completed but did not meet the minimum quality bar."""


class RateLimitError(SourceError):
    """The source is throttling or challenging us."""


class DestinationError(RadMigrateError):
    """Something went wrong talking to the destination platform."""

    exit_code = 5


class AuthError(DestinationError):
    """Destination rejected our credentials."""

    exit_code = 6


class TransientError(DestinationError):
    """A retryable upstream failure."""


class ProcessingError(DestinationError):
    """The destination accepted the upload but transcoding failed."""


class RunCancelledError(RadMigrateError):
    """User interrupted the run."""

    exit_code = 130
