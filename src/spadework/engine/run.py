"""The pipeline behind ``spadework run``. See docs/spec/04-engine.md.

``run_project`` loads and validates the project (as ``check`` does), takes
the project lock, plans with the real keys and state, and then, for each task
that needs it, in order: research (when its key changed, it has no stored
result, or ``--refresh-research``) → verification → draft → validators →
one repair when a failure is repairable. State is saved after every session
that changes it, and the manifest after every session. The review rounds and
the summary come next (``review``), then rendering into ``out/`` (``pack``).

Nothing raised after the run starts escapes without a manifest: budget and
time stops, rate limits, interrupts, executor errors and bugs all end with
``manifest.json`` written and its outcome matching the exit code. A run that
stops can be started again, and it resumes: a fresh task is never redrafted.
Checks that fail before the run starts (an invalid project, a bad option, a
held lock) write no manifest, since no run started.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from spadework import __version__
from spadework.brief import BRIEF_FILE
from spadework.budget import DEFAULT_ESTIMATES_USD
from spadework.config import LimitsSection, ModelsSection
from spadework.durations import parse_duration
from spadework.engine import events as ev
from spadework.engine.pack import adopt_rendered, render_pack, render_pending
from spadework.engine.planning import Planned, plan_project
from spadework.engine.prompts import (
    RESEARCH_NOTES,
    RESEARCH_SOURCES,
    draft_prompt,
    notes_file,
    repair_prompt,
    research_prompt,
    research_system_prompt,
    sources_file,
    writer_system_prompt,
)
from spadework.engine.review import Review, summary_owed
from spadework.engine.session import (
    MIN_SESSION_S,
    RESUME,
    TIME_MARGIN_S,
    TRANSIENT_WAITS_S,
    Clock,
    RunLimits,
    RunStopped,
    SessionOutcome,
    SessionRequest,
    SessionRunner,
    Staged,
    SystemClock,
    local_time,
)
from spadework.engine.store import (
    DraftRecord,
    ResearchRecord,
    draft_record_name,
    read_draft,
    read_research,
    read_summary,
    research_record_name,
    state_dir,
    work_path,
    write_record,
    write_work,
)
from spadework.engine.verification import (
    BADGES,
    STATUSES,
    NetworkVerifier,
    SourceVerifier,
    VerificationResult,
    as_result,
    counts,
)
from spadework.errors import ExitCode, SpadeworkError, UsageError
from spadework.executors import Executor, SessionKind, get_executor, is_simulated
from spadework.inputs import INPUTS_DIR, first_heading, match_inputs, scan_inputs
from spadework.keys import TaskMaterial
from spadework.lock import LOCK_FILE, ProjectLock
from spadework.manifest import (
    Component,
    Limits,
    Outcome,
    RunManifest,
    exit_code,
    manifest_path,
    new_run_id,
    run_dir,
    utc_timestamp,
    write_manifest,
)
from spadework.plan import SUMMARY_RESERVE_USD
from spadework.playbook import Task
from spadework.project import Project, load_project
from spadework.records import DraftResult, ResearchResult
from spadework.render import (
    DependencyFacts,
    ResearchFindings,
    StagedFile,
    render_research_questions,
    render_task_body,
    task_context,
)
from spadework.render import Source as SourceView
from spadework.state import OUT_DIR, ReviewProgress, State, save_state, state_path
from spadework.validators import (
    ValidatorResult,
    can_repair,
    failure_summary,
    failures,
    run_validators,
)

EXECUTOR_ENV = "SPADEWORK_EXECUTOR"


@dataclass(frozen=True)
class RunOptions:
    """``run``'s options (docs/spec/01-cli.md). None means the project's setting."""

    only: tuple[str, ...] = ()
    force: bool = False
    refresh_research: bool = False
    review: bool = True
    rounds: int | None = None
    budget_usd: float | None = None
    max_time: str | None = None
    wait_on_rate_limit: bool = False
    verify: bool = True
    draft_model: str | None = None
    review_model: str | None = None


@dataclass(frozen=True)
class RunResult:
    exit_code: ExitCode
    outcome: Outcome | None
    """None when nothing needed doing: the project is up to date."""
    run_id: str | None = None
    manifest: RunManifest | None = None
    exception: BaseException | None = None
    """An unexpected error that ended the run, for ``-v``."""
    reporter_error: Exception | None = None
    """What stopped the progress output, if something did. The run went on."""


def run_project(
    root: Path,
    options: RunOptions,
    *,
    reporter: ev.Reporter = ev.ignore,
    executor: Executor | None = None,
    verifier: SourceVerifier | None = None,
    clock: Clock | None = None,
    transient_waits: Sequence[float] = TRANSIENT_WAITS_S,
    temp_root: Path | None = None,
) -> RunResult:
    """Run the pipeline on the project in ``root``.

    Raises ``SpadeworkError`` for anything that stops the run before it
    starts: an invalid project (exit 3), a bad option (2), or a lock held by
    another run (7). Everything after that is in the result.
    """
    guarded = _Guarded(reporter)
    result = _run_project(
        root,
        options,
        reporter=guarded,
        executor=executor,
        verifier=verifier,
        clock=clock,
        transient_waits=transient_waits,
        temp_root=temp_root,
    )
    if guarded.error is None:
        return result
    return replace(result, reporter_error=guarded.error)


class _Guarded:
    """A reporter that cannot break the run.

    The first exception it raises stops all progress output and is kept for
    the caller, so a display that fails, or a reader that goes away, never
    changes how a run ends: the exit code always matches the manifest.
    """

    def __init__(self, reporter: ev.Reporter) -> None:
        self.reporter = reporter
        self.error: Exception | None = None

    def __call__(self, event: ev.Event) -> None:
        if self.error is not None:
            return
        try:
            self.reporter(event)
        except Exception as exc:
            self.error = exc


def _run_project(
    root: Path,
    options: RunOptions,
    *,
    reporter: ev.Reporter,
    executor: Executor | None,
    verifier: SourceVerifier | None,
    clock: Clock | None,
    transient_waits: Sequence[float],
    temp_root: Path | None,
) -> RunResult:
    project = load_project(root)
    task_ids = [task.id for task in project.playbook.tasks]
    for task_id in options.only:
        if task_id not in task_ids:
            raise UsageError(
                f"--only {task_id}: there is no task {task_id!r}",
                f"Tasks in {project.playbook.name}: {', '.join(task_ids)}.",
            )
    models = _models(project, options)
    limits = _limits(project.config.limits, options)
    if executor is None:
        name = os.environ.get(EXECUTOR_ENV) or project.config.executor.name
        executor = get_executor(name, claude_bin=project.config.executor.bin)
    with ProjectLock(state_dir(root) / LOCK_FILE) as lock:
        if lock.warning:
            reporter(ev.Notice(lock.warning))
        planned = plan_project(
            project,
            models=models,
            limits=limits,
            only=options.only,
            force=options.force,
            refresh_research=options.refresh_research,
        )
        reporter(ev.PlanReady(planned.plan, planned.warnings))
        _check_dependencies(project, planned, options.only)
        if (
            not planned.to_run
            and planned.state.review is None
            and not summary_owed(project, planned.state)
            and not render_pending(project, planned.state)
        ):
            invalid = tuple(
                task.id for task in planned.plan.tasks if task.state == "invalid"
            )
            code = ExitCode.PARTIAL if invalid else ExitCode.OK
            reporter(ev.UpToDate(int(code), invalid))
            return RunResult(code, None)
        run = _Run(
            project,
            planned,
            options,
            models=models,
            limits=limits,
            executor=executor,
            verifier=verifier or NetworkVerifier(),
            clock=clock or SystemClock(),
            reporter=reporter,
            transient_waits=transient_waits,
            temp_root=temp_root,
        )
        return run.execute()


def _models(project: Project, options: RunOptions) -> ModelsSection:
    models = project.config.models
    for flag, value in (
        ("--draft-model", options.draft_model),
        ("--review-model", options.review_model),
    ):
        if value is not None and not value.strip():
            raise UsageError(f"{flag} is empty", "Give a model name, such as sonnet.")
    return models.model_copy(
        update={
            "draft": options.draft_model or models.draft,
            "review": options.review_model or models.review,
        }
    )


def _limits(limits: LimitsSection, options: RunOptions) -> LimitsSection:
    update: dict[str, object] = {}
    if options.budget_usd is not None:
        if not 0 < options.budget_usd < float("inf"):
            raise UsageError(
                f"--budget-usd {options.budget_usd} is not a positive amount",
                "Give the run's cap in US dollars, such as --budget-usd 4.50.",
            )
        update["budget_usd"] = float(options.budget_usd)
    if options.max_time is not None:
        try:
            parse_duration(options.max_time)
        except ValueError as exc:
            raise UsageError(
                f"--max-time: {exc}", "Use a duration such as 90m or 2h."
            ) from None
        update["max_time"] = options.max_time
    if not options.review:
        update["rounds"] = 0
    elif options.rounds is not None:
        if options.rounds < 0:
            raise UsageError(
                "--rounds cannot be negative", "Use 0 or more, or --no-review."
            )
        update["rounds"] = options.rounds
    return limits.model_copy(update=update)


def _check_dependencies(
    project: Project, planned: Planned, only: Sequence[str]
) -> None:
    """With ``--only``, every dependency left out must have an output to use."""
    if not only:
        return
    running = set(planned.to_run)
    for task_id in planned.to_run:
        for dep in project.playbook.task(task_id).depends_on:
            if dep in running or dep in planned.pinned:
                continue
            entry = planned.state.tasks.get(dep)
            output = project.playbook.task(dep).output
            if (
                entry is None
                or entry.output is None
                or not work_path(project.root, output).is_file()
            ):
                raise UsageError(
                    f"--only {task_id}: its dependency {dep!r} has never produced "
                    "an output",
                    f"Run it first with `spadework run --only {dep}`, or run "
                    "without --only.",
                )


@dataclass
class _Dependency:
    task: Task
    path: Path
    key: str
    """The key recorded as read: its output's draft key, or its current key
    when a person's edits stand in for it (pinned)."""
    pinned: bool
    key_facts: tuple[str, ...]


