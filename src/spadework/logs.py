"""``spadework logs``: show the traces of a run's sessions.

See docs/spec/01-cli.md. Each session's raw event log is in
``.spadework/runs/<run-id>/sessions/``, and the manifest names it
(``sessions[].log``). Logs are shown by replaying them through the
executor's own parser and event model (``claude_stream.StreamParser``), so
``logs`` shows what the live view saw. The ``fake`` executor writes its
normalised events instead, one JSON object per line, and those are read
back into the same events.

``--follow`` tails the logs of a run that is still going. A run is going
while its manifest has no outcome and the project lock is held by a live
process; a session that is still running is not in the manifest yet, so its
log is found in the sessions folder.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spadework.durations import format_duration
from spadework.errors import InvalidError, SpadeworkError, UsageError
from spadework.executors.base import (
    Denied,
    Event,
    Notice,
    RateLimit,
    SessionResult,
    Text,
    ToolCall,
)

if TYPE_CHECKING:
    from rich.text import Text as RichText

    from spadework.console import Ui
    from spadework.executors.claude_stream import StreamParser
    from spadework.manifest import RunManifest, SessionEntry

STATE_DIR = ".spadework"
RUNS_DIR = "runs"
SESSIONS_DIR = "sessions"
_MANIFEST_FILE = "manifest.json"
LOCK_FILE = "lock"
POLL_S = 0.5

_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{4}$")
_LOG_NAME = re.compile(r"^(?P<n>[0-9]+)-(?P<name>.+)\.jsonl$")
_KINDS = ("research", "draft", "repair", "critic", "fix", "summary")
_TOOL_WIDTH = 9  # the longest tool name, WebSearch
_STATUS_STYLES = {
    "ok": "green",
    "budget": "yellow",
    "rate_limited": "yellow",
    "interrupted": "yellow",
}

Sleep = Callable[[float], None]


# --- Finding runs and sessions ----------------------------------------------


@dataclass(frozen=True)
class SessionLog:
    """One session of a run, and where its log is."""

    n: int
    task: str | None
    kind: str
    log: str
    """Relative to the run folder, as the manifest gives it."""
    entry: SessionEntry | None = None
    """The manifest's record, once the session has finished."""

    @property
    def title(self) -> str:
        parts = [f"{self.n:03d} {self.task or self.kind}", self.kind]
        if self.entry is not None:
            parts.append(self.entry.model)
            if self.entry.round:
                parts.append(f"round {self.entry.round}")
        return " · ".join(parts)

    def matches(self, task: str) -> bool:
        """Whether ``TASK`` names this session: its task, or its kind when it
        has no task (the critic and the summary)."""
        return self.task == task or (self.task is None and self.kind == task)


