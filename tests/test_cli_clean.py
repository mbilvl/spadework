"""``spadework clean``: remove engine state, and nothing else."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from typer.testing import CliRunner

from spadework.clean import human_size, plan_clean
from spadework.cli import app
from spadework.lock import LockOwner
from tests.conftest import Invoke

RUN_1 = "20260927T210411Z-3f9a"
RUN_2 = "20260928T090000Z-0b1c"


@pytest.fixture(autouse=True)
def beside_the_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project after two runs, with research records and a pack."""
    root = tmp_path / "acme-fabrication"
    state = root / ".spadework"
    for run in (RUN_1, RUN_2):
        sessions = state / "runs" / run / "sessions"
        sessions.mkdir(parents=True)
        (state / "runs" / run / "manifest.json").write_text("{}")
        (sessions / "001-scope-draft.jsonl").write_text("x" * 1500)
    records = state / "records"
    records.mkdir()
    (records / "landscape.research.json").write_text("r" * 2000)
    (records / "stack.research.json").write_text("r" * 500)
    (records / "landscape.draft.json").write_text("{}")
    (state / "state.json").write_text("{}")
    (state / "work").mkdir()
    (state / "work" / "01-scope.md").write_text("# Scope\n")
    (root / "spadework.toml").write_text("schema_version = 1\n")
    (root / "brief.md").write_text("---\ntitle: x\n---\n")
    (root / "inputs").mkdir()
    (root / "inputs" / "call.md").write_text("# Call\n")
    (root / "out").mkdir()
    (root / "out" / "01-scope.md").write_text("# Scope\n")
    return root


def user_files(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if ".spadework" not in path.parts
    )


USER_FILES = [
    "brief.md",
    "inputs",
    "inputs/call.md",
    "out",
    "out/01-scope.md",
    "spadework.toml",
]


def test_runs_removes_every_run(cli: Invoke, project: Path) -> None:
    result = cli("clean", "--runs", "--yes", "-C", project)

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "removed acme-fabrication/.spadework/runs/ (2 runs, 3.0 KB)\n"
    )
    assert result.stderr == ""
    state = project / ".spadework"
    assert not (state / "runs").exists()
    assert (state / "records" / "landscape.research.json").exists()
    assert (state / "state.json").exists()
    assert not (state / "lock").exists()
    assert user_files(project) == USER_FILES


def test_cache_removes_the_research_records_only(cli: Invoke, project: Path) -> None:
    result = cli("clean", "--cache", "--yes", "-C", project)

    assert result.exit_code == 0, result.output
    assert result.stdout == (
        "removed 2 research records from acme-fabrication/.spadework/records/ "
        "(2.5 KB)\n"
    )
    assert result.stderr == (
        "  acme-fabrication/.spadework/records/landscape.research.json\n"
        "  acme-fabrication/.spadework/records/stack.research.json\n"
        "The next run researches these tasks again.\n"
    )
    records = project / ".spadework" / "records"
    assert sorted(path.name for path in records.iterdir()) == ["landscape.draft.json"]
    assert (project / ".spadework" / "runs" / RUN_1).is_dir()
    assert user_files(project) == USER_FILES


def test_all_removes_the_whole_state_folder_and_warns_about_out(
    cli: Invoke, project: Path
) -> None:
    result = cli("clean", "--all", "--yes", "-C", project)

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(
        "removed acme-fabrication/.spadework/ (all engine state, with 2 runs; "
    )
    assert result.stderr == (
        "warning: the next run will have no record of the files in "
        "acme-fabrication/out/, so it will leave them as they are\n"
        "Delete out/ as well if you want the next run to write the pack afresh.\n"
    )
    assert not (project / ".spadework").exists()
    assert user_files(project) == USER_FILES


def test_all_without_a_pack_does_not_warn(cli: Invoke, project: Path) -> None:
    (project / "out" / "01-scope.md").unlink()

    result = cli("clean", "--all", "--yes", "-C", project)

    assert result.exit_code == 0
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("flags", "message"),
    [
        ([], "error: choose what to remove: --runs, --cache or --all\n"),
        (["--runs", "--all"], "error: choose one of --runs and --all, not both\n"),
        (
            ["--runs", "--cache", "--all"],
            "error: choose one of --runs, --cache and --all, not all three\n",
        ),
    ],
)
def test_exactly_one_of_the_three(
    cli: Invoke, project: Path, flags: list[str], message: str
) -> None:
    result = cli("clean", *flags, "--yes", "-C", project)

    assert result.exit_code == 2
    assert result.stderr.startswith(message)
    assert (project / ".spadework" / "runs").is_dir()


def test_without_a_terminal_it_needs_yes(cli: Invoke, project: Path) -> None:
    result = cli("clean", "--runs", "-C", project)

    assert result.exit_code == 2
    assert result.stderr == (
        "error: not removing acme-fabrication/.spadework/runs/ (2 runs, 3.0 KB) "
        "without --yes\n"
        "Pass --yes to remove it, or run clean in a terminal to be asked first.\n"
    )
    assert (project / ".spadework" / "runs").is_dir()
    assert not (project / ".spadework" / "lock").exists()