class _Run:
    def __init__(
        self,
        project: Project,
        planned: Planned,
        options: RunOptions,
        *,
        models: ModelsSection,
        limits: LimitsSection,
        executor: Executor,
        verifier: SourceVerifier,
        clock: Clock,
        reporter: ev.Reporter,
        transient_waits: Sequence[float],
        temp_root: Path | None,
    ) -> None:
        self.project = project
        self.root = project.root
        self.playbook = project.playbook
        self.planned = planned
        self.options = options
        self.models = models
        self.executor = executor
        self.verifier = verifier
        self.clock = clock
        self.report = reporter
        self.state: State = planned.state
        self.run_id = new_run_id()
        self.dot = state_dir(self.root)
        self.run_dir = run_dir(self.dot, self.run_id)
        self.started = clock.monotonic()
        self.limits = RunLimits(
            budget_usd=limits.budget_usd,
            reserve_usd=SUMMARY_RESERVE_USD,
            max_time_s=limits.max_time_s,
            session_timeout_s=limits.session_timeout_s,
            started=self.started,
        )
        self.manifest = RunManifest(
            run_id=self.run_id,
            spadework_version=__version__,
            executor=Component(name=executor.name, version=executor.version()),
            playbook=Component(name=self.playbook.name, version=self.playbook.version),
            limits=Limits(
                budget_usd=float(limits.budget_usd),
                max_time_s=limits.max_time_s,
                rounds=limits.rounds,
            ),
            started_at=utc_timestamp(),
        )
        self.sessions = SessionRunner(
            root=self.root,
            run_dir=self.run_dir,
            manifest=self.manifest,
            save_manifest=self._save_manifest,
            executor=executor,
            limits=self.limits,
            clock=clock,
            reporter=reporter,
            wait_on_rate_limit=options.wait_on_rate_limit,
            transient_waits=transient_waits,
            temp_root=temp_root,
        )
        self.done: dict[str, ev.TaskOutcome] = {}
        self.rendered: list[str] = []
        self.final: dict[str, str] | None = None
        """Each task's state once the run has rendered."""
        self.summary_error: str | None = None
        self.render_status: Literal["not started", "rendering", "done"] = "not started"
        """A render that raised is not tried again on the way out."""

    def execute(self) -> RunResult:
        outcome: Outcome = "error"
        message: str | None = None
        hint: str | None = None
        reset: dt.datetime | None = None
        failure: BaseException | None = None
        try:
            try:
                self._save_manifest()
                self.report(self._started())
                adopt_rendered(self.project, self.state)
                if self.state.review is None and summary_owed(self.project, self.state):
                    # The last summary failed, or there never was one: only
                    # the summary and the render are owed.
                    self.state.review = self._summary_only()
                self.save_state()
                for task_id in self.planned.to_run:
                    self._task(self.playbook.task(task_id))
                self._review()
                self._render()
                outcome, message, hint = self._outcome()
            except RunStopped as stop:
                outcome, message, hint, reset = (
                    stop.outcome,
                    stop.message,
                    stop.hint,
                    stop.reset,
                )
            except KeyboardInterrupt:
                outcome, message, hint = "interrupted", "interrupted", RESUME
            except SpadeworkError as exc:
                outcome, message, hint = "error", exc.message, exc.hint
                failure = exc
            except Exception as exc:
                outcome = "error"
                message = f"unexpected error: {type(exc).__name__}: {exc}"
                hint = "Run again with -v to see the traceback. Please report it."
                failure = exc
            if self.render_status == "not started":
                self._after_stop(outcome)
        finally:
            self.manifest.finish(
                outcome,
                error=message if outcome == "error" else None,
                rate_limit_reset=None if reset is None else utc_timestamp(reset),
            )
            self._save_manifest()
        self.report(self._finished(outcome, message, hint, reset))
        return RunResult(
            exit_code(outcome), outcome, self.run_id, self.manifest, failure
        )

    def _started(self) -> ev.RunStarted:
        return ev.RunStarted(
            run_id=self.run_id,
            project=self.project.config.project.name,
            playbook=self.playbook.name,
            playbook_version=self.playbook.version,
            executor=self.manifest.executor.name,
            executor_version=self.manifest.executor.version,
            budget_usd=self.limits.budget_usd,
            max_time_s=self.limits.max_time_s,
            rounds=self.manifest.limits.rounds,
            tasks=self.planned.to_run,
        )

    # One task: research → verification → draft → validators → repair.

    def _task(self, task: Task) -> None:
        material = self.planned.materials[task.id]
        deps = self._dependencies(task)
        if isinstance(deps, str):
            self._finish_task(task.id, "skipped", deps)
            return
        research: ResearchRecord | None = None
        if task.research is not None:
            research = self._research(task, material)
            if research is None:
                return
        self._draft(task, material, deps, research)

    def _dependencies(self, task: Task) -> list[_Dependency] | str:
        """The dependency outputs to stage, or why the task cannot run now."""
        deps = []
        for dep_id in task.depends_on:
            if self.done.get(dep_id) in ("failed", "skipped"):
                return f"dependency {dep_id} {self.done[dep_id]}"
            dep = self.playbook.task(dep_id)
            pinned = dep_id in self.planned.pinned and dep_id not in self.done
            entry = self.state.tasks.get(dep_id)
            if pinned:
                path = self.root / OUT_DIR / dep.output
                key = self.planned.materials[dep_id].key
            elif entry is None or entry.output is None:
                return f"dependency {dep_id} has no output"
            else:
                path = work_path(self.root, dep.output)
                key = entry.output.key
            if not path.is_file():
                return f"the output of dependency {dep_id} is missing"
            # A person's edits may contradict the key facts of the engine's
            # version, so a pinned dependency passes none: its file is read.
            record = None if pinned else read_draft(self.root, dep_id)
            facts = () if record is None else tuple(record.result.key_facts)
            deps.append(_Dependency(dep, path, key, pinned, facts))
        return deps

    def _research(self, task: Task, material: TaskMaterial) -> ResearchRecord | None:
        research = task.research
        key = material.research_key
        assert research is not None
        assert key is not None
        name = self.state.research_record(task.id, key)
        record = None
        if name is not None and not self.options.refresh_research:
            record = read_research(self.root, name)
        if record is None:
            brief = self.project.brief
            public_context = brief.public_context or ""
            questions = render_research_questions(
                self.playbook,
                task,
                public_context=public_context,
                language=brief.front.language,
            )
            outcome = self.sessions.run(
                SessionRequest(
                    kind=SessionKind.RESEARCH,
                    task=task.id,
                    prompt=research_prompt(
                        public_context=public_context,
                        questions=questions,
                        max_searches=research.max_searches,
                        max_fetches=research.max_fetches,
                        today=dt.datetime.now(dt.UTC).date(),
                    ),
                    system_append=research_system_prompt(self.playbook),
                    model=self.models.draft,
                    key=key,
                    estimate_usd=DEFAULT_ESTIMATES_USD["research"],
                    max_searches=research.max_searches,
                    max_fetches=research.max_fetches,
                )
            )
            if not isinstance(outcome.record, ResearchResult):
                self._fail(task, material, f"research failed: {outcome.error}")
                return None
            result = outcome.record
            record = ResearchRecord(
                task=task.id,
                key=key,
                run_id=self.run_id,
                result=result,
                verification={
                    source.id: VerificationResult() for source in result.sources
                },
                guard_denials=outcome.guard_denials,
            )
            name = write_record(self.root, research_record_name(task.id), record)
            self.state.record_research(
                task.id, key=key, record=name, run_id=self.run_id
            )
            self.save_state()
        assert name is not None
        return self._verify(record, name)

    def _verify(self, record: ResearchRecord, name: str) -> ResearchRecord:
        """Check the sources not yet checked, unless verification is off.

        Checking counts against the run's time. It is given what is left
        once a draft session could still start; with less than that, the
        sources stay unchecked for the next run, which drafts next anyway.
        """
        verify = self.options.verify and self.project.config.verify.enabled
        unchecked = [
            source
            for source in record.result.sources
            if record.status(source.id) == "unchecked"
        ]
        if not verify or not unchecked:
            return record
        left = self.limits.deadline - self.clock.monotonic()
        room = left - TIME_MARGIN_S - MIN_SESSION_S
        if room <= 0:
            return record
        found = self.verifier.verify_sources(unchecked, time_limit=room)
        changed = {
            source_id: as_result(value)
            for source_id, value in found.items()
            if as_result(value).status != "unchecked"
            and source_id in record.verification
        }
        if not changed:
            return record
        record = record.model_copy(
            update={"verification": {**record.verification, **changed}}
        )
        write_record(self.root, name, record)
        tally = counts([result.status for result in changed.values()])
        shown = ", ".join(
            f"{number} {BADGES[status].split(' ', 1)[1]}"
            for status, number in tally.items()
            if number and status in STATUSES
        )
        self.report(
            ev.Notice(
                f"{record.task}: checked {len(changed)} of "
                f"{len(record.result.sources)} sources: {shown}",
                level="info",
                task=record.task,
            )
        )
        return record

    def _draft(
        self,
        task: Task,
        material: TaskMaterial,
        deps: list[_Dependency],
        research: ResearchRecord | None,
    ) -> None:
        brief = self.project.brief
        inputs = match_inputs(task.inputs, self.project.inputs)
        # Links inside the project are staged by what they point to; check
        # refuses a brief.md that points outside it.
        brief_file = Path(os.path.realpath(self.root / BRIEF_FILE))
        files = [Staged(BRIEF_FILE, source=brief_file)]
        staged = [StagedFile(BRIEF_FILE, first_heading(brief.body))]
        for file in inputs:
            dest = f"{INPUTS_DIR}/{file.name}"
            files.append(Staged(dest, source=Path(os.path.realpath(file.path))))
            staged.append(StagedFile(dest, file.heading))
        for dep in deps:
            files.append(Staged(dep.task.output, source=dep.path))
            heading = _heading(dep.path) or dep.task.title
            staged.append(StagedFile(dep.task.output, heading))
        findings = None
        sources: tuple[SourceView, ...] = ()
        if research is not None:
            sources = tuple(
                SourceView(
                    **source.model_dump(),
                    status=research.status(source.id),
                    detail=research.result_of(source.id).detail,
                )
                for source in research.result.sources
            )
            findings = ResearchFindings(
                notes=research.result.notes,
                sources=sources,
                unanswered=tuple(research.result.unanswered),
            )
            files.append(Staged(RESEARCH_NOTES, text=notes_file(findings)))
            files.append(Staged(RESEARCH_SOURCES, text=sources_file(findings)))
            staged.append(StagedFile(RESEARCH_NOTES, "Research notes"))
            staged.append(StagedFile(RESEARCH_SOURCES, "Sources"))
        facts = [DependencyFacts(dep.task, dep.key_facts) for dep in deps]
        try:
            body = render_task_body(
                self.playbook,
                task,
                task_context(
                    project_name=self.project.config.project.name,
                    brief=brief,
                    task=task,
                    files=staged,
                    deps=facts,
                    research=findings,
                ),
            )
        except Exception as exc:
            self._fail(task, material, f"the task template failed: {exc}")
            return
        prompt = draft_prompt(
            task,
            body,
            files=staged,
            deps=facts,
            research=findings,
            pinned={dep.task.id for dep in deps if dep.pinned},
        )
        # What this draft is made from, with the dependency outputs it read.
        drafted = material.model_copy(
            update={
                "draft": material.draft.model_copy(
                    update={"dependencies": {dep.task.id: dep.key for dep in deps}}
                )
            }
        )
        research_sources = () if research is None else research.result.sources

        def check(record: object, content: bytes | None) -> list[ValidatorResult]:
            assert isinstance(record, DraftResult)
            text = (content or b"").decode("utf-8", errors="replace")
            return run_validators(
                text, task.validators, result=record, sources=research_sources
            )

        request = SessionRequest(
            kind=SessionKind.DRAFT,
            task=task.id,
            prompt=prompt,
            system_append=writer_system_prompt("draft", self.playbook, brief),
            model=self.model_for(task),
            key=drafted.key,
            estimate_usd=task.budget_usd,
            output=task.output,
            files=tuple(files),
            check=check,
        )
        outcome = self.sessions.run(request)
        if not outcome.ok:
            self._fail(task, material, outcome.error or "the draft failed")
            return
        kind = "draft"
        failed = failures(outcome.validators)
        if failed and can_repair(outcome.validators):
            try:
                repaired = self.sessions.run(
                    SessionRequest(
                        kind=SessionKind.REPAIR,
                        task=task.id,
                        prompt=repair_prompt(task, prompt, failed),
                        system_append=writer_system_prompt(
                            "repair", self.playbook, brief
                        ),
                        model=request.model,
                        key=drafted.key,
                        estimate_usd=DEFAULT_ESTIMATES_USD["repair"],
                        output=task.output,
                        files=request.files,
                        seed=outcome.content,
                        check=check,
                    )
                )
            except (RunStopped, KeyboardInterrupt):
                # Keep the draft that was paid for; it is invalid until fixed.
                self._record(task, drafted, outcome, "draft")
                raise
            if repaired.ok:
                outcome, kind = repaired, "repair"
            else:
                self.report(
                    ev.Notice(
                        f"{task.id}: the repair failed ({repaired.error}); keeping "
                        "the draft",
                        task=task.id,
                    )
                )
        self._record(task, drafted, outcome, kind)

    def _record(
        self,
        task: Task,
        material: TaskMaterial,
        outcome: SessionOutcome,
        kind: str,
    ) -> None:
        result = outcome.record
        assert isinstance(result, DraftResult)
        assert outcome.content is not None
        previous = self.state.tasks.get(task.id)
        before = (
            None
            if previous is None or previous.output is None
            else (previous.output.hash)
        )
        digest = write_work(self.root, task.output, outcome.content)
        write_record(
            self.root,
            draft_record_name(task.id),
            DraftRecord(
                task=task.id,
                key=material.key,
                run_id=self.run_id,
                kind="repair" if kind == "repair" else "draft",
                result=result,
            ),
        )
        entry = self.state.record_draft(
            task.id,
            material=material,
            output_hash=digest,
            validators=outcome.validators,
            run_id=self.run_id,
        )
        review = self.state.review
        carried = review is not None and review.outputs_changed
        if review is None or review.run_id != self.run_id or review.reviewed:
            # This run drafted an output: its review, summary and render are
            # owed from now on, even if it is interrupted.
            review = ReviewProgress(run_id=self.run_id, outputs_changed=carried)
        if digest != before and not review.outputs_changed:
            review = review.model_copy(update={"outputs_changed": True})
        self.state.review = review
        self.save_state()
        reason = failure_summary(outcome.validators)
        self._finish_task(task.id, entry.status, reason, outcome.validators)

    def _fail(self, task: Task, material: TaskMaterial, error: str) -> None:
        self.state.record_failure(
            task.id, material=material, error=error, run_id=self.run_id
        )
        self.save_state()
        self._finish_task(task.id, "failed", error)

    def _finish_task(
        self,
        task_id: str,
        state: ev.TaskOutcome,
        reason: str | None,
        validators: Sequence[ValidatorResult] = (),
    ) -> None:
        self.done[task_id] = state
        self.report(ev.TaskFinished(task_id, state, reason, tuple(validators)))

    def model_for(self, task: Task) -> str:
        return self.models.draft if task.tier == "draft" else self.models.review

    # After the drafts.

    def _replaced(self) -> frozenset[str]:
        """Pinned tasks whose edits this run replaced: ``--force --only``
        redrafted them and recorded a new output. A failed redraft leaves
        the task pinned and its file untouched."""
        redrafted = {
            task_id
            for task_id, state in self.done.items()
            if state in ("fresh", "invalid")
        }
        return self.planned.replaced & redrafted

    def _pinned(self) -> frozenset[str]:
        return self.planned.pinned - self._replaced()

    def _summary_only(self) -> ReviewProgress:
        """Progress for a run that owes only the summary and the render.

        The last summary's findings for a person are kept for the documents
        a person still has edited: no review runs to find them again.
        """
        record = read_summary(self.root)
        pinned = self._pinned()
        kept = [] if record is None else record.pinned_findings
        return ReviewProgress(
            run_id=self.run_id,
            reviewed=True,
            pinned_findings=[f for f in kept if f.task in pinned],
        )

    def _review(self) -> None:
        """The review rounds and the summary, when an output changed."""
        review = self._reviewer()
        review.run()
        self.summary_error = review.summary_error

    def _reviewer(self) -> Review:
        return Review(
            project=self.project,
            state=self.state,
            sessions=self.sessions,
            limits=self.limits,
            clock=self.clock,
            report=self.report,
            run_id=self.run_id,
            manifest=self.manifest,
            models=self.models,
            rounds=self.manifest.limits.rounds,
            pinned=self._pinned(),
            save_state=self.save_state,
            final_states=self.final_states,
        )

    def _after_stop(self, outcome: Outcome) -> None:
        """Render what a run that stopped had finished, and a summary of it.

        The documents finished so far, ``sources.md`` and ``pack.json`` go
        into ``out/``, as at the end of any run, and a person's edits stay.
        If this run (or an unfinished one) changed an output's bytes, the
        summary is owed: on a budget stop with time left, the summary session
        runs on its reserve; otherwise ``00-summary.md`` has only what code
        writes. Either way it is marked pending, and the next run that
        finishes writes it again. Otherwise the summary is left as it is.
        """
        try:
            if self._summary_owed_on_stop():
                ran = any(s.kind == "summary" for s in self.manifest.sessions)
                session = (
                    outcome == "budget"
                    and not ran
                    and self.limits.session_fits(self.clock.monotonic())
                )
                self._reviewer().summary_after_stop(session=session)
            self._render(final=False)
        except KeyboardInterrupt:
            self.report(ev.Notice("the pack was not rendered: interrupted"))
        except Exception as exc:
            self.report(
                ev.Notice(
                    f"the pack could not be rendered: {type(exc).__name__}: {exc}",
                    "`spadework run` renders it when it next finishes.",
                )
            )

    def _summary_owed_on_stop(self) -> bool:
        """Whether a stopped run must replace the summary: an output's bytes
        changed since it was written, or there is none. A redraft to the same
        bytes leaves the prose summary and its record as they are."""
        progress = self.state.review
        if progress is None:
            return False
        return progress.outputs_changed or read_summary(self.root) is None

    def _render(self, *, final: bool = True) -> None:
        """Render the pack into ``out/``. ``final`` ends the review: a run
        that stopped leaves it owed."""
        self.render_status = "rendering"
        self.final = self.final_states()
        executor = self.manifest.executor
        rendered = render_pack(
            self.project,
            self.state,
            states=self.final,
            pinned=self._pinned(),
            versions={
                "spadework": __version__,
                "playbook": f"{self.playbook.name}@{self.playbook.version}",
                "executor": executor.name
                if executor.version is None
                else f"{executor.name}@{executor.version}",
            },
            generated_at=utc_timestamp(),
            save=self.save_state,
            replace=self._replaced(),
        )
        for message, hint in rendered.warnings:
            self.report(ev.Notice(message, hint))
        self.rendered = rendered.written
        if final:
            self.state.review = None
        self.save_state()
        self.render_status = "done"

    def _outcome(self) -> tuple[Outcome, str | None, str | None]:
        final = self.final or self.final_states()
        self.final = final
        unfinished = [
            task_id
            for task_id, state in final.items()
            if state in ("failed", "invalid", "skipped")
        ]
        if not unfinished:
            if self.summary_error is not None:
                return (
                    "partial",
                    f"the summary failed: {self.summary_error}",
                    "The next `spadework run` writes it again.",
                )
            return "complete", None, None
        listed = ", ".join(f"{task_id} ({final[task_id]})" for task_id in unfinished)
        return (
            "partial",
            f"{len(unfinished)} of {len(final)} tasks "
            f"{'is' if len(unfinished) == 1 else 'are'} not done: {listed}",
            "`spadework status` says why. Failed and skipped tasks run again next "
            "time; to redraft an invalid one, use `spadework run --force --only "
            "TASK`.",
        )

    def final_states(self) -> dict[str, str]:
        """Each task's state now, planned against inputs/ as it is now.

        inputs/ may have changed during the run: a file added, edited or
        removed is a reason for staleness, never an error.
        """
        try:
            inputs = tuple(scan_inputs(self.root / INPUTS_DIR, self.root))
            project = replace(self.project, inputs=inputs)
            after = plan_project(project, self.state, models=self.models)
            final: dict[str, str] = {task.id: task.state for task in after.plan.tasks}
        except (SpadeworkError, OSError):
            # inputs/ holds something check would refuse, or changed while it
            # was read: the states this run knows are the best answer.
            final = {task.id: task.state for task in self.planned.plan.tasks}
            final.update(self.done)
        for task_id, state in self.done.items():
            # A failed attempt counts even when a person's edits stand in
            # for the task (pinned): the work asked for was not done.
            if state in ("failed", "skipped"):
                final[task_id] = state
        for task_id in self._replaced():
            # The render replaces the person's edits with this run's output,
            # so the task is no longer pinned, even before it renders.
            entry = self.state.tasks.get(task_id)
            if final.get(task_id) == "pinned" and entry is not None:
                final[task_id] = entry.status
        return final

    def _finished(
        self,
        outcome: Outcome,
        message: str | None,
        hint: str | None,
        reset: dt.datetime | None,
    ) -> ev.RunFinished:
        totals = self.manifest.totals
        final = self.final or {
            task.id: self.done.get(task.id, task.state)
            for task in self.planned.plan.tasks
        }
        return ev.RunFinished(
            run_id=self.run_id,
            outcome=outcome,
            exit_code=int(exit_code(outcome)),
            cost_usd=totals.cost_usd,
            budget_usd=self.limits.budget_usd,
            sessions=totals.sessions,
            guard_denials=totals.guard_denials,
            duration_s=round(self.clock.monotonic() - self.started, 3),
            max_time_s=self.limits.max_time_s,
            message=message,
            hint=hint,
            rate_limit_reset=None if reset is None else utc_timestamp(reset),
            resume_after=None if reset is None else local_time(reset),
            rendered=tuple(self.rendered),
            tasks=dict(final),
            possible_injection=tuple(
                dict.fromkeys(
                    session.task
                    for session in self.manifest.sessions
                    if session.kind == "research"
                    and session.guard_denials
                    and session.task is not None
                )
            ),
            manifest=os.path.relpath(
                manifest_path(self.dot, self.run_id), self.root
            ).replace(os.sep, "/"),
            simulated=is_simulated(self.manifest.executor.name),
        )

    def save_state(self) -> None:
        save_state(state_path(self.root), self.state)

    def _save_manifest(self) -> None:
        write_manifest(self.dot, self.manifest)


def _heading(path: Path) -> str | None:
    try:
        return first_heading(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
