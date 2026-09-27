"""How ``run`` shows its progress: a live view, plain lines, or NDJSON.

The plan and the final result are results, so they go to stdout. Progress
goes to stderr: a Rich live view on a terminal (docs/spec/01-cli.md, Live
view), and otherwise the same information as timestamped lines, one per
event. With ``--json``, every event is one NDJSON line on stdout instead.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TextIO

from spadework.console import silence
from spadework.durations import format_duration
from spadework.engine import events as ev
from spadework.manifest import utc_timestamp

if TYPE_CHECKING:
    from rich.console import RenderableType
    from rich.live import Live

    from spadework.console import Ui


def elapsed(seconds: float) -> str:
    """A duration as the live view shows it: 0m58s, 1m12s, 1h02m."""
    whole = max(int(seconds), 0)
    hours, rest = divmod(whole, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m{secs:02d}s"


def usd(amount: float) -> str:
    return f"${amount:.2f}"


class NdjsonDisplay:
    """``run --json``: one JSON object per line on stdout."""

    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream
        self.closed = False
        """Set once the reader has gone away: nothing more is written."""

    def __call__(self, event: ev.Event) -> None:
        self.write(ev.event_document(event, utc_timestamp()))

    def write(self, document: dict[str, Any]) -> None:
        if self.closed:
            return
        stream = self.stream or sys.stdout
        try:
            stream.write(json.dumps(document, ensure_ascii=False) + "\n")
            stream.flush()
        except BrokenPipeError:
            # As with `run --json | head`: the run goes on without output.
            self.closed = True
            silence(stream)

    def error(self, document: dict[str, Any]) -> None:
        """A run that could not start: the CLI's error document, as an event."""
        self.write({"event": "error", "ts": utc_timestamp(), **document})

    def close(self) -> None:
        pass


@dataclass
class _Row:
    """One task in the live view."""

    task: str
    state: str = "pending"
    before: str = "pending"
    """The state before the current session, shown again if it ends
    without changing it, as a failed review fix does."""
    kind: str = ""
    model: str = ""
    cost: float = 0.0
    seconds: float = 0.0
    started: float | None = None
    target: str = ""
    denials: int = 0
    research_denials: int = 0
    reason: str | None = None


_SYMBOLS = {
    "pending": ("·", "dim"),
    "running": ("▸", "cyan"),
    "done": ("✓", "green"),
    "fresh": ("✓", "green"),
    "invalid": ("!", "red"),
    "failed": ("✗", "red"),
    "skipped": ("-", "yellow"),
}


@dataclass
class _Progress:
    """What the displays know about the run so far."""

    project: str = ""
    playbook: str = ""
    budget: float = 0.0
    max_time_s: int = 0
    spent: float = 0.0
    round: int = 0
    rounds: int = 0
    started: float = field(default_factory=time.monotonic)
    rows: dict[str, _Row] = field(default_factory=dict)

    def row(self, task: str | None, kind: str = "") -> _Row | None:
        """The row of a task, or of the critic or the summary, which have
        a row of their own named after their kind."""
        name = task if task is not None else kind
        if not name:
            return None
        return self.rows.setdefault(name, _Row(name))

    def update(self, event: ev.Event) -> None:
        match event:
            case ev.RunStarted():
                self.project, self.playbook = (
                    event.project,
                    f"{event.playbook} {event.playbook_version}",
                )
                self.budget, self.max_time_s = event.budget_usd, event.max_time_s
                self.rounds = event.rounds
                self.started = time.monotonic()
                self.rows = {task: _Row(task) for task in event.tasks}
            case ev.SessionStarted():
                self.round = max(self.round, event.round)
                row = self.row(event.task, event.kind)
                if row is not None:
                    if row.state != "running":
                        row.before = row.state
                    row.state, row.kind, row.model = "running", event.kind, event.model
                    row.started, row.target = time.monotonic(), ""
            case ev.ToolUsed():
                row = self.row(event.task, event.kind)
                if row is not None:
                    row.target = f"{event.name} {event.target}"
            case ev.ToolDenied():
                row = self.row(event.task, event.kind)
                if row is not None:
                    row.denials += 1
                    row.research_denials += int(event.possible_injection)
            case ev.SessionFinished():
                self.spent = event.spent_usd
                row = self.row(event.task, event.kind)
                if row is not None:
                    row.cost += event.cost_usd
                    row.seconds += event.duration_s
                    row.started, row.target = None, ""
                    if event.task is None and not event.retry:
                        row.state = "done" if event.status == "ok" else "failed"
                    elif not event.retry and row.state == "running":
                        row.state = row.before
            case ev.TaskFinished():
                row = self.row(event.task)
                if row is not None:
                    row.state, row.reason = event.state, event.reason
            case _:
                pass


class _HumanDisplay:
    """The plan and the result on stdout, for the live view and the lines."""

    def __init__(self, ui: Ui) -> None:
        self.ui = ui
        self.progress = _Progress()

    def __call__(self, event: ev.Event) -> None:
        self.progress.update(event)
        match event:
            case ev.PlanReady():
                from spadework import views

                views.print_plan(self.ui, event.plan)
                for message, hint in [
                    *event.warnings,
                    *views.plan_warnings(event.plan),
                ]:
                    self.ui.warning(message, hint)
            case ev.UpToDate():
                self.ui.result("up to date")
                if event.invalid:
                    listed = ", ".join(event.invalid)
                    self.ui.warning(
                        f"nothing is stale, but some tasks are invalid: {listed}",
                        "`spadework status` says why. To redraft one: "
                        "spadework run --force --only TASK",
                    )
            case ev.RunFinished():
                self.stop()
                from spadework import views

                views.print_run_finished(self.ui, event)
            case _:
                self.progress_event(event)

    def progress_event(self, event: ev.Event) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.stop()


