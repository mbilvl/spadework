"""The ``fake`` executor: deterministic sessions for tests and demos.

See docs/spec/05-executors.md. No model is called and nothing is spent. By
default every session does something plausible for its kind: writing kinds
write a short Markdown document to their output, and every kind returns a
result that matches its schema, at a fixed small cost.

A script named by ``SPADEWORK_FAKE_SCRIPT`` (JSON, or TOML if the name ends
in ``.toml``) changes that per session. Its keys are ``<task>:<kind>``,
``<task>``, ``<kind>`` or ``*``, and the most specific entry that applies to
a session wins::

    ["stack:draft"]
    fail = "timeout"
    times = 1          # only the first matching session; later ones fall through

    [critic]
    verdict = "ship"

    [research]
    replay = "research.jsonl"   # a stream-json log, relative to the script
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tomllib
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import asdict
from pathlib import Path
from typing import IO, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from spadework import __version__
from spadework.errors import UsageError
from spadework.executors.base import (
    Denied,
    Diagnostic,
    Event,
    EventSink,
    Finished,
    RateLimit,
    SessionKind,
    SessionResult,
    SessionSpec,
    Text,
    ToolCall,
    Usage,
)

SCRIPT_ENV = "SPADEWORK_FAKE_SCRIPT"

COSTS: Mapping[SessionKind, float] = {
    SessionKind.RESEARCH: 0.05,
    SessionKind.DRAFT: 0.08,
    SessionKind.REPAIR: 0.04,
    SessionKind.CRITIC: 0.06,
    SessionKind.FIX: 0.04,
    SessionKind.SUMMARY: 0.05,
}
"""What each kind of session costs, unless a script says otherwise."""

DURATION_S = 1.0
USAGE = Usage(input_tokens=1200, output_tokens=300, cache_read_tokens=800)

Failure = Literal[
    "timeout",
    "interrupted",
    "rate_limit",
    "budget",
    "transient",
    "error",
    "sandbox",
    "no_result",
    "invalid_schema",
    "missing_output",
    "extra_file",
    "guard_denial",
]

DEFAULT_HEADINGS = ("Summary", "Findings", "Open questions")
SUMMARY_HEADINGS = ("Executive summary", "Key decisions", "Open questions")
EXTRA_FILE = "notes.md"

_SOURCES = (
    (
        "directory.example",
        "Example Directory",
        "Listed providers … most offer a free tier for small teams",
        "Most listed providers offer a free tier for small teams",
    ),
    (
        "benchmarks.example",
        "Sample Benchmarks",
        "Median set-up time was two days across the products tested",
        "Set-up takes about two days for the products tested",
    ),
    (
        "journal.example",
        "Placeholder Journal",
        "Buyers ranked ease of editing above price",
        "Buyers rank ease of editing above price",
    ),
)
_REQUIRED_HEADINGS = re.compile(r"^\s*required headings?:\s*(.+)$", re.I | re.M)
_BOLD_BULLET = re.compile(r"^[-*] \*\*(.+?)\*\*")
_SOURCE_ID = re.compile(r"(?<![A-Za-z0-9])S([1-9][0-9]?)(?![A-Za-z0-9])")
_FINDING_ID = re.compile(r"(?<![A-Za-z0-9])F([1-9][0-9]?)(?![A-Za-z0-9])")
_QUALIFIED_QUESTION = re.compile(r"\b([a-z][a-z0-9-]*:Q[1-9][0-9]*)\b")
_DOCUMENT = re.compile(r"\b[0-9]{2}-[a-z0-9-]+\.md\b")


class FakeBehaviour(BaseModel):
    """What the fake does for the sessions that one script entry matches."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    fail: Failure | None = None
    times: int | None = Field(default=None, ge=1)
    """Apply to this many matching sessions, then let later entries apply."""
    cost_usd: float | None = Field(default=None, ge=0)
    resets_at: dt.datetime | int | None = None
    """For ``rate_limit``: an ISO 8601 time or epoch seconds. Default: in an hour."""
    denials: int = Field(default=1, ge=1)
    """For ``guard_denial``: how many calls the guard denied."""
    verdict: Literal["continue", "ship"] | None = None
    """For critic sessions."""
    structured: dict[str, Any] | None = None
    """Return this structured result in place of the generated one."""
    replay: str | None = None
    """A recorded stream-json log to feed through the claude-cli parser."""
    guard_checks: bool = True
    """For ``replay``: set to false for logs recorded without the guard hooks."""


_SCRIPT = TypeAdapter(dict[str, FakeBehaviour])


