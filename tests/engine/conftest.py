"""Helpers for the engine tests: a clock that never waits, an executor that
records what each session was given, and a way to run the pipeline."""

from __future__ import annotations

import json
import shutil
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from spadework.engine import events as ev
from spadework.engine.run import RunOptions, RunResult, run_project
from spadework.engine.verification import SourceVerifier
from spadework.executors.base import (
    Diagnostic,
    Event,
    EventSink,
    SessionKind,
    SessionResult,
    SessionSpec,
)
from spadework.executors.fake import FakeBehaviour, FakeExecutor
from spadework.manifest import RunManifest, load_manifest, manifest_path
from spadework.state import State, load_state, state_path
from tests.conftest import MakeProject


class FakeClock:
    """Time that moves only when asked. ``sleep`` returns at once."""

    def __init__(self) -> None:
        self.mono = 1000.0
        self.wall = time.time()
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.mono

    def time(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.advance(seconds)

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.wall += seconds


@dataclass
class Session:
    """What one session was given."""

    spec: SessionSpec
    files: dict[str, str]
    """The staged files at the start, by path relative to the workdir."""

    @property
    def kind(self) -> str:
        return self.spec.kind.value


class Recording:
    """The fake executor, keeping each session's spec and staged files.

    ``before`` runs before each session; ``after`` may replace its result.
    """

    name = "fake"

    def __init__(
        self,
        script: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        before: Callable[[SessionSpec], None] | None = None,
        after: Callable[[SessionSpec, SessionResult], SessionResult | None]
        | None = None,
        emit: tuple[Event, ...] = (),
    ) -> None:
        behaviours = {
            key: FakeBehaviour(**value) for key, value in (script or {}).items()
        }
        self.inner = FakeExecutor(behaviours)
        self.before = before
        self.after = after
        self.emit = emit
        """Events every session sends first, as a backend might."""
        self.sessions: list[Session] = []

    def version(self) -> str | None:
        return "test"

    def diagnose(self) -> list[Diagnostic]:
        return []

    def run(self, spec: SessionSpec, events: EventSink) -> SessionResult:
        files = {
            path.relative_to(spec.workdir).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted(spec.workdir.rglob("*"))
            if path.is_file()
        }
        self.sessions.append(Session(spec, files))
        if self.before is not None:
            self.before(spec)
        for event in self.emit:
            events(event)
        result = self.inner.run(spec, events)
        if self.after is not None:
            result = self.after(spec, result) or result
        return result

    def kinds(self) -> list[str]:
        """Each session as task:kind, in order."""
        return [f"{s.spec.task}:{s.kind}" for s in self.sessions]

    def of(self, task: str, kind: SessionKind | str) -> list[Session]:
        value = kind.value if isinstance(kind, SessionKind) else kind
        return [s for s in self.sessions if s.spec.task == task and s.kind == value]


@dataclass
class Ran:
    """A finished run, and everything it left behind."""

    root: Path
    result: RunResult
    events: list[ev.Event]
    executor: Recording
    clock: FakeClock

    @property
    def code(self) -> int:
        return int(self.result.exit_code)

    @property
    def manifest(self) -> RunManifest:
        assert self.result.run_id is not None
        return load_manifest(
            manifest_path(self.root / ".spadework", self.result.run_id)
        )

    @property
    def state(self) -> State:
        return load_state(state_path(self.root))

    def of(self, kind: type[Any]) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]

    @property
    def finished(self) -> ev.RunFinished:
        [event] = self.of(ev.RunFinished)
        return event

    def tasks(self) -> dict[str, str]:
        return {event.task: event.state for event in self.of(ev.TaskFinished)}


@dataclass
class Runner:
    """Runs the pipeline on one project, with a fresh executor each time."""

    root: Path
    tmp: Path
    clock: FakeClock = field(default_factory=FakeClock)

    def __call__(
        self,
        script: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        executor: Recording | None = None,
        verifier: SourceVerifier | None = None,
        transient_waits: tuple[float, ...] = (10.0, 30.0),
        **options: Any,
    ) -> Ran:
        executor = executor or Recording(script)
        events: list[ev.Event] = []
        if "only" in options:
            options["only"] = tuple(options["only"])
        result = run_project(
            self.root,
            RunOptions(**options),
            reporter=events.append,
            executor=executor,
            verifier=verifier,
            clock=self.clock,
            transient_waits=transient_waits,
            temp_root=self.tmp,
        )
        return Ran(self.root, result, events, executor, self.clock)


@pytest.fixture
def research_tmp(tmp_path: Path) -> Path:
    path = tmp_path / "research-tmp"
    path.mkdir()
    return path


@pytest.fixture
def runner(make_project: MakeProject, research_tmp: Path) -> Runner:
    """Runs the fixture playbook ``valid``: overview → landscape (research) → plan."""
    return Runner(make_project(), research_tmp)


def write_playbook(root: Path, tasks: Mapping[str, str], **manifest: Any) -> None:
    """Replace the project's playbook with one made of ``tasks`` (id → file)."""
    folder = root / "playbooks" / "valid"
    shutil.rmtree(folder)
    (folder / "tasks").mkdir(parents=True)
    lines = [
        "schema_version = 1",
        'name = "fixture"',
        f'version = "{manifest.get("version", "1.0.0")}"',
        'description = "A playbook for the engine tests."',
        "",
        "[defaults]",
        f"budget_usd = {manifest.get('budget_usd', 0.5)}",
    ]
    (folder / "playbook.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for task_id, text in tasks.items():
        (folder / "tasks" / f"{task_id}.md").write_text(text, encoding="utf-8")


def task_file(
    output: str,
    *,
    depends_on: tuple[str, ...] = (),
    inputs: tuple[str, ...] | None = None,
    extra: str = "",
    body: str = "Write it.\n",
) -> str:
    lines = ["---", f"title: {Path(output).stem.split('-', 1)[-1].title()}"]
    lines.append(f"output: {output}")
    if depends_on:
        lines.append(f"depends_on: [{', '.join(depends_on)}]")
    if inputs is not None:
        lines.append(f"inputs: {json.dumps(list(inputs))}")
    if extra:
        lines.append(extra.rstrip("\n"))
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def edit_config(root: Path, text: str) -> None:
    config = root / "spadework.toml"
    config.write_text(config.read_text(encoding="utf-8") + "\n" + text + "\n")
