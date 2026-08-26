"""Command line interface.

The headline command takes a channel and does everything::

    radmigrate @yourchannel

Everything else exists to answer a question that command might raise: what *would*
it do (``plan``), why is quality poor (``doctor``), what happened (``status``), and
can we try the failures again (``retry``).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Annotated, Any

import typer
from typer.core import TyperGroup

from . import __version__, auth, potoken
from .config import QualityStrategy, Settings
from .dest.radtv import RadTVDestination
from .doctor import Level, Report
from .errors import RadMigrateError, RunCancelledError
from .pipeline import Pipeline
from .reporting import Mode, Reporter
from .state import Store


class _DefaultToRun(TyperGroup):
    """Let ``radmigrate @channel`` mean ``radmigrate run @channel``.

    The promise on the tin is one command, so requiring a subcommand for the
    overwhelmingly common case works against it. Anything that is not a known
    subcommand and does not look like an option is treated as a channel.

    Implemented by rewriting the args rather than by catching the resulting error:
    typer vendors its own copy of click, so the ``UsageError`` it raises is not the
    same class as ``click.UsageError`` and a straightforward ``except`` silently
    never matches.
    """

    default_command = "run"

    # ctx/return are left untyped: typer passes its *vendored* click Context, which is
    # a different class from click.Context, so annotating either one is wrong.
    def resolve_command(self, ctx: Any, args: list[str]) -> Any:
        if args and not args[0].startswith("-") and self.get_command(ctx, args[0]) is None:
            args = [self.default_command, *args]
        return super().resolve_command(ctx, args)


app = typer.Typer(
    cls=_DefaultToRun,
    name="radmigrate",
    help="Move your YouTube channel to Rad TV in one command.",
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
)

# -- shared options --------------------------------------------------------

ChannelArg = Annotated[str, typer.Argument(help="Channel URL or @handle.", show_default=False)]
JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output on stdout.")]


def _settings(**overrides: Any) -> Settings:
    """Build settings from env/.env, then apply non-None CLI overrides."""
    clean = {k: v for k, v in overrides.items() if v is not None}
    settings = Settings(**clean)
    settings.register_secrets()
    settings.validate_selection()
    return settings


def _reporter(as_json: bool) -> Reporter:
    return Reporter(Mode.JSON if as_json else Mode.HUMAN)


def _fail(reporter: Reporter, exc: RadMigrateError) -> None:
    reporter.error(exc.message, hint=exc.hint)
    reporter.result({"ok": False, "error": exc.message, "hint": exc.hint})
    raise typer.Exit(exc.exit_code)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"radmigrate {__version__}")
        raise typer.Exit


@app.callback()
def main_callback(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Move your YouTube channel to Rad TV in one command."""


# -- run -------------------------------------------------------------------


