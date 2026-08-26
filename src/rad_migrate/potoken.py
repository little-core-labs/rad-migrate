"""Installer for the proof-of-origin token provider.

YouTube now binds PO tokens to individual video ids, so they cannot be extracted by
hand any more — obtaining them takes a provider that mints one per video. We use the
Rust implementation of bgutil because it is a **single self-contained binary**: no
Node, no npm, no Docker, and no Google account. That combination is what makes "get
HD without doing anything" achievable for an ordinary user.

This is a deliberate, explicit, opt-in step. Downloading and executing a third-party
binary is a supply-chain decision, so:

* the release is **pinned** in this file, never resolved as "latest";
* every file is verified against the SHA-256 digest GitHub publishes for that asset,
  and the digest is fetched from the API rather than from the download itself;
* the resolved URL and hash are shown before anything is written;
* it only runs when the user asks for it, never automatically;
* everything lands under rad-migrate's own data directory, and ``uninstall`` removes it.
"""

from __future__ import annotations

import hashlib
import io
import shutil
import stat
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from .auth import bin_dir, plugin_dir, potoken_platform_tag
from .errors import RadMigrateError
from .reporting import Reporter

#: Pinned release. Bump deliberately after re-checking the asset names and digests.
REPO = "jim60105/bgutil-ytdlp-pot-provider-rs"
VERSION = "0.8.1"

_API_RELEASE = f"https://api.github.com/repos/{REPO}/releases/tags/v{VERSION}"
_PLUGIN_ASSET = "bgutil-ytdlp-pot-provider-rs.zip"
_BINARY_NAME = "bgutil-pot"

_TIMEOUT = httpx.Timeout(120.0, connect=15.0)
_HEADERS = {
    "User-Agent": "rad-migrate",
    "X-GitHub-Api-Version": "2022-11-28",
    "Accept": "application/vnd.github+json",
}


def binary_path() -> Path:
    """Where our copy of the provider binary lives, installed or not."""
    tag = potoken_platform_tag() or ""
    suffix = ".exe" if tag.startswith("windows") else ""
    return bin_dir() / f"{_BINARY_NAME}{suffix}"


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str | None


@dataclass(frozen=True)
class InstallPlan:
    """Exactly what will be downloaded. Shown to the user before we act."""

    version: str
    platform_tag: str
    binary: Asset
    plugin: Asset
    target_bin: Path
    target_plugins: Path

    def describe(self) -> list[str]:
        return [
            f"release   {REPO} v{self.version}",
            f"platform  {self.platform_tag}",
            f"binary    {self.binary.name}  ({self.binary.size / 1_048_576:.1f} MB)",
            f"          sha256 {self.binary.sha256 or 'unpublished'}",
            f"plugin    {self.plugin.name}  ({self.plugin.size / 1024:.0f} KB)",
            f"          sha256 {self.plugin.sha256 or 'unpublished'}",
            f"installs  {self.target_bin.parent}",
            f"          {self.target_plugins}",
        ]


def _get_json(url: str) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as client:
            response = client.get(url)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
            return data
    except httpx.HTTPStatusError as exc:
        raise RadMigrateError(
            f"Could not read release metadata ({exc.response.status_code}).",
            hint=(
                f"Check that {REPO} still publishes v{VERSION}. GitHub also rate-limits "
                "unauthenticated API calls; try again shortly."
            ),
        ) from exc
    except httpx.HTTPError as exc:
        raise RadMigrateError(f"Network error reading release metadata: {exc}") from exc


def _assets() -> dict[str, Asset]:
    payload = _get_json(_API_RELEASE)
    table: dict[str, Asset] = {}
    for raw in payload.get("assets", []):
        digest = raw.get("digest") or ""
        table[raw["name"]] = Asset(
            name=raw["name"],
            url=raw["browser_download_url"],
            size=int(raw.get("size") or 0),
            # GitHub reports this as "sha256:<hex>".
            sha256=digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else None,
        )
    return table