class LinesDisplay(_HumanDisplay):
    """Progress as timestamped lines on stderr, one per event."""

    def progress_event(self, event: ev.Event) -> None:
        if isinstance(event, ev.Notice) and event.level == "warning":
            self._line(event.message, warning=True)
            if event.hint and not self.ui.quiet:
                self._line(event.hint)
            return
        if isinstance(event, ev.ToolDenied):
            self._line(denial(event), warning=True)
            return
        text = line(event, self.progress)
        if text is not None and not self.ui.quiet:
            self._line(text)

    def _line(self, text: str, *, warning: bool = False) -> None:
        from rich.text import Text

        message = Text(dt.datetime.now().strftime("%H:%M:%S "))
        if warning:
            message.append("warning: ", style="bold yellow")
        message.append(text)
        self.ui.err.print(message, soft_wrap=True)


def _who(task: str | None, kind: str) -> str:
    """How a line names a session: "stack draft", or "critic"."""
    return kind if task is None else f"{task} {kind}"


def denial(event: ev.ToolDenied) -> str:
    text = (
        f"{_who(event.task, event.kind)}: the sandbox blocked {event.name} "
        f"{event.target}: {event.reason}"
    )
    if event.possible_injection:
        text += " (possible prompt injection encountered)"
    return text


def line(event: ev.Event, progress: _Progress) -> str | None:
    """One plain line for an event, or None for events without one."""
    match event:
        case ev.RunStarted():
            count = len(event.tasks)
            return (
                f"run {event.run_id} started: {count} "
                f"{'task' if count == 1 else 'tasks'}, budget {usd(event.budget_usd)}, "
                f"max time {format_duration(event.max_time_s)}"
            )
        case ev.SessionStarted():
            attempt = f", attempt {event.attempt}" if event.attempt > 1 else ""
            review = f", round {event.round}" if event.round else ""
            return (
                f"{_who(event.task, event.kind)}: started ({event.model}, cap "
                f"{usd(event.max_budget_usd)}, timeout "
                f"{format_duration(event.timeout_s)}{review}{attempt})"
            )
        case ev.ToolUsed():
            return f"{_who(event.task, event.kind)}: {event.name} {event.target}"
        case ev.SessionFinished():
            text = (
                f"{_who(event.task, event.kind)}: {event.status}, "
                f"{usd(event.cost_usd)}, "
                f"{elapsed(event.duration_s)} (spent {usd(event.spent_usd)} of "
                f"{usd(progress.budget)})"
            )
            if event.error:
                text += f": {event.error}"
            if event.retry:
                text += "; trying again"
            return text
        case ev.TaskFinished():
            return f"{event.task}: {event.state}" + (
                f": {event.reason}" if event.reason else ""
            )
        case ev.Notice():
            return event.message
    return None


class LiveDisplay(_HumanDisplay):
    """A Rich live view on stderr, for a terminal."""

    def __init__(self, ui: Ui) -> None:
        super().__init__(ui)
        self.live: Live | None = None

    def progress_event(self, event: ev.Event) -> None:
        if isinstance(event, ev.RunStarted) and not self.ui.quiet:
            from rich.live import Live

            self.live = Live(
                LiveView(self.progress),
                console=self.ui.err,
                refresh_per_second=4,
                transient=False,
            )
            self.live.start()
        elif isinstance(event, ev.Notice):
            if event.level == "warning":
                self.ui.warning(event.message, event.hint)
            elif not self.ui.quiet:
                self.ui.info(event.message)
        if self.live is not None:
            self.live.refresh()

    def stop(self) -> None:
        if self.live is not None:
            self.live.stop()
            self.live = None


class LiveView:
    """The live view's picture of the run, redrawn from ``_Progress``."""

    def __init__(self, progress: _Progress) -> None:
        self.progress = progress

    def __rich__(self) -> RenderableType:
        from rich.console import Group
        from rich.table import Table
        from rich.text import Text

        progress = self.progress
        now = time.monotonic()
        title = f"spadework · {progress.project} · {progress.playbook}"
        if progress.round:
            title += f" · round {progress.round}/{progress.rounds}"
        header = Text(title, style="bold")
        table = Table.grid(padding=(0, 2))
        for _ in range(7):
            table.add_column()
        warnings = []
        for row in progress.rows.values():
            symbol, style = _SYMBOLS.get(row.state, ("·", "dim"))
            if row.state == "pending":
                table.add_row(
                    Text(symbol, style=style), row.task, Text("pending", style="dim")
                )
            else:
                seconds = row.seconds
                if row.started is not None:
                    seconds += now - row.started
                target = f"→ {row.target}" if row.target else (row.reason or "")
                table.add_row(
                    Text(symbol, style=style),
                    row.task,
                    row.kind,
                    row.model,
                    usd(row.cost),
                    elapsed(seconds),
                    Text(target, style="dim", overflow="ellipsis", no_wrap=True),
                )
            if row.denials:
                calls = (
                    "1 tool call" if row.denials == 1 else f"{row.denials} tool calls"
                )
                text = (
                    f"{calls} blocked by the sandbox (see: spadework logs {row.task})"
                )
                if row.research_denials:
                    text = f"possible prompt injection: {text}"
                warnings.append((row.task, text))
        footer = Text(
            f"spent {usd(progress.spent)} of {usd(progress.budget)} · "
            f"{elapsed(now - progress.started)} of "
            f"{format_duration(progress.max_time_s)}",
            style="dim",
        )
        lines = [
            Text.assemble(("⚠ ", "yellow"), f"{task}  ", (text, "yellow"))
            for task, text in warnings
        ]
        return Group(header, Text(""), table, *lines, Text(""), footer)