@pytest.mark.parametrize(("answer", "removed"), [("y\n", True), ("\n", False)])
def test_on_a_terminal_it_asks_first(
    project: Path, monkeypatch: pytest.MonkeyPatch, answer: str, removed: bool
) -> None:
    monkeypatch.setattr("spadework.cli._can_ask", lambda: True)

    result = CliRunner().invoke(
        app, ["clean", "--runs", "-C", str(project)], input=answer
    )

    assert result.exit_code == 0, result.output
    assert result.stderr.startswith(
        "Remove acme-fabrication/.spadework/runs/ (2 runs, 3.0 KB)? [y/N]:"
    )
    assert (project / ".spadework" / "runs").exists() is not removed
    # CliRunner echoes the answer to stdout, as a terminal would show it.
    assert ("removed acme-fabrication" in result.stdout) is removed
    if not removed:
        assert result.stderr.endswith("Nothing was removed.\n")


def test_refuses_while_a_run_holds_the_lock(cli: Invoke, project: Path) -> None:
    lock = project / ".spadework" / "lock"
    owner = LockOwner(os.getppid(), socket.gethostname(), "2026-09-27T21:04:11Z")
    lock.write_bytes(owner.to_bytes())

    result = cli("clean", "--all", "--yes", "-C", project)

    assert result.exit_code == 7
    assert result.stderr.startswith("error: another run holds the project lock")
    assert (project / ".spadework" / "runs").is_dir()
    assert lock.exists()


def test_a_dead_runs_lock_is_reclaimed_with_a_warning(
    cli: Invoke, project: Path
) -> None:
    lock = project / ".spadework" / "lock"
    owner = LockOwner(2**22 + 12345, socket.gethostname(), "2026-09-27T21:04:11Z")
    lock.write_bytes(owner.to_bytes())

    result = cli("clean", "--runs", "--yes", "-C", project)

    assert result.exit_code == 0
    assert result.stderr.startswith("warning: reclaimed the project lock")
    assert not lock.exists()


@pytest.mark.parametrize(
    ("flag", "message"),
    [
        ("--runs", "Nothing to remove: there are no runs.\n"),
        ("--cache", "Nothing to remove: there are no research records.\n"),
    ],
)
def test_nothing_to_remove(cli: Invoke, project: Path, flag: str, message: str) -> None:
    cli("clean", "--runs", "--yes", "-C", project)
    cli("clean", "--cache", "--yes", "-C", project)

    result = cli("clean", flag, "-C", project)

    assert (result.exit_code, result.stdout, result.stderr) == (0, "", message)


def test_nothing_to_remove_without_state(cli: Invoke, project: Path) -> None:
    cli("clean", "--all", "--yes", "-C", project)

    result = cli("clean", "--all", "-C", project)

    assert result.exit_code == 0
    assert result.stderr == (
        "Nothing to remove: there is no acme-fabrication/.spadework/.\n"
    )
    assert not (project / ".spadework").exists()


def test_needs_a_project(cli: Invoke, tmp_path: Path) -> None:
    (tmp_path / "elsewhere" / ".spadework").mkdir(parents=True)

    result = cli("clean", "--all", "--yes", "-C", tmp_path / "elsewhere")

    assert result.exit_code == 3
    assert (tmp_path / "elsewhere" / ".spadework").is_dir()


def test_finds_the_project_from_a_folder_inside_it(
    cli: Invoke, project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(project / "out")

    result = cli("clean", "--runs", "--yes")

    assert result.exit_code == 0
    assert result.stdout == "removed ../.spadework/runs/ (2 runs, 3.0 KB)\n"


def test_a_project_argument_gets_a_hint(cli: Invoke, project: Path) -> None:
    result = cli("clean", "acme-fabrication", "--runs")

    assert result.exit_code == 2
    assert result.stderr == (
        "error: clean takes no arguments, but was given 'acme-fabrication'\n"
        "Did you mean `spadework clean -C acme-fabrication`?\n"
    )


def test_plan_clean_counts_files_and_sizes(project: Path) -> None:
    plan = plan_clean(project, "cache")

    assert [target.relative for target in plan.targets] == [
        ".spadework/records/landscape.research.json",
        ".spadework/records/stack.research.json",
    ]
    assert plan.size == 2500
    assert plan.detail == "2 research records"
    assert not plan.empty


@pytest.mark.parametrize(
    ("size", "shown"),
    [
        (0, "0 bytes"),
        (1, "1 byte"),
        (999, "999 bytes"),
        (1000, "1.0 KB"),
        (2_500_000, "2.5 MB"),
        (3_000_000_000, "3.0 GB"),
    ],
)
def test_human_size(size: int, shown: str) -> None:
    assert human_size(size) == shown


def test_help_names_every_option(cli: Invoke) -> None:
    text = cli("clean", "--help").stdout

    for option in ("--runs", "--cache", "--all", "--yes"):
        assert option in text
