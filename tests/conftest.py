"""Shared helpers for spadework's tests."""

from __future__ import annotations

import ipaddress
import shutil
import socket
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner, Result

from spadework.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
PLAYBOOKS = FIXTURES / "playbooks"

BRIEF = """\
---
title: "Acme Fabrication — test pack"
reader: "A developer scoping a catalogue website"
public_context: "A stainless-steel fabricator planning a catalogue website."
priorities:
  - "Staff can edit the catalogue unaided"
---

# The client

Acme Fabrication makes stainless-steel equipment.
"""

CONFIG = """\
schema_version = 1

[project]
name = "acme-fabrication"
playbook = "{playbook}"
"""

MakeProject = Callable[..., Path]
Invoke = Callable[..., Result]


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The tests never look a name up in DNS: a test that would (a source URL
    that is not a special-use name, say) fails instead of reaching the
    network. IP literals and localhost still resolve, for local servers."""
    real = socket.getaddrinfo

    def getaddrinfo(host: object, *args: Any, **kwargs: Any) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        if name is None or name == "localhost" or _is_address(str(name)):
            return real(host, *args, **kwargs)  # type: ignore[arg-type]
        raise OSError(f"the tests are offline: no DNS lookup for {name!r}")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


def _is_address(name: str) -> bool:
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        return False
    return True


@pytest.fixture(autouse=True)
def plain_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Output that does not depend on the terminal or the CI environment."""
    monkeypatch.setenv("COLUMNS", "100")
    for name in ("FORCE_COLOR", "NO_COLOR", "SPADEWORK_DEBUG", "TTY_COMPATIBLE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def make_project(tmp_path: Path) -> MakeProject:
    """Create a project that uses a copy of a fixture playbook in playbooks/."""

    def make(
        playbook: str = "valid",
        *,
        brief: str | None = BRIEF,
        config: str | None = None,
        inputs: Mapping[str, str | bytes] | None = None,
    ) -> Path:
        root = tmp_path / "acme-fabrication"
        root.mkdir()
        shutil.copytree(PLAYBOOKS / playbook, root / "playbooks" / playbook)
        text = (
            config
            if config is not None
            else CONFIG.format(playbook=f"./playbooks/{playbook}")
        )
        (root / "spadework.toml").write_text(text, encoding="utf-8")
        if brief is not None:
            (root / "brief.md").write_text(brief, encoding="utf-8")
        (root / "inputs").mkdir()
        for name, content in (inputs or {}).items():
            path = root / "inputs" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
        return root

    return make


@pytest.fixture
def cli() -> Invoke:
    runner = CliRunner()

    def invoke(*args: str | Path, env: Mapping[str, str] | None = None) -> Result:
        return runner.invoke(app, [str(arg) for arg in args], env=env)

    return invoke
