"""The review loop and the summary (docs/spec/04-engine.md).

They run when a run changed an output, or when an interrupted run left them
owed (``state.review``):

    for round in 1..rounds:
        stop if the budget or time left cannot cover a critic plus one fix
        critic → findings, plus a finding for each validator failure
        record the findings against documents a person edited, for the summary
        stop if the verdict is ship and no validator is failing
        for each other document with findings, in task order:
            fix → validators → one repair if they fail and there is room
        stop if the round changed no document
    summary

Each step is saved as it ends (``ReviewProgress``), so a resumed run picks
up where the last one stopped: a round whose critic ran uses its findings, a
finished fix is not done again, a repair still owed is done alone, and a
finished summary leaves only the render. A fixed document is marked done,
with its hash, before it is written, so a run that stops in between never
fixes it again. A critic or summary record saved by a run that stopped before
noting it is used, not paid for twice.

The review never takes the summary's money or time. Its sessions keep a slot
of time for the summary free, and when the budget or time left cannot cover
the next critic, fix or repair, or a review session uses up what was left of
either, the review ends and the summary, whose estimate was kept back from
the start, still runs.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from spadework import __version__
from spadework.brief import BRIEF_FILE
from spadework.budget import DEFAULT_ESTIMATES_USD
from spadework.config import ModelsSection
from spadework.engine import events as ev
from spadework.engine.documents import Document, load_documents, summary_prose
from spadework.engine.prompts import (
    RECORDS_DIGEST,
    RESEARCH_SOURCES,
    critic_prompt,
    fix_prompt,
    records_digest,
    repair_prompt,
    sources_file,
    summary_prompt,
    writer_system_prompt,
)
from spadework.engine.session import (
    MIN_SESSION_S,
    Clock,
    RunLimits,
    RunStopped,
    SessionOutcome,
    SessionRequest,
    SessionRunner,
    Staged,
)
from spadework.engine.store import (
    SUMMARY_FILE,
    SUMMARY_RECORD,
    CriticRecord,
    DraftRecord,
    Produced,
    ResearchRecord,
    SummaryRecord,
    critic_record_name,
    draft_record_name,
    read_critic,
    read_draft,
    read_research,
    read_summary,
    read_work,
    work_path,
    write_record,
    write_work,
)
from spadework.errors import SpadeworkError
from spadework.executors.base import SessionKind, is_simulated
from spadework.inputs import INPUTS_DIR, InputFile, match_inputs, scan_inputs
from spadework.keys import hash_bytes
from spadework.manifest import RunManifest, load_manifest
from spadework.playbook import Task
from spadework.project import Project
from spadework.records import (
    CriticResult,
    DraftResult,
    FixResult,
    Record,
    SummaryResult,
)
from spadework.render import (
    DependencyFacts,
    ResearchFindings,
    StagedFile,
    render_task_body,
    task_context,
)
from spadework.render import Source as SourceView
from spadework.state import (
    PendingWrite,
    ReviewFinding,
    ReviewProgress,
    State,
    file_hash,
)
from spadework.validators import (
    ValidatorResult,
    can_repair,
    failure_summary,
    failures,
    run_validators,
)

CRITIC_USD = DEFAULT_ESTIMATES_USD["critic"]
FIX_USD = DEFAULT_ESTIMATES_USD["fix"]
REPAIR_USD = DEFAULT_ESTIMATES_USD["repair"]
SUMMARY_USD = DEFAULT_ESTIMATES_USD["summary"]

# The kind of finding each validator's failure becomes.
VALIDATOR_KINDS = {
    "max_lines": "length",
    "min_lines": "length",
    "starts_with_h1": "missing",
    "required_headings": "missing",
    "forbidden_phrases": "filler",
    "max_open_questions": "other",
    "citations_resolve": "fabrication",
    "min_sources": "missing",
}


def summary_owed(project: Project, state: State) -> bool:
    """Whether the pack has documents but no summary: none was ever written,
    its prose is gone, or the last summary session failed."""
    if not any(
        entry.output is not None
        for task_id, entry in state.tasks.items()
        if task_id in {task.id for task in project.playbook.tasks}
    ):
        return False
    record = read_summary(project.root)
    if record is None or record.result is None:
        return True
    return summary_prose(project.root) is None


@dataclass
class Review:
    """The review and the summary of one run. Shares the run's state."""

    project: Project
    state: State
    sessions: SessionRunner
    limits: RunLimits
    clock: Clock
    report: ev.Reporter
    run_id: str
    manifest: RunManifest
    models: ModelsSection
    rounds: int
    pinned: frozenset[str]
    """Documents a person edited: never fixed, their findings recorded."""
    save_state: Callable[[], None]
    final_states: Callable[[], dict[str, str]]
    summary_error: str | None = field(default=None, init=False)
    """Why the summary session failed, if it did."""

    @property
    def root(self) -> Path:
        return self.project.root

    def run(self) -> None:
        if self.state.review is None:
            return
        self._resume_write()
        if not self._progress().reviewed:
            try:
                self._rounds()
            except RunStopped as stop:
                # A session that used what was left of the budget, or of the
                # time before the summary's slot: the summary still runs, on
                # its reserve, if there is time for it.
                summary_fits = self.limits.session_fits(self.clock.monotonic())
                if stop.outcome not in ("budget", "time") or not summary_fits:
                    raise
                self._notice(f"the review stopped: {stop.message}")
            self._set(self._progress().model_copy(update={"reviewed": True}))
        if not self._progress().summarised and not self._adopt_summary():
            self._summary()

    # Progress.

    def _progress(self) -> ReviewProgress:
        progress = self.state.review
        assert progress is not None
        return progress

    def _set(self, progress: ReviewProgress) -> None:
        self.state.review = progress
        self.save_state()

    def _notice(self, message: str, *, task: str | None = None) -> None:
        self.report(ev.Notice(message, level="info", task=task))

    # The rounds.

    def _rounds(self) -> None:
        round_ = max(self._progress().round, 1)
        while round_ <= self.rounds:
            progress = self._progress()
            if progress.round != round_:
                self._set(progress.start_round(round_))
            critic = self._critic_of(round_)
            if critic is None:
                return
            self._keep_pinned(critic)
            if critic.ship:
                self._notice(f"review round {round_}: the critic's verdict is ship")
                return
            if not self._fixes(critic):
                return
            if not self._progress().changed:
                # Another critic would see the same pack: every finding was
                # on a document a person edited, or every fix failed.
                self._notice(
                    f"review round {round_} changed no document, so the review stopped"
                )
                return
            round_ += 1

    def _room(self, estimate: float, sessions: int) -> str | None:
        """Why the review cannot go on: the money or time for ``sessions``
        more sessions, and the summary after them, is not there."""
        return self.limits.room(estimate, sessions + 1, self.clock.monotonic())

    def _critic_of(self, round_: int) -> CriticRecord | None:
        """The round's findings: from its critic record, or a new critic."""
        progress = self._progress()
        name = progress.critic or critic_record_name(round_)
        record = read_critic(self.root, name)
        if (
            record is not None
            and record.round == round_
            and (progress.critic is not None or record.review == progress.run_id)
        ):
            if progress.critic is None:
                # Saved by a run that stopped before it noted so.
                self._set(progress.with_critic(name))
            return record
        reason = self._room(CRITIC_USD + FIX_USD, 2)
        if reason is not None:
            self._notice(f"the review stopped before round {round_}: {reason}")
            return None
        return self._critic(round_)

    def _critic(self, round_: int) -> CriticRecord | None:
        playbook = self.project.playbook
        documents = self._documents()
        failing = _validator_findings(documents, self.pinned, round_)
        files, staged = self._pack_files(documents)
        limit = playbook.manifest.review.max_findings
        outcome = self.sessions.run(
            SessionRequest(
                kind=SessionKind.CRITIC,
                task=None,
                prompt=critic_prompt(
                    round_=round_,
                    rounds=self.rounds,
                    documents=documents,
                    files=staged,
                    priorities=self.project.brief.front.priorities or [],
                    failures=failing,
                    max_findings=limit,
                ),
                system_append=writer_system_prompt(
                    "critic", playbook, self.project.brief
                ),
                model=self.models.review,
                key=None,
                estimate_usd=CRITIC_USD,
                files=tuple(files),
                round=round_,
                max_findings=limit,
                keep_s=MIN_SESSION_S,
            )
        )
        if not isinstance(outcome.record, CriticResult):
            self.report(
                ev.Notice(
                    f"review round {round_}: the critic failed ({outcome.error}), "
                    "so the review stopped"
                )
            )
            return None
        findings, dropped = _findings(outcome.record, documents, failing, round_)
        for reason in dropped:
            self.report(ev.Notice(f"review round {round_}: dropped {reason}"))
        record = CriticRecord(
            run_id=self.run_id,
            review=self._progress().run_id,
            round=round_,
            result=outcome.record,
            findings=findings,
            dropped=dropped,
            validators_failing=bool(failing),
        )
        name = write_record(self.root, critic_record_name(round_), record)
        self._set(self._progress().with_critic(name))
        return record

    def _keep_pinned(self, critic: CriticRecord) -> None:
        """Record the findings against documents a person edited, for the
        summary: they are for that person, and are never fixed."""
        progress = self._progress()
        known = {(f.round, f.id) for f in progress.pinned_findings}
        kept = [
            f
            for f in critic.findings
            if f.task in self.pinned and (f.round, f.id) not in known
        ]
        if not kept:
            return
        self._set(progress.with_pinned(kept))
        for task in self.project.playbook.tasks:
            count = sum(1 for f in kept if f.task == task.id)
            if count:
                self._notice(
                    f"{task.id}: {_count(count, 'review finding')} kept for the "
                    f"summary: a person edited {task.output}",
                    task=task.id,
                )

    def _fixes(self, critic: CriticRecord) -> bool:
        """Fix each document with findings, in task order. False when the
        review has to stop here."""
        for task in self.project.playbook.tasks:
            findings = [f for f in critic.findings if f.task == task.id]
            progress = self._progress()
            if (
                not findings
                or task.id in self.pinned
                or (task.id in progress.fixed and task.id not in progress.repairs)
            ):
                continue
            if task.id in progress.repairs:
                request = self._fix_request(task, findings, critic.round)
                if request is None:
                    self._set(progress.with_repaired(task.id))
                else:
                    self._repair(task, request)
                continue
            reason = self._room(FIX_USD, 1)
            if reason is not None:
                self._notice(f"the review stopped in round {critic.round}: {reason}")
                return False
            request = self._fix_request(task, findings, critic.round)
            if request is None:
                self._set(progress.with_fixed(task.id))
            else:
                self._fix(task, request)
        return True

    def _fix_request(
        self, task: Task, findings: Sequence[ReviewFinding], round_: int
    ) -> SessionRequest | None:
        entry = self.state.tasks.get(task.id)
        content = read_work(self.root, task.output)
        if entry is None or entry.output is None or content is None:
            self.report(
                ev.Notice(
                    f"{task.id}: its document is missing, so it was not fixed",
                    task=task.id,
                )
            )
            return None
        brief = self.project.brief
        files = [self._brief()]
        staged = [StagedFile(BRIEF_FILE, None)]
        for file in match_inputs(task.inputs, self._inputs()):
            dest = f"{INPUTS_DIR}/{file.name}"
            files.append(Staged(dest, source=Path(os.path.realpath(file.path))))
            staged.append(StagedFile(dest, file.heading))
        research = self._research(task)
        view = None if research is None else _findings_view(research)
        if view is not None:
            files.append(Staged(RESEARCH_SOURCES, text=sources_file(view)))
            staged.append(StagedFile(RESEARCH_SOURCES, None))
        body = self._task_body(task, staged, view)
        validators = task.validators
        research_sources = () if research is None else research.result.sources

        def check(record: Record, content: bytes | None) -> list[ValidatorResult]:
            assert isinstance(record, DraftResult)
            text = (content or b"").decode("utf-8", errors="replace")
            return run_validators(
                text, validators, result=record, sources=research_sources
            )

        return SessionRequest(
            kind=SessionKind.FIX,
            task=task.id,
            prompt=fix_prompt(
                task,
                findings,
                files=staged,
                has_research=task.research is not None,
                body=body,
            ),
            system_append=writer_system_prompt("fix", self.project.playbook, brief),
            model=self._model(task),
            key=entry.output.key,
            estimate_usd=FIX_USD,
            output=task.output,
            files=tuple(files),
            seed=content,
            check=check,
            round=round_,
            findings={finding.id: finding.instruction for finding in findings},
            keep_s=MIN_SESSION_S,
        )

    def _task_body(
        self,
        task: Task,
        staged: Sequence[StagedFile],
        research: ResearchFindings | None,
    ) -> str | None:
        """The task body, rendered as for its draft: the instructions a fix
        must stay consistent with. None, with a notice, if it fails."""
        deps = []
        for dep_id in task.depends_on:
            dep = self.project.playbook.task(dep_id)
            # A person's edits may contradict the engine's key facts.
            record = None if dep_id in self.pinned else read_draft(self.root, dep_id)
            facts = () if record is None else tuple(record.result.key_facts)
            deps.append(DependencyFacts(dep, facts))
        try:
            return render_task_body(
                self.project.playbook,
                task,
                task_context(
                    project_name=self.project.config.project.name,
                    brief=self.project.brief,
                    task=task,
                    files=staged,
                    deps=deps,
                    research=research,
                ),
            )
        except Exception as exc:
            self.report(
                ev.Notice(
                    f"{task.id}: the task template failed ({exc}); the fix is not "
                    "given the task's instructions",
                    task=task.id,
                )
            )
            return None

    def _fix(self, task: Task, request: SessionRequest) -> None:
        outcome = self.sessions.run(request)
        if not outcome.ok:
            self.report(
                ev.Notice(
                    f"{task.id}: the fix failed ({outcome.error}); the document "
                    "stays as it was",
                    task=task.id,
                )
            )
            self._set(self._progress().with_fixed(task.id))
            return
        repair = can_repair(outcome.validators)
        self._keep(
            task, outcome, "fix", self._progress().with_fixed(task.id, repair=repair)
        )
        if repair:
            self._repair(task, request)

    def _repair(self, task: Task, fix: SessionRequest) -> None:
        """One repair of a fixed document whose validators fail, if there is
        room for it before the summary; otherwise the fix stands as it is."""
        entry = self.state.tasks[task.id]
        failed = failures(entry.validators)
        content = read_work(self.root, task.output)
        if not failed or content is None:
            self._set(self._progress().with_repaired(task.id))
            return
        reason = self._room(REPAIR_USD, 1)
        if reason is not None:
            self._notice(
                f"{task.id}: the repair after the fix was skipped: {reason}",
                task=task.id,
            )
            self._set(self._progress().with_repaired(task.id))
            return
        repaired = self.sessions.run(
            SessionRequest(
                kind=SessionKind.REPAIR,
                task=task.id,
                prompt=repair_prompt(task, fix.prompt, failed),
                system_append=writer_system_prompt(
                    "repair", self.project.playbook, self.project.brief
                ),
                model=fix.model,
                key=fix.key,
                estimate_usd=REPAIR_USD,
                output=task.output,
                files=fix.files,
                seed=content,
                check=fix.check,
                round=fix.round,
                keep_s=MIN_SESSION_S,
            )
        )
        if repaired.ok:
            self._keep(
                task, repaired, "repair", self._progress().with_repaired(task.id)
            )
            return
        self.report(
            ev.Notice(
                f"{task.id}: the repair after the fix failed ({repaired.error}); "
                "keeping the fix",
                task=task.id,
            )
        )
        self._set(self._progress().with_repaired(task.id))

    def _keep(
        self,
        task: Task,
        outcome: SessionOutcome,
        kind: Literal["fix", "repair"],
        progress: ReviewProgress,
    ) -> None:
        """Keep a fixed (or repaired) document. Its key does not change.

        ``progress`` marks the fix (or repair) done. It is saved, with the
        new document's hash, before the document is written: a run that
        stops in between never does the fix again (``_resume_write``).
        """
        record = outcome.record
        assert isinstance(record, DraftResult)
        assert outcome.content is not None
        result = record
        if isinstance(record, FixResult):
            result = DraftResult.model_validate(
                record.model_dump(exclude={"applied", "rejected"})
            )
        pending = PendingWrite(
            task=task.id,
            kind=kind,
            hash=hash_bytes(outcome.content),
            validators=list(outcome.validators),
            result=result,
        )
        self._set(progress.model_copy(update={"writing": pending}))
        write_work(self.root, task.output, outcome.content)
        self._recorded(pending)

    def _recorded(self, pending: PendingWrite) -> None:
        """Record a document written into ``work/``, and clear the write."""
        entry = self.state.tasks[pending.task]
        assert entry.output is not None
        write_record(
            self.root,
            draft_record_name(pending.task),
            DraftRecord(
                task=pending.task,
                key=entry.output.key,
                run_id=self.run_id,
                kind=pending.kind,
                result=pending.result,
            ),
        )
        changed = entry.output.hash != pending.hash
        entry = self.state.record_fix(
            pending.task, output_hash=pending.hash, validators=pending.validators
        )
        progress = self._progress()
        self._set(
            progress.model_copy(
                update={"writing": None, "changed": progress.changed or changed}
            )
        )
        reason = failure_summary(pending.validators)
        self.report(
            ev.TaskFinished(
                pending.task, entry.status, reason, tuple(pending.validators)
            )
        )

    def _resume_write(self) -> None:
        """Finish recording a fixed document that a stopped run wrote, or drop
        it if the run stopped before writing it. Either way it is not fixed
        again."""
        pending = self._progress().writing
        if pending is None:
            return
        try:
            task = self.project.playbook.task(pending.task)
        except KeyError:
            task = None
        entry = self.state.tasks.get(pending.task)
        if (
            task is not None
            and entry is not None
            and entry.output is not None
            and file_hash(work_path(self.root, task.output)) == pending.hash
        ):
            self._recorded(pending)
            return
        progress = self._progress().with_repaired(pending.task)
        self._set(progress.model_copy(update={"writing": None}))
        self.report(
            ev.Notice(
                f"{pending.task}: the last run stopped before it saved the "
                f"{pending.kind}, so the document stays as it was",
                task=pending.task,
            )
        )

    # The summary.

    def _summary(self) -> None:
        result, error = self._summary_session()
        if error is not None:
            self.report(
                ev.Notice(
                    f"the summary failed: {error}",
                    "The pack is rendered without it; the next `spadework run` "
                    "writes it again.",
                )
            )
        self.summary_error = error
        self._save_summary(result, error=error)
        self._set(self._progress().model_copy(update={"summarised": True}))

    def summary_after_stop(self, *, session: bool) -> None:
        """The summary of a run that stopped before it finished.

        With ``session``, the summary session runs on its reserve. Without
        it, or if it does not finish, ``00-summary.md`` has only what code
        writes, with a note. Either way the summary stays owed: the next run
        that finishes writes it again.
        """
        progress = self.state.review
        if progress is not None and progress.summarised:
            return  # this review's summary is written already
        result = None
        if session:
            try:
                result, _ = self._summary_session()
            except RunStopped as stop:
                self._notice(f"the summary did not run: {stop.message}")
        self._save_summary(result, error=None, pending=True)

    def _summary_session(self) -> tuple[SummaryResult | None, str | None]:
        """Run the summary session: its result, or why it failed."""
        playbook = self.project.playbook
        brief = self.project.brief
        documents = self._documents()
        files, staged = self._pack_files(documents)
        outcome = self.sessions.run(
            SessionRequest(
                kind=SessionKind.SUMMARY,
                task=None,
                prompt=summary_prompt(
                    title=brief.front.title, documents=documents, files=staged
                ),
                system_append=writer_system_prompt("summary", playbook, brief),
                model=self.models.review,
                key=None,
                estimate_usd=SUMMARY_USD,
                output=SUMMARY_FILE,
                files=tuple(files),
                use_reserve=True,
            )
        )
        if isinstance(outcome.record, SummaryResult) and outcome.content is not None:
            write_work(self.root, SUMMARY_FILE, outcome.content)
            return outcome.record, None
        return None, outcome.error or "the summary session failed"

    def _save_summary(
        self,
        result: SummaryResult | None,
        *,
        error: str | None,
        pending: bool = False,
    ) -> None:
        progress = self.state.review
        write_record(
            self.root,
            SUMMARY_RECORD,
            SummaryRecord(
                run_id=self.run_id,
                review=None if progress is None else progress.run_id,
                result=result,
                error=error,
                pending=pending,
                pinned_findings=[] if progress is None else progress.pinned_findings,
                produced=self._produced(),
            ),
        )

    def _adopt_summary(self) -> bool:
        """Whether this review's summary was saved by a run that stopped
        before it noted so; if it was, it is not written again."""
        record = read_summary(self.root)
        if record is None or record.pending or record.review != self._progress().run_id:
            return False
        if record.result is not None and summary_prose(self.root) is None:
            return False
        self.summary_error = record.error
        self._set(self._progress().model_copy(update={"summarised": True}))
        return True

    def _produced(self) -> Produced:
        from spadework.engine.status import run_manifests

        total, runs = 0.0, 0
        for path in run_manifests(self.root):
            try:
                total += load_manifest(path).totals.cost_usd
            except SpadeworkError:
                continue  # unreadable: status warns about it
            runs += 1
        executor = self.manifest.executor
        playbook = self.manifest.playbook
        return Produced(
            date=dt.datetime.now(dt.UTC).date().isoformat(),
            spadework=__version__,
            playbook=f"{playbook.name}@{playbook.version}",
            executor=executor.name
            if executor.version is None
            else f"{executor.name}@{executor.version}",
            cost_usd=round(total, 6),
            runs=runs,
            run_cost_usd=self.manifest.totals.cost_usd,
            simulated=is_simulated(executor.name),
        )

    # What the critic and the summary are given.

    def _documents(self) -> list[Document]:
        return load_documents(
            self.root,
            self.project.playbook,
            self.state,
            self.final_states(),
            self.pinned,
        )

    def _brief(self) -> Staged:
        # A link inside the project is staged by what it points to; check
        # refuses a brief.md that points outside it.
        return Staged(BRIEF_FILE, source=Path(os.path.realpath(self.root / BRIEF_FILE)))

    def _pack_files(
        self, documents: Sequence[Document]
    ) -> tuple[list[Staged], list[StagedFile]]:
        """The brief, every input, every document, and the records digest."""
        files = [self._brief()]
        staged = [StagedFile(BRIEF_FILE, None)]
        for file in self._inputs():
            dest = f"{INPUTS_DIR}/{file.name}"
            files.append(Staged(dest, source=Path(os.path.realpath(file.path))))
            staged.append(StagedFile(dest, file.heading))
        for document in documents:
            if document.path is not None:
                files.append(Staged(document.output, source=document.path))
                staged.append(StagedFile(document.output, None))
        files.append(Staged(RECORDS_DIGEST, text=records_digest(documents)))
        staged.append(StagedFile(RECORDS_DIGEST, None))
        return files, staged

    def _inputs(self) -> tuple[InputFile, ...]:
        """inputs/ as it is now: files may be added or removed during a run."""
        try:
            return tuple(scan_inputs(self.root / INPUTS_DIR, self.root))
        except (SpadeworkError, OSError):
            # inputs/ holds something check would refuse: the files known at
            # the start that are still there.
            return tuple(file for file in self.project.inputs if file.path.is_file())

    def _research(self, task: Task) -> ResearchRecord | None:
        entry = self.state.research.get(task.id)
        if task.research is None or entry is None:
            return None
        return read_research(self.root, entry.record)

    def _model(self, task: Task) -> str:
        return self.models.draft if task.tier == "draft" else self.models.review


