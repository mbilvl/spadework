from pathlib import Path

import pytest

from spadework.errors import ExitCode, InvalidError
from spadework.project import find_project, load_project
from tests.conftest import BRIEF, MakeProject


def test_loads_everything(make_project: MakeProject) -> None:
    project = load_project(make_project(inputs={"notes.md": "# Notes"}))

    assert project.config.project.name == "acme-fabrication"
    assert project.playbook.name == "fixture"
    assert project.brief.front.title == "Acme Fabrication — test pack"
    assert [f.name for f in project.inputs] == ["notes.md"]
    assert project.warnings == ()


def test_needs_a_config(tmp_path: Path) -> None:
    with pytest.raises(InvalidError) as caught:
        load_project(tmp_path)

    assert caught.value.message == f"there is no spadework.toml in {tmp_path}"
    assert caught.value.exit_code == ExitCode.INVALID


def test_an_unknown_playbook_points_at_the_config(make_project: MakeProject) -> None:
    root = make_project()
    config = root / "spadework.toml"
    config.write_text(
        config.read_text().replace("./playbooks/valid", "./playbooks/nope")
    )

    with pytest.raises(InvalidError) as caught:
        load_project(root)

    assert [str(p) for p in caught.value.problems] == [
        "spadework.toml:5: playbook folder './playbooks/nope' does not exist"
    ]


def test_warns_about_large_inputs(make_project: MakeProject) -> None:
    project = load_project(make_project(inputs={"big.txt": "x" * 500_001}))

    (warning,) = project.warnings
    assert warning.message == "inputs/ holds 501 KB, over the 500 KB soft limit"
    assert "Trim or summarise" in warning.hint


def test_warns_when_research_has_no_public_context(make_project: MakeProject) -> None:
    brief = BRIEF.replace(
        '"A stainless-steel fabricator planning a catalogue website."', "'  '"
    )
    project = load_project(make_project(brief=brief))

    (warning,) = project.warnings
    assert warning.message == (
        "public_context in brief.md is empty, so research sessions will have nothing "
        "to go on"
    )
    assert "spadework check --show-public" in warning.hint


def test_no_public_context_warning_without_research(make_project: MakeProject) -> None:
    brief = BRIEF.replace(
        'public_context: "A stainless-steel fabricator planning a catalogue '
        'website."\n',
        "",
    )
    root = make_project(brief=brief)
    tasks = root / "playbooks" / "valid" / "tasks"
    (tasks / "landscape.md").unlink()
    (tasks / "plan.md").unlink()

    project = load_project(root)

    assert project.brief.public_context is None
    assert project.warnings == ()


@pytest.mark.parametrize("spec", [".", ".."])
def test_the_project_cannot_be_inside_its_playbook(
    make_project: MakeProject, spec: str
) -> None:
    # Otherwise a template could {% include "brief.md" %} into research questions.
    root = make_project()
    playbook = root / "playbooks" / "valid"
    for entry in playbook.iterdir():
        entry.rename((root if spec == "." else root.parent) / entry.name)
    config = root / "spadework.toml"
    config.write_text(config.read_text().replace("./playbooks/valid", spec))

    with pytest.raises(InvalidError) as caught:
        load_project(root)

    assert [str(p) for p in caught.value.problems] == [
        f"spadework.toml:5: playbook '{spec}' contains the project folder"
    ]
    assert caught.value.hint == (
        "Keep the playbook in a folder of its own, such as ./playbooks/mine."
    )


def test_find_project_looks_here_then_in_each_folder_above(tmp_path: Path) -> None:
    root = tmp_path / "acme"
    (root / "out" / "deep").mkdir(parents=True)
    (root / "spadework.toml").write_text("schema_version = 1\n")

    assert find_project(root) == root
    assert find_project(root / "out" / "deep") == root
    assert find_project(tmp_path) is None


def test_find_project_takes_the_nearest(tmp_path: Path) -> None:
    (tmp_path / "spadework.toml").write_text("")
    inner = tmp_path / "clients" / "acme"
    inner.mkdir(parents=True)
    (inner / "spadework.toml").write_text("")

    assert find_project(inner) == inner
    assert find_project(tmp_path / "clients") == tmp_path


def test_warns_about_brief_keys_nothing_reads(make_project: MakeProject) -> None:
    brief = BRIEF.replace("priorities:", "priorites: [x]\npriorities:")

    (warning,) = load_project(make_project(brief=brief)).warnings

    assert (warning.path, warning.line) == ("brief.md", 5)
    assert warning.message == "unknown key 'priorites' (did you mean 'priorities'?)"


def test_warns_about_inputs_it_ignores(make_project: MakeProject) -> None:
    project = load_project(make_project(inputs={"a.md": "# A", "scan.pdf": b"%PDF"}))

    assert project.ignored_inputs == ("inputs/scan.pdf",)
    (warning,) = project.warnings
    assert warning == project.ignored_inputs_warning()
    assert warning.message == (
        "inputs/scan.pdf is ignored: only .md and .txt inputs are read"
    )
