"""Tests for CLI argument routing and output contracts."""

from __future__ import annotations

import json

import pytest
import typer
from click.testing import CliRunner

from rad_migrate.cli import app

runner = CliRunner()
cli = typer.main.get_command(app)


def invoke(*args: str, **env: str):  # type: ignore[no-untyped-def]
    return runner.invoke(cli, list(args), env=env or None, catch_exceptions=False)


class TestDefaultCommand:
    """`rad-migrate @channel` must mean `rad-migrate run @channel`.

    The product promise is one command; requiring a subcommand for the common case
    undercuts it.
    """

    def test_bare_channel_routes_to_run(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        # Reaches `run` and fails on the missing key, rather than "no such command".
        result = invoke("@somechannel", RAD_MIGRATE_WORKDIR=str(tmp_path), RAD_API_KEY="")
        assert "No such command" not in result.output
        assert "No Rad API key" in result.output

    def test_bare_url_routes_to_run(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        result = invoke(
            "https://youtube.com/@somechannel",
            RAD_MIGRATE_WORKDIR=str(tmp_path),
            RAD_API_KEY="",
        )
        assert "No such command" not in result.output

    @pytest.mark.parametrize("name", ["run", "plan", "doctor", "status", "retry", "mcp"])
    def test_real_subcommands_are_not_shadowed(self, name: str) -> None:
        result = invoke(name, "--help")
        assert result.exit_code == 0
        assert name in result.output

    def test_unknown_option_still_errors(self) -> None:
        """Only bare arguments are rewritten; a bad option is still a bad option."""
        result = invoke("--definitely-not-an-option")
        assert result.exit_code != 0
        assert "No such option" in result.output

    def test_help_lists_every_command(self) -> None:
        result = invoke("--help")
        assert result.exit_code == 0
        for name in ("run", "plan", "doctor", "status", "retry", "setup-potoken", "mcp"):
            assert name in result.output

    def test_version(self) -> None:
        from rad_migrate import __version__

        result = invoke("--version")
        assert result.exit_code == 0
        assert __version__ in result.output

    def test_no_args_shows_help(self) -> None:
        result = runner.invoke(cli, [], catch_exceptions=False)
        assert "Usage" in result.output


class TestJsonOutput:
    """`--json` must put a single parseable document on stdout and nothing else."""

    def test_status_json_is_parseable(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        result = invoke("status", "--json", RAD_MIGRATE_WORKDIR=str(tmp_path))
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["channels"] == []

    def test_failure_json_carries_the_hint(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """An agent needs the remedy, not just the error."""
        result = invoke("run", "@x", "--json", RAD_MIGRATE_WORKDIR=str(tmp_path), RAD_API_KEY="")
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert "RAD_API_KEY" in payload["hint"]


class TestSelectionValidation:
    def test_contradictory_durations_are_rejected_early(self, tmp_path) -> None:  # type: ignore[no-untyped-def]
        """Caught before any network work, not hours in."""
        result = invoke(
            "plan",
            "@x",
            "--min-duration",
            "600",
            "--max-duration",
            "60",
            RAD_MIGRATE_WORKDIR=str(tmp_path),
        )
        assert result.exit_code != 0
        assert "nothing can match" in result.output
