"""``spadework status``."""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

import pytest

from spadework.lock import LockOwner
from tests.conftest import Invoke, MakeProject


@pytest.fixture
def project(make_project: MakeProject, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("SPADEWORK_EXECUTOR", "fake")
    monkeypatch.delenv("SPADEWORK_FAKE_SCRIPT", raising=False)
    return make_project()


def test_before_any_run(cli: Invoke, project: Path) -> None:
    result = cli("status", "-C", project)

    assert result.exit_code == 0
    assert result.stdout == (
        "project   acme-fabrication · fixture 1.0.0\n"
        "run       no run is active\n"
        "last run  none yet\n"
        "\n"
        "  task       state    reason\n"
        "  overview   pending\n"
        "  landscape  pending\n"
        "  plan       pending\n"
    )
    assert not (project / ".spadework").exists()


def test_after_a_run(cli: Invoke, project: Path) -> None:
    cli("run", "-C", project)
    (project / "inputs" / "note.md").write_text("# Note\n")

    result = cli("status", "-C", project)

    lines = result.stdout.splitlines()
    assert re.fullmatch(
        r"last run  \d{8}T\d{6}Z-[0-9a-f]{4} · complete · \$0\.29 · 4 sessions · "
        r"0m\d\ds · started \d{4}-\d\d-\d\d \d\d:\d\d",
        lines[2],
    )
    assert lines[5] == "  overview   stale  inputs/note.md added"
    assert lines[6].startswith("  landscape  stale  dependency overview is stale")


def test_json(cli: Invoke, project: Path) -> None:
    cli("run", "-C", project)

    document = json.loads(cli("status", "--json", "-C", project).stdout)

    assert document["schema_version"] == 1
    assert document["ok"] is True
    assert document["active_run"] is None
    last = document["last_run"]
    assert last["outcome"] == "complete"
    assert last["finished"] is True
    assert last["unfinished"] is False
    assert last["sessions"] == 4
    assert last["cost_usd"] == pytest.approx(0.29)
    assert document["tasks"][0] == {
        "id": "overview",
        "output": "01-overview.md",
        "state": "fresh",
        "reason": None,
    }


def test_an_active_run(cli: Invoke, project: Path) -> None:
    lock = project / ".spadework" / "lock"
    lock.parent.mkdir()
    owner = LockOwner(os.getppid(), socket.gethostname(), "2026-09-27T21:04:11Z")
    lock.write_bytes(owner.to_bytes())

    result = cli("status", "-C", project)
    document = json.loads(cli("status", "--json", "-C", project).stdout)

    assert re.search(
        rf"^run       a run is active \(pid {os.getppid()}, started ",
        result.stdout,
        re.M,
    )
    assert document["active_run"] == {
        "pid": os.getppid(),
        "host": socket.gethostname(),
        "started_at": "2026-09-27T21:04:11Z",
        "alive": True,
    }


def test_a_lock_from_another_host_or_unreadable(cli: Invoke, project: Path) -> None:
    lock = project / ".spadework" / "lock"
    lock.parent.mkdir()
    lock.write_bytes(
        LockOwner(1, "elsewhere.example", "2026-09-27T21:04:11Z").to_bytes()
    )
    assert "a run on host elsewhere.example holds the lock" in (
        cli("status", "-C", project).stdout
    )

    lock.write_text("not a lock")
    assert "run       .spadework/lock exists but cannot be read" in (
        cli("status", "-C", project).stdout
    )


def test_an_invalid_project_exits_3(cli: Invoke, project: Path) -> None:
    (project / "brief.md").unlink()

    result = cli("status", "-C", project)

    assert result.exit_code == 3


def test_the_last_run_is_the_latest_even_within_one_second(
    cli: Invoke, project: Path
) -> None:
    runs = project / ".spadework" / "runs"
    assert cli("run", "-q", "-C", project).exit_code == 0
    [first] = runs.iterdir()
    # A second run in the same second, whose random suffix sorts first.
    second = runs / (first.name.split("-")[0] + "-0000")
    second.mkdir()
    manifest = json.loads((first / "manifest.json").read_text())
    manifest.update(run_id=second.name, outcome=None, ended_at=None)
    (second / "manifest.json").write_text(json.dumps(manifest))
    os.utime(first / "manifest.json", ns=(1, 1))

    document = json.loads(cli("status", "--json", "-C", project).stdout)

    assert document["last_run"]["run_id"] == second.name
    assert document["last_run"]["unfinished"] is True
