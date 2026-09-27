"""Running one session: its staging, guard policy, limits, and retries.

See docs/spec/04-engine.md (Session kinds, Budgets and limits) and
docs/spec/07-security.md. For each attempt, ``SessionRunner.run``:

1. waits for a rate limit to reset, or stops the run (``--wait-on-rate-limit``)
2. checks the run's time and budget, and works out the session's timeout and
   cap: ``min(session_timeout, time left - 30 s)`` and
   ``min(1.5 * estimate, budget left)``, where the budget left keeps the
   summary's reserve back
3. stages a fresh directory with read-only copies of the session's files,
   and writes its guard policy into ``.spadework/control/``
4. runs the session through the executor, and collects its one output
5. validates the structured result against the record schema, then records
   the session in the manifest

A transient error is retried twice, after 10 s and then 30 s. A result that
does not match its schema, or a session that did not write its output, is
retried once, with the problem added to the prompt. Anything else that stops
a session either fails the task (it is retried by the next run) or stops the
run (``RunStopped``), which leaves state and manifest valid for a resume.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, Protocol

from pydantic import ValidationError

from spadework.durations import format_duration
from spadework.engine import events as ev
from spadework.engine.prompts import retry_prompt
from spadework.executors.base import (
    MIN_BUDGET_USD,
    Denied,
    Event,
    Executor,
    Notice,
    SessionKind,
    SessionResult,
    SessionSpec,
    ToolCall,
)
from spadework.manifest import Outcome, RunManifest, SessionEntry, session_log
from spadework.manifest import Usage as ManifestUsage
from spadework.records import Record, output_schema, validate_result
from spadework.sandbox.staging import Staging, StagingError
from spadework.sandbox.tools import TOOLS_BY_KIND
from spadework.validators import ValidatorResult

TRANSIENT_WAITS_S = (10.0, 30.0)
"""How long to wait before each retry of a transient error."""
SCHEMA_RETRIES = 1
CAP_FACTOR = 1.5
"""A session's cap is this times its estimate, or less."""
TIME_MARGIN_S = 30
"""Kept back from the run's time left when setting a session's timeout."""
MIN_SESSION_S = 60
"""A session is not started with a shorter timeout than this."""
RATE_LIMIT_MARGIN_S = 60
"""``--wait-on-rate-limit`` waits this long past the reset."""
RATE_LIMIT_BACKOFF_S = 300
"""``--wait-on-rate-limit`` waits at least this long: a reset time that is not
known (Claude Code with an API key reports only an HTTP 429), already past, or
nearer than this counts as unknown."""
RATE_LIMIT_WAITS = 6
"""A session is tried again after at most this many rate-limit waits."""
_EPSILON = 1e-9  # float noise in sums of dollars


class Clock(Protocol):
    """Time, as the engine sees it. Tests pass one that never really waits."""

    def monotonic(self) -> float: ...

    def time(self) -> float:
        """Seconds since the epoch."""
        ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class RunStopped(Exception):
    """The run stops here, cleanly: state and manifest stay valid."""

    def __init__(
        self,
        outcome: Outcome,
        message: str,
        hint: str,
        *,
        reset: dt.datetime | None = None,
    ) -> None:
        super().__init__(message)
        self.outcome: Outcome = outcome
        self.message = message
        self.hint = hint
        self.reset = reset


RESUME = "Everything done so far is saved; `spadework run` continues from here."


@dataclass
class RunLimits:
    """The run's money and time, and what it has spent of them."""

    budget_usd: float
    reserve_usd: float
    """Kept back from the start for the summary (docs/spec/04-engine.md)."""
    max_time_s: int
    session_timeout_s: int
    started: float
    """The clock's ``monotonic`` time when the run started."""
    spent_usd: float = 0.0

    @property
    def deadline(self) -> float:
        return self.started + self.max_time_s

    def budget_left(self) -> float:
        """What sessions other than the summary may still spend."""
        return self.budget_usd - self.spent_usd - self.reserve_usd


@dataclass(frozen=True)
class Staged:
    """A file for the staging directory: a copy of ``source``, or ``text``."""

    dest: str
    source: Path | None = None
    text: str | None = None


Check = Callable[[Record, bytes | None], list[ValidatorResult]]


