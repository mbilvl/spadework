"""``spadework verify``: check every source of the pack again.

See docs/spec/01-cli.md and docs/spec/06-records.md (Verification). No model
is involved and nothing is spent: each source's page is fetched under the
rules of I9, its quote is looked for, and the research records are updated.
Then the parts of the pack made from them are rendered again: ``sources.md``,
``pack.json``, ``00-summary.md`` and each document's Sources section.

A document a person edited is never touched, and neither is an edited
``00-summary.md``, ``sources.md`` or ``pack.json``: the report lists them.
When a run was interrupted before it rendered, only the records are updated,
and the next ``spadework run`` renders the pack.

It holds the project lock, like ``run``. It writes no run manifest: it is
not a run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from spadework import __version__
from spadework.engine.pack import render_pack
from spadework.engine.planning import plan_project
from spadework.engine.store import read_research, state_dir, write_record
from spadework.engine.verification import (
    NetworkVerifier,
    SourceVerifier,
    VerificationResult,
    VerificationStatus,
    as_result,
    counts,
)
from spadework.errors import SpadeworkError
from spadework.lock import LOCK_FILE, ProjectLock
from spadework.manifest import utc_timestamp
from spadework.project import Project, load_project
from spadework.state import load_state, save_state, state_path

Progress = Callable[[str], None]


@dataclass(frozen=True)
class CheckedSource:
    task: str
    id: str
    """Local to the task, such as S1."""
    url: str
    result: VerificationResult


@dataclass(frozen=True)
class VerifyReport:
    sources: tuple[CheckedSource, ...]
    written: tuple[str, ...] = ()
    """Files rendered again, relative to the project."""
    kept: tuple[str, ...] = ()
    """Documents a person edited, left as they are."""
    render_owed: bool = False
    """An interrupted run owes the render: the next run does it."""
    warnings: tuple[tuple[str, str | None], ...] = field(default=())

    def counts(self) -> dict[VerificationStatus, int]:
        return counts([source.result.status for source in self.sources])

    @property
    def tasks(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(source.task for source in self.sources))


def verify_pack(
    root: Path,
    *,
    verifier: SourceVerifier | None = None,
    progress: Progress | None = None,
    time_limit: float | None = None,
) -> VerifyReport:
    """Check every source of the pack again, and render what depends on them.

    Raises ``SpadeworkError`` for an invalid project (exit 3) or a lock held
    by a run (7). ``time_limit`` bounds the whole check; it defaults to the
    project's ``max_time``. Whatever the checks find, it returns a report:
    a source that fails its check is information, not an error.
    """
    project = load_project(root)
    say = progress or (lambda message: None)
    with ProjectLock(state_dir(root) / LOCK_FILE) as lock:
        warnings: list[tuple[str, str | None]] = []
        if lock.warning:
            warnings.append((lock.warning, None))
        state = load_state(state_path(root))
        verifier = verifier or NetworkVerifier()
        limit = project.config.limits.max_time_s if time_limit is None else time_limit
        deadline = time.monotonic() + limit
        checked: list[CheckedSource] = []
        for task in project.playbook.tasks:
            entry = state.research.get(task.id)
            if task.research is None or entry is None:
                continue
            record = read_research(root, entry.record)
            if record is None:
                warnings.append(
                    (
                        f"the research record of task {task.id!r} cannot be read, "
                        "so its sources were not checked",
                        f"`spadework run --force --only {task.id} "
                        "--refresh-research` does its research again.",
                    )
                )
                continue
            sources = record.result.sources
            if not sources:
                continue
            say(f"{task.id}: checking {len(sources)} sources")
            found = verifier.verify_sources(
                sources, time_limit=max(deadline - time.monotonic(), 0.0)
            )
            results = {}
            for source in sources:
                result = as_result(found.get(source.id, "unchecked"))
                if result.status == "unchecked":
                    result = record.result_of(source.id)  # not checked this time
                results[source.id] = result
                checked.append(CheckedSource(task.id, source.id, source.url, result))
            write_record(
                root,
                entry.record,
                record.model_copy(
                    update={"verification": {**record.verification, **results}}
                ),
            )
        if not checked:
            return VerifyReport((), warnings=tuple(warnings))
        if state.review is not None:
            return VerifyReport(
                tuple(checked), render_owed=True, warnings=tuple(warnings)
            )
        planned = plan_project(project, state)
        rendered = render_pack(
            project,
            state,
            states={task.id: task.state for task in planned.plan.tasks},
            pinned=planned.pinned,
            versions=_versions(project),
            generated_at=utc_timestamp(),
            save=lambda: save_state(state_path(root), state),
        )
        save_state(state_path(root), state)
        warnings += [(message, hint) for message, hint in rendered.warnings]
        return VerifyReport(
            tuple(checked),
            written=tuple(rendered.written),
            kept=tuple(rendered.kept),
            warnings=tuple(warnings),
        )


def _versions(project: Project) -> dict[str, str]:
    """For ``pack.json`` when no summary recorded them: this spadework, the
    playbook, and the executor of the last run."""
    from spadework.engine.status import run_manifests
    from spadework.manifest import load_manifest

    executor = "unknown"
    for path in reversed(run_manifests(project.root)):
        try:
            component = load_manifest(path).executor
        except SpadeworkError:
            continue
        executor = component.name
        if component.version is not None:
            executor += f"@{component.version}"
        break
    playbook = project.playbook
    return {
        "spadework": __version__,
        "playbook": f"{playbook.name}@{playbook.version}",
        "executor": executor,
    }
