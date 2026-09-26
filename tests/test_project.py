from pathlib import Path

import pytest

from spadework.errors import ExitCode, InvalidError
from spadework.project import load_project
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
    root = make_project(brief=BRIEF.replace("public_context: ", "unused: "))
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