@app.command()
def run(
    channel: ChannelArg,
    api_key: Annotated[
        str | None, typer.Option("--api-key", envvar="RAD_API_KEY", help="Rad TV API key.")
    ] = None,
    dest_channel: Annotated[
        str | None, typer.Option("--channel", help="Destination Rad channel id or name.")
    ] = None,
    workdir: Annotated[
        Path | None, typer.Option("--workdir", help="Where downloads and state live.")
    ] = None,
    cookies_from_browser: Annotated[
        str | None,
        typer.Option("--cookies-from-browser", help="chrome | firefox | edge | brave | safari"),
    ] = None,
    cookies_file: Annotated[
        Path | None, typer.Option("--cookies-file", help="Netscape cookies.txt (use in Docker).")
    ] = None,
    quality: Annotated[
        QualityStrategy | None, typer.Option("--quality", help="How hard to insist on HD.")
    ] = None,
    min_height: Annotated[
        int | None, typer.Option("--min-height", help="Reject downloads below this height.")
    ] = None,
    allow_sd: Annotated[
        bool, typer.Option("--allow-sd", help="Accept standard definition.")
    ] = False,
    landscape_only: Annotated[
        bool, typer.Option("--landscape-only", help="Skip vertical videos (Shorts).")
    ] = False,
    min_duration: Annotated[int | None, typer.Option(help="Skip videos shorter than N s.")] = None,
    max_duration: Annotated[int | None, typer.Option(help="Skip videos longer than N s.")] = None,
    since: Annotated[
        str | None, typer.Option(help="Only videos uploaded on/after YYYY-MM-DD.")
    ] = None,
    newest: Annotated[int | None, typer.Option(help="Only the N most recent videos.")] = None,
    limit: Annotated[int | None, typer.Option(help="Cap this run at N videos.")] = None,
    playlist: Annotated[
        str | None, typer.Option("--playlist", help="Create a playlist with this title.")
    ] = None,
    cleanup: Annotated[
        bool,
        typer.Option(
            # `--cleanup` leads because typer's help table caps this column at ~22
            # characters and renders the longer name elided and uncopyable. The
            # descriptive form still works.
            "--cleanup",
            "--cleanup-after-publish",
            help="Delete local media once Rad confirms processing.",
        ),
    ] = False,
    create_channel: Annotated[
        bool, typer.Option("--create-channel", help="Create the Rad channel if it is missing.")
    ] = False,
    skip_preflight: Annotated[
        bool, typer.Option("--skip-preflight", help="Do not run checks first.")
    ] = False,
    as_json: JsonOpt = False,
) -> None:
    """Migrate a YouTube channel to Rad TV."""
    reporter = _reporter(as_json)
    try:
        settings = _settings(
            api_key=api_key,
            channel=dest_channel,
            workdir=workdir,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
            quality=quality,
            min_height=min_height,
            allow_sd=allow_sd or None,
            landscape_only=landscape_only or None,
            min_duration=min_duration,
            max_duration=max_duration,
            since=since,
            newest=newest,
            limit=limit,
            playlist=playlist,
            cleanup_after_publish=cleanup or None,
        )
        settings.require_api_key()

        if not skip_preflight:
            from . import doctor as doctor_mod

            report = doctor_mod.run(settings, check_dest=True)
            _render_report(reporter, report, verbose=False)
            if not report.ok:
                raise RadMigrateError(
                    "Preflight failed — fix the items above, or pass --skip-preflight.",
                )

        with RadTVDestination(settings, reporter) as dest:
            if create_channel and settings.channel is None:
                settings.channel = None  # resolve_channel creates from the source title
            pipeline = Pipeline(settings, reporter)
            result = pipeline.run(channel, dest)

        reporter.result({"ok": not result.failed, **result.to_dict()})
        if result.failed:
            raise typer.Exit(1)
    except RunCancelledError:
        reporter.warn("Cancelled. Re-run the same command to resume.")
        raise typer.Exit(130) from None
    except RadMigrateError as exc:
        _fail(reporter, exc)
    except KeyboardInterrupt:
        reporter.warn("Interrupted. Re-run the same command to resume.")
        raise typer.Exit(130) from None


# -- plan ------------------------------------------------------------------


