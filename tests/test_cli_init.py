import os
import shutil
import tomllib
from pathlib import Path

import pytest

from spadework.scaffold import project_name
from tests.conftest import PLAYBOOKS, Invoke


@pytest.fixture
def builtins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Built-in playbooks made from test fixtures, so tests do not depend on content."""
    root = tmp_path / "builtins"
    shutil.copytree(PLAYBOOKS / "valid", root / "discovery-website")
    shutil.copytree(PLAYBOOKS / "valid", root / "quickstart")
    shutil.copytree(PLAYBOOKS / "cycle", root / "broken")
    monkeypatch.setattr("spadework.playbook.builtin_root", lambda: root)
    return root


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def test_creates_a_project(cli: Invoke, builtins: Path, workdir: Path) -> None:
    result = cli("init", "demo", "--playbook", "quickstart")

    assert result.exit_code == 0
    assert result.stdout == (
        "Created a fixture project in demo:\n"
        "  spadework.toml\n  brief.md\n  inputs/\n  .gitignore\n"
    )
    assert (
        result.stderr
        == "Next: edit demo/brief.md, then run `spadework check -C demo`.\n"
    )
    root = workdir / "demo"
    config = tomllib.loads((root / "spadework.toml").read_text())
    assert config["project"] == {"name": "demo", "playbook": "quickstart"}
    template = (builtins / "quickstart" / "brief.template.md").read_text()
    assert (root / "brief.md").read_text() == template
    assert (root / "inputs").is_dir()
    assert list((root / "inputs").iterdir()) == []
    assert (
        root / ".gitignore"
    ).read_text() == "# spadework's engine state\n.spadework/\n"


def test_the_new_project_passes_check(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    assert cli("init", "demo", "--playbook", "quickstart").exit_code == 0

    result = cli("check", "-C", "demo")

    assert result.exit_code == 0
    assert result.stdout.startswith("playbook  fixture 1.0.0\n")


def test_the_default_playbook_is_discovery_website(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    cli("init", "demo")

    config = tomllib.loads((workdir / "demo" / "spadework.toml").read_text())
    assert config["project"]["playbook"] == "discovery-website"


def test_init_in_the_current_folder(cli: Invoke, builtins: Path, workdir: Path) -> None:
    result = cli("init", "--playbook", "quickstart")

    assert result.exit_code == 0
    assert result.stdout.startswith("Created a fixture project in .:\n")
    assert result.stderr == "Next: edit brief.md, then run `spadework check`.\n"
    assert (
        tomllib.loads((workdir / "spadework.toml").read_text())["project"]["name"]
        == "work"
    )


def test_init_relative_to_the_project_option(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    (workdir / "clients").mkdir()

    result = cli("-C", "clients", "init", "acme", "--playbook", "quickstart")

    assert result.exit_code == 0
    assert (workdir / "clients" / "acme" / "brief.md").is_file()
    assert "run `spadework check -C clients/acme`" in result.stderr


def test_paths_outside_the_current_folder_are_shown_absolute(
    cli: Invoke, builtins: Path, workdir: Path, tmp_path: Path
) -> None:
    result = cli("init", tmp_path / "elsewhere", "--playbook", "quickstart")

    shown = (tmp_path / "elsewhere").resolve().as_posix()
    assert result.stdout.startswith(f"Created a fixture project in {shown}:\n")


def test_quiet_prints_nothing(cli: Invoke, builtins: Path, workdir: Path) -> None:
    result = cli("init", "demo", "-q", "--playbook", "quickstart")

    assert result.exit_code == 0
    assert (result.stdout, result.stderr) == ("", "")


def test_a_local_playbook_is_written_relative_to_the_project(
    cli: Invoke, workdir: Path
) -> None:
    shutil.copytree(PLAYBOOKS / "valid", workdir / "playbooks" / "mine")

    result = cli("init", "clients/acme", "--playbook", "./playbooks/mine")

    assert result.exit_code == 0
    root = workdir / "clients" / "acme"
    config = tomllib.loads((root / "spadework.toml").read_text())
    assert config["project"]["playbook"] == "../../playbooks/mine"
    assert cli("check", "-C", root).exit_code == 0


def test_a_playbook_inside_the_project_gets_a_dot_slash(
    cli: Invoke, workdir: Path
) -> None:
    shutil.copytree(PLAYBOOKS / "valid", workdir / "demo" / "playbooks" / "mine")

    result = cli("init", "demo", "--force", "--playbook", "demo/playbooks/mine")

    assert result.exit_code == 0
    config = tomllib.loads((workdir / "demo" / "spadework.toml").read_text())
    assert config["project"]["playbook"] == "./playbooks/mine"


def test_refuses_a_folder_that_is_not_empty(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    (workdir / "demo").mkdir()
    (workdir / "demo" / "notes.md").write_text("mine")

    result = cli("init", "demo", "--playbook", "quickstart")

    assert result.exit_code == 2
    assert result.stderr == (
        "error: demo is not empty\n"
        "Create the project in a new folder with `spadework init NAME`, or pass "
        "--force to add it here; any spadework.toml and brief.md it replaces are "
        "kept as .bak files.\n"
    )
    assert sorted(p.name for p in (workdir / "demo").iterdir()) == ["notes.md"]


def test_a_git_repository_counts_as_empty(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    (workdir / "demo" / ".git").mkdir(parents=True)

    result = cli("init", "demo", "--playbook", "quickstart")

    assert result.exit_code == 0


def test_any_other_file_makes_a_folder_not_empty(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    (workdir / "demo" / ".git").mkdir(parents=True)
    (workdir / "demo" / ".DS_Store").write_bytes(b"\0")

    result = cli("init", "demo", "--playbook", "quickstart")

    assert result.exit_code == 2
    assert result.stderr.startswith("error: demo is not empty\n")


def test_force_replaces_config_and_brief_and_keeps_the_rest(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    root = workdir / "demo"
    (root / "inputs").mkdir(parents=True)
    (root / "inputs" / "call.md").write_text("# Call")
    (root / "brief.md").write_text("old")
    (root / "spadework.toml").write_text("old")
    (root / ".gitignore").write_text("node_modules/")

    result = cli("init", "demo", "--force", "--playbook", "quickstart")

    assert result.exit_code == 0
    assert result.stdout == (
        "Created a fixture project in demo:\n"
        "  .gitignore (added .spadework/)\n  spadework.toml\n  brief.md\n"
    )
    assert result.stderr.startswith(
        "warning: replaced the existing spadework.toml and brief.md; the old ones "
        "are kept as spadework.toml.bak and brief.md.bak\n"
    )
    assert (root / "inputs" / "call.md").read_text() == "# Call"
    assert (root / "brief.md").read_text().startswith("---\n# REQUIRED")
    assert (root / "brief.md.bak").read_text() == "old"
    assert (root / "spadework.toml.bak").read_text() == "old"
    assert (root / ".gitignore").read_text() == "node_modules/\n.spadework/\n"


def test_force_numbers_backups_and_skips_unchanged_files(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    root = workdir / "demo"
    root.mkdir()
    (root / "brief.md").write_text("first")
    (root / "brief.md.bak").write_text("older")
    template = (builtins / "quickstart" / "brief.template.md").read_text()

    assert cli("init", "demo", "--force", "--playbook", "quickstart").exit_code == 0
    (root / "brief.md").write_text("second")
    result = cli("init", "demo", "--force", "--playbook", "quickstart")
    again = cli("init", "demo", "--force", "--playbook", "quickstart")

    assert (root / "brief.md.bak").read_text() == "older"
    assert (root / "brief.md.1.bak").read_text() == "first"
    assert (root / "brief.md.2.bak").read_text() == "second"
    assert (root / "brief.md").read_text() == template
    assert "the old one is kept as brief.md.2.bak" in result.stderr
    # Nothing is lost when the files hold what init writes, so no backup.
    assert again.stderr.startswith(
        "warning: replaced the existing spadework.toml and brief.md\n"
    )
    assert not (root / "brief.md.3.bak").exists()


def test_force_leaves_a_gitignore_that_already_ignores_state(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    (workdir / "demo").mkdir()
    (workdir / "demo" / ".gitignore").write_text("/.spadework\n")

    cli("init", "demo", "--force", "--playbook", "quickstart")

    assert (workdir / "demo" / ".gitignore").read_text() == "/.spadework\n"


def test_refuses_a_file(cli: Invoke, builtins: Path, workdir: Path) -> None:
    (workdir / "demo").write_text("x")

    result = cli("init", "demo", "--playbook", "quickstart")

    assert result.exit_code == 2
    assert result.stderr == "error: demo is a file\nChoose a folder for the project.\n"


def test_an_unknown_playbook_is_a_usage_error(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    result = cli("init", "demo", "--playbook", "quickstrat")

    assert result.exit_code == 2
    assert result.stderr == (
        "error: there is no built-in playbook named 'quickstrat'\n"
        "Did you mean 'quickstart'? Built-in playbooks: broken, discovery-website, "
        "quickstart.\n"
    )
    assert not (workdir / "demo").exists()


def test_an_invalid_playbook_exits_3(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    result = cli("init", "demo", "--playbook", "broken")

    assert result.exit_code == 3
    assert "tasks depend on each other in a cycle" in result.stderr
    assert "bug in a built-in playbook" in result.stderr
    assert not (workdir / "demo").exists()


def test_a_playbook_needs_a_brief_template(cli: Invoke, workdir: Path) -> None:
    shutil.copytree(PLAYBOOKS / "valid", workdir / "mine")
    (workdir / "mine" / "brief.template.md").unlink()

    result = cli("init", "demo", "--playbook", "./mine")

    assert result.exit_code == 3
    assert result.stderr == (
        "error: playbook 'fixture' has no brief.template.md\n"
        "Add a brief.template.md to the playbook for init to copy to brief.md.\n"
    )


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Acme Fabrication", "acme-fabrication"),
        ("2026 bid!", "2026-bid"),
        ("---", "project"),
    ],
)
def test_project_names_are_slugs(name: str, slug: str) -> None:
    assert project_name(Path(name)) == slug


# Regressions from the M1 review.


@pytest.mark.parametrize("target", ["pb/demo", "pb"])
def test_refuses_a_project_inside_the_playbook_folder(
    cli: Invoke, workdir: Path, target: str
) -> None:
    shutil.copytree(PLAYBOOKS / "valid", workdir / "pb")
    before = sorted(p.name for p in (workdir / "pb").iterdir())

    result = cli("init", target, "--force", "--playbook", "./pb")

    assert result.exit_code == 2
    assert result.stderr == (
        f"error: {target} is inside the playbook folder\n"
        "Create the project outside the playbook folder, so templates cannot read "
        "its brief.\n"
    )
    assert sorted(p.name for p in (workdir / "pb").iterdir()) == before


@pytest.mark.parametrize(
    ("name", "make", "kind"),
    [
        ("inputs", lambda p: p.write_text("x"), "a folder"),
        (".gitignore", lambda p: p.mkdir(), "a file"),
        ("brief.md", lambda p: p.mkdir(), "a file"),
        ("spadework.toml", lambda p: p.mkdir(), "a file"),
    ],
)
def test_force_refuses_paths_of_the_wrong_kind_before_writing(
    cli: Invoke, builtins: Path, workdir: Path, name: str, make: object, kind: str
) -> None:
    (workdir / "demo").mkdir()
    make(workdir / "demo" / name)  # type: ignore[operator]

    result = cli("init", "demo", "--force", "--playbook", "quickstart")

    assert result.exit_code == 2
    assert result.stderr == (
        f"error: demo/{name} is not {kind}\n"
        "Rename or remove it, then run `spadework init` again.\n"
    )
    assert sorted(p.name for p in (workdir / "demo").iterdir()) == [name]


def test_force_keeps_a_gitignore_in_another_encoding(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    (workdir / "demo").mkdir()
    (workdir / "demo" / ".gitignore").write_bytes("# caf\xe9".encode("latin-1"))

    result = cli("init", "demo", "--force", "--playbook", "quickstart")

    assert result.exit_code == 0
    assert (workdir / "demo" / ".gitignore").read_bytes() == b"# caf\xe9\n.spadework/\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_read_only_folder_is_an_os_error_not_a_bug(
    cli: Invoke, builtins: Path, workdir: Path
) -> None:
    (workdir / "ro").mkdir()
    (workdir / "ro").chmod(0o555)
    try:
        result = cli("init", "ro/demo", "--playbook", "quickstart")
    finally:
        (workdir / "ro").chmod(0o755)

    assert result.exit_code == 1
    assert result.stderr == (
        "error: cannot use ro/demo: Permission denied\n"
        "Check that the path exists and that you may read and write it.\n"
    )


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_playbook_is_not_a_bug(cli: Invoke, workdir: Path) -> None:
    shutil.copytree(PLAYBOOKS / "valid", workdir / "pb")
    (workdir / "pb").chmod(0)
    try:
        result = cli("init", "demo", "--playbook", "./pb")
    finally:
        (workdir / "pb").chmod(0o755)

    assert result.exit_code == 3
    assert result.stderr == (
        "error: pb/playbook.toml: cannot read the file: Permission denied\n"
        "Make the playbook folder and its files readable.\n"
    )
    assert not (workdir / "demo").exists()


def test_paths_are_absolute_when_run_from_the_file_system_root(
    cli: Invoke, builtins: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir("/")

    result = cli("init", tmp_path / "demo", "--playbook", "quickstart")

    shown = (tmp_path / "demo").resolve().as_posix()
    assert result.stdout.startswith(f"Created a fixture project in {shown}:\n")
