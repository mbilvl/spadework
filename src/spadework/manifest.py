"""The run manifest: ``.spadework/runs/<run-id>/manifest.json``.

One per run, written on every exit path, including errors and interrupts.
Its schema is versioned and public (docs/spec/04-engine.md, Run manifest).
"""

from __future__ import annotations

import datetime as dt
import secrets
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Literal

from pydantic import ConfigDict, Field

from spadework.budget import SessionKind
from spadework.errors import ExitCode, SpadeworkError
from spadework.models import SCHEMA_VERSION, SchemaVersion, StrictModel
from spadework.state import STATE_DIR, parse_versioned, write_atomic
from spadework.validators import ValidatorResult

RUNS_DIR = "runs"
MANIFEST_FILE = "manifest.json"

RUN_ID = r"[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4}"
TIMESTAMP = r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"

Outcome = Literal[
    "complete", "partial", "budget", "time", "rate_limited", "interrupted", "error"
]
SessionStatus = Literal[
    "ok", "error", "timeout", "budget", "rate_limited", "interrupted"
]

# docs/spec/01-cli.md, Exit codes.
OUTCOME_EXIT_CODES: Mapping[Outcome, ExitCode] = {
    "complete": ExitCode.OK,
    "partial": ExitCode.PARTIAL,
    "budget": ExitCode.LIMIT,
    "time": ExitCode.LIMIT,
    "rate_limited": ExitCode.RATE_LIMITED,
    "interrupted": ExitCode.INTERRUPTED,
    "error": ExitCode.ERROR,
}

RunId = Annotated[str, Field(pattern=f"^{RUN_ID}$")]
Timestamp = Annotated[str, Field(pattern=f"^{TIMESTAMP}$")]
Count = Annotated[int, Field(ge=0)]
Amount = Annotated[float, Field(ge=0, allow_inf_nan=False)]


def exit_code(outcome: Outcome) -> ExitCode:
    return OUTCOME_EXIT_CODES[outcome]


def utc_timestamp(moment: dt.datetime | None = None) -> str:
    """A UTC time to the second, such as 2026-09-27T21:04:11Z."""
    moment = dt.datetime.now(dt.UTC) if moment is None else moment.astimezone(dt.UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def new_run_id(moment: dt.datetime | None = None) -> str:
    """A UTC timestamp and 4 random hex digits, such as 20260927T210411Z-3f9a."""
    moment = dt.datetime.now(dt.UTC) if moment is None else moment.astimezone(dt.UTC)
    return f"{moment:%Y%m%dT%H%M%SZ}-{secrets.token_hex(2)}"


def session_log(n: int, kind: SessionKind, task: str | None = None) -> str:
    """A session's log, relative to its run folder: sessions/001-scope-draft.jsonl."""
    name = kind if task is None else f"{task}-{kind}"
    return f"sessions/{n:03d}-{name}.jsonl"


class Component(StrictModel):
    name: str
    version: str | None
    """None when the executor cannot report its version."""


class Limits(StrictModel):
    budget_usd: Amount
    max_time_s: Count
    rounds: Count


class Usage(StrictModel):
    input_tokens: Count = 0
    output_tokens: Count = 0
    cache_read_tokens: Count = 0


class RejectedFinding(StrictModel):
    """A critic finding the fixer declined. Never written into the document."""

    finding: str
    reason: str


class SessionEntry(StrictModel):
    n: Annotated[int, Field(ge=1)]
    task: str | None
    """None for the critic and the summary."""
    kind: SessionKind
    round: Count
    """0 for research, drafts and repairs; the review round otherwise."""
    model: str
    key: str | None
    """The research key for research, the draft key for a task's other
    sessions, None for the critic and the summary."""
    status: SessionStatus
    cost_usd: Amount
    duration_s: Amount
    usage: Usage = Field(default_factory=Usage)
    tool_calls: dict[str, Count] = Field(default_factory=dict)
    guard_denials: Count = 0
    validators: list[ValidatorResult] = Field(default_factory=list)
    rejected_findings: list[RejectedFinding] = Field(default_factory=list)
    log: str
    """Relative to the run folder."""
    error: str | None = None
    """One line on what went wrong, when ``status`` is not ok."""


class Totals(StrictModel):
    cost_usd: Amount = 0.0
    sessions: Count = 0
    guard_denials: Count = 0


class RunManifest(StrictModel):
    """Mutable while the run goes on: add sessions, then ``finish``."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=False)

    schema_version: SchemaVersion = SCHEMA_VERSION
    run_id: RunId
    spadework_version: str
    executor: Component
    playbook: Component
    limits: Limits
    started_at: Timestamp
    ended_at: Timestamp | None = None
    """None until the run ends."""
    outcome: Outcome | None = None
    """None until the run ends. It matches the exit code (``exit_code``)."""
    error: str | None = None
    """For ``error``: what went wrong, in one line."""
    rate_limit_reset: Timestamp | None = None
    """For ``rate_limited``: when the usage window resets."""
    sessions: list[SessionEntry] = Field(default_factory=list)
    totals: Totals = Field(default_factory=Totals)

    def next_session(self) -> int:
        return len(self.sessions) + 1

    def add_session(self, entry: SessionEntry) -> None:
        if entry.n != self.next_session():
            raise ValueError(
                f"session {entry.n} added as session {self.next_session()}"
            )
        self.sessions.append(entry)
        self.totals = Totals(
            # Rounded, so sums such as 0.1 + 0.2 do not show float noise.
            cost_usd=round(sum(s.cost_usd for s in self.sessions), 6),
            sessions=len(self.sessions),
            guard_denials=sum(s.guard_denials for s in self.sessions),
        )

    def finish(
        self,
        outcome: Outcome,
        *,
        ended_at: str | None = None,
        error: str | None = None,
        rate_limit_reset: str | None = None,
    ) -> None:
        self.outcome = outcome
        self.ended_at = ended_at or utc_timestamp()
        self.error = error
        self.rate_limit_reset = rate_limit_reset

    @property
    def exit_code(self) -> ExitCode | None:
        return None if self.outcome is None else exit_code(self.outcome)


def run_dir(state_dir: Path, run_id: str) -> Path:
    """``state_dir`` is the project's ``.spadework`` folder."""
    return state_dir / RUNS_DIR / run_id


def manifest_path(state_dir: Path, run_id: str) -> Path:
    return run_dir(state_dir, run_id) / MANIFEST_FILE


def write_manifest(state_dir: Path, manifest: RunManifest) -> Path:
    """Write the manifest atomically, replacing any earlier copy of this run's."""
    path = manifest_path(state_dir, manifest.run_id)
    data = manifest.model_dump_json(indent=2) + "\n"
    write_atomic(path, data.encode("utf-8"))
    return path


def load_manifest(path: Path) -> RunManifest:
    display = f"{STATE_DIR}/{RUNS_DIR}/{path.parent.name}/{path.name}"
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise SpadeworkError(
            f"cannot read {display}: {exc.strerror}",
            "Check the run id. `spadework status` shows the last run.",
        ) from exc
    return parse_versioned(
        data,
        RunManifest,
        display,
        "The manifest of this run cannot be shown. Later runs are not affected.",
    )
