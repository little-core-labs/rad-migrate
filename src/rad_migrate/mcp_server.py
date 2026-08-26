"""MCP server, so agents can drive a migration.

One design constraint dominates: **a migration takes hours, and no MCP tool call may
block for hours.** So ``start_migration`` launches the work on a background thread and
returns a job id immediately; the agent polls ``migration_status``. Progress lives in
the same SQLite journal the CLI uses, which means an agent and a human can watch the
same run.

stdout belongs to the JSON-RPC transport here, so the reporter is constructed with
``allow_stdout=False``. Writing a stray progress line to stdout would corrupt the
protocol stream.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from . import __version__, auth, potoken
from . import doctor as doctor_mod
from .config import Settings
from .dest.radtv import RadTVDestination
from .errors import RadMigrateError
from .pipeline import Pipeline
from .reporting import Mode, Reporter
from .state import Store

mcp_app = MCPServer(
    "rad-migrate",
    version=__version__,
    instructions=(
        "Migrates a YouTube channel to Rad TV. Call check_environment first; if it "
        "reports a quality problem, follow the command it suggests. plan_migration "
        "shows what would happen. start_migration returns a job id immediately and "
        "runs in the background — poll migration_status rather than waiting."
    ),
)

_threads: dict[str, threading.Thread] = {}
_lock = threading.Lock()


def _reporter() -> Reporter:
    # Never stdout: that is the JSON-RPC channel.
    return Reporter(Mode.QUIET, allow_stdout=False)


def _settings(**overrides: Any) -> Settings:
    clean = {k: v for k, v in overrides.items() if v is not None}
    settings = Settings(**clean)
    settings.register_secrets()
    settings.validate_selection()
    return settings


def _error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, RadMigrateError):
        return {"ok": False, "error": exc.message, "hint": exc.hint}
    return {"ok": False, "error": str(exc)[:500]}


@mcp_app.tool()
def check_environment(workdir: str | None = None, channel: str | None = None) -> dict[str, Any]:
    """Check whether this machine can run a migration.

    Pass a channel to also probe the resolution YouTube will actually serve — worth
    doing before starting, since a degraded session silently caps quality.
    """
    try:
        settings = _settings(workdir=Path(workdir) if workdir else None)
        source_channel = None
        if channel:
            from .source.youtube import YouTubeSource

            source_channel = YouTubeSource(settings, _reporter()).resolve_channel(channel)
        report = doctor_mod.run(
            settings,
            channel=source_channel,
            check_dest=settings.api_key is not None,
            check_source_quality=channel is not None,
        )
        return {"ok": report.ok, **report.to_dict()}
    except Exception as exc:  # noqa: BLE001 - report to the agent, never crash the server
        return _error(exc)


@mcp_app.tool()
def plan_migration(
    channel: str,
    destination_channel: str | None = None,
    workdir: str | None = None,
    landscape_only: bool = False,
    newest: int | None = None,
    since: str | None = None,
) -> dict[str, Any]:
    """Show what migrating this channel would do. Downloads and publishes nothing."""
    try:
        settings = _settings(
            channel=destination_channel,
            workdir=Path(workdir) if workdir else None,
            landscape_only=landscape_only or None,
            newest=newest,
            since=since,
        )
        dest = RadTVDestination(settings, _reporter()) if settings.api_key else None
        try:
            pipeline = Pipeline(settings, _reporter())
            result = pipeline.plan(channel, dest)
        finally:
            if dest is not None:
                dest.close()
        return {"ok": True, **result.to_dict()}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp_app.tool()
def start_migration(
    channel: str,
    destination_channel: str | None = None,
    workdir: str | None = None,
    limit: int | None = None,
    newest: int | None = None,
    since: str | None = None,
    landscape_only: bool = False,
    playlist: str | None = None,
    cleanup_after_publish: bool = False,
) -> dict[str, Any]:
    """Start migrating in the background. Returns a job id immediately.

    Poll ``migration_status`` with the job id. A full channel can take hours, so do
    not wait on this call.
    """
    try:
        settings = _settings(
            channel=destination_channel,
            workdir=Path(workdir) if workdir else None,
            limit=limit,
            newest=newest,
            since=since,
            landscape_only=landscape_only or None,
            playlist=playlist,
            cleanup_after_publish=cleanup_after_publish or None,
        )
        settings.require_api_key()
        settings.ensure_workdir()
        store = Store(settings.state_path)
        job_id = store.create_job(
            channel,
            {"channel": channel, "destination_channel": destination_channel, "limit": limit},
        )

        thread = threading.Thread(
            target=_run_job,
            args=(job_id, channel, settings),
            name=f"rad-migrate-{job_id}",
            daemon=True,
        )
        with _lock:
            _threads[job_id] = thread
        thread.start()

        return {
            "ok": True,
            "job_id": job_id,
            "state_path": str(settings.state_path),
            "next": f"Poll migration_status(job_id='{job_id}'). This may take hours.",
        }
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _run_job(job_id: str, channel: str, settings: Settings) -> None:
    """Background worker. Never raises — outcomes are recorded on the job."""
    store = Store(settings.state_path)
    reporter = _reporter()
    try:
        pipeline = Pipeline(
            settings,
            reporter,
            cancel_check=lambda: store.job_is_cancelling(job_id),
        )
        with RadTVDestination(settings, reporter) as dest:
            result = pipeline.run(channel, dest)
        store.finish_job(
            job_id,
            status="cancelled" if result.cancelled else "completed",
            result=result.to_dict(),
        )
    except Exception as exc:  # noqa: BLE001 - the job records the failure
        store.finish_job(job_id, status="failed", error=str(exc)[:1000])
    finally:
        with _lock:
            _threads.pop(job_id, None)


@mcp_app.tool()
def migration_status(job_id: str, workdir: str | None = None) -> dict[str, Any]:
    """Progress of a background migration, including per-state video counts."""
    try:
        settings = _settings(workdir=Path(workdir) if workdir else None)
        store = Store(settings.state_path)
        job = store.get_job(job_id)
        if job is None:
            return {"ok": False, "error": f"No job {job_id!r} in {settings.state_path}."}

        with _lock:
            alive = job_id in _threads

        # Live progress comes from the item journal, which the worker writes after
        # every state transition — so counts are current even mid-video.
        progress = {channel_id: store.counts(channel_id) for channel_id in store.channel_ids()}
        return {"ok": True, "running": alive, "progress": progress, **job}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp_app.tool()
def cancel_migration(job_id: str, workdir: str | None = None) -> dict[str, Any]:
    """Ask a running migration to stop after the current video.

    Already-published videos stay published; re-running resumes from where it stopped.
    """
    try:
        settings = _settings(workdir=Path(workdir) if workdir else None)
        store = Store(settings.state_path)
        return {"ok": True, "cancelling": store.cancel_job(job_id)}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp_app.tool()
def retry_failed(
    channel: str, destination_channel: str | None = None, workdir: str | None = None
) -> dict[str, Any]:
    """Reset failed videos so the next migration attempts them again."""
    try:
        settings = _settings(
            channel=destination_channel, workdir=Path(workdir) if workdir else None
        )
        pipeline = Pipeline(settings, _reporter())
        source_channel = pipeline.source.resolve_channel(channel)
        return {"ok": True, "reset": pipeline.reset_failures(source_channel.id)}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp_app.tool()
def install_potoken_provider(confirm: bool = False) -> dict[str, Any]:
    """Install a proof-of-origin token provider so HD works without signing in.

    Downloads one self-contained binary and verifies it against the checksum GitHub
    publishes. Call with ``confirm=false`` first to see exactly what would be fetched;
    it will not install anything until ``confirm=true``.
    """
    try:
        install_plan = potoken.build_plan()
        if not confirm:
            return {
                "ok": True,
                "installed": False,
                "would_install": install_plan.describe(),
                "next": "Call again with confirm=true to install.",
            }
        summary = potoken.install(_reporter(), install_plan)
        providers = auth.potoken_status().providers
        return {"ok": True, "installed": True, **summary, "providers": list(providers)}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def serve() -> None:
    """Run the stdio server."""
    mcp_app.run(transport="stdio")


if __name__ == "__main__":
    serve()