@dataclass
class Run:
    root: Path
    """The project folder."""
    run_id: str
    manifest: RunManifest | None = None
    """None until the engine has written it."""

    @property
    def folder(self) -> Path:
        return self.root / STATE_DIR / RUNS_DIR / self.run_id

    def reload(self) -> None:
        from spadework.manifest import MANIFEST_FILE, load_manifest

        path = self.folder / MANIFEST_FILE
        self.manifest = load_manifest(path) if path.is_file() else None

    def sessions(self) -> list[SessionLog]:
        """The manifest's sessions, then any log it does not list yet."""
        listed = [
            SessionLog(entry.n, entry.task, entry.kind, entry.log, entry)
            for entry in (self.manifest.sessions if self.manifest else [])
        ]
        known = {session.log for session in listed}
        unlisted = []
        folder = self.folder / SESSIONS_DIR
        names = sorted(os.listdir(folder)) if folder.is_dir() else []
        for name in names:
            log = f"{SESSIONS_DIR}/{name}"
            session = _from_name(name, log)
            if session is not None and log not in known:
                unlisted.append(session)
        return sorted([*listed, *unlisted], key=lambda session: session.n)

    @property
    def simulated_label(self) -> str:
        """The label after a cost, " (simulated)", when the run's executor
        calls no model."""
        from spadework.executors.base import is_simulated

        if self.manifest is None or not is_simulated(self.manifest.executor.name):
            return ""
        return " (simulated)"

    def outcome_text(self, *, active: bool) -> str:
        manifest = self.manifest
        if manifest is None:
            return "running" if active else "no manifest"
        return manifest.outcome or ("running" if active else "did not finish")

    def started_text(self) -> str:
        """When the run started, in local time to the minute."""
        started = None
        if self.manifest is not None:
            started = _time(self.manifest.started_at.replace("Z", "+00:00"))
        if started is None:
            started = dt.datetime.strptime(
                self.run_id.split("-")[0], "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=dt.UTC)
        return started.astimezone().strftime("%Y-%m-%d %H:%M")

    def describe(self, *, active: bool) -> str:
        manifest = self.manifest
        if manifest is None:
            return f"run {self.run_id} · {self.outcome_text(active=active)}"
        outcome = self.outcome_text(active=active)
        totals = manifest.totals
        parts = [
            f"run {self.run_id}",
            outcome,
            f"${totals.cost_usd:.2f}{self.simulated_label}",
            _plural(totals.sessions, "session"),
        ]
        if totals.guard_denials:
            parts.append(_plural(totals.guard_denials, "denied call"))
        return " · ".join(parts)


def _from_name(name: str, log: str) -> SessionLog | None:
    """A session from its log's file name, such as ``001-scope-draft.jsonl``."""
    match = _LOG_NAME.match(name)
    if match is None:
        return None
    task, _, kind = match["name"].rpartition("-")
    if kind not in _KINDS:
        return None
    return SessionLog(int(match["n"]), task or None, kind, log)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def run_ids(root: Path) -> list[str]:
    """The project's runs, oldest first.

    A run id starts with its time to the second, then 4 random digits. Two
    runs in the same second are ordered by their manifest's last write, as
    ``status`` orders them: runs never overlap, so the later run wrote last.
    """
    folder = root / STATE_DIR / RUNS_DIR
    if not folder.is_dir():
        return []
    names = [
        name
        for name in os.listdir(folder)
        if _RUN_ID.match(name) and (folder / name).is_dir()
    ]
    return sorted(names, key=lambda name: _started(folder / name))


def _started(run: Path) -> tuple[str, int, str]:
    written = 0
    for path in (run / _MANIFEST_FILE, run):  # the folder, if no manifest yet
        try:
            written = path.stat().st_mtime_ns
        except OSError:
            continue
        break
    return run.name.split("-")[0], written, run.name


class NoRuns(Exception):
    """The project has no runs yet: ``logs`` says so, and exits 0."""


def find_run(root: Path, run_id: str | None) -> Run:
    """The run called ``run_id`` (or the only one it is a prefix of), or the
    last run. Raises ``NoRuns`` when there is no run at all and none was
    named."""
    ids = _project_run_ids(root)
    if run_id is None:
        if not ids:
            raise NoRuns
        return load_run(root, ids[-1])
    matches = [name for name in ids if name == run_id] or [
        name for name in ids if name.startswith(run_id)
    ]
    if len(matches) != 1:
        what = "no run" if not matches else "more than one run"
        raise UsageError(
            f"there is {what} called '{run_id}'",
            recent_runs_hint(root, ids) if ids else "This project has no runs yet.",
        )
    return load_run(root, matches[0])


def load_run(root: Path, run_id: str) -> Run:
    run = Run(root, run_id)
    run.reload()
    return run


def _project_run_ids(root: Path) -> list[str]:
    """``run_ids``, after checking that ``root`` is a project at all."""
    from spadework.config import CONFIG_FILE

    ids = run_ids(root)
    if (
        not ids
        and not (root / CONFIG_FILE).is_file()
        and not (root / STATE_DIR).is_dir()
    ):
        raise InvalidError(
            f"there is no {CONFIG_FILE} in {root}",
            "Run spadework in a project folder, or pass -C PROJECT_FOLDER.",
        )
    return ids


def latest_with_sessions(root: Path, task: str | None) -> tuple[Run, Run | None]:
    """The run ``logs`` shows when no ``--run`` is given, and the later run it
    passed over, if any.

    That is the last run, unless it ended before any session (or before any
    of ``task``'s): then the most recent run that has them. A run that is
    still going is always shown. Raises ``NoRuns`` without runs, and a usage
    error when ``task`` names no session of any run.
    """
    ids = _project_run_ids(root)
    if not ids:
        raise NoRuns
    last = load_run(root, ids[-1])
    if is_active(last):
        return last, None
    for run_id in reversed(ids):
        run = last if run_id == last.run_id else load_run(root, run_id)
        sessions = run.sessions()
        if any(task is None or session.matches(task) for session in sessions):
            return run, None if run is last else last
    if task is None:
        return last, None
    names = sorted(
        {
            session.task or session.kind
            for run_id in ids
            for session in load_run(root, run_id).sessions()
        }
    )
    listed = f"Sessions so far are for: {', '.join(names)}. " if names else ""
    raise UsageError(
        f"no run has sessions for '{task}'", listed + recent_runs_hint(root, ids)
    )


def recent_runs_hint(root: Path, ids: list[str] | None = None, count: int = 3) -> str:
    """The last few runs, newest first, for a hint."""
    ids = run_ids(root) if ids is None else ids
    recent = []
    for run_id in reversed(ids[-count:]):
        run = load_run(root, run_id)
        recent.append(
            f"{run_id} ({run.outcome_text(active=is_active(run))}, "
            f"{run.started_text()})"
        )
    more = " `spadework logs --list` lists them all." if len(ids) > count else ""
    return f"Recent runs: {', '.join(recent)}.{more}"


def select(run: Run, task: str | None) -> list[SessionLog]:
    sessions = run.sessions()
    if task is None:
        return sessions
    chosen = [session for session in sessions if session.matches(task)]
    if not chosen and sessions:
        names = sorted({session.task or session.kind for session in sessions})
        raise UsageError(
            f"run {run.run_id} has no sessions for '{task}'",
            f"Its sessions are for: {', '.join(names)}. " + recent_runs_hint(run.root),
        )
    return chosen


def is_active(run: Run) -> bool:
    """Whether the run is still going: no outcome yet, it is the project's
    latest run, and the lock is held by a live process."""
    if run.manifest is not None and run.manifest.outcome is not None:
        return False
    ids = run_ids(run.root)
    if ids and ids[-1] != run.run_id:
        return False
    return lock_held(run.root)


def lock_held(root: Path) -> bool:
    from spadework.lock import read_lock

    owner = read_lock(root / STATE_DIR / LOCK_FILE)
    if owner is None:
        return False
    if owner.host != socket.gethostname():
        return True  # it cannot be checked from here
    try:
        os.kill(owner.pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists, and belongs to another user
    return True


# --- Replaying a log into events --------------------------------------------


@dataclass(frozen=True)
class Summary:
    """How a session ended, for the last line of its trace."""

    status: str
    cost_usd: float | None = None
    duration_s: float | None = None
    tool_calls: int = 0
    guard_denials: int = 0
    error: str | None = None

    @classmethod
    def of_entry(cls, entry: SessionEntry) -> Summary:
        return cls(
            entry.status,
            entry.cost_usd,
            entry.duration_s,
            sum(entry.tool_calls.values()),
            entry.guard_denials,
            entry.error,
        )

    @classmethod
    def of_result(cls, result: SessionResult) -> Summary:
        return cls(
            result.status,
            result.cost_usd if result.cost_known else None,
            result.duration_s,
            sum(result.tool_calls.values()),
            result.guard_denials,
            result.error,
        )


class Replay:
    """Turns the lines of one session log into the executor's events.

    ``claude-cli`` logs are Claude Code's raw ``stream-json``, fed through the
    same parser that read them live, with the sandbox checks off: they were
    made when the session ran. ``fake`` logs hold the events themselves.
    """

    def __init__(self, emit: Callable[[Event], None]) -> None:
        from spadework.executors.claude_stream import StreamParser

        self.emit = emit
        self.parser: StreamParser = StreamParser(
            expected_tools=None, events=emit, require_guard=False
        )
        self._fake_result: dict[str, Any] | None = None

    def feed(self, line: str) -> None:
        try:
            record = json.loads(line)
        except ValueError:
            record = None
        if isinstance(record, dict):
            event = _fake_event(record)
            if event is not None:
                self.emit(event)
                return
            kind = record.get("type")
            if kind == "result" and "subtype" not in record and "status" in record:
                self._fake_result = record
                return
            cwd = record.get("cwd")
            init = kind == "system" and record.get("subtype") == "init"
            if init and isinstance(cwd, str) and self.parser.workdir is None:
                # Paths in the staging folder are shown relative to it.
                self.parser.workdir = Path(cwd)
        self.parser.feed(line)

    def summary(self) -> Summary | None:
        """How the session ended, if its log says; None if it has no result."""
        if self._fake_result is not None:
            return _fake_summary(self._fake_result)
        result = self.parser.result()  # also reports denials not yet shown
        return Summary.of_result(result) if self.parser.finished else None


def _fake_event(record: dict[str, Any]) -> Event | None:
    kind = record.get("type")
    try:
        if kind == "ToolCall":
            return ToolCall(str(record["name"]), str(record["target"]))
        if kind == "Text":
            return Text(str(record["snippet"]))
        if kind == "Denied":
            return Denied(
                str(record["name"]), str(record["target"]), str(record["reason"])
            )
        if kind == "Notice":
            return Notice(str(record["message"]))
        if kind == "RateLimit":
            return RateLimit(str(record["status"]), _time(record.get("resets_at")))
    except KeyError:
        return None
    return None


def _fake_summary(record: dict[str, Any]) -> Summary:
    def number(key: str) -> float | None:
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return float(value)

    calls = record.get("tool_calls")
    denials = record.get("guard_denials")
    error = record.get("error")
    return Summary(
        str(record.get("status")),
        number("cost_usd") if record.get("cost_known", True) else None,
        number("duration_s"),
        sum(value for value in calls.values() if isinstance(value, int))
        if isinstance(calls, dict)
        else 0,
        denials if isinstance(denials, int) else 0,
        error if isinstance(error, str) else None,
    )


def _time(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


# --- Presenting ---------------------------------------------------------------


def event_line(event: Event) -> RichText | None:
    """One line of a trace, or None for events not worth a line."""
    from rich.text import Text as RichText

    if isinstance(event, ToolCall):
        return RichText.assemble("  ", event.name.ljust(_TOOL_WIDTH), " ", event.target)
    if isinstance(event, Text):
        return RichText(f'  "{event.snippet}"', style="dim italic")
    if isinstance(event, Denied):
        target = f" {event.target}" if event.target else ""
        return RichText(
            f"  ✗ denied {event.name}{target}: {event.reason}", style="bold red"
        )
    if isinstance(event, Notice):
        return RichText(f"  ! {event.message}", style="yellow")
    if isinstance(event, RateLimit):
        if event.status == "allowed":
            return None
        when = (
            f", resets {event.resets_at.astimezone(dt.UTC):%Y-%m-%d %H:%M} UTC"
            if event.resets_at
            else ""
        )
        return RichText(f"  ! rate limit: {event.status}{when}", style="yellow")
    return None


def summary_lines(
    summary: Summary | None, *, running: bool, label: str = ""
) -> list[RichText]:
    """How a session ended. ``label`` follows its cost, as " (simulated)"."""
    from rich.text import Text as RichText

    if summary is None:
        text = "still running" if running else "no result was recorded"
        return [RichText(f"  {text}", style="dim")]
    parts = [summary.status]
    if summary.cost_usd is not None:
        parts.append(f"${summary.cost_usd:.2f}{label}")
    if summary.duration_s is not None:
        parts.append(format_duration(round(summary.duration_s)))
    parts.append(_plural(summary.tool_calls, "tool call"))
    if summary.guard_denials:
        parts.append(_plural(summary.guard_denials, "denied"))
    style = _STATUS_STYLES.get(summary.status, "bold red")
    lines = [RichText("  " + " · ".join(parts), style=style)]
    if summary.error and summary.status != "ok":
        lines.append(RichText(f"  {summary.error}", style=style))
    return lines


@dataclass
class _Tail:
    """Where we are in one session's log."""

    session: SessionLog
    path: Path
    replay: Replay | None = None
    """None in raw mode."""
    started: bool = False
    offset: int = 0
    partial: bytes = b""
    missing_reported: bool = False
    done: bool = False


class LogPrinter:
    """Prints session logs, whole or as they grow."""

    def __init__(self, ui: Ui, run: Run, *, raw: bool) -> None:
        self.ui = ui
        self.run = run
        self.raw = raw
        self._tails: dict[str, _Tail] = {}

    def show_header(self, *, active: bool) -> None:
        from rich.text import Text as RichText

        line = self.run.describe(active=active)
        if self.raw:
            self.ui.info(line)
        else:
            self._print(RichText(line, style="bold"))

    def pump(self, session: SessionLog, *, final: bool, running: bool) -> bool:
        """Print what is new in a session's log. With ``final``, finish it.

        Returns False if the log does not exist (yet).
        """
        tail = self._tails.get(session.log)
        if tail is None:
            tail = _Tail(session, self.run.folder / session.log)
            self._tails[session.log] = tail
        if tail.done:
            return True
        if session.entry is not None and tail.session.entry is None:
            tail.session = session
        data = self._read(tail)
        if data is None:
            if final:
                self._missing(tail)
            return False
        if not tail.started:
            self._start(tail)
        chunk = tail.partial + data
        *complete, tail.partial = chunk.split(b"\n")
        if final and tail.partial:
            complete.append(tail.partial)
            tail.partial = b""
        for raw_line in complete:
            self._line(tail, raw_line.decode("utf-8", errors="replace"))
        if final:
            self._finish(tail, running=running)
        return True

    def _read(self, tail: _Tail) -> bytes | None:
        try:
            with tail.path.open("rb") as file:
                file.seek(tail.offset)
                data = file.read()
        except FileNotFoundError:
            return None
        except OSError as exc:
            if not tail.missing_reported:
                tail.missing_reported = True
                self.ui.warning(
                    f"cannot read the log of session {tail.session.title}: "
                    f"{exc.strerror}",
                    "Check the permissions of .spadework/runs.",
                )
            tail.done = True
            return None
        tail.offset += len(data)
        return data

    def _missing(self, tail: _Tail) -> None:
        if not tail.missing_reported:
            tail.missing_reported = True
            self.ui.warning(
                f"the log of session {tail.session.title} is missing "
                f"({tail.session.log})",
                "It was moved or deleted.",
            )
        tail.done = True

    def _start(self, tail: _Tail) -> None:
        from rich.text import Text as RichText

        tail.started = True
        if self.raw:
            self.ui.info(f"==> {tail.session.log} <==")
            return
        self.ui.result()
        self._print(RichText(tail.session.title, style="bold"))

        def emit(event: Event) -> None:
            line = event_line(event)
            if line is not None:
                self._print(line)

        tail.replay = Replay(emit)

    def _line(self, tail: _Tail, line: str) -> None:
        if not line.strip():
            return
        if self.raw:
            sys.stdout.write(line.rstrip("\r\n") + "\n")
            sys.stdout.flush()
            return
        assert tail.replay is not None
        tail.replay.feed(line)

    def _finish(self, tail: _Tail, *, running: bool) -> None:
        tail.done = True
        if self.raw:
            return
        assert tail.replay is not None
        entry = tail.session.entry
        summary = (
            Summary.of_entry(entry) if entry is not None else tail.replay.summary()
        )
        if entry is not None:
            tail.replay.summary()  # reports denials whose calls never finished
        label = self.run.simulated_label
        for line in summary_lines(summary, running=running, label=label):
            self._print(line)

    def _print(self, line: RichText) -> None:
        # One line per event, never re-wrapped, so a trace can be grepped.
        self.ui.out.print(line, soft_wrap=True)


NO_RUNS = "no runs yet"
NO_RUNS_HINT = (
    "Start one with `spadework run`. `spadework check` shows what it will do."
)


def show_logs(
    ui: Ui,
    root: Path,
    *,
    task: str | None,
    run_id: str | None,
    follow: bool,
    raw: bool,
    poll_s: float = POLL_S,
    sleep: Sleep = time.sleep,
) -> None:
    """``spadework logs``: print the logs of a run's sessions."""
    try:
        if follow:
            follow_logs(
                ui, root, task=task, run_id=run_id, raw=raw, poll_s=poll_s, sleep=sleep
            )
        else:
            _show(ui, root, task=task, run_id=run_id, raw=raw)
    except NoRuns:
        ui.info(NO_RUNS)
        ui.info(NO_RUNS_HINT)


def _show(
    ui: Ui, root: Path, *, task: str | None, run_id: str | None, raw: bool
) -> None:
    if run_id is None:
        run, passed_over = latest_with_sessions(root, task)
        if passed_over is not None:
            what = "any session" if task is None else f"any session of '{task}'"
            ui.info(
                f"Run {passed_over.run_id} ({passed_over.outcome_text(active=False)}) "
                f"ended before {what}; showing run {run.run_id}."
            )
    else:
        run = find_run(root, run_id)
    sessions = select(run, task)
    active = is_active(run)
    printer = LogPrinter(ui, run, raw=raw)
    printer.show_header(active=active)
    if not sessions:
        ui.info(f"Run {run.run_id} has no sessions yet.")
        return
    shown = [
        printer.pump(session, final=True, running=active and session.entry is None)
        for session in sessions
    ]
    if not any(shown):
        raise SpadeworkError(
            "none of the session logs of this run exist",
            f"They were moved or deleted from .spadework/{RUNS_DIR}/{run.run_id}/.",
        )
    if active:
        ui.info("The run is still going. Follow it with: spadework logs --follow")


def list_runs(ui: Ui, root: Path) -> None:
    """``spadework logs --list``: every run, newest first."""
    from spadework.views import print_runs

    ids = _project_run_ids(root)
    if not ids:
        ui.info(NO_RUNS)
        ui.info(NO_RUNS_HINT)
        return
    runs = [load_run(root, run_id) for run_id in reversed(ids)]
    print_runs(ui, [(run, is_active(run)) for run in runs])
    ui.info("Show one with: spadework logs --run ID")


def follow_logs(
    ui: Ui,
    root: Path,
    *,
    task: str | None,
    run_id: str | None,
    raw: bool,
    poll_s: float = POLL_S,
    sleep: Sleep = time.sleep,
) -> None:
    """Print the logs of a run as it goes, until it ends. Raises ``NoRuns``
    when there is no run to follow."""
    run = _wait_for_run(ui, root, run_id, poll_s=poll_s, sleep=sleep)
    printer = LogPrinter(ui, run, raw=raw)
    active = followed = is_active(run)
    printer.show_header(active=active)
    seen_any = False
    while True:
        # A task's session may not have started yet, so no error for it here.
        sessions = [
            session
            for session in run.sessions()
            if task is None or session.matches(task)
        ]
        for session in sessions:
            finished = session.entry is not None or not active
            seen_any |= printer.pump(session, final=finished, running=active)
        if not active:
            break
        sleep(poll_s)
        run.reload()
        active = is_active(run)
        if not active:
            run.reload()  # the last sessions and the outcome
    if not seen_any and task is not None:
        ui.info(f"Run {run.run_id} had no sessions for '{task}'.")
    outcome = run.manifest.outcome if run.manifest is not None else None
    if outcome is not None:
        ended = "ended" if followed else "had already ended"
        ui.info(f"Run {run.run_id} {ended}: {outcome}.")
    else:
        stopped = "stopped" if followed else "had stopped"
        ui.warning(
            f"run {run.run_id} {stopped} without finishing, and no run is going now",
            "It may have been killed. `spadework run` picks up where it stopped.",
        )


def _wait_for_run(
    ui: Ui, root: Path, run_id: str | None, *, poll_s: float, sleep: Sleep
) -> Run:
    """The run to follow. Without ``--run``, a run that has taken the lock but
    not yet written its folder is waited for."""
    if run_id is not None:
        return find_run(root, run_id)
    told = False
    while True:
        ids = run_ids(root)
        run = find_run(root, None) if ids else None
        if run is not None and (is_active(run) or not lock_held(root)):
            return run
        if run is None and not lock_held(root):
            return find_run(root, None)  # raises NoRuns
        if not told:
            ui.info("Waiting for the run to start…")
            told = True
        sleep(poll_s)