@dataclass(frozen=True)
class SessionRequest:
    kind: SessionKind
    task: str
    prompt: str
    system_append: str
    model: str
    key: str
    """For the manifest: the research key, or the draft key."""
    estimate_usd: float
    output: str | None = None
    files: tuple[Staged, ...] = ()
    seed: bytes | None = None
    """The output's starting content, as for a repair."""
    max_searches: int | None = None
    max_fetches: int | None = None
    check: Check | None = None
    """Runs the validators on an accepted result, for the manifest."""


@dataclass(frozen=True)
class SessionOutcome:
    """A session that succeeded, or why the task failed."""

    n: int | None
    record: Record | None = None
    content: bytes | None = None
    validators: tuple[ValidatorResult, ...] = ()
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.record is not None


class SessionRunner:
    """Runs a run's sessions one at a time, and records each in the manifest."""

    def __init__(
        self,
        *,
        root: Path,
        run_dir: Path,
        manifest: RunManifest,
        save_manifest: Callable[[], None],
        executor: Executor,
        limits: RunLimits,
        clock: Clock,
        reporter: ev.Reporter,
        wait_on_rate_limit: bool = False,
        transient_waits: Sequence[float] = TRANSIENT_WAITS_S,
        temp_root: Path | None = None,
    ) -> None:
        self.root = root
        self.run_dir = run_dir
        self.manifest = manifest
        self.save_manifest = save_manifest
        self.executor = executor
        self.limits = limits
        self.clock = clock
        self.report = reporter
        self.wait_on_rate_limit = wait_on_rate_limit
        self.transient_waits = tuple(transient_waits)
        self.temp_root = temp_root
        self._rate_limited = False
        self._reset: dt.datetime | None = None

    def run(self, request: SessionRequest) -> SessionOutcome:
        """Run ``request`` until it succeeds, fails the task, or stops the run."""
        transient = 0
        schema = 0
        limited = 0
        prompt = request.prompt
        while True:
            retries = _Retries(
                schema=schema < SCHEMA_RETRIES,
                transient=transient < len(self.transient_waits),
                rate_limit=self.wait_on_rate_limit and limited < RATE_LIMIT_WAITS,
            )
            ran = self._attempt(
                replace(request, prompt=prompt), transient + schema + 1, retries
            )
            verdict = ran.verdict
            if verdict.accepted:
                return SessionOutcome(ran.n, ran.record, ran.content, ran.validators)
            if verdict.retry == "schema" and retries.schema:
                schema += 1
                prompt = retry_prompt(request.prompt, verdict.problem)
            elif verdict.retry == "transient" and retries.transient:
                self.clock.sleep(self.transient_waits[transient])
                transient += 1
            elif verdict.retry == "rate_limited":
                limited += 1
                if limited > RATE_LIMIT_WAITS:
                    raise RunStopped(
                        "rate_limited",
                        f"the usage limit was still reached after {RATE_LIMIT_WAITS} "
                        "waits",
                        RESUME,
                        reset=self._reset,
                    )
                # The next attempt waits for the reset, or stops the run.
            else:
                tries = transient + schema + 1
                error = verdict.problem
                if tries > 1:
                    error += f" (after {tries} attempts)"
                return SessionOutcome(ran.n, error=error)

    # One attempt.

    def _attempt(
        self, request: SessionRequest, attempt: int, retries: _Retries
    ) -> _Ran:
        self._wait_for_rate_limit()
        timeout, time_limited = self._timeout()
        cap, cap_limited = self._cap(request)
        n = self.manifest.next_session()
        kind = request.kind
        session_id = f"{n:03d}-{request.task}-{kind.value}"
        log = session_log(n, kind.value, request.task)
        try:
            staging = self._stage(request, session_id, timeout)
        except StagingError as exc:
            # Nothing ran, so there is no session to record.
            return _Ran(n, _Verdict(problem=f"cannot stage the session: {exc}"))
        try:
            spec = SessionSpec(
                session_id=session_id,
                kind=kind,
                prompt=request.prompt,
                system_append=request.system_append,
                model=request.model,
                tools=TOOLS_BY_KIND[kind.value],
                workdir=staging.paths.workdir,
                policy_path=staging.paths.policy,
                output_schema=output_schema(kind.value),
                max_budget_usd=cap,
                timeout_s=timeout,
                log_path=self.run_dir / log,
                output=None if request.output is None else staging.output_path,
                task=request.task,
            )
            self.report(
                ev.SessionStarted(
                    n=n,
                    task=request.task,
                    kind=kind.value,
                    model=request.model,
                    round=0,
                    attempt=attempt,
                    max_budget_usd=cap,
                    timeout_s=timeout,
                )
            )
            started = self.clock.monotonic()
            try:
                result = self.executor.run(spec, self._sink(n, request))
            except BaseException as exc:
                # The session may have spent money: record it before the
                # run ends, so the manifest and the spend stay whole.
                took = self.clock.monotonic() - started
                self._record_cut_short(request, n, log, cap, took, exc)
                raise
            collected = staging.collect()
            denials = staging.denials()
            counters = staging.counters()
        finally:
            staging.cleanup()

        charged = result.cost_usd if result.cost_known else cap
        self.limits.spent_usd += charged
        denied = max(result.guard_denials, len(denials))
        if denials:
            self._keep_denials(log, denials)
        if collected.extras:
            self.report(
                ev.Notice(
                    f"{request.task} {kind.value}: ignored "
                    f"{_files(len(collected.extras))} the session wrote besides its "
                    f"output: {', '.join(collected.extras[:5])}",
                    task=request.task,
                )
            )

        record: Record | None = None
        validators: list[ValidatorResult] = []
        problem: str | None = None
        if result.status == "ok":
            record, problem = self._accept(request, result, collected.content)
            if record is not None and request.check is not None:
                validators = request.check(record, collected.content)
            if result.rate_limit_reset is not None:
                self._rate_limited, self._reset = True, result.rate_limit_reset
        status = "error" if problem is not None else result.status
        error = problem if problem is not None else result.error

        tool_calls = dict(result.tool_calls) or {
            name: count for name, count in counters.items() if name != "total"
        }
        entry = SessionEntry(
            n=n,
            task=request.task,
            kind=kind.value,
            round=0,
            model=request.model,
            key=request.key,
            status=status,
            cost_usd=round(float(charged), 6),
            duration_s=round(float(result.duration_s), 3),
            usage=ManifestUsage(
                input_tokens=result.usage.input_tokens,
                output_tokens=result.usage.output_tokens,
                cache_read_tokens=result.usage.cache_read_tokens,
                cache_creation_tokens=result.usage.cache_creation_tokens,
            ),
            tool_calls=tool_calls,
            guard_denials=denied,
            validators=validators,
            log=log,
            error=error,
        )
        self.manifest.add_session(entry)
        self.save_manifest()

        verdict = self._decide(
            request,
            result,
            problem,
            cap,
            cap_limited=cap_limited,
            time_limited=time_limited,
        )
        retrying = (
            (verdict.retry == "rate_limited" and retries.rate_limit)
            or (verdict.retry == "schema" and retries.schema)
            or (verdict.retry == "transient" and retries.transient)
        )
        self.report(
            ev.SessionFinished(
                n=n,
                task=request.task,
                kind=kind.value,
                status=status,
                cost_usd=entry.cost_usd,
                duration_s=entry.duration_s,
                guard_denials=denied,
                spent_usd=round(self.limits.spent_usd, 6),
                error=error,
                retry=retrying,
            )
        )
        if verdict.stop is not None:
            raise verdict.stop
        return _Ran(n, verdict, record, collected.content, tuple(validators))

    def _stage(self, request: SessionRequest, session_id: str, timeout: int) -> Staging:
        staging = Staging.create(
            self.root,
            session_id,
            request.kind.value,
            output=request.output,
            temp_root=self.temp_root,
        )
        try:
            for item in request.files:
                if item.source is not None:
                    staging.add_file(item.source, item.dest)
                else:
                    staging.add_text(item.dest, item.text or "")
            if request.seed is not None:
                staging.seed_output(request.seed)
            staging.write_policy(
                deadline_epoch=self.clock.time() + timeout,
                max_searches=request.max_searches,
                max_fetches=request.max_fetches,
            )
        except BaseException:
            staging.cleanup()
            raise
        return staging

    def _sink(self, n: int, request: SessionRequest) -> Callable[[Event], None]:
        kind = request.kind

        def sink(event: Event) -> None:
            if isinstance(event, ToolCall):
                self.report(
                    ev.ToolUsed(n, request.task, kind.value, event.name, event.target)
                )
            elif isinstance(event, Denied):
                self.report(
                    ev.ToolDenied(
                        n,
                        request.task,
                        kind.value,
                        event.name,
                        event.target,
                        event.reason,
                        possible_injection=kind is SessionKind.RESEARCH,
                    )
                )
            elif isinstance(event, Notice):
                self.report(ev.Notice(event.message, task=request.task))

        return sink

    def _record_cut_short(
        self,
        request: SessionRequest,
        n: int,
        log: str,
        cap: float,
        seconds: float,
        exc: BaseException,
    ) -> None:
        """Record a session that an exception ended, at its full cap."""
        interrupted = isinstance(exc, KeyboardInterrupt)
        self.limits.spent_usd += cap
        self.manifest.add_session(
            SessionEntry(
                n=n,
                task=request.task,
                kind=request.kind.value,
                round=0,
                model=request.model,
                key=request.key,
                status="interrupted" if interrupted else "error",
                cost_usd=round(float(cap), 6),
                duration_s=round(max(seconds, 0.0), 3),
                log=log,
                error="interrupted"
                if interrupted
                else f"stopped by an error: {type(exc).__name__}: {exc}",
            )
        )
        self.save_manifest()

    def _accept(
        self, request: SessionRequest, result: SessionResult, content: bytes | None
    ) -> tuple[Record | None, str | None]:
        """The validated record, or what is wrong with the session's work."""
        if result.structured is None:
            return None, "the session ended without a structured result"
        try:
            record = validate_result(request.kind.value, result.structured)
        except ValidationError as exc:
            return None, f"its result does not match the schema: {_first_error(exc)}"
        if request.output is not None and content is None:
            return None, f"it did not write {request.output}"
        return record, None

    def _decide(
        self,
        request: SessionRequest,
        result: SessionResult,
        problem: str | None,
        cap: float,
        *,
        cap_limited: bool,
        time_limited: bool,
    ) -> _Verdict:
        what = f"{request.task} {request.kind.value}"
        if result.status == "ok":
            if problem is None:
                return _Verdict(accepted=True)
            return _Verdict(retry="schema", problem=problem)
        error = result.error or "the session failed"
        match result.status:
            case "error":
                if result.error_kind in ("schema", "transient"):
                    return _Verdict(retry=result.error_kind, problem=error)
                hint = "Run `spadework doctor` to check Claude Code, then run again."
                if result.error_kind == "sandbox":
                    hint = (
                        "The sandbox could not be confirmed, so no further session "
                        "was started. Run `spadework doctor --canary`."
                    )
                return _Verdict(stop=RunStopped("error", f"{what}: {error}", hint))
            case "timeout":
                if time_limited:
                    return _Verdict(stop=self._out_of_time())
                limit = format_duration(self.limits.session_timeout_s)
                return _Verdict(
                    problem=f"the session did not finish within session_timeout "
                    f"({limit})"
                )
            case "budget":
                if cap_limited:
                    return _Verdict(
                        stop=RunStopped(
                            "budget",
                            f"{what} used what was left of the run budget "
                            f"(${self.limits.budget_usd:.2f})",
                            f"{RESUME} Raise --budget-usd or limits.budget_usd to go "
                            "further.",
                        )
                    )
                return _Verdict(
                    problem=f"the session reached its cap of ${cap:.2f}; raise the "
                    "task's budget_usd if it needs more"
                )
            case "rate_limited":
                self._rate_limited, self._reset = True, result.rate_limit_reset
                return _Verdict(retry="rate_limited", problem=error)
            case "interrupted":
                return _Verdict(stop=RunStopped("interrupted", "interrupted", RESUME))
        return _Verdict(problem=error)  # pragma: no cover - every status is handled

    # Limits.

    def _timeout(self) -> tuple[int, bool]:
        """The next session's timeout, and whether the run's time limited it."""
        left = self.limits.deadline - self.clock.monotonic()
        room = int(left - TIME_MARGIN_S)
        if room < MIN_SESSION_S:
            raise self._out_of_time()
        timeout = min(self.limits.session_timeout_s, room)
        return timeout, timeout < self.limits.session_timeout_s

    def _out_of_time(self) -> RunStopped:
        limit = format_duration(self.limits.max_time_s)
        return RunStopped(
            "time",
            f"the run reached its time limit ({limit})",
            f"{RESUME} Pass a longer --max-time if needed.",
        )

    def _cap(self, request: SessionRequest) -> tuple[float, bool]:
        """The session's budget cap, and whether the run's budget limited it."""
        left = self.limits.budget_left()
        estimate = request.estimate_usd
        # That is, the budget cannot cover spent + estimate + the reserve.
        if estimate > left + _EPSILON:
            raise RunStopped(
                "budget",
                f"the run budget (${self.limits.budget_usd:.2f}) cannot cover the "
                f"next session: {request.task} {request.kind.value}, estimated at "
                f"${estimate:.2f}, with ${self.limits.reserve_usd:.2f} kept for the "
                "summary",
                f"{RESUME} Raise --budget-usd or limits.budget_usd to go further.",
            )
        cap = min(CAP_FACTOR * estimate, left)
        return max(cap, MIN_BUDGET_USD), left < CAP_FACTOR * estimate

    def _wait_for_rate_limit(self) -> None:
        if not self._rate_limited:
            return
        now = self.clock.time()
        reset = self._reset
        if reset is not None and reset.timestamp() <= now:
            reset = None  # a reset in the past says nothing about when to resume
        message = "the usage limit was reached"
        if not self.wait_on_rate_limit:
            if reset is None:
                hint = (
                    "Its reset time is not known. Everything done so far is saved: "
                    "run `spadework run` again later, or pass --wait-on-rate-limit "
                    f"to try again every {format_duration(RATE_LIMIT_BACKOFF_S)}."
                )
            else:
                hint = f"{RESUME} Or pass --wait-on-rate-limit to wait for the reset."
            raise RunStopped("rate_limited", message, hint, reset=reset)
        if reset is None or reset.timestamp() - now < RATE_LIMIT_BACKOFF_S:
            wait = float(RATE_LIMIT_BACKOFF_S)
            resume = dt.datetime.fromtimestamp(now + wait, dt.UTC)
        else:
            resume = reset + dt.timedelta(seconds=RATE_LIMIT_MARGIN_S)
            wait = resume.timestamp() - now
        # Waiting is worth it only if a session can still start afterwards.
        after = self.clock.monotonic() + wait + TIME_MARGIN_S + MIN_SESSION_S
        if after > self.limits.deadline:
            raise RunStopped(
                "rate_limited",
                f"{message}, and waiting for it would pass the run's time limit",
                RESUME,
                reset=reset,
            )
        self.report(
            ev.Notice(f"{message}; waiting until {local_time(resume)}", level="info")
        )
        self.clock.sleep(wait)
        self._rate_limited, self._reset = False, None

    def _keep_denials(self, log: str, denials: list[dict[str, object]]) -> None:
        """The guard's denial log goes with the session log (I11)."""
        path = self.run_dir / log.replace(".jsonl", ".denials.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as file:
            for record in denials:
                file.write(json.dumps(record, default=str) + "\n")


@dataclass(frozen=True)
class _Retries:
    """Whether another attempt of each kind is allowed."""

    schema: bool
    transient: bool
    rate_limit: bool


@dataclass(frozen=True)
class _Verdict:
    """What one attempt means: accepted, retried, failed, or the run stops."""

    accepted: bool = False
    retry: Literal["schema", "transient", "rate_limited"] | None = None
    problem: str = "the session failed"
    stop: RunStopped | None = None


@dataclass(frozen=True)
class _Ran:
    n: int
    verdict: _Verdict
    record: Record | None = None
    content: bytes | None = None
    validators: tuple[ValidatorResult, ...] = ()


def _first_error(exc: ValidationError) -> str:
    first = exc.errors(include_url=False)[0]
    where = ".".join(str(part) for part in first["loc"]) or "the result"
    return f"{where}: {first['msg']}"


def _files(count: int) -> str:
    return "1 file" if count == 1 else f"{count} files"


def local_time(moment: dt.datetime) -> str:
    """``moment`` in local time, as HH:MM."""
    return moment.astimezone().strftime("%H:%M")
