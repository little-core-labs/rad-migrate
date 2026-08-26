"""Configuration.

Precedence, highest first: explicit CLI flags → environment → ``.env`` → defaults.

Secrets are never persisted. ``Settings.api_key`` is a ``SecretStr`` so an accidental
``repr()`` in a log line or traceback prints ``**********`` rather than the key, and
:meth:`Settings.register_secrets` additionally registers the real value with the
redaction layer so it is masked even when deliberately unwrapped.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError
from .redact import register_secret

DEFAULT_API_URL = "https://api.rad.live"
DEFAULT_WORKDIR = Path("rad-migrate-work")
DEFAULT_MIN_HEIGHT = 720


class QualityStrategy(str, Enum):
    """How hard to insist on HD.

    ``HD_STRICT`` reproduces the behaviour that made the original scripts work on a
    flagged IP: HLS-only with no standard-definition fallback, so a degraded session
    *fails* the video instead of silently saving a 360p copy. A failed video is never
    recorded as done, so the next run retries it.
    """

    HD_STRICT = "hd-strict"
    HD_PREFERRED = "hd-preferred"
    BEST = "best"


class CookieSource(str, Enum):
    AUTO = "auto"
    NONE = "none"
    BROWSER = "browser"
    FILE = "file"


BROWSERS = ("chrome", "firefox", "edge", "brave", "chromium", "opera", "vivaldi", "safari")


class Settings(BaseSettings):
    """Runtime configuration."""

    model_config = SettingsConfigDict(
        env_prefix="RAD_MIGRATE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # -- destination credentials ------------------------------------------
    api_key: Annotated[
        SecretStr | None,
        Field(
            default=None,
            validation_alias=AliasChoices("RAD_API_KEY", "RAD_MIGRATE_API_KEY", "api_key"),
            description="Rad TV API key.",
        ),
    ]
    api_url: str = Field(default=DEFAULT_API_URL, description="Rad API base URL.")
    channel: str | None = Field(
        default=None,
        description="Destination channel id or name. Resolved or created if unset.",
    )

    # -- workspace ---------------------------------------------------------
    workdir: Path = Field(
        default=DEFAULT_WORKDIR,
        description="Where downloads and resume state live.",
    )

    # -- source auth -------------------------------------------------------
    cookies_from_browser: str | None = Field(
        default=None,
        description=f"Browser to read YouTube cookies from. One of: {', '.join(BROWSERS)}",
    )
    cookies_file: Path | None = Field(
        default=None,
        description="Netscape-format cookies.txt. Use this in Docker.",
    )

    # -- quality -----------------------------------------------------------
    quality: QualityStrategy = QualityStrategy.HD_STRICT
    min_height: int = Field(
        default=DEFAULT_MIN_HEIGHT,
        ge=144,
        description="Reject downloads shorter than this many pixels tall.",
    )
    allow_sd: bool = Field(
        default=False,
        description="Accept standard definition instead of failing.",
    )

    # -- selection filters -------------------------------------------------
    landscape_only: bool = Field(default=False, description="Skip vertical videos (Shorts).")
    min_duration: int | None = Field(default=None, ge=0, description="Seconds.")
    max_duration: int | None = Field(default=None, ge=1, description="Seconds.")
    newest: int | None = Field(default=None, ge=1, description="Only the N most recent videos.")
    since: str | None = Field(default=None, description="Only videos uploaded on/after YYYY-MM-DD.")
    limit: int | None = Field(default=None, ge=1, description="Cap this run at N videos.")

    # -- behaviour ---------------------------------------------------------
    playlist: str | None = Field(
        default=None,
        description="Create a playlist with this title from the migrated set.",
    )
    cleanup_after_publish: bool = Field(
        default=False,
        description="Delete local media once the destination confirms processing.",
    )
    concurrency: int = Field(default=1, ge=1, le=4, description="Parallel video workers.")
    ffmpeg_location: Path | None = Field(default=None, description="Override ffmpeg discovery.")

    # -- pacing ------------------------------------------------------------
    # YouTube throttles the *player API* (extraction), not fragment downloads, so
    # these govern how often we ask for a new video rather than download speed.
    break_every: int = Field(default=0, ge=0, description="Long pause every N videos. 0 = off.")
    break_seconds: int = Field(default=1800, ge=0, description="Length of that pause.")
    max_backoff_seconds: int = Field(
        default=3600,
        ge=0,
        description="Cap on rate-limit backoff. The original scripts slept unboundedly.",
    )

    @field_validator("cookies_from_browser")
    @classmethod
    def _check_browser(cls, value: str | None) -> str | None:
        if value is None:
            return None
        # yt-dlp accepts "chrome:Profile 2" and "firefox:/path/to/profile".
        name = value.split(":", 1)[0].strip().lower()
        if name not in BROWSERS:
            raise ValueError(f"unknown browser {name!r}; expected one of {', '.join(BROWSERS)}")
        return value

    @field_validator("api_url")
    @classmethod
    def _strip_slash(cls, value: str) -> str:
        return value.rstrip("/")

    # -- derived -----------------------------------------------------------

    @property
    def cookie_source(self) -> CookieSource:
        if self.cookies_file:
            return CookieSource.FILE
        if self.cookies_from_browser:
            return CookieSource.BROWSER
        return CookieSource.AUTO

    @property
    def effective_min_height(self) -> int:
        return 0 if self.allow_sd else self.min_height

    @property
    def media_dir(self) -> Path:
        return self.workdir / "media"

    @property
    def state_path(self) -> Path:
        return self.workdir / "state.db"

    def require_api_key(self) -> str:
        """Return the API key or raise with an actionable hint."""
        if self.api_key is None or not self.api_key.get_secret_value().strip():
            raise ConfigError(
                "No Rad API key configured.",
                hint=(
                    "Set RAD_API_KEY in your environment or a .env file, or pass "
                    "--api-key. Create a key in your Rad account settings."
                ),
            )
        return self.api_key.get_secret_value().strip()

    def register_secrets(self) -> None:
        """Teach the redaction layer about our secrets. Call once at startup."""
        if self.api_key is not None:
            register_secret(self.api_key.get_secret_value())

    def ensure_workdir(self) -> Path:
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.media_dir.mkdir(parents=True, exist_ok=True)
        return self.workdir

    def validate_selection(self) -> None:
        """Catch contradictory filters before doing hours of work."""
        if (
            self.min_duration is not None
            and self.max_duration is not None
            and self.min_duration > self.max_duration
        ):
            raise ConfigError(
                f"--min-duration ({self.min_duration}s) is greater than "
                f"--max-duration ({self.max_duration}s); nothing can match.",
            )
        if self.cookies_file and not self.cookies_file.exists():
            raise ConfigError(
                f"Cookies file not found: {self.cookies_file}",
                hint="Check the path, or drop --cookies-file to read cookies from your browser.",
            )
        if self.cookies_file and self.cookies_from_browser:
            raise ConfigError(
                "--cookies-file and --cookies-from-browser are mutually exclusive.",
                hint="Pick one. In Docker, use --cookies-file.",
            )