def load_script(path: Path) -> dict[str, FakeBehaviour]:
    """Read a fake executor script. Relative ``replay`` paths become absolute."""
    hint = "Fix the script, or unset SPADEWORK_FAKE_SCRIPT."
    try:
        text = path.read_text(encoding="utf-8")
        data = tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)
        script = _SCRIPT.validate_python(data)
    except OSError as exc:
        raise UsageError(f"cannot read the fake executor script {path}", hint) from exc
    except (ValueError, ValidationError) as exc:
        first = str(exc).strip().splitlines()[0]
        raise UsageError(
            f"{path} is not a valid fake executor script: {first}", hint
        ) from exc
    return {
        key: behaviour
        if behaviour.replay is None
        else behaviour.model_copy(
            update={"replay": str(path.parent / behaviour.replay)}
        )
        for key, behaviour in script.items()
    }


class FakeExecutor:
    """A deterministic executor that never calls a model."""

    name = "fake"

    def __init__(self, script: Mapping[str, FakeBehaviour] | None = None) -> None:
        self.script = dict(script or {})
        self._used: Counter[str] = Counter()
        self._critic_rounds = 0

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> FakeExecutor:
        environ = os.environ if environ is None else environ
        path = environ.get(SCRIPT_ENV)
        return cls(load_script(Path(path)) if path else None)

    def version(self) -> str | None:
        return __version__

    def diagnose(self) -> list[Diagnostic]:
        return [
            Diagnostic(
                "Executor", True, "fake: no model is called and nothing is spent"
            )
        ]

    def run(self, spec: SessionSpec, events: EventSink) -> SessionResult:
        behaviour = self._behaviour(spec)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        with spec.log_path.open("w", encoding="utf-8") as log:
            if behaviour.replay is not None:
                result = self._replay(spec, behaviour, events, log)
            else:
                result = self._play(spec, behaviour, _Logged(events, log))
                _log(log, {"type": "result", **asdict(result)})
        events(Finished(result))
        return result

    def _behaviour(self, spec: SessionSpec) -> FakeBehaviour:
        keys = [f"{spec.task}:{spec.kind}", spec.task] if spec.task else []
        for key in [*keys, str(spec.kind), "*"]:
            behaviour = self.script.get(key)
            if behaviour is None:
                continue
            if behaviour.times is not None and self._used[key] >= behaviour.times:
                continue
            self._used[key] += 1
            return behaviour
        return FakeBehaviour()

    def _replay(
        self,
        spec: SessionSpec,
        behaviour: FakeBehaviour,
        events: EventSink,
        log: IO[str],
    ) -> SessionResult:
        from spadework.executors.claude_cli import expected_tools
        from spadework.executors.claude_stream import replay

        assert behaviour.replay is not None
        lines = Path(behaviour.replay).read_text(encoding="utf-8").splitlines()
        return replay(
            lines,
            expected_tools=expected_tools(spec),
            events=events,
            workdir=spec.workdir,
            log=log,
            require_guard=behaviour.guard_checks,
        )

    def _play(
        self, spec: SessionSpec, behaviour: FakeBehaviour, events: EventSink
    ) -> SessionResult:
        fail = behaviour.fail
        cost = COSTS[spec.kind] if behaviour.cost_usd is None else behaviour.cost_usd
        stopped = _stopped(spec, fail, events, behaviour)
        if stopped is not None:
            return stopped
        if fail == "budget" or cost > spec.max_budget_usd:
            return SessionResult(
                status="budget",
                cost_usd=max(cost, spec.max_budget_usd),
                duration_s=DURATION_S,
                usage=USAGE,
                error=f"reached the budget cap (${spec.max_budget_usd:.2f})",
            )

        tool_calls: Counter[str] = Counter()

        def call(name: str, target: str) -> None:
            tool_calls[name] += 1
            events(ToolCall(name, target))

        structured = self._structured(spec, behaviour, call)
        if spec.output is not None:
            if fail == "missing_output":
                spec.output.unlink(missing_ok=True)
            else:
                _write_output(spec, spec.output)
                call("Write", spec.output.name)
        if fail == "extra_file":
            (spec.workdir / EXTRA_FILE).write_text("scratch\n", encoding="utf-8")
            call("Write", EXTRA_FILE)
        denials = behaviour.denials if fail == "guard_denial" else 0
        for _ in range(denials):
            target = (
                "http://127.0.0.1/" if spec.kind is SessionKind.RESEARCH else EXTRA_FILE
            )
            name = "WebFetch" if spec.kind is SessionKind.RESEARCH else "Write"
            tool_calls[name] += 1
            events(Denied(name, target, "spadework guard: denied (fake)"))
        events(Text(f"Finished the {spec.kind} session (fake executor)."))

        result = SessionResult(
            status="ok",
            cost_usd=cost,
            duration_s=DURATION_S,
            usage=USAGE,
            tool_calls=dict(tool_calls),
            guard_denials=denials,
        )
        if fail == "no_result":
            result.status, result.error_kind = "error", "schema"
            result.error = "the session ended without a structured result"
            return result
        if fail == "invalid_schema":
            # Drop a required key and add an unknown one.
            structured = {"unexpected": True, **dict(list(structured.items())[1:])}
        if behaviour.structured is not None:
            structured = behaviour.structured
        result.structured = structured
        return result

    def _structured(
        self,
        spec: SessionSpec,
        behaviour: FakeBehaviour,
        call: Callable[[str, str], None],
    ) -> dict[str, Any]:
        kind = spec.kind
        if kind is SessionKind.RESEARCH:
            result = research_result(spec.task or "research")
            call("WebSearch", "placeholder search")
            for source in result["sources"]:
                call("WebFetch", source["url"])
            return result
        for name in _staged_files(spec.workdir)[:10]:
            call("Read", name)
        if kind is SessionKind.CRITIC:
            first = self._critic_rounds == 0
            self._critic_rounds += 1
            return critic_result(
                _documents(spec), behaviour.verdict or ("continue" if first else "ship")
            )
        if kind is SessionKind.SUMMARY:
            return summary_result(spec.prompt)
        result = draft_result(_title(spec.output))
        if kind is SessionKind.FIX:
            result["applied"] = _ids(_FINDING_ID, "F", spec.prompt)
            result["rejected"] = []
        return result


