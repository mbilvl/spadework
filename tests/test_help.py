"""Help text is part of the interface, so changes to it are reviewed on purpose.

The expected text lives in tests/snapshots/help/. After changing help text,
regenerate it with:

    SPADEWORK_UPDATE_SNAPSHOTS=1 uv run pytest tests/test_help.py
"""

import os
from pathlib import Path

import pytest

from tests.conftest import Invoke

SNAPSHOTS = Path(__file__).parent / "snapshots" / "help"

COMMANDS = {
    "spadework": [],
    "spadework-init": ["init"],
    "spadework-check": ["check"],
    "spadework-run": ["run"],
    "spadework-status": ["status"],
    "spadework-verify": ["verify"],
    "spadework-logs": ["logs"],
    "spadework-doctor": ["doctor"],
    "spadework-clean": ["clean"],
    "spadework-playbooks": ["playbooks"],
    "spadework-playbooks-list": ["playbooks", "list"],
    "spadework-playbooks-show": ["playbooks", "show"],
}


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_help_matches_the_snapshot(cli: Invoke, name: str) -> None:
    result = cli(*COMMANDS[name], "--help")
    assert result.exit_code == 0

    snapshot = SNAPSHOTS / f"{name}.txt"
    if os.environ.get("SPADEWORK_UPDATE_SNAPSHOTS"):
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(result.stdout, encoding="utf-8")
    assert result.stdout == snapshot.read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_help_ends_with_examples(cli: Invoke, name: str) -> None:
    lines = cli(*COMMANDS[name], "--help").stdout.rstrip().splitlines()

    examples = lines.index("  Examples:")
    commands = [line.strip() for line in lines[examples + 1 :] if line.strip()]
    assert commands
    assert all(command.startswith("spadework ") for command in commands)
    assert any(" ".join(COMMANDS[name]) in command for command in commands)


@pytest.mark.parametrize("name", sorted(COMMANDS))
def test_global_options_are_in_every_help(cli: Invoke, name: str) -> None:
    text = cli(*COMMANDS[name], "--help").stdout

    for option in ("-C, --project PATH", "-q, --quiet", "-v, --verbose", "--no-color"):
        assert option in text
