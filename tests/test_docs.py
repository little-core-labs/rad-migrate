"""Tests that keep the documentation honest.

Docs drift silently: a renamed flag breaks the README with nothing to catch it. These
check the claims the docs make against what the CLI actually offers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
from click.testing import CliRunner

from radmigrate.cli import app

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
runner = CliRunner()
cli = typer.main.get_command(app)

#: Flags belonging to tools other than radmigrate that legitimately appear in the docs.
FOREIGN_FLAGS = {
    "--rm",  # docker
    "--source",  # gitleaks / docker
    "--config",
    "--redact",
    "--verbose",
    "--no-banner",
    "--dev",  # uv
    "--frozen",
    "--help",
}


def _cli_flags() -> set[str]:
    """Every flag the CLI accepts, across the root and all subcommands.

    Reads the command objects rather than parsing ``--help`` output: help text is
    wrapped and truncated to terminal width, so a long flag like
    ``--cleanup-after-publish`` shows up elided and a text scrape misses it.
    """
    flags: set[str] = set()
    commands = [cli, *cli.commands.values()]  # type: ignore[attr-defined]
    for command in commands:
        for param in command.params:
            flags.update(o for o in param.opts if o.startswith("--"))
            flags.update(o for o in param.secondary_opts if o.startswith("--"))
    return flags


def _readme_flags() -> set[str]:
    text = README.read_text(encoding="utf-8")
    found: set[str] = set()
    # Flags in fenced code blocks and in the flag tables.
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL):
        found.update(re.findall(r"(--[a-z][a-z0-9-]+)", block))
    for cell in re.findall(r"\|\s*`(--[a-z][a-z0-9-]+)[^`]*`", text):
        found.add(cell)
    return found - FOREIGN_FLAGS


class TestReadmeAccuracy:
    def test_every_documented_flag_exists(self) -> None:
        documented = _readme_flags()
        actual = _cli_flags()
        missing = sorted(documented - actual)
        assert not missing, f"README documents flags the CLI does not have: {missing}"

    def test_every_command_is_documented(self) -> None:
        text = README.read_text(encoding="utf-8")
        undocumented = [
            name
            for name in cli.commands  # type: ignore[attr-defined]
            if f"radmigrate {name}" not in text
        ]
        assert not undocumented, f"commands missing from the README: {undocumented}"

    def test_headline_example_uses_the_shorthand(self) -> None:
        """The pitch is one command; the first example should demonstrate exactly that."""
        first_block = re.search(r"```bash\n(.*?)```", README.read_text(encoding="utf-8"), re.DOTALL)
        assert first_block is not None
        assert "radmigrate @yourchannel" in first_block.group(1)

    def test_install_instructions_are_currently_accurate(self) -> None:
        """radmigrate is not on PyPI yet, so `uvx radmigrate` must not be presented as
        the working install path. Remove this test when the package is published."""
        text = README.read_text(encoding="utf-8")
        install = text.split("## Install", 1)[1].split("---", 1)[0]
        primary = install.split("<details>", 1)[0]
        assert "git+https://github.com/" in primary
        assert "uvx radmigrate" not in primary

    @pytest.mark.parametrize(
        "path",
        [
            "SECURITY.md",
            "CONTRIBUTING.md",
            "docs/rad-api-notes.md",
            ".env.example",
            "src/radmigrate/source/base.py",
            "src/radmigrate/dest/base.py",
            "skills/radmigrate/references/troubleshooting.md",
        ],
    )
    def test_linked_files_exist(self, path: str) -> None:
        assert (ROOT / path).exists(), f"README links to a missing file: {path}"


class TestSkillAccuracy:
    SKILL = ROOT / "skills" / "radmigrate" / "SKILL.md"

    def test_skill_commands_exist(self) -> None:
        """Only commands inside code fences are checked; prose mentions radmigrate
        in ordinary sentences and matching those produces noise, not signal."""
        text = self.SKILL.read_text(encoding="utf-8")
        blocks = "\n".join(re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL))
        referenced = set(re.findall(r"^radmigrate ([a-z][a-z-]+)", blocks, re.MULTILINE))
        unknown = sorted(referenced - set(cli.commands))  # type: ignore[attr-defined]
        assert not unknown, f"SKILL.md references unknown commands: {unknown}"

    def test_skill_flags_exist(self) -> None:
        text = self.SKILL.read_text(encoding="utf-8")
        documented = set(re.findall(r"`(--[a-z][a-z0-9-]+)", text))
        missing = sorted(documented - _cli_flags() - FOREIGN_FLAGS)
        assert not missing, f"SKILL.md documents flags the CLI does not have: {missing}"
