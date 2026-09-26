import json
from pathlib import Path

import pytest

from spadework.render import TEMPLATE_HINT
from tests.conftest import BRIEF, Invoke, MakeProject

PLAN = """\
playbook  fixture 1.0.0
budget    $6.00 · 2h · 2 review rounds

  task       state    reason   est.
  overview   pending          $0.50
  landscape  pending          $1.25
  plan       pending          $1.25
estimated spend for pending and stale work: up to $3.00 (cap $6.00)
"""


def test_prints_the_plan(cli: Invoke, make_project: MakeProject) -> None:
    result = cli("check", "-C", make_project())

    assert result.exit_code == 0
    assert result.stdout == PLAN
    assert result.stderr == (
        "See what research sessions will see: spadework check --show-public\n"
    )


def test_uses_the_current_folder(
    cli: Invoke, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(make_project())

    result = cli("check")

    assert result.exit_code == 0
    assert result.stdout == PLAN


def test_verbose_says_where_things_came_from(
    cli: Invoke, make_project: MakeProject
) -> None:
    result = cli("check", "-v", "-C", make_project(inputs={"a.md": "# A"}))

    assert "playbook: playbooks/valid\ninputs: 1 files\n" in result.stderr


def test_review_rounds_are_spelled_out(cli: Invoke, make_project: MakeProject) -> None:
    root = make_project()
    config = root / "spadework.toml"
    base = config.read_text()

    config.write_text(base + '\n[limits]\nrounds = 1\nmax_time = "90m"\n')
    assert "budget    $6.00 · 90m · 1 review round\n" in cli("check", "-C", root).stdout
    config.write_text(base + "\n[limits]\nrounds = 0\n")
    assert "budget    $6.00 · 2h · no review\n" in cli("check", "-C", root).stdout


def test_json(cli: Invoke, make_project: MakeProject) -> None:
    result = cli("check", "--json", "-C", make_project(inputs={"calls/a.md": "# A"}))

    assert result.exit_code == 0
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["ok"] is True
    assert document["project"] == {"name": "acme-fabrication"}
    assert document["playbook"] == {
        "name": "fixture",
        "version": "1.0.0",
        "source": "path",
        "location": "./playbooks/valid",
    }
    assert document["limits"] == {
        "budget_usd": 6.0,
        "max_time": "2h",
        "max_time_s": 7200,
        "rounds": 2,
        "session_timeout": "25m",
        "session_timeout_s": 1500,
    }
    assert document["models"] == {"draft": "sonnet", "review": "opus"}
    assert document["inputs"] == [{"path": "inputs/calls/a.md", "bytes": 3}]
    assert document["tasks"][1] == {
        "id": "landscape",
        "title": "Landscape",
        "output": "02-landscape.md",
        "tier": "draft",
        "model": "sonnet",
        "depends_on": ["overview"],
        "research": True,
        "inputs": ["inputs/calls/a.md"],
        "state": "pending",
        "reason": None,
        "estimate_usd": 1.25,
    }
    assert document["estimate_usd"] == 3.0
    assert document["warnings"] == []
    assert "public" not in document


def test_json_with_show_public(cli: Invoke, make_project: MakeProject) -> None:
    result = cli("check", "--json", "--show-public", "-C", make_project())

    document = json.loads(result.stdout)
    assert document["public"] == {
        "public_context": "A stainless-steel fabricator planning a catalogue website.",
        "research": [
            {
                "task": "landscape",
                "questions": "For A stainless-steel fabricator planning a catalogue "
                'website.:\n- five comparable companies for "Landscape", in en\n',
            }
        ],
    }


def test_show_public_prints_what_research_sees(
    cli: Invoke, make_project: MakeProject
) -> None:
    result = cli("check", "--show-public", "-C", make_project())

    assert result.exit_code == 0
    lines = result.stdout.splitlines()
    assert lines[0].startswith("public_context ─")
    assert lines[1] == "A stainless-steel fabricator planning a catalogue website."
    assert lines[2] == ""
    assert lines[3].startswith(
        "landscape: research questions (up to 4 searches, 5 fetches) ─"
    )
    assert lines[4:] == [
        "For A stainless-steel fabricator planning a catalogue website.:",
        '- five comparable companies for "Landscape", in en',
    ]
    assert "Brief.md" not in result.stdout
    assert "Acme Fabrication makes" not in result.stdout
    assert result.stderr == (
        "Research sessions see this and nothing else from brief.md or inputs/.\n"
    )


def test_show_public_with_an_empty_public_context(
    cli: Invoke, make_project: MakeProject
) -> None:
    brief = BRIEF.replace(
        '"A stainless-steel fabricator planning a catalogue website."', '""'
    )

    result = cli("check", "--show-public", "-C", make_project(brief=brief))

    assert result.stdout.splitlines()[1] == "(empty)"
    assert "warning: public_context in brief.md is empty" in result.stderr


def test_show_public_without_research(cli: Invoke, make_project: MakeProject) -> None:
    root = make_project()
    tasks = root / "playbooks" / "valid" / "tasks"
    (tasks / "landscape.md").unlink()
    (tasks / "plan.md").unlink()

    result = cli("check", "--show-public", "-C", root)

    assert result.stdout == (
        "This playbook has no research tasks, so nothing from this project goes to a "
        "web-facing session.\n"
    )


def test_a_total_over_the_cap_is_not_a_warning(
    cli: Invoke, make_project: MakeProject
) -> None:
    # est. is a ceiling: a run that reaches the cap stops and can resume.
    root = make_project()
    config = root / "spadework.toml"
    config.write_text(config.read_text() + "\n[limits]\nbudget_usd = 2.5\n")

    result = cli("check", "-q", "-C", root)

    assert result.exit_code == 0
    assert result.stderr == ""
    assert result.stdout.endswith(
        "estimated spend for pending and stale work: up to $3.00 (cap $2.50)\n"
    )


def test_warns_when_a_task_could_never_start(
    cli: Invoke, make_project: MakeProject
) -> None:
    root = make_project()
    config = root / "spadework.toml"
    config.write_text(config.read_text() + "\n[limits]\nbudget_usd = 1.70\n")

    result = cli("check", "-C", root)
    quiet = cli("check", "-q", "-C", root)
    as_json = cli("check", "--json", "-C", root)

    assert result.exit_code == 0
    hint = (
        "Raise limits.budget_usd in spadework.toml, or lower the task's budget_usd "
        "in the playbook.\n"
    )
    assert result.stderr == (
        "warning: task 'landscape' can never start: its estimate ($1.25) plus the "
        "summary's reserve ($0.50) is over the run budget ($1.70)\n"
        + hint
        + "warning: task 'plan' can never start: its estimate ($1.25) plus the "
        "summary's reserve ($0.50) is over the run budget ($1.70)\n"
        + hint
        + "See what research sessions will see: spadework check --show-public\n"
    )
    assert quiet.stderr.count("warning: ") == 2
    assert "Raise limits" not in quiet.stderr
    warnings = json.loads(as_json.stdout)["warnings"]
    assert [w["message"].split(" can")[0] for w in warnings] == [
        "task 'landscape'",
        "task 'plan'",
    ]


def test_a_task_that_just_fits_is_not_a_warning(
    cli: Invoke, make_project: MakeProject
) -> None:
    root = make_project()
    config = root / "spadework.toml"
    config.write_text(config.read_text() + "\n[limits]\nbudget_usd = 1.75\n")

    assert "warning" not in cli("check", "-C", root).stderr


def test_template_exceptions_exit_3(cli: Invoke, make_project: MakeProject) -> None:
    root = make_project()
    task = root / "playbooks" / "valid" / "tasks" / "plan.md"
    task.write_text("---\ntitle: P\noutput: 03-plan.md\n---\nx\n{{ 1 / 0 }}\n")

    result = cli("check", "-C", root)

    assert result.exit_code == 3
    assert result.stderr.startswith(
        "error: playbooks/valid/tasks/plan.md:6: template: division by zero "
        "(ZeroDivisionError)\n"
    )
    assert "bug" not in result.stderr


def test_os_errors_name_the_path_and_are_not_bugs(
    cli: Invoke, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = make_project()

    def denied(folder: Path) -> None:
        raise PermissionError(13, "Permission denied", str(folder / "spadework.toml"))

    monkeypatch.setattr("spadework.project.load_project", denied)

    result = cli("check", "-C", root)

    assert result.exit_code == 1
    assert result.stderr == (
        f"error: cannot use {(root / 'spadework.toml').resolve().as_posix()}: "
        "Permission denied\n"
        "Check that the path exists and that you may read and write it.\n"
    )


def test_warns_about_large_inputs(cli: Invoke, make_project: MakeProject) -> None:
    result = cli("check", "-C", make_project(inputs={"big.md": "x" * 600_000}))

    assert result.exit_code == 0
    assert "warning: inputs/ holds 600 KB, over the 500 KB soft limit" in result.stderr


@pytest.mark.parametrize(
    ("fixture", "expected"),
    [
        (
            "cycle",
            "error: playbooks/cycle/tasks/alpha.md:4: depends_on: tasks depend on each "
            "other in a cycle: alpha → gamma → beta → alpha\n",
        ),
        (
            "unknown-dependency",
            "error: playbooks/unknown-dependency/tasks/ia.md:6: depends_on: there is "
            "no task 'scop' (did you mean 'scope'?)\n",
        ),
        (
            "duplicate-output",
            "error: playbooks/duplicate-output/tasks/summary.md:3: output "
            "'01-Scope.md' is also used by task 'scope'\n",
        ),
        (
            "bad-task",
            "error: playbook 'bad-task' is not valid\n"
            "  playbooks/bad-task/tasks/scope.md:3: output: '00-summary.md' is "
            "reserved for the file spadework writes itself\n",
        ),
        (
            "bad-manifest",
            "error: playbooks/bad-manifest/playbook.toml is not valid\n"
            "  playbooks/bad-manifest/playbook.toml:3: version: 'one' is not valid",
        ),
        (
            "undefined-variable",
            "error: playbooks/undefined-variable/tasks/scope.md:7: template: brief has "
            "no 'budget'\n",
        ),
        (
            "leaky-research",
            "error: playbooks/leaky-research/tasks/landscape.md:7: research.questions: "
            "brief.body is not available to research questions",
        ),
    ],
)
def test_invalid_playbooks_exit_3_with_an_actionable_error(
    cli: Invoke, make_project: MakeProject, fixture: str, expected: str
) -> None:
    result = cli("check", "-C", make_project(fixture))

    assert result.exit_code == 3
    assert result.stdout == ""
    assert result.stderr.startswith(expected)
    hint = result.stderr.splitlines()[-1]
    if fixture in ("undefined-variable", "leaky-research"):
        assert hint == TEMPLATE_HINT
    else:
        assert hint == "Fix the playbook, then run `spadework check` again."


def test_invalid_config_exits_3(cli: Invoke, make_project: MakeProject) -> None:
    root = make_project()
    config = root / "spadework.toml"
    config.write_text(config.read_text() + "\n[limits]\nbudget = 5\n")

    result = cli("check", "-C", root)

    assert result.exit_code == 3
    assert result.stderr == (
        "error: spadework.toml:8: unknown key 'limits.budget' (did you mean "
        "'budget_usd'?)\n"
        "Fix spadework.toml, then run `spadework check` again.\n"
    )


def test_invalid_brief_exits_3(cli: Invoke, make_project: MakeProject) -> None:
    brief = BRIEF.replace('reader: "A developer scoping a catalogue website"\n', "")

    result = cli("check", "-C", make_project(brief=brief))

    assert result.exit_code == 3
    assert result.stderr == (
        "error: brief.md: 'reader' is required\n"
        "Fix the front matter of brief.md, then run `spadework check` again.\n"
    )


def test_missing_brief_exits_3(cli: Invoke, make_project: MakeProject) -> None:
    result = cli("check", "-C", make_project(brief=None))

    assert result.exit_code == 3
    assert result.stderr.startswith("error: brief.md: the file does not exist\n")


def test_unsupported_inputs_exit_3(cli: Invoke, make_project: MakeProject) -> None:
    result = cli("check", "-C", make_project(inputs={"scan.pdf": b"%PDF-1.7"}))

    assert result.exit_code == 3
    assert result.stderr == (
        "error: inputs/scan.pdf: only .md and .txt inputs are supported\n"
        "Convert them to Markdown or plain text, or move them out of inputs/.\n"
    )


def test_errors_as_json(cli: Invoke, make_project: MakeProject) -> None:
    result = cli("check", "--json", "-C", make_project("unknown-dependency"))

    assert result.exit_code == 3
    assert json.loads(result.stdout) == {
        "schema_version": 1,
        "ok": False,
        "error": {
            "message": "playbook 'unknown-dependency' is not valid",
            "hint": "Fix the playbook, then run `spadework check` again.",
            "exit_code": 3,
            "problems": [
                {
                    "path": "playbooks/unknown-dependency/tasks/ia.md",
                    "line": 6,
                    "message": (
                        "depends_on: there is no task 'scop' (did you mean 'scope'?)"
                    ),
                }
            ],
        },
    }
    assert result.stderr.startswith("error: ")


def test_an_unknown_builtin_playbook_points_at_the_config(
    cli: Invoke, make_project: MakeProject, tmp_path: Path
) -> None:
    root = make_project()
    config = root / "spadework.toml"
    config.write_text(
        config.read_text().replace("./playbooks/valid", "no-such-playbook")
    )

    result = cli("check", "-C", root)

    assert result.exit_code == 3
    assert result.stderr.startswith(
        "error: spadework.toml:5: there is no built-in playbook named "
        "'no-such-playbook'\n"
        "Built-in playbooks: "
    )


def test_os_errors_show_their_traceback_with_verbose(
    cli: Invoke, make_project: MakeProject, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(folder: Path) -> None:
        raise PermissionError(13, "Permission denied", str(folder))

    monkeypatch.setattr("spadework.project.load_project", denied)

    result = cli("check", "-v", "-C", make_project())

    assert result.exit_code == 1
    assert isinstance(result.exception, PermissionError)
