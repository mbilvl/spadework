"""``.spadework/state.json``: what the engine remembers between runs.

For each task: the key material of its last attempt and how it went, the
output in ``.spadework/work/``, the file last rendered into ``out/``, and its
research record. For the project: how far the review of the last run's
changes has got, so that an interrupted run resumes it.

The file records ``fresh``, ``failed`` and ``invalid``. ``pending``,
``stale`` and ``pinned`` are derived each time from the current key
material and ``out/`` (``derive_statuses``). See docs/spec/04-engine.md.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spadework.errors import SpadeworkError
from spadework.keys import TaskMaterial, hash_file, stale_reasons
from spadework.models import SCHEMA_VERSION, SchemaVersion, StrictModel
from spadework.plan import TaskState, TaskStatus
from spadework.records import DraftResult
from spadework.validators import ValidatorResult, failure_summary

STATE_DIR = ".spadework"
STATE_FILE = "state.json"
WORK_DIR = "work"
OUT_DIR = "out"

RecordedState = Literal["fresh", "failed", "invalid"]


class DraftOutput(StrictModel):
    """The task's output in ``.spadework/work/``."""

    key: str
    """The draft key it was drafted at. Fixes change the text, never the key."""
    hash: str


class RenderedFile(StrictModel):
    """The file last rendered into ``out/``, to tell when a person edits it."""

    path: str
    """Relative to ``out/``."""
    hash: str


class ResearchEntry(StrictModel):
    """The task's last research, reused while its key matches."""

    key: str
    record: str
    """The research record, relative to ``.spadework/``."""
    run_id: str | None = None


class TaskEntry(StrictModel):
    status: RecordedState
    material: TaskMaterial
    """The key material of the last attempt, whether it succeeded or not."""
    output: DraftOutput | None = None
    """None until a draft has succeeded. After a failure it is still the
    earlier output, drafted at an older key."""
    rendered: RenderedFile | None = None
    validators: list[ValidatorResult] = Field(default_factory=list)
    """The last results for ``output``."""
    error: str | None = None
    """Why the last attempt failed, for ``failed``."""
    run_id: str | None = None
    """The run of the last attempt."""

    @property
    def key(self) -> str:
        return self.material.key


FindingOrigin = Literal["critic", "validator"]


class ReviewFinding(StrictModel):
    """A finding the review acts on: the critic's, or a validator failure."""

    id: str
    """F1, F2, …: the critic's own, then those code added."""
    task: str
    target: str
    """The document's file name."""
    kind: str
    severity: str
    instruction: str
    origin: FindingOrigin = "critic"
    round: int = Field(default=0, ge=0)


class PendingWrite(StrictModel):
    """A fixed (or repaired) document about to be written into ``work/``.

    It is saved first, with the fix marked done, so a run that stops before
    the document is recorded never fixes it again: the next run finishes the
    recording if the file holds these bytes, and drops it otherwise.
    """

    task: str
    kind: Literal["fix", "repair"]
    hash: str
    """Of the new document's bytes."""
    validators: list[ValidatorResult]
    result: DraftResult
    """The session's result for the new document, for its draft record."""


class ReviewProgress(StrictModel):
    """How far a run's review has got. Cleared once the run has rendered.

    It is set when a run first changes an output, so a run interrupted
    before its review, fixes, summary or render finishes that work next
    time, even when nothing is stale any more. Each step is saved as it
    ends, so a resumed run never repeats one: the critic of a round, each
    fix, the repair a fix still owes, the summary.
    """

    run_id: str
    """The run that started this review."""
    round: int = Field(default=0, ge=0)
    """The current round. 0 while the drafts are still running."""
    critic: str | None = None
    """This round's critic record, relative to ``.spadework/``, once it ran."""
    fixed: list[str] = Field(default_factory=list)
    """Tasks whose fix is done in this round, in order."""
    repairs: list[str] = Field(default_factory=list)
    """Fixed tasks whose validators failed, and whose repair is still owed."""
    changed: bool = False
    """A fix or repair changed a document in this round."""
    writing: PendingWrite | None = None
    """A fixed document being written, until it is recorded."""
    reviewed: bool = False
    """The review rounds are over."""
    summarised: bool = False
    """The summary is done: only the render is left."""
    pinned_findings: list[ReviewFinding] = Field(default_factory=list)
    """Findings against documents a person edited, for the summary."""

    def next_round(self) -> ReviewProgress:
        return self.start_round(self.round + 1)

    def start_round(self, round_: int) -> ReviewProgress:
        return self.model_copy(
            update={
                "round": round_,
                "critic": None,
                "fixed": [],
                "repairs": [],
                "changed": False,
            }
        )

    def with_critic(self, record: str) -> ReviewProgress:
        return self.model_copy(update={"critic": record})

    def with_fixed(self, task_id: str, *, repair: bool = False) -> ReviewProgress:
        fixed = self.fixed if task_id in self.fixed else [*self.fixed, task_id]
        repairs = [task for task in self.repairs if task != task_id]
        if repair:
            repairs.append(task_id)
        return self.model_copy(update={"fixed": fixed, "repairs": repairs})

    def with_repaired(self, task_id: str) -> ReviewProgress:
        return self.with_fixed(task_id)

    def with_pinned(self, findings: Iterable[ReviewFinding]) -> ReviewProgress:
        known = {(f.round, f.id) for f in self.pinned_findings}
        added = [f for f in findings if (f.round, f.id) not in known]
        return self.model_copy(
            update={"pinned_findings": [*self.pinned_findings, *added]}
        )