class _Logged:
    """An event sink that also writes each event to the session log."""

    def __init__(self, events: EventSink, log: IO[str]) -> None:
        self._events = events
        self._log = log

    def __call__(self, event: Event) -> None:
        _log(self._log, {"type": type(event).__name__, **asdict(event)})
        self._events(event)


def _log(log: IO[str], record: dict[str, Any]) -> None:
    log.write(json.dumps(record, default=str) + "\n")


def _stopped(
    spec: SessionSpec,
    fail: Failure | None,
    events: EventSink,
    behaviour: FakeBehaviour,
) -> SessionResult | None:
    """The result of a session that ends before doing anything."""
    if fail == "timeout":
        return SessionResult(
            status="timeout",
            duration_s=float(spec.timeout_s),
            error="the session did not finish within its time limit",
            cost_known=False,
        )
    if fail == "interrupted":
        return SessionResult(
            status="interrupted", error="interrupted", cost_known=False
        )
    if fail == "rate_limit":
        reset = _reset_time(behaviour.resets_at)
        events(RateLimit("rejected", reset))
        return SessionResult(
            status="rate_limited", rate_limit_reset=reset, error="rate limited"
        )
    if fail == "transient":
        return SessionResult(
            status="error",
            error_kind="transient",
            error="Claude Code exited without a result (fake)",
        )
    if fail == "error":
        return SessionResult(
            status="error", error_kind="executor", error="the executor failed (fake)"
        )
    if fail == "sandbox":
        return SessionResult(
            status="error",
            error_kind="sandbox",
            error="the sandbox guard did not start (fake)",
        )
    return None


def _reset_time(value: dt.datetime | int | None) -> dt.datetime:
    if value is None:
        now = dt.datetime.now(dt.UTC).replace(second=0, microsecond=0)
        return now + dt.timedelta(hours=1)
    if isinstance(value, int):
        return dt.datetime.fromtimestamp(value, dt.UTC)
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


# --- Plausible results and documents -----------------------------------------


def research_result(task: str) -> dict[str, Any]:
    """A small ResearchResult with fictional sources on ``.example`` hosts."""
    today = dt.datetime.now(dt.UTC).date().isoformat()
    sources = [
        {
            "id": f"S{number}",
            "url": f"https://{host}/{task}",
            "title": f"{publisher}: {task.replace('-', ' ')}",
            "publisher": publisher,
            "accessed": today,
            "quote": quote,
            "claim": claim,
        }
        for number, (host, publisher, quote, claim) in enumerate(_SOURCES, start=1)
    ]
    return {
        "sources": sources,
        "notes": (
            "Placeholder findings from the fake executor.\n\n"
            "- Most listed providers offer a free tier for small teams [S1].\n"
            "- Set-up takes about two days [S2].\n"
            "- Buyers rank ease of editing above price [S3]."
        ),
        "unanswered": ["Anything that needs a real web search"],
    }


