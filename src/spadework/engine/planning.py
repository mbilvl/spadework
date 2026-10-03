"""Each task's state from its keys, ``state.json`` and ``out/``, and the plan.

``check``, ``status`` and ``run`` all plan this way, so they agree on every
task's state and reason. Planning reads and never writes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from spadework.config import LimitsSection, ModelsSection
from spadework.engine.prompts import ENGINE_PROMPT_VERSION
from spadework.keys import TaskMaterial, task_materials
from spadework.plan import Plan, build_plan
from spadework.project import Project
from spadework.state import (
    OUT_DIR,
    STATE_DIR,
    DerivedStatus,
    State,
    derive_statuses,
    load_state,
    read_out_hashes,
    state_path,
)


@dataclass(frozen=True)
class Planned:
    """A plan, and what it was made from."""

    plan: Plan
    state: State
    materials: dict[str, TaskMaterial]
    statuses: dict[str, DerivedStatus]
    pinned: frozenset[str]
    """Tasks whose ``out/`` file a person edited, whatever ``--force`` says."""
    replaced: frozenset[str] = frozenset()
    """Pinned tasks that ``--force --only`` redrafts, replacing the edits."""
    warnings: tuple[tuple[str, str | None], ...] = field(default=())

    @property
    def to_run(self) -> tuple[str, ...]:
        return tuple(task.id for task in self.plan.tasks if task.will_run)


def _unedited_outputs(
    state: State, out_hashes: dict[str, str | None]
) -> dict[str, str | None]:
    """Treat a file in ``out/`` that holds what the engine was rendering as
    unedited.

    A run that stopped while rendering may have written the file without
    recording its hash (``state.rendering`` holds it). Its bytes are still
    the engine's, so it is not a person's edit and must not pin the task.
    """
    seen = dict(out_hashes)
    for entry in state.tasks.values():
        rendered = entry.rendered
        if rendered is None:
            continue
        pending = state.rendering.get(rendered.path)
        if pending is not None and seen.get(rendered.path) == pending:
            seen[rendered.path] = rendered.hash
    return seen


def reused_research(
    root: Path, state: State, materials: Mapping[str, TaskMaterial]
) -> frozenset[str]:
    """Tasks whose stored research matches their research key: a run reuses it."""
    reused = set()
    for task_id, material in materials.items():
        key = material.research_key
        name = None if key is None else state.research_record(task_id, key)
        if name is not None and (root / STATE_DIR / name).is_file():
            reused.add(task_id)
    return frozenset(reused)


RESEARCH_MISSING = "research record missing"


def _research_missing(
    statuses: dict[str, DerivedStatus],
    materials: Mapping[str, TaskMaterial],
    present: frozenset[str],
) -> dict[str, DerivedStatus]:
    """A fresh or invalid task with research whose record is gone (``clean
    --cache``) is stale: the next run researches and drafts it again."""
    changed = dict(statuses)
    for task_id, status in statuses.items():
        has_research = materials[task_id].research is not None
        if (
            has_research
            and task_id not in present
            and status.state
            in (
                "fresh",
                "invalid",
            )
        ):
            changed[task_id] = DerivedStatus.of("stale", [RESEARCH_MISSING])
    return changed


def plan_project(
    project: Project,
    state: State | None = None,
    *,
    models: ModelsSection | None = None,
    limits: LimitsSection | None = None,
    only: Sequence[str] = (),
    force: bool = False,
    refresh_research: bool = False,
) -> Planned:
    """Plan a run of ``project``. ``state`` defaults to its ``state.json``.

    ``--force`` makes the selected tasks stale. A pinned task is forced only
    when ``--only`` names it: ``--force`` on its own never replaces a
    person's edits (docs/spec/02-project-layout.md, Human edits). Research
    whose key is unchanged and whose record exists is reused, so it costs
    nothing in the plan, unless ``--refresh-research``.
    """
    if state is None:
        state = load_state(state_path(project.root))
    materials = task_materials(project, engine=ENGINE_PROMPT_VERSION, models=models)
    order = [task.id for task in project.playbook.tasks]
    out_hashes = _unedited_outputs(state, read_out_hashes(project.root, state))
    statuses = derive_statuses(order, materials, state, out_hashes)
    pinned = frozenset(task for task, s in statuses.items() if s.state == "pinned")
    selected = set(only) if only else set(order)
    warnings: list[tuple[str, str | None]] = []
    replaced: frozenset[str] = frozenset()
    if force:
        forced = selected if only else selected - pinned
        replaced = frozenset(forced & pinned)
        statuses = derive_statuses(order, materials, state, out_hashes, force=forced)
    for task_id in order:
        if task_id not in pinned or task_id not in selected:
            continue
        output = project.playbook.task(task_id).output
        if task_id in replaced:
            warnings.append(
                (f"--force replaces your edits to {OUT_DIR}/{output}", None)
            )
        elif only or force:
            warnings.append(
                (
                    f"task {task_id!r} is pinned: {OUT_DIR}/{output} was edited, so it "
                    "is not redrafted",
                    f"To replace your edits: spadework run --force --only {task_id}",
                )
            )
    present = reused_research(project.root, state, materials)
    statuses = _research_missing(statuses, materials, present)
    reused = frozenset() if refresh_research else present
    plan = build_plan(
        project,
        statuses,
        models=models,
        limits=limits,
        only=only,
        reused_research=reused,
    )
    return Planned(plan, state, materials, statuses, pinned, replaced, tuple(warnings))
