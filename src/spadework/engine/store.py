"""What the engine keeps under ``.spadework/``, apart from ``state.json``.

- ``work/<output>``: each task's current output, as drafted, repaired or
  fixed. Rendering copies it into ``out/``, with its Sources section.
- ``work/00-summary.md``: the prose the summary session wrote.
- ``records/<task>.research.json``: the task's last research result, with
  each source's verification result.
- ``records/<task>.draft.json``: the result of the session that produced the
  output in ``work/``: a draft, its repair, or a review fix. Its key facts go
  to dependent sessions.
- ``records/review.round-<n>.json``: the findings of a review round.
- ``records/summary.json``: the summary's result, and what the code-rendered
  parts of ``00-summary.md`` need.

Every file is written atomically. A record that is missing or cannot be read
counts as absent: the research is done again, a dependency's key facts are
left out, a review round runs its critic again, and the summary runs again.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, TypeVar

from pydantic import Field, field_validator

from spadework.engine.verification import (
    UNCHECKED,
    VerificationResult,
    VerificationStatus,
)
from spadework.errors import SpadeworkError
from spadework.keys import hash_bytes
from spadework.models import SCHEMA_VERSION, SchemaVersion, StrictModel
from spadework.records import CriticResult, DraftResult, ResearchResult, SummaryResult
from spadework.state import (
    STATE_DIR,
    WORK_DIR,
    ReviewFinding,
    parse_versioned,
    write_atomic,
)

RECORDS_DIR = "records"
SUMMARY_FILE = "00-summary.md"
SUMMARY_RECORD = f"{RECORDS_DIR}/summary.json"


class ResearchRecord(StrictModel):
    """``records/<task>.research.json``."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    task: str
    key: str
    """The research key the session ran at."""
    run_id: str | None
    result: ResearchResult
    verification: dict[str, VerificationResult]
    """Each source's verification result, by source id."""
    guard_denials: int = Field(default=0, ge=0)
    """Calls the sandbox denied in the research session: possible prompt
    injection encountered (I11)."""

    @field_validator("verification", mode="before")
    @classmethod
    def _statuses(cls, value: Any) -> Any:
        # Records written before verification kept only each status.
        if isinstance(value, dict):
            return {
                key: {"status": item} if isinstance(item, str) else item
                for key, item in value.items()
            }
        return value

    def result_of(self, source_id: str) -> VerificationResult:
        return self.verification.get(source_id, UNCHECKED)

    def status(self, source_id: str) -> VerificationStatus:
        return self.result_of(source_id).status


class DraftRecord(StrictModel):
    """``records/<task>.draft.json``: the result behind the output in ``work/``."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    task: str
    key: str
    """The draft key of the output. A fix changes the output, never its key."""
    run_id: str | None
    kind: Literal["draft", "repair", "fix"]
    result: DraftResult


class CriticRecord(StrictModel):
    """``records/review.round-<n>.json``: what one review round acts on."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    run_id: str
    round: int = Field(ge=1)
    result: CriticResult
    findings: list[ReviewFinding]
    """The critic's findings on known documents, then one for each failing
    validator, in the order the fixes run."""
    dropped: list[str] = Field(default_factory=list)
    """Why each finding left out was dropped, such as an unknown file."""
    validators_failing: bool = False
    """Some document's validators were failing when the critic ran."""

    @property
    def ship(self) -> bool:
        """The critic's verdict is ship and no validator is failing."""
        return self.result.verdict == "ship" and not self.validators_failing


class Produced(StrictModel):
    """How the pack was produced, for the footer of ``00-summary.md``."""

    date: str
    """UTC, YYYY-MM-DD."""
    spadework: str
    playbook: str
    """name@version."""
    executor: str
    """name@version, or the name alone when the version is not known."""
    cost_usd: float = Field(ge=0)
    """What every run recorded for the project spent."""
    runs: int = Field(ge=0)
    run_cost_usd: float = Field(ge=0)
    """What the run that wrote the summary spent."""


class SummaryRecord(StrictModel):
    """``records/summary.json``."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    run_id: str
    result: SummaryResult | None
    """None when the summary session failed: ``error`` says why."""
    error: str | None = None
    pinned_findings: list[ReviewFinding] = Field(default_factory=list)
    """Review findings against documents a person edited."""
    produced: Produced


def state_dir(root: Path) -> Path:
    return root / STATE_DIR


def research_record_name(task_id: str) -> str:
    """Relative to ``.spadework/``, as ``state.json`` stores it."""
    return f"{RECORDS_DIR}/{task_id}.research.json"


def draft_record_name(task_id: str) -> str:
    return f"{RECORDS_DIR}/{task_id}.draft.json"


def critic_record_name(round_: int) -> str:
    return f"{RECORDS_DIR}/review.round-{round_}.json"


def work_path(root: Path, output: str) -> Path:
    return state_dir(root) / WORK_DIR / output


def write_record(root: Path, name: str, record: StrictModel) -> str:
    data = record.model_dump_json(indent=2, by_alias=True) + "\n"
    write_atomic(state_dir(root) / name, data.encode("utf-8"))
    return name


def read_research(root: Path, name: str) -> ResearchRecord | None:
    return _read(root, name, ResearchRecord)


def read_draft(root: Path, task_id: str) -> DraftRecord | None:
    return _read(root, draft_record_name(task_id), DraftRecord)


def read_critic(root: Path, name: str) -> CriticRecord | None:
    return _read(root, name, CriticRecord)


def read_summary(root: Path) -> SummaryRecord | None:
    return _read(root, SUMMARY_RECORD, SummaryRecord)


_Record = TypeVar("_Record", ResearchRecord, DraftRecord, CriticRecord, SummaryRecord)


def _read(root: Path, name: str, model: type[_Record]) -> _Record | None:
    try:
        data = (state_dir(root) / name).read_bytes()
    except OSError:
        return None
    try:
        return parse_versioned(data, model, f"{STATE_DIR}/{name}", "")
    except SpadeworkError:
        return None


def write_work(root: Path, output: str, data: bytes) -> str:
    """Store a task's output in ``work/``, and return the hash of its bytes."""
    write_atomic(work_path(root, output), data)
    return hash_bytes(data)


def read_work(root: Path, output: str) -> bytes | None:
    try:
        return work_path(root, output).read_bytes()
    except OSError:
        return None
