from pathlib import Path

import pytest

from spadework.plan import (
    SUMMARY_RESERVE_USD,
    TaskStatus,
    build_plan,
    task_estimate_usd,
)
from spadework.project import load_project
from tests.conftest import MakeProject


def test_every_task_is_pending_without_engine_state(make_project: MakeProject) -> None:
    project = load_project(
        make_project(inputs={"calls/one.md": "# One", "two.txt": "x"})
    )

    plan = build_plan(project)

    assert (plan.project, plan.playbook, plan.playbook_version) == (
        "acme-fabrication",
        "fixture",
        "1.0.0",
    )
    assert [(t.id, t.state, t.reason) for t in plan.tasks] == [
        ("overview", "pending", None),
        ("landscape", "pending", None),
        ("plan", "pending", None),
    ]
    overview, landscape, plan_task = plan.tasks
    assert [f.name for f in overview.inputs] == ["calls/one.md", "two.txt"]
    assert [f.name for f in landscape.inputs] == ["calls/one.md", "two.txt"]
    assert plan_task.inputs == ()
    assert (overview.model, plan_task.model) == ("sonnet", "opus")


def test_estimates_add_research_to_the_draft_budget(make_project: MakeProject) -> None:
    project = load_project(make_project())

    plan = build_plan(project)

    assert [t.estimate_usd for t in plan.tasks] == [0.50, 1.25, 1.25]
    assert plan.tasks[1].research_estimate_usd == 0.75
    assert plan.tasks[1].draft_estimate_usd == 0.50
    assert plan.estimate_usd == 3.00
    assert task_estimate_usd(project.playbook.task("landscape")) == 1.25


@pytest.mark.parametrize(
    ("state", "runs"),
    [
        ("fresh", False),
        ("pinned", False),
        ("stale", True),
        ("failed", True),
        # An invalid task waits for its key to change, or --force.
        ("invalid", False),
    ],
)
def test_statuses_from_the_engine(
    make_project: MakeProject, state: str, runs: bool
) -> None:
    project = load_project(make_project())

    plan = build_plan(project, {"overview": TaskStatus(state, "brief changed")})  # type: ignore[arg-type]

    overview = plan.tasks[0]
    assert (overview.state, overview.reason, overview.will_run) == (
        state,
        "brief changed",
        runs,
    )
    assert overview.estimate_usd == (0.50 if runs else 0.0)


def test_only_selects_tasks(make_project: MakeProject) -> None:
    project = load_project(make_project())

    plan = build_plan(project, only=["landscape"])

    assert [(t.id, t.selected, t.will_run) for t in plan.tasks] == [
        ("overview", False, False),
        ("landscape", True, True),
        ("plan", False, False),
    ]
    assert plan.estimate_usd == 1.25


def test_models_and_limits_can_be_overridden(make_project: MakeProject) -> None:
    project = load_project(make_project())
    models = project.config.models.model_copy(update={"review": "haiku"})
    limits = project.config.limits.model_copy(update={"budget_usd": 2.0})

    plan = build_plan(project, models=models, limits=limits)

    assert [t.model for t in plan.tasks] == ["sonnet", "sonnet", "haiku"]
    assert plan.limits.budget_usd == 2.0


def test_models_follow_the_config(make_project: MakeProject, tmp_path: Path) -> None:
    root = make_project()
    config = root / "spadework.toml"
    config.write_text(config.read_text() + '\n[models]\ndraft = "haiku"\n')

    plan = build_plan(load_project(root))

    assert [t.model for t in plan.tasks] == ["haiku", "haiku", "opus"]


def test_never_start_lists_tasks_that_cannot_fit_with_the_summary_reserve(
    make_project: MakeProject,
) -> None:
    root = make_project()
    config = root / "spadework.toml"
    config.write_text(config.read_text() + "\n[limits]\nbudget_usd = 1.70\n")

    plan = build_plan(load_project(root))
    fresh = build_plan(load_project(root), {"landscape": TaskStatus("fresh")})

    assert SUMMARY_RESERVE_USD == 0.50
    # Sessions are gated one by one: landscape's research ($0.75) and draft
    # ($0.50) each fit in $1.20, though not together. plan's draft does not.
    assert [task.id for task in plan.never_start()] == ["plan"]
    assert [(t.id, kind, usd) for t, kind, usd in plan.never_start_sessions()] == [
        ("plan", "draft", 1.25)
    ]
    assert [task.id for task in fresh.never_start()] == ["plan"]


def test_never_start_names_research_that_cannot_fit(make_project: MakeProject) -> None:
    root = make_project()
    config = root / "spadework.toml"
    config.write_text(config.read_text() + "\n[limits]\nbudget_usd = 1.20\n")

    plan = build_plan(load_project(root))

    assert [(t.id, kind) for t, kind, _ in plan.never_start_sessions()] == [
        ("landscape", "research"),
        ("plan", "draft"),
    ]
    # Research a run would reuse costs nothing, and cannot block the task.
    reused = build_plan(load_project(root), reused_research={"landscape"})
    landscape = next(t for t in reused.tasks if t.id == "landscape")
    assert landscape.research_reused
    assert landscape.research_estimate_usd == 0.0
    assert landscape.estimate_usd == 0.5
    assert landscape.sessions == (("draft", 0.5),)
    assert [t.id for t in reused.never_start()] == ["plan"]
    assert reused.estimate_usd == plan.estimate_usd - 0.75
