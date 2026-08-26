"""Output: human console, NDJSON events, and machine-readable results.

The stream contract matters and is deliberate:

* **stdout** carries the final machine-readable result, and nothing else, and only
  in ``--json`` mode. An agent or CI job can `rad-migrate plan ... --json | jq`
  without filtering noise.
* **stderr** carries everything else — the pretty console in human mode, NDJSON
  progress events in json mode.

That split also keeps the MCP stdio server safe: stdout there belongs to the JSON-RPC
protocol, so the server constructs a reporter that never touches it.

Every string leaving this module goes through :func:`~rad_migrate.redact.redact`.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from enum import Enum
from typing import IO, Any

from rich.console import Console
from rich.theme import Theme

from .redact import redact, redact_obj

_THEME = Theme(
    {
        "ok": "bold green",
        "warn": "bold yellow",
        "err": "bold red",
        "step": "bold cyan",
        "dim": "dim",
    }
)


class Mode(str, Enum):
    HUMAN = "human"
    JSON = "json"
    QUIET = "quiet"


class Reporter:
    """Emits progress and results in whichever mode the caller selected."""

    def __init__(
        self,
        mode: Mode = Mode.HUMAN,
        *,
        stdout: IO[str] | None = None,
        stderr: IO[str] | None = None,
        allow_stdout: bool = True,
    ) -> None:
        self.mode = mode
        self._allow_stdout = allow_stdout
        self._stdout = stdout if stdout is not None else sys.stdout
        self._stderr = stderr if stderr is not None else sys.stderr
        self._console = Console(
            file=self._stderr,
            theme=_THEME,
            highlight=False,
            soft_wrap=True,
        )
        self._started = time.monotonic()

    # -- lifecycle ---------------------------------------------------------

    @property
    def console(self) -> Console:
        return self._console

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    # -- human-facing messages --------------------------------------------

    def step(self, message: str) -> None:
        self.event("step", message=message)
        if self.mode is Mode.HUMAN:
            self._console.print(f"[step]→[/step] {redact(message)}")

    def success(self, message: str) -> None:
        self.event("success", message=message)
        if self.mode is Mode.HUMAN:
            self._console.print(f"[ok]✓[/ok] {redact(message)}")

    def info(self, message: str) -> None:
        self.event("info", message=message)
        if self.mode is Mode.HUMAN:
            self._console.print(f"  {redact(message)}")

    def detail(self, message: str) -> None:
        """Secondary information — dimmed, and not worth an NDJSON event."""
        if self.mode is Mode.HUMAN:
            self._console.print(f"[dim]  {redact(message)}[/dim]")

    def warn(self, message: str, *, hint: str | None = None) -> None:
        self.event("warning", message=message, hint=hint)
        if self.mode is Mode.HUMAN:
            self._console.print(f"[warn]![/warn] {redact(message)}")
            if hint:
                self._console.print(f"[dim]  {redact(hint)}[/dim]")

    def error(self, message: str, *, hint: str | None = None) -> None:
        self.event("error", message=message, hint=hint)
        if self.mode is Mode.HUMAN:
            self._console.print(f"[err]✗[/err] {redact(message)}")
            if hint:
                self._console.print(f"[dim]  {redact(hint)}[/dim]")

    @contextmanager
    def phase(self, title: str) -> Iterator[None]:
        """Group related output under a heading."""
        self.event("phase_start", phase=title)
        if self.mode is Mode.HUMAN:
            self._console.print(f"\n[step]{redact(title)}[/step]")
        start = time.monotonic()
        try:
            yield
        finally:
            self.event("phase_end", phase=title, seconds=round(time.monotonic() - start, 2))

    # -- machine-facing ----------------------------------------------------

    def event(self, event_type: str, **fields: Any) -> None:
        """Emit one NDJSON progress event on stderr (json mode only)."""
        if self.mode is not Mode.JSON:
            return
        payload = {"event": event_type, "t": round(self.elapsed(), 3)}
        payload.update({k: v for k, v in fields.items() if v is not None})
        self._write_ndjson(payload)

    def result(self, payload: dict[str, Any]) -> None:
        """Emit the final result. stdout in json mode; nothing otherwise."""
        if self.mode is not Mode.JSON or not self._allow_stdout:
            return
        text = json.dumps(redact_obj(payload), indent=2, ensure_ascii=False, default=str)
        print(text, file=self._stdout)
        self._stdout.flush()

    def _write_ndjson(self, payload: dict[str, Any]) -> None:
        line = json.dumps(redact_obj(payload), ensure_ascii=False, default=str)
        print(line, file=self._stderr)
        self._stderr.flush()


def null_reporter() -> Reporter:
    """A reporter that produces no output. For library use and tests."""
    return Reporter(Mode.QUIET, allow_stdout=False)