def draft_result(title: str) -> dict[str, Any]:
    """A DraftResult for a document called ``title``."""
    subject = title[:1].lower() + title[1:]
    return {
        "status": "ok",
        "summary": f"Placeholder {subject} from the fake executor.",
        "key_facts": [f"{title}: placeholder content, no model was called"],
        "decisions": [
            {
                "id": "D1",
                "decision": f"Keep the placeholder {subject}",
                "reason": "The fake executor always makes this decision.",
                "reversibility": "easy",
                "alternatives": ["Run spadework with the claude-cli executor"],
            }
        ],
        "open_questions": [
            {
                "id": "Q1",
                "question": f"Is a placeholder {subject} enough to start with?",
                "why": "It decides whether this document needs a real run.",
                "answer_format": "yes_no",
            }
        ],
        "assumptions": ["No model was called for this document"],
        "risks": [
            {
                "risk": "Placeholder content is mistaken for research",
                "likelihood": "low",
                "impact": "high",
                "mitigation": "Run spadework with the claude-cli executor first.",
            }
        ],
        "gaps": [],
    }


def critic_result(documents: list[str], verdict: str) -> dict[str, Any]:
    """A CriticResult: one finding on the first document, or none."""
    if verdict != "continue" or not documents:
        return {"verdict": "ship", "findings": [], "biggest_gap": ""}
    finding = {
        "id": "F1",
        "target": documents[0],
        "kind": "specificity",
        "severity": "medium",
        "instruction": "Tie the first section to a fact from the brief.",
    }
    return {
        "verdict": "continue",
        "findings": [finding],
        "biggest_gap": "Every document is placeholder content.",
    }


def summary_result(prompt: str) -> dict[str, Any]:
    """A SummaryResult that consolidates the qualified questions in the prompt."""
    questions = [
        {
            "question": f"Placeholder consolidated question {number}",
            "why": "The fake executor merges nothing; this stands for one question.",
            "from": [qualified],
        }
        for number, qualified in enumerate(
            _unique(_QUALIFIED_QUESTION.findall(prompt))[:10], start=1
        )
    ]
    return {"status": "ok", "open_questions": questions}


def document(title: str, headings: list[str], sources: list[str]) -> str:
    """A short Markdown document: an H1, a one-line purpose, then sections."""
    lines = [
        f"# {title}",
        "",
        "Placeholder text from spadework's fake executor: no model was called.",
    ]
    for number, heading in enumerate(headings):
        cite = f" [{sources[number % len(sources)]}]" if sources else ""
        lines += ["", f"## {heading}", "", f"Placeholder content for {heading}.{cite}"]
    return "\n".join(lines) + "\n"


def headings_in(prompt: str, default: tuple[str, ...] = DEFAULT_HEADINGS) -> list[str]:
    """The headings a prompt asks for, or ``default``.

    Reads a "Required headings: A, B" line, or the bold bullets that follow
    the first line that mentions headings, as the built-in tasks write them.
    """
    match = _REQUIRED_HEADINGS.search(prompt)
    if match:
        found = [part.strip(" .`*") for part in match.group(1).split(",")]
        return [heading for heading in found if heading] or list(default)
    headings: list[str] = []
    collecting = False
    for line in prompt.splitlines():
        if not collecting:
            collecting = "heading" in line.lower()
            continue
        bullet = _BOLD_BULLET.match(line)
        if bullet:
            headings.append(bullet.group(1).strip())
        elif headings and line.strip() and not line[:1].isspace():
            break
    return headings or list(default)


def _write_output(spec: SessionSpec, output: Path) -> None:
    if spec.kind is SessionKind.FIX and output.exists():
        text = output.read_text(encoding="utf-8").rstrip("\n")
        applied = ", ".join(_ids(_FINDING_ID, "F", spec.prompt)) or "none"
        output.write_text(
            f"{text}\n\nRevised after review ({applied}).\n", encoding="utf-8"
        )
        return
    default = SUMMARY_HEADINGS if spec.kind is SessionKind.SUMMARY else DEFAULT_HEADINGS
    sources = (
        [] if spec.kind is SessionKind.SUMMARY else _ids(_SOURCE_ID, "S", spec.prompt)
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        document(_title(output), headings_in(spec.prompt, default), sources),
        encoding="utf-8",
    )


def _title(output: Path | None) -> str:
    if output is None:
        return "Document"
    words = re.sub(r"^[0-9]+-", "", output.stem).replace("-", " ").strip()
    return words[:1].upper() + words[1:] if words else "Document"


def _ids(pattern: re.Pattern[str], prefix: str, text: str) -> list[str]:
    numbers = sorted({int(number) for number in pattern.findall(text)})
    return [f"{prefix}{number}" for number in numbers]


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _staged_files(workdir: Path) -> list[str]:
    if not workdir.is_dir():
        return []
    files = sorted(path for path in workdir.rglob("*") if path.is_file())
    return [str(path.relative_to(workdir)) for path in files]


def _documents(spec: SessionSpec) -> list[str]:
    staged = [Path(name).name for name in _staged_files(spec.workdir)]
    names = _unique([*staged, *_DOCUMENT.findall(spec.prompt)])
    return [
        name for name in names if _DOCUMENT.fullmatch(name) and name != "00-summary.md"
    ]
