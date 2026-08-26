"""Tests for the MCP server surface.

The protocol-level property that matters: stdout belongs to JSON-RPC, so nothing else
may write there. A stray progress line corrupts the stream for every subsequent message.
"""

from __future__ import annotations

import io

import pytest

from rad_migrate.reporting import Mode, Reporter


class TestToolRegistration:
    async def test_expected_tools_are_exposed(self) -> None:
        from rad_migrate.mcp_server import mcp_app

        names = {tool.name for tool in await mcp_app.list_tools()}
        assert names == {
            "check_environment",
            "plan_migration",
            "start_migration",
            "migration_status",
            "cancel_migration",
            "retry_failed",
            "install_potoken_provider",
        }

    async def test_every_tool_is_documented(self) -> None:
        """Descriptions are how an agent picks the right tool."""
        from rad_migrate.mcp_server import mcp_app

        for tool in await mcp_app.list_tools():
            assert tool.description, f"{tool.name} has no description"

    async def test_start_migration_advertises_that_it_is_async(self) -> None:
        """An agent must not sit blocking on a migration that takes hours."""
        from rad_migrate.mcp_server import mcp_app

        tool = next(t for t in await mcp_app.list_tools() if t.name == "start_migration")
        assert "background" in (tool.description or "").lower()
        assert "poll" in (tool.description or "").lower()


class TestStdoutDiscipline:
    def test_server_reporter_never_writes_to_stdout(self) -> None:
        from rad_migrate.mcp_server import _reporter

        reporter = _reporter()
        assert reporter.mode is Mode.QUIET
        assert not reporter._allow_stdout

    def test_quiet_reporter_produces_nothing_anywhere(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        reporter = Reporter(Mode.QUIET, stdout=out, stderr=err, allow_stdout=False)
        reporter.step("working")
        reporter.success("done")
        reporter.detail("noise")
        reporter.event("progress", sent=1)
        reporter.result({"ok": True})
        assert out.getvalue() == ""
        assert err.getvalue() == ""

    def test_json_reporter_keeps_events_off_stdout(self) -> None:
        """Progress goes to stderr so stdout stays a single parseable document."""
        import json

        out, err = io.StringIO(), io.StringIO()
        reporter = Reporter(Mode.JSON, stdout=out, stderr=err)
        reporter.step("downloading")
        reporter.event("upload_progress", sent=50, total=100)
        reporter.result({"ok": True, "published": 3})

        assert json.loads(out.getvalue()) == {"ok": True, "published": 3}
        events = [json.loads(line) for line in err.getvalue().splitlines() if line.strip()]
        assert {e["event"] for e in events} == {"step", "upload_progress"}

    def test_ytdlp_is_configured_to_avoid_stdout(self) -> None:
        from rad_migrate.config import Settings
        from rad_migrate.source.youtube import YouTubeSource

        opts = YouTubeSource(Settings())._base_opts()
        assert opts["logtostderr"] is True
        assert opts["quiet"] is True
        assert opts["noprogress"] is True


class TestErrorShape:
    def test_errors_are_returned_not_raised(self) -> None:
        """A crashed tool call would take down the agent's session."""
        from rad_migrate.errors import ConfigError
        from rad_migrate.mcp_server import _error

        result = _error(ConfigError("no key", hint="set RAD_API_KEY"))
        assert result == {"ok": False, "error": "no key", "hint": "set RAD_API_KEY"}

    def test_unexpected_errors_are_still_wrapped(self) -> None:
        from rad_migrate.mcp_server import _error

        result = _error(RuntimeError("boom"))
        assert result["ok"] is False
        assert "boom" in result["error"]


class TestPotokenGate:
    def test_install_requires_explicit_confirmation(self) -> None:
        """Downloading and running a third-party binary is never implicit."""
        from rad_migrate.mcp_server import install_potoken_provider

        result = install_potoken_provider(confirm=False)
        if not result.get("ok"):
            pytest.skip(f"release metadata unavailable: {result.get('error')}")
        assert result["installed"] is False
        assert "would_install" in result
