"""What the engine keeps under ``.spadework/``, apart from ``state.json``.

- ``work/<output>``: each task's current output, as drafted, repaired or
  fixed. Rendering copies it into ``out/``.
- ``records/<task>.research.json``: the task's last research result, with
  each source's verification status.
- ``records/<task>.draft.json``: the result of the session that produced the
  output in ``work/``. Its key facts go to dependent sessions.

Every file is written atomically. A record that is missing or cannot be read
counts as absent: the research is done again, and a dependency's key facts
are left out.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, TypeVar

from spadework.engine.verification import VerificationStatus
from spadework.errors import SpadeworkError
from spadework.keys import hash_bytes
from spadework.models import SCHEMA_VERSION, SchemaVersion, StrictModel
from spadework.records import DraftResult, ResearchResult
from spadework.state import STATE_DIR, WORK_DIR, parse_versioned, write_atomic

RECORDS_DIR = "records"


class ResearchRecord(StrictModel):
    """``records/<task>.research.json``."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    task: str
    key: str
    """The research key the session ran at."""
    run_id: str | None
    result: ResearchResult
    verification: dict[str, VerificationStatus]
    """Each source's status, by source id."""

    def status(self, source_id: str) -> VerificationStatus:
        return self.verification.get(source_id, "unchecked")


class DraftRecord(StrictModel):
    """``records/<task>.draft.json``: the result behind the output in ``work/``."""

    schema_version: SchemaVersion = SCHEMA_VERSION
    task: str
    key: str
    """The draft key of the output."""
    run_id: str | None
    kind: Literal["draft", "repair"]
    result: DraftResult


def state_dir(root: Path) -> Path:
    return root / STATE_DIR


def research_record_name(task_id: str) -> str:
    """Relative to ``.spadework/``, as ``state.json`` stores it."""
    return f"{RECORDS_DIR}/{task_id}.research.json"


def draft_record_name(task_id: str) -> str:
    return f"{RECORDS_DIR}/{task_id}.draft.json"


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


_Record = TypeVar("_Record", ResearchRecord, DraftRecord)


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