def _findings_view(research: ResearchRecord) -> ResearchFindings:
    return ResearchFindings(
        notes=research.result.notes,
        sources=tuple(
            SourceView(
                **source.model_dump(),
                status=research.status(source.id),
                detail=research.result_of(source.id).detail,
            )
            for source in research.result.sources
        ),
        unanswered=tuple(research.result.unanswered),
    )


def _validator_findings(
    documents: Sequence[Document], pinned: frozenset[str], round_: int
) -> list[ReviewFinding]:
    """A finding for each failing validator a fix could make pass.

    Documents a person edited are left out: their checks ran on the
    engine's version. So are failures no session can fix, such as
    ``min_sources`` when the research found too few sources.
    """
    found = []
    for document in documents:
        entry = document.entry
        if (
            document.task.id in pinned
            or document.path is None
            or entry is None
            or entry.output is None
        ):
            continue
        for result in entry.validators:
            if result.ok or not result.repairable:
                continue
            found.append(
                ReviewFinding(
                    id="F0",
                    task=document.task.id,
                    target=document.output,
                    kind=VALIDATOR_KINDS.get(result.name, "other"),
                    severity="high",
                    instruction=result.message or f"The check {result.name} fails.",
                    origin="validator",
                    round=round_,
                )
            )
    return found


