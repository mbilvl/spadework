"""The plan: which tasks a run would do, in what order, and what they cost.

Each task's state comes from ``statuses``, which the engine derives from
``state.json`` and the task keys (``spadework.engine.planning``). A task
without one is ``pending``.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import Literal

from spadework.budget import DEFAULT_ESTIMATES_USD
from spadework.config import LimitsSection, ModelsSection
from spadework.inputs import InputFile, match_inputs
from spadework.playbook import Task
from spadework.project import Project

SUMMARY_RESERVE_USD = DEFAULT_ESTIMATES_USD["summary"]

TaskState = Literal["pending", "fresh", "stale", "failed", "invalid", "pinned"]

# States whose task a run drafts. A failed task is retried; an invalid one
# waits for its key to change or --force (docs/spec/04-engine.md).
_TO_RUN: frozenset[TaskState] = frozenset({"pending", "stale", "failed"})


@dataclass(frozen=True)
class TaskStatus:
    state: TaskState
    # Why a task is stale, for example "inputs/2026-09-30-client-call.md added".
    reason: str | None = None


@dataclass(frozen=True)
class PlannedTask:
    id: str
    title: str
    output: str
    tier: str
    model: str
    depends_on: tuple[str, ...]
    has_research: bool
    inputs: tuple[InputFile, ...]
    state: TaskState
    reason: str | None
    # The estimates are upper bounds: the task's budget_usd for the draft,
    # and the research default when the task has research.
    research_estimate_usd: float
    draft_estimate_usd: float
    selected: bool = True
    """False for a task that ``--only`` leaves out."""

    @property
    def will_run(self) -> bool:
        return self.selected and self.state in _TO_RUN

    @property
    def estimate_usd(self) -> float:
        if not self.will_run:
            return 0.0
        return round(self.research_estimate_usd + self.draft_estimate_usd, 2)


@dataclass(frozen=True)
class Plan:
    project: str
    playbook: str
    playbook_version: str
    limits: LimitsSection
    tasks: tuple[PlannedTask, ...]

    @property
    def estimate_usd(self) -> float:
        """A ceiling on the spend for the tasks that would run."""
        return round(sum(task.estimate_usd for task in self.tasks), 2)

    def never_start(self) -> tuple[PlannedTask, ...]:
        """Tasks that could not start within the run budget.

        The engine starts a session only if its estimate fits in what is left
        after the summary's reserve (docs/spec/04-engine.md).
        """
        room = self.limits.budget_usd - SUMMARY_RESERVE_USD
        return tuple(
            task
            for task in self.tasks
            if task.will_run and task.estimate_usd > round(room, 2)
        )


def research_estimate_usd(task: Task) -> float:
    return DEFAULT_ESTIMATES_USD["research"] if task.research is not None else 0.0


def task_estimate_usd(task: Task) -> float:
    """An upper bound on a task's cost: its draft budget, plus any research."""
    return round(task.budget_usd + research_estimate_usd(task), 2)


def build_plan(
    project: Project,
    statuses: Mapping[str, TaskStatus] | None = None,
    *,
    models: ModelsSection | None = None,
    limits: LimitsSection | None = None,
    only: Collection[str] = (),
) -> Plan:
    """The plan for ``project``.

    ``models`` and ``limits`` replace the project's, as ``run``'s options do.
    ``only`` selects tasks, as ``--only`` does; empty selects every task.
    """
    statuses = statuses or {}
    models = models or project.config.models
    planned = []
    for task in project.playbook.tasks:
        status = statuses.get(task.id, TaskStatus("pending"))
        planned.append(
            PlannedTask(
                id=task.id,
                title=task.title,
                output=task.output,
                tier=task.tier,
                model=models.draft if task.tier == "draft" else models.review,
                depends_on=task.depends_on,
                has_research=task.research is not None,
                inputs=tuple(match_inputs(task.inputs, project.inputs)),
                state=status.state,
                reason=status.reason,
                research_estimate_usd=research_estimate_usd(task),
                draft_estimate_usd=task.budget_usd,
                selected=not only or task.id in only,
            )
        )
    return Plan(
        project=project.config.project.name,
        playbook=project.playbook.name,
        playbook_version=project.playbook.version,
        limits=limits or project.config.limits,
        tasks=tuple(planned),
    )
