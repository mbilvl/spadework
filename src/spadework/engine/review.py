"""The review loop and the summary (docs/spec/04-engine.md).

They run when a run changed an output, or when an interrupted run left them
owed (``state.review``):

    for round in 1..rounds:
        stop if the budget or time left cannot cover a critic plus one fix
        critic → findings, plus a finding for each validator failure
        stop if the verdict is ship and no validator is failing
        for each document with findings, in task order:
            edited by a person → record the findings for the summary
            fix → validators → one repair if they fail
    summary

Each step is saved as it ends (``ReviewProgress``), so a resumed run picks
up where the last one stopped: a round whose critic ran uses its findings, a
finished fix is not done again, a repair still owed is done alone, and a
finished summary leaves only the render.

The review never stops the run for money: when the budget cannot cover the
next critic or fix, the review ends and the summary, whose estimate was kept
back from the start, still runs.
"""

from __future__ import annotations

import datetime as dt
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

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
    read_research,
    read_summary,
    read_work,
    write_record,
    write_work,
)
from spadework.errors import SpadeworkError
from spadework.executors.base import SessionKind
from spadework.inputs import INPUTS_DIR, InputFile, match_inputs, scan_inputs
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
from spadework.render import ResearchFindings, StagedFile
from spadework.render import Source as SourceView
from spadework.state import ReviewFinding, ReviewProgress, State
from spadework.validators import (
    ValidatorResult,
    can_repair,
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
        if not self._progress().reviewed:
            try:
                self._rounds()
            except RunStopped as stop:
                # A session that used what was left of the budget: the
                # summary's reserve is intact, so the pack still gets one.
                if stop.outcome != "budget":
                    raise
                self._notice(f"the review stopped: {stop.message}")
            self._set(self._progress().model_copy(update={"reviewed": True}))
        if not self._progress().summarised:
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
                self._set(
                    progress.model_copy(
                        update={
                            "round": round_,
                            "critic": None,
                            "fixed": [],
                            "repairs": [],
                        }
                    )
                )
            critic = self._critic_of(round_)
            if critic is None:
                return
            self._keep_pinned(critic)
            if critic.ship:
                self._notice(f"review round {round_}: the critic's verdict is ship")
                return
            if not self._fixes(critic):
                return
            round_ += 1

    def _room(self, estimate: float, sessions: int) -> str | None:
        """Why the review cannot go on: the money or time for ``sessions``
        more sessions, and the summary after them, is not there."""
        return self.limits.room(estimate, sessions + 1, self.clock.monotonic())

    def _critic_of(self, round_: int) -> CriticRecord | None:
        """The round's findings: from its critic record, or a new critic."""
        progress = self._progress()
        if progress.critic is not None:
            record = read_critic(self.root, progress.critic)
            if record is not None and record.round == round_:
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
            repair = task.id in progress.repairs
            reason = self._room(REPAIR_USD if repair else FIX_USD, 1)
            if reason is not None:
                self._notice(f"the review stopped in round {critic.round}: {reason}")
                return False
            request = self._fix_request(task, findings, critic.round)
            if request is None:
                self._set(self._progress().with_fixed(task.id))
            elif repair:
                self._repair(task, request)
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
        if research is not None:
            view = _findings_view(research)
            files.append(Staged(RESEARCH_SOURCES, text=sources_file(view)))
            staged.append(StagedFile(RESEARCH_SOURCES, None))
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
                task, findings, files=staged, has_research=task.research is not None
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
        )

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
        self._record(task, outcome, "fix")
        repair = can_repair(outcome.validators)
        self._set(self._progress().with_fixed(task.id, repair=repair))
        if repair:
            self._repair(task, request)

    def _repair(self, task: Task, fix: SessionRequest) -> None:
        """One repair of a fixed document whose validators fail."""
        entry = self.state.tasks[task.id]
        failed = failures(entry.validators)
        content = read_work(self.root, task.output)
        if failed and content is not None:
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
                )
            )
            if repaired.ok:
                self._record(task, repaired, "repair")
            else:
                self.report(
                    ev.Notice(
                        f"{task.id}: the repair after the fix failed "
                        f"({repaired.error}); keeping the fix",
                        task=task.id,
                    )
                )
        self._set(self._progress().with_repaired(task.id))

    def _record(self, task: Task, outcome: SessionOutcome, kind: str) -> None:
        """Keep a fixed (or repaired) document. Its key does not change."""
        record = outcome.record
        assert isinstance(record, DraftResult)
        assert outcome.content is not None
        entry = self.state.tasks[task.id]
        assert entry.output is not None
        result = record
        if isinstance(record, FixResult):
            result = DraftResult.model_validate(
                record.model_dump(exclude={"applied", "rejected"})
            )
        digest = write_work(self.root, task.output, outcome.content)
        write_record(
            self.root,
            draft_record_name(task.id),
            DraftRecord(
                task=task.id,
                key=entry.output.key,
                run_id=self.run_id,
                kind="repair" if kind == "repair" else "fix",
                result=result,
            ),
        )
        entry = self.state.record_fix(
            task.id, output_hash=digest, validators=outcome.validators
        )
        failed = [r.name for r in outcome.validators if not r.ok]
        reason = f"validators failed: {', '.join(failed)}" if failed else None
        self.report(
            ev.TaskFinished(task.id, entry.status, reason, tuple(outcome.validators))
        )

    # The summary.

    def _summary(self) -> None:
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
        result: SummaryResult | None = None
        error = None
        if isinstance(outcome.record, SummaryResult) and outcome.content is not None:
            write_work(self.root, SUMMARY_FILE, outcome.content)
            result = outcome.record
        else:
            error = outcome.error or "the summary session failed"
            self.report(
                ev.Notice(
                    f"the summary failed: {error}",
                    "The pack is rendered without it; the next `spadework run` "
                    "writes it again.",
                )
            )
        self.summary_error = error
        write_record(
            self.root,
            SUMMARY_RECORD,
            SummaryRecord(
                run_id=self.run_id,
                result=result,
                error=error,
                pinned_findings=self._progress().pinned_findings,
                produced=self._produced(),
            ),
        )
        self._set(self._progress().model_copy(update={"summarised": True}))

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
