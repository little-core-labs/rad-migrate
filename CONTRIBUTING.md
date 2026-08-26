# Contributing

Thanks for taking a look.

## Setup

```bash
git clone https://github.com/little-core-labs/rad-migrate && cd rad-migrate
uv sync --dev
uv run pytest
```

You will also need **Node 20+** and **ffmpeg** on your PATH for anything that touches
real downloads. `uv run rad-migrate doctor` tells you what is missing.

## Before opening a pull request

```bash
uv run ruff check .
uv run ruff format .
uv run mypy src
uv run pytest
```

CI runs the same on Linux and Windows across Python 3.10–3.13, plus a gitleaks scan
over the full history.

## Never commit

- `.env`, cookie files, API keys, or downloaded media — all gitignored, but check
  `git status` before committing anyway
- Real channel ids, content ids, account emails, or video ids in test fixtures. Use
  invented values; the existing fixtures show the pattern.
- Credential-shaped string literals, even fake ones. Assemble them at runtime instead —
  see the note at the top of `tests/test_redact.py`. A convincing-looking token in a
  source file trips secret scanners forever, for no added safety.

## Adding a source or destination

Both sit behind protocols:

- [`src/rad_migrate/source/base.py`](src/rad_migrate/source/base.py) — where content comes
  from
- [`src/rad_migrate/dest/base.py`](src/rad_migrate/dest/base.py) — where it goes

Implement the protocol, add it to the relevant `__init__.py`, and wire it into
`config.py`. New destinations are welcome; the Rad TV implementation is the reference.

## Conventions

**Comments explain why, not what.** Most of the non-obvious code here exists because of
a specific platform behaviour, and the comment says which one. If you change such a
line, update the reasoning with it. If you find one of these comments is now wrong,
that is a bug worth reporting on its own.

**Tests should state the failure they prevent.** A docstring saying which real-world
breakage a test guards against is worth more than the assertion itself.
[`tests/test_radtv_live_findings.py`](tests/test_radtv_live_findings.py) is the model —
every case there corresponds to something that was genuinely broken.

**Errors carry hints.** Failures in this tool are overwhelmingly environmental — stale
sessions, missing runtimes, flaky upstreams — so `RadMigrateError` takes a `hint` with
the concrete next action. Use it.

## Reporting bugs

Include the output of `rad-migrate doctor` and, where relevant, `rad-migrate <cmd> --json`.
Both are already redacted, but do skim them before pasting.

For Rad API behaviour, [`docs/rad-api-notes.md`](docs/rad-api-notes.md) records what is
known — check whether your issue is already documented there.

## Licence

Contributions are accepted under the [MIT Licence](LICENSE).