def _findings(
    critic: CriticResult,
    documents: Sequence[Document],
    failing: Sequence[ReviewFinding],
    round_: int,
) -> tuple[list[ReviewFinding], list[str]]:
    """The critic's findings on documents of the pack, then the validators'.

    A finding naming a file that is not a document of the pack is dropped.
    """
    by_name = {document.output.casefold(): document for document in documents}
    kept: list[ReviewFinding] = []
    dropped: list[str] = []
    for finding in critic.findings:
        name = finding.target.strip().removeprefix("./").removeprefix("out/")
        document = by_name.get(name.casefold())
        if document is None:
            dropped.append(
                f"finding {finding.id}: {_short(finding.target)!r} is not a document "
                "of the pack"
            )
            continue
        if document.path is None:
            dropped.append(
                f"finding {finding.id}: {document.output} has no document to fix"
            )
            continue
        kept.append(
            ReviewFinding(
                id=finding.id,
                task=document.task.id,
                target=document.output,
                kind=finding.kind,
                severity=finding.severity,
                instruction=finding.instruction,
                origin="critic",
                round=round_,
            )
        )
    last = max((int(f.id[1:]) for f in critic.findings), default=0)
    for number, failure in enumerate(failing, start=last + 1):
        kept.append(failure.model_copy(update={"id": f"F{number}"}))
    return kept, dropped


def _short(text: str, limit: int = 60) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _count(number: int, word: str) -> str:
    return f"{number} {word}" if number == 1 else f"{number} {word}s"