class State(StrictModel):
    """The whole of ``state.json``. Mutable: the engine updates it as it runs."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=False)

    schema_version: SchemaVersion = SCHEMA_VERSION
    tasks: dict[str, TaskEntry] = Field(default_factory=dict)
    research: dict[str, ResearchEntry] = Field(default_factory=dict)
    review: ReviewProgress | None = None
    files: dict[str, str] = Field(default_factory=dict)
    """The hash of each pack file that belongs to no task (``00-summary.md``,
    ``sources.md``, ``pack.json``) as last rendered, by name in ``out/``."""
    rendering: dict[str, str] = Field(default_factory=dict)
    """Files in ``out/`` being rendered: the hash of the bytes about to be
    written, saved before they are. A file that holds them is the engine's,
    not a person's edit, even if the run stopped before recording it."""

    def record_research(
        self, task_id: str, *, key: str, record: str, run_id: str | None
    ) -> None:
        self.research[task_id] = ResearchEntry(key=key, record=record, run_id=run_id)

    def research_record(self, task_id: str, key: str) -> str | None:
        """The stored research record for ``task_id``, if its key is ``key``."""
        entry = self.research.get(task_id)
        return entry.record if entry is not None and entry.key == key else None

    def record_draft(
        self,
        task_id: str,
        *,
        material: TaskMaterial,
        output_hash: str,
        validators: Iterable[ValidatorResult],
        run_id: str | None,
    ) -> TaskEntry:
        """A draft (or its repair) produced an output: fresh, or invalid."""
        results = list(validators)
        entry = TaskEntry(
            status="fresh" if all(r.ok for r in results) else "invalid",
            material=material,
            output=DraftOutput(key=material.key, hash=output_hash),
            rendered=self._rendered(task_id),
            validators=results,
            run_id=run_id,
        )
        self.tasks[task_id] = entry
        return entry

    def record_failure(
        self, task_id: str, *, material: TaskMaterial, error: str, run_id: str | None
    ) -> TaskEntry:
        """An attempt at ``material`` failed. Any earlier output is kept."""
        previous = self.tasks.get(task_id)
        entry = TaskEntry(
            status="failed",
            material=material,
            output=None if previous is None else previous.output,
            rendered=self._rendered(task_id),
            validators=[] if previous is None else previous.validators,
            error=error,
            run_id=run_id,
        )
        self.tasks[task_id] = entry
        return entry

    def record_fix(
        self,
        task_id: str,
        *,
        output_hash: str,
        validators: Iterable[ValidatorResult],
    ) -> TaskEntry:
        """A review fix changed the output. It changes no key, by design."""
        previous = self.tasks[task_id]
        if previous.output is None:
            raise ValueError(f"task {task_id!r} has no output to fix")
        results = list(validators)
        status: RecordedState = previous.status
        if status != "failed":
            status = "fresh" if all(r.ok for r in results) else "invalid"
        entry = previous.model_copy(
            update={
                "status": status,
                "output": previous.output.model_copy(update={"hash": output_hash}),
                "validators": results,
            }
        )
        self.tasks[task_id] = entry
        return entry

    def record_render(self, task_id: str, *, path: str, digest: str) -> TaskEntry:
        """The engine rendered ``out/<path>``, whose bytes hash to ``digest``."""
        entry = self.tasks[task_id].model_copy(
            update={"rendered": RenderedFile(path=path, hash=digest)}
        )
        self.tasks[task_id] = entry
        return entry

    def _rendered(self, task_id: str) -> RenderedFile | None:
        previous = self.tasks.get(task_id)
        return None if previous is None else previous.rendered


# Reading and writing.


class StateError(SpadeworkError):
    """state.json cannot be used. Exit code 1: the file is the engine's, not
    part of the project a user fixes, so it is not an invalid project (3)."""


def state_path(root: Path) -> Path:
    return root / STATE_DIR / STATE_FILE


_DISPLAY = f"{STATE_DIR}/{STATE_FILE}"
_START_OVER = (
    f"Move {_DISPLAY} aside and run again: every task will be redrafted. "
    "Back up out/ first if you edited it."
)


