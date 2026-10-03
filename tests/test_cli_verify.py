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
    # The command to run next names the project, as it is not the current folder.
    shown = quickstart.resolve().as_posix()
    assert f"`spadework run -C {shown}` does it" in result.stderr


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


def hold_lock(root: Path) -> LockOwner:
    owner = LockOwner(os.getppid(), socket.gethostname(), "2026-09-27T21:04:11Z")
    lock = root / ".spadework" / "lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(owner.to_bytes())
    return owner


@pytest.mark.parametrize("json_output", [False, True])
def test_verify_exits_7_while_a_run_holds_the_project(
    cli: Invoke, quickstart: Path, stub: None, json_output: bool
) -> None:
    cli("run", "-q", "-C", quickstart)
    sources = quickstart / "out" / "sources.md"
    before = sources.read_bytes()
    owner = hold_lock(quickstart)

    args = ["verify", "-C", quickstart] + (["--json"] if json_output else [])
    result = cli(*args)

    assert result.exit_code == 7
    # Nothing was checked or written, and the lock is left to its owner.
    assert sources.read_bytes() == before
    assert (quickstart / ".spadework" / "lock").read_bytes() == owner.to_bytes()
    message, hint = result.stderr.splitlines()
    assert message.startswith("error: ")
    assert hint
    if json_output:
        document = json.loads(result.stdout)
        assert document["schema_version"] == 1
        assert document["ok"] is False
        assert document["error"]["exit_code"] == 7
        assert document["error"]["message"] == message.removeprefix("error: ")
    else:
        assert result.stdout == ""


@pytest.mark.parametrize("json_output", [False, True])
def test_verify_exits_3_for_an_invalid_project(
    cli: Invoke, quickstart: Path, json_output: bool
) -> None:
    (quickstart / "brief.md").unlink()

    args = ["verify", "-C", quickstart] + (["--json"] if json_output else [])
    result = cli(*args)

    assert result.exit_code == 3
    assert result.stderr.startswith("error: ")
    if json_output:
        document = json.loads(result.stdout)
        assert (document["ok"], document["error"]["exit_code"]) == (False, 3)
    assert not (quickstart / ".spadework" / "lock").exists()


def test_verify_exits_1_for_an_unexpected_error(
    cli: Invoke, quickstart: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cli("run", "-q", "-C", quickstart)

    class Broken(Stub):
        def verify_sources(
            self, sources: Sequence[Source], *, time_limit: float | None = None
        ) -> Mapping[str, Found]:
            raise RuntimeError("boom")

    monkeypatch.setattr("spadework.engine.reverify.NetworkVerifier", Broken)

    result = cli("verify", "--json", "-C", quickstart)

    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["message"] == (
        "unexpected error: RuntimeError: boom"
    )
    assert not (quickstart / ".spadework" / "lock").exists()


def test_verify_json_shape(cli: Invoke, quickstart: Path, stub: None) -> None:
    cli("run", "-q", "-C", quickstart)
    path = quickstart / "out" / "01-landscape.md"
    path.write_text(path.read_text() + "\nMine.\n")

    document = json.loads(cli("verify", "--json", "-C", quickstart).stdout)

    assert list(document) == [
        "schema_version",
        "ok",
        "counts",
        "sources",
        "written",
        "kept",
        "render_owed",
        "warnings",
    ]
    assert list(document["counts"]) == [
        "verified",
        "quote_not_found",
        "unreachable",
        "unsupported",
        "unchecked",
    ]
    assert len(document["sources"]) == 5
    assert all(
        list(source)
        == ["id", "task", "url", "status", "detail", "checked_at", "final_url"]
        for source in document["sources"]
    )
    assert document["kept"] == ["out/01-landscape.md"]
    assert document["written"] == [
        "out/00-summary.md",
        "out/pack.json",
        "out/sources.md",
    ]
    assert document["warnings"] == []


def test_verify_after_an_interrupted_run_leaves_the_render_to_it(
    cli: Invoke, quickstart: Path, stub: None
) -> None:
    import subprocess
    import sys

    # A real process killed during its fourth session, the first critic.
    code = f"""
import os, sys
from spadework.executors.fake import FakeExecutor

calls = []
original = FakeExecutor.run

def run(self, spec, events):
    calls.append(spec.task)
    if len(calls) == 4:
        os._exit(137)
    return original(self, spec, events)

FakeExecutor.run = run
from spadework.cli import app
sys.argv = ["spadework", "run", "-q", "-C", {str(quickstart)!r}]
app()
"""
    env = {**os.environ, "SPADEWORK_EXECUTOR": "fake"}
    killed = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, check=False
    )
    assert killed.returncode == 137

    result = cli("verify", "--json", "-C", quickstart)

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["render_owed"] is True
    assert document["written"] == []
    assert not (quickstart / "out").exists()
    assert "the last run did not finish" not in result.stderr  # JSON: no prose
    human = cli("verify", "-C", quickstart)
    assert (
        "warning: the last run did not finish, so only the records were updated"
        in human.stderr
    )
    resumed = cli("run", "-q", "-C", quickstart)
    assert resumed.exit_code == 0
    assert "| ✓ verified |" in (quickstart / "out" / "01-landscape.md").read_text()
