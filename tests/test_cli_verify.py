"""``spadework verify`` through the CLI."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from spadework.engine.verification import Found
from spadework.lock import LockOwner
from spadework.records import Source
from tests.conftest import Invoke

QUICKSTART_BRIEF = """\
---
title: "Acme Fabrication — quickstart"
reader: "A developer preparing a second call with Acme Fabrication"
public_context: "A stainless-steel fabricator planning a catalogue website."
---

# The project

A catalogue website.
"""


@pytest.fixture
def quickstart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPADEWORK_EXECUTOR", "fake")
    monkeypatch.delenv("SPADEWORK_FAKE_SCRIPT", raising=False)
    root = tmp_path / "acme-quickstart"
    root.mkdir()
    (root / "spadework.toml").write_text(
        'schema_version = 1\n\n[project]\nname = "acme-quickstart"\n'
        'playbook = "quickstart"\n'
    )
    (root / "brief.md").write_text(QUICKSTART_BRIEF)
    (root / "inputs").mkdir()
    return root


class Stub:
    def __init__(self, **options: Any) -> None:
        pass

    def verify_sources(
        self, sources: Sequence[Source], *, time_limit: float | None = None
    ) -> Mapping[str, Found]:
        return {"S1": "verified", "S2": "quote_not_found"}


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("spadework.engine.reverify.NetworkVerifier", Stub)


def test_verify_checks_the_sources_and_reports(
    cli: Invoke, quickstart: Path, stub: None
) -> None:
    assert cli("run", "-q", "-C", quickstart).exit_code == 0

    result = cli("verify", "-C", quickstart)

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == [
        "task",
        "sources",
        "verified",
        "quote",
        "not",
        "found",
        "unreachable",
    ]
    assert lines[1].split() == ["landscape", "5", "1", "1", "3"]
    assert lines[2] == "5 sources: 1 verified, 1 quote not found, 3 unreachable"
    assert "  wrote out/01-landscape.md" in lines
    assert "  wrote out/sources.md" in lines
    assert result.stderr.splitlines() == [
        "landscape: checking 5 sources",
        "A failed check is not proof that a source was made up: pages change, and "
        "some sites turn away automated readers. See out/sources.md.",
    ]
    assert "| ✓ verified |" in (quickstart / "out" / "01-landscape.md").read_text()


def test_verify_json(cli: Invoke, quickstart: Path, stub: None) -> None:
    cli("run", "-q", "-C", quickstart)

    result = cli("verify", "--json", "-C", quickstart)

    assert result.exit_code == 0
    assert result.stderr == ""
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["ok"] is True
    assert document["counts"] == {
        "verified": 1,
        "quote_not_found": 1,
        "unreachable": 3,
        "unsupported": 0,
        "unchecked": 0,
    }
    assert document["sources"][0] == {
        "id": "landscape:S1",
        "task": "landscape",
        "url": "https://directory.example/landscape",
        "status": "verified",
        "detail": None,
        "checked_at": None,
        "final_url": None,
    }
    assert "out/sources.md" in document["written"]
    assert document["kept"] == []
    assert document["render_owed"] is False


def test_verify_says_which_edited_documents_it_left_alone(
    cli: Invoke, quickstart: Path, stub: None
) -> None:
    cli("run", "-q", "-C", quickstart)
    path = quickstart / "out" / "01-landscape.md"
    path.write_text(path.read_text() + "\nMine.\n")

    result = cli("verify", "-C", quickstart)

    assert result.exit_code == 0
    assert (
        "warning: out/01-landscape.md was edited by a person, so its Sources "
        "section still shows the earlier results\nout/sources.md has the new ones.\n"
    ) in result.stderr
    assert path.read_text().endswith("\nMine.\n")


def test_verify_without_research_yet(cli: Invoke, quickstart: Path) -> None:
    result = cli("verify", "-C", quickstart)

    assert result.exit_code == 0
    assert result.stdout == "no sources to check\n"
    assert "`spadework run` does it" in result.stderr


def test_verify_by_default_never_fetches_special_use_names(
    cli: Invoke, quickstart: Path
) -> None:
    # The fake's sources are on .example hosts: the real verifier refuses
    # them without a lookup, so this runs offline.
    cli("run", "-q", "-C", quickstart)

    result = cli("verify", "--json", "-C", quickstart)

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["counts"]["unreachable"] == 5
    assert {s["detail"] for s in document["sources"]} == {"blocked: special-use domain"}


def test_verify_exits_7_while_a_run_holds_the_project(
    cli: Invoke, quickstart: Path, stub: None
) -> None:
    cli("run", "-q", "-C", quickstart)
    lock = quickstart / ".spadework" / "lock"
    lock.write_bytes(
        LockOwner(os.getppid(), socket.gethostname(), "2026-09-27T21:04:11Z").to_bytes()
    )

    result = cli("verify", "--json", "-C", quickstart)

    assert result.exit_code == 7
    assert json.loads(result.stdout)["error"]["exit_code"] == 7


def test_verify_exits_3_for_an_invalid_project(cli: Invoke, quickstart: Path) -> None:
    (quickstart / "brief.md").unlink()

    result = cli("verify", "-C", quickstart)

    assert result.exit_code == 3
