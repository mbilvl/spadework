import shutil
from pathlib import Path

import pytest

from tests.conftest import PLAYBOOKS, Invoke, MakeProject


@pytest.fixture
def builtins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "builtins"
    shutil.copytree(PLAYBOOKS / "valid", root / "fixture")
    monkeypatch.setattr("spadework.playbook.builtin_root", lambda: root)
    return root


def test_list(cli: Invoke, builtins: Path, tmp_path: Path) -> None:
    result = cli("playbooks", "list", "-C", tmp_path)

    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "name     version  tasks  source    description",
        "fixture  1.0.0        3  built-in  "
        "A small playbook for spadework's own tests.",
    ]
    assert result.stderr == "Show one with: spadework playbooks show NAME\n"


def test_list_includes_the_projects_own_playbook(
    cli: Invoke, builtins: Path, make_project: MakeProject
) -> None:
    result = cli("playbooks", "list", "-C", make_project())

    lines = result.stdout.splitlines()
    assert len(lines) == 3
    assert lines[2].split()[:4] == ["fixture", "1.0.0", "3", "playbooks/valid"]


def test_list_reports_invalid_playbooks_and_carries_on(
    cli: Invoke, builtins: Path, make_project: MakeProject
) -> None:
    shutil.copytree(PLAYBOOKS / "cycle", builtins / "broken")

    result = cli("playbooks", "list", "-C", make_project("unknown-dependency"))

    assert result.exit_code == 0
    assert [line.split()[0] for line in result.stdout.splitlines()] == [
        "name",
        "fixture",
    ]
    assert "warning: playbook 'broken' is not valid: " in result.stderr
    assert (
        "warning: playbook './playbooks/unknown-dependency' is not valid"
        in result.stderr
    )


def test_list_with_an_unreadable_config(
    cli: Invoke, builtins: Path, tmp_path: Path
) -> None:
    (tmp_path / "spadework.toml").write_text("not toml")

    result = cli("playbooks", "list", "-C", tmp_path)

    assert result.exit_code == 0
    assert result.stderr.startswith(
        "warning: cannot read spadework.toml: spadework.toml:1:"
    )
    assert "fixture" in result.stdout


def test_list_with_no_playbooks(
    cli: Invoke, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("spadework.playbook.builtin_root", lambda: tmp_path / "none")

    result = cli("playbooks", "list", "-C", tmp_path)

    assert (result.exit_code, result.stdout, result.stderr) == (
        0,
        "",
        "No playbooks found.\n",
    )


def test_show(cli: Invoke, builtins: Path) -> None:
    result = cli("playbooks", "show", "fixture")

    folder = (builtins / "fixture").resolve().as_posix()
    assert result.exit_code == 0
    assert result.stdout.splitlines() == [
        "fixture 1.0.0 · built-in",
        "A small playbook for spadework's own tests.",
        "",
        f"folder       {folder}",
        "brief needs  title, reader, public_context, priorities",
        "defaults     draft tier · $0.50 per draft · at most 120 lines",
        "review       up to 4 findings per round",
        "appends to   prompts/rules.md",
        "",
        "  overview   Overview",
        "             → 01-overview.md · draft · est. $0.50",
        "  landscape  Landscape",
        "             → 02-landscape.md · draft · est. $1.25 · after overview",
        "             research: up to 4 searches and 5 fetches",
        "  plan       Plan",
        "             → 03-plan.md · review · est. $1.25 · after landscape, overview",
    ]
    assert result.stderr == (
        "Copy the folder to start a playbook of your own: "
        f"cp -R {folder} ./my-playbook\n"
    )


def test_show_a_local_playbook(
    cli: Invoke, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shutil.copytree(PLAYBOOKS / "cycle", tmp_path / "mine")
    shutil.rmtree(tmp_path / "mine" / "tasks")
    (tmp_path / "mine" / "tasks").mkdir()
    (tmp_path / "mine" / "tasks" / "only.md").write_text(
        "---\ntitle: Only\noutput: o.md\n---\n"
    )
    monkeypatch.chdir(tmp_path)

    result = cli("playbooks", "show", "./mine")

    assert result.exit_code == 0
    assert result.stdout.splitlines()[:8] == [
        "cycle 0.1.0 · mine",
        "An invalid playbook for spadework's own tests.",
        "",
        "folder       mine",
        "brief needs  title, reader",
        "defaults     draft tier",
        "review       up to 12 findings per round",
        "appends to   nothing",
    ]


def test_show_an_unknown_playbook_is_a_usage_error(cli: Invoke, builtins: Path) -> None:
    result = cli("playbooks", "show", "fixtur")

    assert result.exit_code == 2
    assert result.stderr == (
        "error: there is no built-in playbook named 'fixtur'\n"
        "Did you mean 'fixture'? Built-in playbooks: fixture.\n"
    )


def test_show_an_invalid_playbook_exits_3(cli: Invoke, tmp_path: Path) -> None:
    result = cli("playbooks", "show", PLAYBOOKS / "cycle", "-C", tmp_path)

    assert result.exit_code == 3
    assert "cycle: alpha → gamma → beta → alpha" in result.stderr


def test_playbooks_without_a_subcommand_prints_help(cli: Invoke) -> None:
    result = cli("playbooks")

    assert result.exit_code == 2
    assert "Usage: spadework playbooks" in result.output


@pytest.mark.parametrize(
    "args",
    [
        ["playbooks", "-C", "{root}", "list"],
        ["-C", "{root}", "playbooks", "list"],
        ["playbooks", "list", "-C", "{root}"],
        ["playbooks", "-q", "list"],
    ],
)
def test_global_options_work_on_the_group_too(
    cli: Invoke, builtins: Path, make_project: MakeProject, args: list[str]
) -> None:
    root = make_project()

    result = cli(*(arg.format(root=root) for arg in args))

    assert result.exit_code == 0, result.output
    if "-q" in args:
        assert result.stderr == ""
    else:
        assert "playbooks/valid" in result.stdout


def test_quiet_and_verbose_on_the_group_and_the_command(cli: Invoke) -> None:
    result = cli("playbooks", "-q", "list", "-v")

    assert result.exit_code == 2
    assert result.stderr.startswith("error: -q and -v cannot be used together\n")


def test_show_s_usage_line_names_its_argument_plainly(cli: Invoke) -> None:
    result = cli("playbooks", "show", "--help")

    assert result.stdout.startswith("Usage: spadework playbooks show [OPTIONS] NAME\n")


def test_list_takes_no_arguments(cli: Invoke, builtins: Path, tmp_path: Path) -> None:
    result = cli("playbooks", "list", "extra")

    assert result.exit_code == 2
    assert result.stderr == (
        "error: playbooks list takes no arguments, but was given 'extra'\n"
        "See `spadework playbooks list --help`.\n"
    )
