"""What the engine reports while it runs, for the live view, the plain lines
and ``run --json``.

Every event becomes one NDJSON line with ``schema_version``, ``event`` and
``ts`` (UTC). The vocabulary, in the order a run emits it:

- ``plan``: every task with its state, reason, and whether it will run
- ``up_to_date``: nothing to do; the last event of such a run
- ``run_started``: the run id, the versions and the limits
- ``session_started``, ``tool_call``, ``denied``, ``session_finished``: one
  session, with its number ``n``
- ``task_finished``: a task's new state: fresh, invalid, failed or skipped
- ``notice``: a warning or information, such as a reclaimed lock
- ``run_finished``: the outcome, its exit code, and the totals
- ``error``: the run could not start; the document of docs/spec/01-cli.md
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from spadework.manifest import Outcome
from spadework.plan import Plan
from spadework.validators import ValidatorResult

EVENT_SCHEMA_VERSION = 1

TaskOutcome = Literal["fresh", "invalid", "failed", "skipped"]


@dataclass(frozen=True)
class PlanReady:
    plan: Plan
    warnings: tuple[tuple[str, str | None], ...] = ()


@dataclass(frozen=True)
class UpToDate:
    exit_code: int
    invalid: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunStarted:
    run_id: str
    project: str
    playbook: str
    playbook_version: str
    executor: str
    executor_version: str | None
    budget_usd: float
    max_time_s: int
    rounds: int
    tasks: tuple[str, ...]
    """The tasks this run will work on, in order."""


@dataclass(frozen=True)
class SessionStarted:
    n: int
    task: str | None
    kind: str
    model: str
    round: int
    attempt: int
    max_budget_usd: float
    timeout_s: int


@dataclass(frozen=True)
class ToolUsed:
    n: int
    task: str | None
    kind: str
    name: str
    target: str


@dataclass(frozen=True)
class ToolDenied:
    n: int
    task: str | None
    kind: str
    name: str
    target: str
    reason: str
    possible_injection: bool
    """A research session met something that tried a call the sandbox
    blocked: the web content may be trying to steer it (I11)."""


@dataclass(frozen=True)
class SessionFinished:
    n: int
    task: str | None
    kind: str
    status: str
    """The manifest's session status: ok, error, timeout, budget,
    rate_limited or interrupted."""
    cost_usd: float
    duration_s: float
    guard_denials: int
    spent_usd: float
    """The run's spend so far."""
    error: str | None = None
    retry: bool = False
    """Whether the engine runs the session again."""


@dataclass(frozen=True)
class TaskFinished:
    task: str
    state: TaskOutcome
    reason: str | None = None
    validators: tuple[ValidatorResult, ...] = ()


@dataclass(frozen=True)
class Notice:
    message: str
    hint: str | None = None
    level: Literal["info", "warning"] = "warning"
    task: str | None = None


@dataclass(frozen=True)
class RunFinished:
    run_id: str
    outcome: Outcome
    exit_code: int
    cost_usd: float
    budget_usd: float
    sessions: int
    guard_denials: int
    duration_s: float
    max_time_s: int
    message: str | None = None
    hint: str | None = None
    rate_limit_reset: str | None = None
    """UTC, such as 2026-09-27T22:15:00Z."""
    resume_after: str | None = None
    """The reset in local time, HH:MM."""
    rendered: tuple[str, ...] = ()
    """The files written into ``out/``, relative to the project."""
    tasks: dict[str, str] = field(default_factory=dict)
    """Each task's state at the end of the run."""
    possible_injection: tuple[str, ...] = ()
    """Tasks whose research met calls the sandbox blocked (I11)."""
    manifest: str = ""
    """The manifest, relative to the project."""


Event: TypeAlias = (
    PlanReady
    | UpToDate
    | RunStarted
    | SessionStarted
    | ToolUsed
    | ToolDenied
    | SessionFinished
    | TaskFinished
    | Notice
    | RunFinished
)
Reporter: TypeAlias = Callable[[Event], None]


def ignore(event: Event) -> None:
    """A reporter that drops everything."""


def event_document(event: Event, ts: str) -> dict[str, Any]:
    """One NDJSON line's object. ``ts`` is the UTC time, to the second."""
    from spadework.views import plan_tasks_document

    head: dict[str, Any] = {"schema_version": EVENT_SCHEMA_VERSION, "ts": ts}
    match event:
        case PlanReady():
            plan = event.plan
            return {
                **head,
                "event": "plan",
                "project": plan.project,
                "playbook": {"name": plan.playbook, "version": plan.playbook_version},
                "limits": {
                    "budget_usd": plan.limits.budget_usd,
                    "max_time_s": plan.limits.max_time_s,
                    "rounds": plan.limits.rounds,
                },
                "tasks": plan_tasks_document(plan),
                "estimate_usd": plan.estimate_usd,
                "warnings": [
                    {"message": message, "hint": hint}
                    for message, hint in event.warnings
                ],
            }
        case UpToDate():
            return {
                **head,
                "event": "up_to_date",
                "exit_code": event.exit_code,
                "invalid": list(event.invalid),
            }
        case RunStarted():
            return {
                **head,
                "event": "run_started",
                "run_id": event.run_id,
                "project": event.project,
                "playbook": {"name": event.playbook, "version": event.playbook_version},
                "executor": {"name": event.executor, "version": event.executor_version},
                "limits": {
                    "budget_usd": event.budget_usd,
                    "max_time_s": event.max_time_s,
                    "rounds": event.rounds,
                },
                "tasks": list(event.tasks),
            }
        case SessionStarted():
            return {**head, "event": "session_started", **_fields(event)}
        case ToolUsed():
            return {**head, "event": "tool_call", **_fields(event)}
        case ToolDenied():
            return {**head, "event": "denied", **_fields(event)}
        case SessionFinished():
            return {**head, "event": "session_finished", **_fields(event)}
        case TaskFinished():
            return {
                **head,
                "event": "task_finished",
                "task": event.task,
                "state": event.state,
                "reason": event.reason,
                "validators": [
                    result.model_dump(mode="json") for result in event.validators
                ],
            }
        case Notice():
            return {**head, "event": "notice", **_fields(event)}
        case RunFinished():
            document = {**head, "event": "run_finished", **_fields(event)}
            document["rendered"] = list(event.rendered)
            document["possible_injection"] = list(event.possible_injection)
            return document
    raise TypeError(f"not an engine event: {event!r}")  # pragma: no cover


def _fields(event: Any) -> dict[str, Any]:
    return {name: getattr(event, name) for name in event.__dataclass_fields__}