def build_plan() -> InstallPlan:
    tag = potoken_platform_tag()
    if tag is None:
        import platform

        raise RadMigrateError(
            f"No prebuilt provider for {platform.system()}/{platform.machine()}.",
            hint=(
                f"Build from source — see https://github.com/{REPO} — or use "
                "--cookies-from-browser instead."
            ),
        )

    binary_asset = f"{_BINARY_NAME}-{tag}" + (".exe" if tag.startswith("windows") else "")
    assets = _assets()
    missing = [n for n in (binary_asset, _PLUGIN_ASSET) if n not in assets]
    if missing:
        raise RadMigrateError(
            f"Release v{VERSION} does not contain: {', '.join(missing)}",
            hint="The release layout changed. Please open an issue so the pin can be updated.",
        )

    suffix = ".exe" if tag.startswith("windows") else ""
    return InstallPlan(
        version=VERSION,
        platform_tag=tag,
        binary=assets[binary_asset],
        plugin=assets[_PLUGIN_ASSET],
        target_bin=bin_dir() / f"{_BINARY_NAME}{suffix}",
        target_plugins=plugin_dir(),
    )


def _download(asset: Asset, reporter: Reporter) -> bytes:
    reporter.detail(f"downloading {asset.name}")
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=True, headers=_HEADERS) as client:
            response = client.get(asset.url)
            response.raise_for_status()
            payload = response.content
    except httpx.HTTPError as exc:
        raise RadMigrateError(f"Download failed for {asset.name}: {exc}") from exc

    digest = hashlib.sha256(payload).hexdigest()
    if asset.sha256 is None:
        raise RadMigrateError(
            f"GitHub publishes no checksum for {asset.name} — refusing to install it.",
            hint="rad-migrate will not install a binary it cannot verify.",
        )
    if digest != asset.sha256:
        raise RadMigrateError(
            f"Checksum mismatch for {asset.name}.\n  expected {asset.sha256}\n  got      {digest}",
            hint="Do not use this file. Please report this.",
        )
    reporter.detail(f"{asset.name} sha256 verified")
    return payload


def install(reporter: Reporter, plan: InstallPlan | None = None) -> dict[str, str]:
    """Download, verify, and install the provider. Returns a summary."""
    plan = plan or build_plan()
    reporter.step(f"Installing proof-of-origin provider {REPO} v{plan.version}")

    binary = _download(plan.binary, reporter)
    plugin_zip = _download(plan.plugin, reporter)

    plan.target_bin.parent.mkdir(parents=True, exist_ok=True)
    plan.target_bin.write_bytes(binary)
    mode = plan.target_bin.stat().st_mode
    plan.target_bin.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Replace rather than merge, so a version bump never leaves stale modules behind.
    if plan.target_plugins.exists():
        shutil.rmtree(plan.target_plugins, ignore_errors=True)
    plan.target_plugins.mkdir(parents=True, exist_ok=True)
    _extract_plugin(plugin_zip, plan.target_plugins)

    reporter.success(f"Provider installed ({plan.binary.size / 1_048_576:.0f} MB).")
    return {
        "version": plan.version,
        "binary": str(plan.target_bin),
        "binary_sha256": plan.binary.sha256 or "",
        "plugins": str(plan.target_plugins),
        "plugin_sha256": plan.plugin.sha256 or "",
    }


def _extract_plugin(payload: bytes, target: Path) -> None:
    """Extract the plugin zip, refusing any entry that escapes the target."""
    root = target.resolve()
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.infolist():
            if member.is_dir():
                continue
            name = member.filename.replace("\\", "/")
            if name.startswith("/") or ".." in Path(name).parts:
                raise RadMigrateError(f"Refusing unsafe path in plugin archive: {name}")
            destination = (target / name).resolve()
            if not destination.is_relative_to(root):
                raise RadMigrateError(f"Refusing path traversal in plugin archive: {name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(archive.read(member))


def uninstall(reporter: Reporter) -> bool:
    """Remove everything :func:`install` put on disk."""
    removed = False
    for candidate in (bin_dir() / _BINARY_NAME, bin_dir() / f"{_BINARY_NAME}.exe"):
        if candidate.exists():
            candidate.unlink()
            removed = True
    plugins = plugin_dir()
    if plugins.exists():
        shutil.rmtree(plugins, ignore_errors=True)
        removed = True
    reporter.success("Provider removed.") if removed else reporter.info("Nothing to remove.")
    return removed
