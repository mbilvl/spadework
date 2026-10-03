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
    # and the research default when the task's research will run. Research
    # whose key is unchanged and whose record exists is reused: 0.
    research_estimate_usd: float
    draft_estimate_usd: float
    selected: bool = True
    """False for a task that ``--only`` leaves out."""
    research_reused: bool = False
    """The task has research, and a run would reuse its stored result."""

    @property
    def will_run(self) -> bool:
        return self.selected and self.state in _TO_RUN

    @property
    def estimate_usd(self) -> float:
        if not self.will_run:
            return 0.0
        return round(self.research_estimate_usd + self.draft_estimate_usd, 2)

    @property
    def sessions(self) -> tuple[tuple[str, float], ...]:
        """Each session a run would start for the task, with its estimate."""
        if not self.will_run:
            return ()
        found: list[tuple[str, float]] = []
        if self.has_research and not self.research_reused:
            found.append(("research", self.research_estimate_usd))
        found.append(("draft", self.draft_estimate_usd))
        return tuple(found)


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
        """Tasks with a session that could not start within the run budget."""
        return tuple(dict.fromkeys(task for task, _, _ in self.never_start_sessions()))

    def never_start_sessions(self) -> tuple[tuple[PlannedTask, str, float], ...]:
        """Each session that could not start within the run budget, as (task,
        kind, estimate).

        The engine gates each session on its own: it starts one only if its
        estimate fits in the budget less the summary's reserve
        (docs/spec/04-engine.md). A task's research and draft are never
        added together.
        """
        room = round(self.limits.budget_usd - SUMMARY_RESERVE_USD, 2)
        return tuple(
            (task, kind, estimate)
            for task in self.tasks
            for kind, estimate in task.sessions
            if estimate > room
        )


def research_estimate_usd(task: Task, *, reused: bool = False) -> float:
    """The research estimate: 0 without research, or when it is reused."""
    if task.research is None or reused:
        return 0.0
    return DEFAULT_ESTIMATES_USD["research"]


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
    reused_research: Collection[str] = (),
) -> Plan:
    """The plan for ``project``.

    ``models`` and ``limits`` replace the project's, as ``run``'s options do.
    ``only`` selects tasks, as ``--only`` does; empty selects every task.
    ``reused_research`` names the tasks whose stored research a run would
    reuse: their research costs nothing.
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
                research_estimate_usd=research_estimate_usd(
                    task, reused=task.id in reused_research
                ),
                draft_estimate_usd=task.budget_usd,
                selected=not only or task.id in only,
                research_reused=task.research is not None
                and task.id in reused_research,
            )
        )
    return Plan(
        project=project.config.project.name,
        playbook=project.playbook.name,
        playbook_version=project.playbook.version,
        limits=limits or project.config.limits,
        tasks=tuple(planned),
    )
