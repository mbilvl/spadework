"""The executor interface: how the engine runs one session.

See docs/spec/05-executors.md. The engine depends on this module only, never
on a backend's flags or wire format.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol, TypeAlias

from spadework.sandbox.policy import check_session_id


class SessionKind(StrEnum):
    RESEARCH = "research"
    DRAFT = "draft"
    REPAIR = "repair"
    CRITIC = "critic"
    FIX = "fix"
    SUMMARY = "summary"


MIN_BUDGET_USD = 0.0001
"""The smallest session cap: the CLI's budget is given to four decimals."""

# Kinds that may write nothing to disk (docs/spec/07-security.md, I3).
READ_ONLY_KINDS = frozenset({SessionKind.RESEARCH, SessionKind.CRITIC})

Status = Literal["ok", "error", "timeout", "budget", "rate_limited", "interrupted"]

ErrorKind = Literal["transient", "schema", "sandbox", "executor"]
"""Why a session with status ``error`` failed, which decides what the engine does.

- ``transient``: the backend stopped without a result, such as a crash or a
  dropped connection. Retried (docs/spec/04-engine.md, Retries).
- ``schema``: no structured result, or one that failed the schema. Retried
  once with the validation error.
- ``sandbox``: the sandbox could not be confirmed (the guard did not start or
  did not answer, or the tool set was wrong). Never retried; the run stops.
- ``executor``: the backend reported an error of its own, such as a login
  problem or an unknown model. Not retried.
"""


@dataclass(frozen=True)
class SessionSpec:
    """Everything an executor needs to run one session."""

    session_id: str
    kind: SessionKind
    prompt: str
    """The user message."""
    system_append: str
    """The engine's base prompt plus the playbook's additions."""
    model: str
    tools: tuple[str, ...]
    """The exact tool allowlist for this kind (docs/spec/07-security.md, I1)."""
    workdir: Path
    """The staging directory: the session's whole world."""
    policy_path: Path
    """The guard policy, outside ``workdir``."""
    output_schema: dict[str, Any]
    """JSON Schema for the structured result (``spadework.records.output_schema``)."""
    max_budget_usd: float
    timeout_s: int
    log_path: Path
    """Where the raw event log is written, outside ``workdir``."""
    output: Path | None = None
    """The one path the session may write, inside ``workdir``, or None (I3)."""
    expected_tools: tuple[str, ...] = ()
    """The exact tool set the backend must report. Empty means ``tools`` plus
    whatever the backend adds for the structured result."""
    task: str | None = None
    """The task this session works on, if any. Used in logs and by ``fake``."""

    def __post_init__(self) -> None:
        try:
            # It names files in the control plane.
            check_session_id(self.session_id)
        except ValueError:
            raise ValueError(
                "session_id must be 1 to 128 letters, digits, '.', '_' or '-', "
                "not starting with '.', '_' or '-'"
            ) from None
        if self.kind in READ_ONLY_KINDS:
            if self.output is not None:
                raise ValueError(f"a {self.kind} session must not have an output")
        elif self.output is None:
            raise ValueError(f"a {self.kind} session needs an output path")
        elif not _is_inside(self.output, self.workdir):
            raise ValueError(f"the output {self.output} is not inside {self.workdir}")
        for name in ("policy_path", "log_path"):
            if _is_inside(getattr(self, name), self.workdir):
                raise ValueError(f"{name} must be outside the staging directory")
        if not MIN_BUDGET_USD <= self.max_budget_usd < math.inf:
            # Anything smaller would round to a cap of zero.
            raise ValueError(
                f"max_budget_usd must be a finite amount of at least {MIN_BUDGET_USD}"
            )
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be positive")


def _is_inside(path: Path, directory: Path) -> bool:
    return path.resolve().is_relative_to(directory.resolve())


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    web_search_requests: int = 0
    web_fetch_requests: int = 0


@dataclass
class SessionResult:
    status: Status
    structured: dict[str, Any] | None = None
    """The structured result. The engine validates it with
    ``spadework.records.validate_result``."""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    usage: Usage = field(default_factory=Usage)
    tool_calls: dict[str, int] = field(default_factory=dict)
    rate_limit_reset: dt.datetime | None = None
    error: str | None = None
    """One line on what went wrong, for the user."""
    error_kind: ErrorKind | None = None
    """Set when ``status`` is ``error``."""
    guard_denials: int = 0
    cost_known: bool = True
    """False when the session was stopped before the backend reported its
    cost. ``cost_usd`` is then 0 and the engine should assume the session
    spent up to its cap."""


@dataclass(frozen=True)
class ToolCall:
    name: str
    target: str
    """The path, URL or query, shortened."""


@dataclass(frozen=True)
class Text:
    snippet: str
    """One line of the model's text, shortened."""


@dataclass(frozen=True)
class Denied:
    """A tool call the guard or the permission mode refused (I11)."""

    name: str
    target: str
    reason: str


@dataclass(frozen=True)
class Notice:
    """Something the user should know about a session that is not an error,
    such as running without restricted mode."""

    message: str


@dataclass(frozen=True)
class RateLimit:
    status: str
    """``allowed``, ``allowed_warning`` or ``rejected``."""
    resets_at: dt.datetime | None


@dataclass(frozen=True)
class Finished:
    result: SessionResult


Event: TypeAlias = ToolCall | Text | Denied | Notice | RateLimit | Finished
EventSink: TypeAlias = Callable[[Event], None]


def ignore_events(event: Event) -> None:
    """An event sink that drops everything."""


@dataclass(frozen=True)
class Diagnostic:
    """One check that ``spadework doctor`` reports."""

    name: str
    ok: bool
    detail: str
    hint: str = ""
    required: bool = True
    """False when a failure is only a warning."""


class Executor(Protocol):
    name: str

    def version(self) -> str | None:
        """The backend's version for the manifest, or None if unknown."""
        ...

    def diagnose(self) -> list[Diagnostic]:
        """Checks for ``spadework doctor``."""
        ...

    def run(self, spec: SessionSpec, events: EventSink) -> SessionResult:
        """Run one session to the end, reporting progress to ``events``.

        Always returns a result, ending with a ``Finished`` event. A Ctrl-C
        (KeyboardInterrupt) during the session stops it and returns status
        ``interrupted`` instead of raising, so the engine can record the
        session before it stops the run.
        """
        ...