@app.command()
def plan(
    channel: ChannelArg,
    api_key: Annotated[str | None, typer.Option("--api-key", envvar="RAD_API_KEY")] = None,
    dest_channel: Annotated[str | None, typer.Option("--channel")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    landscape_only: Annotated[bool, typer.Option("--landscape-only")] = False,
    min_duration: Annotated[int | None, typer.Option()] = None,
    max_duration: Annotated[int | None, typer.Option()] = None,
    since: Annotated[str | None, typer.Option()] = None,
    newest: Annotated[int | None, typer.Option()] = None,
    limit: Annotated[int | None, typer.Option()] = None,
    as_json: JsonOpt = False,
) -> None:
    """Show what a migration would do, without downloading or publishing anything."""
    reporter = _reporter(as_json)
    try:
        settings = _settings(
            api_key=api_key,
            channel=dest_channel,
            workdir=workdir,
            landscape_only=landscape_only or None,
            min_duration=min_duration,
            max_duration=max_duration,
            since=since,
            newest=newest,
            limit=limit,
        )
        dest: RadTVDestination | None = None
        try:
            if settings.api_key is not None:
                dest = RadTVDestination(settings, reporter)
            else:
                reporter.warn(
                    "No API key — planning without checking what is already on Rad.",
                    hint="Set RAD_API_KEY for an accurate count.",
                )
            pipeline = Pipeline(settings, reporter)
            migration_plan = pipeline.plan(channel, dest)
        finally:
            if dest is not None:
                dest.close()

        if not as_json:
            _render_plan(reporter, migration_plan)
        reporter.result({"ok": True, **migration_plan.to_dict()})
    except RadMigrateError as exc:
        _fail(reporter, exc)


def _render_plan(reporter: Reporter, migration_plan: Any) -> None:
    from rich.table import Table

    parts = [f"{migration_plan.total_discovered} videos"]
    if migration_plan.already_present:
        parts.append(f"{len(migration_plan.already_present)} already on Rad")
    if migration_plan.filtered:
        parts.append(f"{len(migration_plan.filtered)} filtered out")
    if migration_plan.deselected:
        parts.append(f"{migration_plan.deselected} not selected")
    parts.append(f"[bold]{len(migration_plan.to_migrate)} to migrate[/bold]")
    reporter.console.print()
    reporter.console.print(f"[bold]{migration_plan.channel.title}[/bold] — " + ", ".join(parts))
    if not migration_plan.to_migrate:
        return
    table = Table(show_header=True, header_style="bold", box=None, pad_edge=False)
    # YouTube's channel listing carries no upload dates, so before download the
    # honest column is the video's position in the channel, not a guessed date.
    table.add_column("on channel", style="dim", no_wrap=True)
    table.add_column("title")
    table.add_column("dur", justify="right", style="dim", no_wrap=True)
    for item in migration_plan.to_migrate[:50]:
        duration = f"{item.duration // 60}:{item.duration % 60:02d}" if item.duration else "—"
        when = item.display_date if item.upload_day else f"#{(item.position or 0) + 1}"
        table.add_row(when, item.title[:70] or item.source_id, duration)
    reporter.console.print(table)
    if len(migration_plan.to_migrate) > 50:
        reporter.console.print(f"[dim]… and {len(migration_plan.to_migrate) - 50} more[/dim]")


# -- doctor ----------------------------------------------------------------


@app.command()
def doctor(
    channel: Annotated[
        str | None, typer.Argument(help="Optional channel to probe real quality against.")
    ] = None,
    api_key: Annotated[str | None, typer.Option("--api-key", envvar="RAD_API_KEY")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    cookies_from_browser: Annotated[str | None, typer.Option("--cookies-from-browser")] = None,
    cookies_file: Annotated[Path | None, typer.Option("--cookies-file")] = None,
    min_height: Annotated[int | None, typer.Option("--min-height")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Check that this machine can actually run a migration."""
    from . import doctor as doctor_mod

    reporter = _reporter(as_json)
    try:
        settings = _settings(
            api_key=api_key,
            workdir=workdir,
            cookies_from_browser=cookies_from_browser,
            cookies_file=cookies_file,
            min_height=min_height,
        )
        source_channel = None
        if channel:
            from .source.youtube import YouTubeSource

            source_channel = YouTubeSource(settings, reporter).resolve_channel(channel)

        report = doctor_mod.run(
            settings,
            channel=source_channel,
            check_dest=settings.api_key is not None,
            check_source_quality=channel is not None,
        )
        if not as_json:
            _render_report(reporter, report, verbose=True)
        reporter.result(report.to_dict())
        if not report.ok:
            raise typer.Exit(1)
    except RadMigrateError as exc:
        _fail(reporter, exc)


def _render_report(reporter: Reporter, report: Report, *, verbose: bool) -> None:
    icons = {Level.OK: "[ok]✓[/ok]", Level.WARN: "[warn]![/warn]", Level.FAIL: "[err]✗[/err]"}
    for check in report.checks:
        if not verbose and check.level is Level.OK:
            continue
        reporter.console.print(f"{icons[check.level]} {check.name:<22} {check.detail}")
        if check.level is not Level.OK:
            if check.hint:
                reporter.console.print(f"[dim]    {check.hint}[/dim]")
            if check.command:
                reporter.console.print(f"[dim]    → {check.command}[/dim]")
    if verbose and report.ok:
        reporter.console.print("\n[ok]Ready to migrate.[/ok]")


# -- status ----------------------------------------------------------------


@app.command()
def status(
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Show progress recorded for previous runs."""
    reporter = _reporter(as_json)
    settings = _settings(workdir=workdir)
    if not settings.state_path.exists():
        reporter.info(f"No state yet at {settings.state_path}.")
        reporter.result({"ok": True, "channels": []})
        return

    store = Store(settings.state_path)
    channels: list[dict[str, Any]] = []
    for channel_id in store.channel_ids():
        counts = store.counts(channel_id)
        channels.append({"channel_id": channel_id, "counts": counts})
        if not as_json:
            total = sum(counts.values())
            reporter.console.print(f"\n[bold]{channel_id}[/bold] — {total} videos tracked")
            for state, count in sorted(counts.items()):
                reporter.console.print(f"  {state:<20} {count}")
    reporter.result({"ok": True, "channels": channels})


# -- retry -----------------------------------------------------------------


@app.command()
def retry(
    channel: ChannelArg,
    api_key: Annotated[str | None, typer.Option("--api-key", envvar="RAD_API_KEY")] = None,
    dest_channel: Annotated[str | None, typer.Option("--channel")] = None,
    workdir: Annotated[Path | None, typer.Option("--workdir")] = None,
    as_json: JsonOpt = False,
) -> None:
    """Reset failed videos and try them again."""
    reporter = _reporter(as_json)
    try:
        settings = _settings(api_key=api_key, channel=dest_channel, workdir=workdir)
        settings.require_api_key()
        pipeline = Pipeline(settings, reporter)
        source_channel = pipeline.source.resolve_channel(channel)
        reset = pipeline.reset_failures(source_channel.id)
        if not reset:
            reporter.success("No failures to retry.")
            reporter.result({"ok": True, "reset": 0})
            return
        reporter.step(f"Retrying {reset} failed videos")
        with RadTVDestination(settings, reporter) as dest:
            result = pipeline.run(channel, dest)
        reporter.result({"ok": not result.failed, "reset": reset, **result.to_dict()})
        if result.failed:
            raise typer.Exit(1)
    except RadMigrateError as exc:
        _fail(reporter, exc)


# -- setup-potoken ---------------------------------------------------------


@app.command("setup-potoken")
def setup_potoken(
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Skip the confirmation prompt.")] = False,
    remove: Annotated[bool, typer.Option("--remove", help="Uninstall it instead.")] = False,
    as_json: JsonOpt = False,
) -> None:
    """Install a proof-of-origin token provider so HD works without signing in.

    Downloads one self-contained binary — no Node, no npm, no Docker, no Google
    account — and verifies it against the checksum GitHub publishes for the release.
    """
    reporter = _reporter(as_json)
    try:
        if remove:
            removed = potoken.uninstall(reporter)
            reporter.result({"ok": True, "removed": removed})
            return

        install_plan = potoken.build_plan()
        if not as_json:
            reporter.console.print()
            for line in install_plan.describe():
                reporter.console.print(f"  {line}")
            reporter.console.print()
        needs_confirm = not yes and not as_json
        if needs_confirm and not typer.confirm("Download and install this?", default=True):
            reporter.info("Cancelled.")
            raise typer.Exit(1)

        summary = potoken.install(reporter, install_plan)
        status_now = auth.potoken_status()
        if status_now.providers:
            reporter.success(f"yt-dlp now sees: {', '.join(status_now.providers)}")
        reporter.result({"ok": True, **summary})
    except RadMigrateError as exc:
        _fail(reporter, exc)


# -- mcp -------------------------------------------------------------------


@app.command()
def mcp() -> None:
    """Run as an MCP server over stdio, for Claude Desktop and other agents."""
    from .mcp_server import serve

    serve()


def main() -> None:
    try:
        app()
    except RadMigrateError as exc:
        # Anything that escaped a command handler.
        print(exc.render(), file=sys.stderr)
        raise SystemExit(exc.exit_code) from exc


if __name__ == "__main__":
    main()