def load_state(path: Path) -> State:
    """Read state.json. A missing file is an empty state."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return State()
    except OSError as exc:
        raise StateError(
            f"cannot read {_DISPLAY}: {exc.strerror}",
            f"Make {STATE_DIR}/ and its files readable.",
        ) from exc
    return parse_versioned(data, State, _DISPLAY, _START_OVER, StateError)


def save_state(path: Path, state: State) -> None:
    """Write state.json atomically: a reader sees the old file or the new one."""
    write_atomic(path, (state.model_dump_json(indent=2) + "\n").encode("utf-8"))


Versioned = TypeVar("Versioned", bound=BaseModel)


def parse_versioned(
    data: bytes,
    model: type[Versioned],
    display: str,
    hint: str,
    error: type[SpadeworkError] = SpadeworkError,
) -> Versioned:
    """Parse one of the engine's JSON files, which carry a ``schema_version``.

    A file that is not JSON, or does not match ``model``, is corrupt. One
    with a higher ``schema_version`` was written by a newer spadework.
    """
    try:
        document = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        where = f" (line {exc.lineno})" if isinstance(exc, json.JSONDecodeError) else ""
        raise error(f"{display} is corrupt: it is not valid JSON{where}", hint) from exc
    version = document.get("schema_version") if isinstance(document, dict) else None
    if isinstance(version, int) and version > SCHEMA_VERSION:
        raise error(
            f"{display} was written by a newer version of spadework "
            f"(schema_version {version}; this version reads {SCHEMA_VERSION})",
            f"Upgrade spadework. {hint}",
        )
    try:
        return model.model_validate_json(data)
    except ValidationError as exc:
        first = exc.errors(include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or "the top level"
        raise error(f"{display} is corrupt: {where}: {first['msg']}", hint) from exc


def write_atomic(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` in one step, even across a crash.

    The data goes to a temporary file in the same folder, which is synced
    and then renamed over ``path``. The file is private to the user (0600).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    _sync_folder(path.parent)


def _sync_folder(folder: Path) -> None:
    # Makes the rename itself durable. Not every platform can open a folder.
    try:
        fd = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(fd)
    finally:
        os.close(fd)


def file_hash(path: Path) -> str | None:
    """The hash of a file's bytes, or None if there is no such file."""
    try:
        return hash_file(path)
    except (FileNotFoundError, IsADirectoryError, NotADirectoryError):
        return None


def read_out_hashes(root: Path, state: State) -> dict[str, str | None]:
    """The current hash of each file the engine rendered into ``out/``."""
    out = root / OUT_DIR
    return {
        entry.rendered.path: file_hash(out / entry.rendered.path)
        for entry in state.tasks.values()
        if entry.rendered is not None
    }


# Deriving each task's state.


@dataclass(frozen=True)
class DerivedStatus(TaskStatus):
    """A task's state and every reason for it. ``reason`` joins ``reasons``."""

    reasons: tuple[str, ...] = ()

    @classmethod
    def of(cls, state: TaskState, reasons: Iterable[str] = ()) -> DerivedStatus:
        listed = tuple(reasons)
        return cls(state, ", ".join(listed) or None, listed)


def derive_statuses(
    task_ids: Iterable[str],
    materials: Mapping[str, TaskMaterial],
    state: State,
    out_hashes: Mapping[str, str | None],
    *,
    force: Collection[str] = (),
) -> dict[str, DerivedStatus]:
    """Each task's state and reason, for ``build_plan``.

    ``task_ids`` is the execution order, so dependencies come first.
    ``materials`` is the current key material (``keys.task_materials``),
    ``out_hashes`` the current hash of each rendered file
    (``read_out_hashes``),
    and ``force`` the tasks selected with ``--force``.

    A task with no entry is pending. An edited ``out/`` file makes it pinned,
    whatever else changed. Otherwise, when its key differs from the last
    attempt's, it is stale and the reasons say why; when it matches, it keeps
    the state that attempt recorded (fresh, failed or invalid).
    """
    derived: dict[str, DerivedStatus] = {}
    for task_id in task_ids:
        derived[task_id] = _derive(
            task_id, materials[task_id], state, out_hashes, force, derived
        )
    return derived


def _derive(
    task_id: str,
    material: TaskMaterial,
    state: State,
    out_hashes: Mapping[str, str | None],
    force: Collection[str],
    derived: Mapping[str, DerivedStatus],
) -> DerivedStatus:
    entry = state.tasks.get(task_id)
    if entry is None:
        return DerivedStatus.of("pending")
    forced = task_id in force
    rendered = entry.rendered
    if rendered is not None:
        current = out_hashes.get(rendered.path)
        # A deleted file is not an edit: the next render writes it again.
        if current is not None and current != rendered.hash:
            if forced:
                return DerivedStatus.of(
                    "stale",
                    [f"--force replaces your edits to {OUT_DIR}/{rendered.path}"],
                )
            return DerivedStatus.of("pinned", [f"{OUT_DIR}/{rendered.path} edited"])
    if forced:
        return DerivedStatus.of("stale", ["--force"])
    if entry.key != material.key:
        wording = {
            dep: f"dependency {dep} changed"
            for dep in material.draft.dependencies
            if dep in derived and derived[dep].state == "fresh"
        }
        return DerivedStatus.of(
            "stale",
            stale_reasons(entry.material, material, changed_dependencies=wording),
        )
    if entry.status == "failed":
        return DerivedStatus.of("failed", [entry.error or "the last attempt failed"])
    if entry.status == "invalid":
        return DerivedStatus.of(
            "invalid", [failure_summary(entry.validators) or "validators failed"]
        )
    return DerivedStatus.of("fresh")
