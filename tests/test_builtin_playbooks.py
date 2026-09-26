"""The built-in playbooks and the examples must always load and render."""

import json
from pathlib import Path

import pytest

from spadework.config import render_config
from spadework.playbook import builtin_root, load_playbook, resolve_playbook
from spadework.project import load_project
from spadework.render import placeholder_context, public_view, render_task_body
from tests.conftest import Invoke

REPO = Path(__file__).parent.parent
BUILTINS = (
    sorted(
        path.name
        for path in builtin_root().iterdir()
        if (path / "playbook.toml").is_file()
    )
    if builtin_root().is_dir()
    else []
)
EXAMPLES = sorted(
    path.parent.name for path in (REPO / "examples").glob("*/spadework.toml")
)


@pytest.mark.parametrize("name", BUILTINS)
def test_builtin_playbook_loads(name: str, tmp_path: Path) -> None:
    playbook = load_playbook(resolve_playbook(name, tmp_path))

    assert playbook.name == name
    assert playbook.ref.kind == "built-in"
    assert playbook.brief_template is not None
    assert playbook.tasks


@pytest.mark.parametrize("name", BUILTINS)
def test_a_project_made_from_a_builtin_passes_check(
    cli: Invoke, name: str, tmp_path: Path
) -> None:
    root = tmp_path / "demo"
    assert cli("init", root, "--playbook", name).exit_code == 0

    # load_project renders every template against the brief template as written.
    project = load_project(root)
    result = cli("check", "--json", "-C", root)

    assert result.exit_code == 0, result.stderr
    document = json.loads(result.stdout)
    assert [task["id"] for task in document["tasks"]] == [
        t.id for t in project.playbook.tasks
    ]


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_passes_check(cli: Invoke, name: str) -> None:
    result = cli("check", "-C", REPO / "examples" / name)

    assert result.exit_code == 0, result.stderr


@pytest.mark.parametrize("name", EXAMPLES)
def test_example_show_public(cli: Invoke, name: str) -> None:
    result = cli("check", "--show-public", "-C", REPO / "examples" / name)

    assert result.exit_code == 0, result.stderr


# Regressions from the M1 review, on the shipped content.

EMPTY_OPTIONALS = """\
---
title: T
reader: R
public_context: >
  A folded public description
  that ends with a newline.
priorities: [Fast]
site_languages: []
launch_target: ""
---
Body
"""


@pytest.mark.parametrize("name", BUILTINS)
def test_research_questions_have_no_blank_line_left_by_public_context(
    name: str, tmp_path: Path
) -> None:
    root = tmp_path / "demo"
    playbook = load_playbook(resolve_playbook(name, tmp_path))
    (root / "inputs").mkdir(parents=True)
    (root / "spadework.toml").write_text(render_config("demo", name))
    (root / "brief.md").write_text(EMPTY_OPTIONALS)

    project = load_project(root)
    view = public_view(project.brief, project.playbook)

    assert playbook.research_tasks
    for prompt in view.research:
        assert "that ends with a newline.\n\n\n" not in prompt.questions
        assert "that ends with a newline.\n\n" in prompt.questions


def test_empty_optional_keys_count_as_not_given(tmp_path: Path) -> None:
    root = tmp_path / "demo"
    (root / "inputs").mkdir(parents=True)
    (root / "spadework.toml").write_text(render_config("demo", "discovery-website"))
    (root / "brief.md").write_text(EMPTY_OPTIONALS)
    project = load_project(root)

    def body(task_id: str) -> str:
        task = project.playbook.task(task_id)
        context = placeholder_context("demo", project.brief, task, project.playbook, [])
        return render_task_body(project.playbook, task, context)

    assert "The brief does not say which languages" in body("scope")
    assert "The brief gives a launch target" not in body("roadmap")


def test_quickstart_no_longer_promises_a_price(tmp_path: Path) -> None:
    playbook = load_playbook(resolve_playbook("quickstart", tmp_path))

    assert "$" not in playbook.description
