"""Getting YouTube to serve HD — without making the user do homework.

There are three ways to convince YouTube to hand over high-definition streams, and
they are *not* equally good. In preference order:

**Tier 0 — nothing at all.** A JS runtime (node) plus current yt-dlp is enough for a
great many videos. This is always tried first, and for a typical channel migration it
is frequently all that is needed. No account, no setup, no risk.

**Tier 1 — a PO token provider.** YouTube increasingly demands a "proof of origin"
token, now bound to each individual video id, so tokens cannot be extracted by hand
any more — it takes a provider plugin. ``rad-migrate setup-potoken`` installs one as a
single self-contained binary: no Node, no npm, no Docker, and **no Google account**.

**Tier 2 — browser cookies.** Effective, but this is the *risky* rung, not the easy
one: bulk-downloading against your own signed-in session is exactly the pattern
YouTube's abuse systems look for, and for a creator migrating their own channel that
session belongs to the account the channel lives on. Prefer Tier 1.

A hard platform fact shapes the advice we give: **Chrome 127+ on Windows encrypts
cookies with a key bound to the Chrome process** (app-bound encryption), so no
external tool can read them — yt-dlp included. Firefox keeps cookies in a plain
SQLite file and works. We detect this rather than letting the user discover it as a
confusing decryption error.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import platformdirs

APP_NAME = "rad-migrate"

#: Browsers yt-dlp can read, in the order we would recommend trying them.
_BROWSER_PREFERENCE = ("firefox", "brave", "edge", "chromium", "vivaldi", "opera", "chrome")


class AuthTier(str, Enum):
    NONE = "none"
    POTOKEN = "potoken"
    BROWSER_COOKIES = "browser-cookies"
    COOKIE_FILE = "cookie-file"


def data_dir() -> Path:
    """Where we keep the PO token provider and its plugin."""
    override = os.environ.get("RAD_MIGRATE_DATA_DIR")
    if override:
        return Path(override)
    return Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))


def plugin_dir() -> Path:
    """A private yt-dlp plugin directory.

    Kept inside rad-migrate's own data directory rather than the user's global
    ``~/yt-dlp-plugins`` so installing (or removing) a provider never disturbs their
    other yt-dlp usage.
    """
    return data_dir() / "plugins"


def bin_dir() -> Path:
    return data_dir() / "bin"


def activate_plugins() -> None:
    """Make our private plugin directory visible to yt-dlp.

    yt-dlp discovers plugins via the ``yt_dlp_plugins`` namespace package on
    ``sys.path``, so this must run before the first ``YoutubeDL`` is constructed.
    """
    directory = plugin_dir()
    if directory.is_dir():
        path = str(directory)
        if path not in sys.path:
            sys.path.insert(0, path)
    binaries = bin_dir()
    if binaries.is_dir():
        os.environ["PATH"] = f"{binaries}{os.pathsep}{os.environ.get('PATH', '')}"


# -- PO token provider -----------------------------------------------------


@dataclass(frozen=True)
class PotokenStatus:
    """Whether a proof-of-origin token provider is installed and usable."""

    plugin_installed: bool = False
    binary_path: Path | None = None
    providers: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return bool(self.providers) or (self.plugin_installed and self.binary_path is not None)

    def describe(self) -> str:
        if self.providers:
            return f"installed ({', '.join(self.providers)})"
        if self.plugin_installed and self.binary_path:
            return "installed"
        if self.plugin_installed:
            return "plugin present but the provider binary is missing"
        return "not installed"


def _find_potoken_binary() -> Path | None:
    names = ("bgutil-pot", "bgutil-pot.exe", "rustypipe-botguard", "rustypipe-botguard.exe")
    for name in names:
        candidate = bin_dir() / name
        if candidate.exists():
            return candidate
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def potoken_status() -> PotokenStatus:
    """Report on PO token provision, asking yt-dlp what it actually registered."""
    activate_plugins()
    providers: tuple[str, ...] = ()
    try:
        from yt_dlp.extractor.youtube.pot._registry import _pot_providers

        providers = tuple(sorted(_pot_providers.value))
    except Exception:  # noqa: BLE001 - yt-dlp internals; absence is not fatal
        providers = ()

    directory = plugin_dir()
    plugin_installed = bool(providers) or (directory / "yt_dlp_plugins").is_dir()
    return PotokenStatus(
        plugin_installed=plugin_installed,
        binary_path=_find_potoken_binary(),
        providers=providers,
    )


def potoken_platform_tag() -> str | None:
    """The release asset tag for this machine, or None if no prebuilt binary exists.

    Matches the provider's published asset naming: ``bgutil-pot-<os>-<arch>``.
    """
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = (
        "x86_64"
        if machine in {"x86_64", "amd64"}
        else "aarch64"
        if machine in {"arm64", "aarch64"}
        else None
    )
    if arch is None:
        return None

    if system == "windows":
        return "windows-x86_64" if arch == "x86_64" else None
    if system == "linux":
        return f"linux-{arch}"
    if system == "darwin":
        return f"macos-{arch}"
    return None


# -- browser cookies -------------------------------------------------------


@dataclass(frozen=True)
class BrowserStatus:
    name: str
    installed: bool
    readable: bool
    note: str | None = None

    @property
    def usable(self) -> bool:
        return self.installed and self.readable


def _windows_chromium_blocked(name: str) -> bool:
    """Chromium-family cookie encryption on Windows that no external tool can read.

    Chrome 127 introduced app-bound encryption: the cookie decryption key is tied to
    the Chrome process itself, so copying the database gets you ciphertext. Edge,
    Brave, and other Chromium forks have followed. Firefox is unaffected.
    """
    return platform.system() == "Windows" and name in {
        "chrome",
        "chromium",
        "edge",
        "brave",
        "vivaldi",
        "opera",
    }


def _browser_profile_paths(name: str) -> list[Path]:
    home = Path.home()
    system = platform.system()
    appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
    local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
    support = home / "Library/Application Support"

    table: dict[str, dict[str, list[Path]]] = {
        "firefox": {
            "Windows": [appdata / "Mozilla/Firefox"],
            "Darwin": [support / "Firefox"],
            "Linux": [home / ".mozilla/firefox", home / "snap/firefox/common/.mozilla/firefox"],
        },
        "chrome": {
            "Windows": [local / "Google/Chrome/User Data"],
            "Darwin": [support / "Google/Chrome"],
            "Linux": [home / ".config/google-chrome"],
        },
        "chromium": {
            "Windows": [local / "Chromium/User Data"],
            "Darwin": [support / "Chromium"],
            "Linux": [home / ".config/chromium", home / "snap/chromium/common/chromium"],
        },
        "edge": {
            "Windows": [local / "Microsoft/Edge/User Data"],
            "Darwin": [support / "Microsoft Edge"],
            "Linux": [home / ".config/microsoft-edge"],
        },
        "brave": {
            "Windows": [local / "BraveSoftware/Brave-Browser/User Data"],
            "Darwin": [support / "BraveSoftware/Brave-Browser"],
            "Linux": [home / ".config/BraveSoftware/Brave-Browser"],
        },
        "vivaldi": {
            "Windows": [local / "Vivaldi/User Data"],
            "Darwin": [support / "Vivaldi"],
            "Linux": [home / ".config/vivaldi"],
        },
        "opera": {
            "Windows": [appdata / "Opera Software/Opera Stable"],
            "Darwin": [support / "com.operasoftware.Opera"],
            "Linux": [home / ".config/opera"],
        },
        "safari": {"Darwin": [home / "Library/Cookies", support / "Safari"]},
    }
    return table.get(name, {}).get(system, [])


def detect_browsers() -> list[BrowserStatus]:
    """Which browsers are present, and whether their cookies are actually readable."""
    results: list[BrowserStatus] = []
    names = list(_BROWSER_PREFERENCE)
    if platform.system() == "Darwin":
        names.append("safari")

    for name in names:
        installed = any(path.exists() for path in _browser_profile_paths(name))
        if not installed:
            results.append(BrowserStatus(name, False, False))
            continue
        if _windows_chromium_blocked(name):
            results.append(
                BrowserStatus(
                    name,
                    True,
                    False,
                    note=(
                        "Windows app-bound encryption — no external tool can read "
                        "these cookies, including yt-dlp."
                    ),
                )
            )
            continue
        note = None
        if name == "safari":
            note = "macOS may prompt for Full Disk Access."
        results.append(BrowserStatus(name, True, True, note=note))
    return results


def best_browser(statuses: list[BrowserStatus] | None = None) -> str | None:
    """The browser most likely to work without the user fighting anything."""
    for status in statuses if statuses is not None else detect_browsers():
        if status.usable:
            return status.name
    return None


# -- recommendation --------------------------------------------------------


@dataclass
class AuthAdvice:
    """What to tell the user when quality is not good enough."""

    tier: AuthTier
    headline: str
    command: str | None = None
    notes: list[str] = field(default_factory=list)


def recommend(
    *,
    current_height: int | None,
    min_height: int,
    potoken: PotokenStatus | None = None,
    browsers: list[BrowserStatus] | None = None,
    using_cookies: bool = False,
) -> AuthAdvice | None:
    """Pick the next rung of the ladder. Returns None when nothing is needed.

    Ordered so the option that needs no Google account is offered before the one
    that does.
    """
    if current_height is not None and current_height >= min_height:
        return None

    potoken = potoken if potoken is not None else potoken_status()
    browsers = browsers if browsers is not None else detect_browsers()

    if not potoken.available:
        advice = AuthAdvice(
            tier=AuthTier.POTOKEN,
            headline="Install a proof-of-origin token provider — no Google account needed.",
            command="rad-migrate setup-potoken",
            notes=[
                "One self-contained binary. No Node, no npm, no Docker, no sign-in.",
                "This is the safest fix: it does not put your YouTube account anywhere "
                "near the download.",
            ],
        )
        if potoken_platform_tag() is None:
            advice.notes.append(
                f"No prebuilt binary for {platform.system()}/{platform.machine()} — "
                "you may need to build it from source."
            )
        return advice

    if not using_cookies:
        browser = best_browser(browsers)
        if browser:
            return AuthAdvice(
                tier=AuthTier.BROWSER_COOKIES,
                headline=f"Try your signed-in session: --cookies-from-browser {browser}",
                command=f"rad-migrate <channel> --cookies-from-browser {browser}",
                notes=[
                    "Sign in to YouTube in that browser first, then close it.",
                    "Heads up: bulk downloading against your own account carries some "
                    "risk of it being flagged. Prefer the PO token route where it works.",
                ],
            )
        blocked = [b for b in browsers if b.installed and not b.readable]
        notes = [f"{b.name}: {b.note}" for b in blocked if b.note]
        notes.append(
            "Install Firefox and sign in there — its cookies are readable, unlike "
            "Chromium's on Windows."
        )
        return AuthAdvice(
            tier=AuthTier.COOKIE_FILE,
            headline="No browser on this machine has readable cookies.",
            command="rad-migrate <channel> --cookies-file cookies.txt",
            notes=notes,
        )

    return AuthAdvice(
        tier=AuthTier.COOKIE_FILE,
        headline=(f"Still capped at {current_height or 'unknown'}p with cookies in play."),
        notes=[
            "The session may be stale — sign in to YouTube again and re-run.",
            "Or wait: YouTube throttles per-session, and it lifts.",
            "Use --allow-sd to accept the lower resolution instead.",
        ],
    )
