"""How run's progress is shown: plain lines, the live view, and NDJSON."""

from __future__ import annotations

import io
import json
import re

import pytest
from rich.console import Console

from spadework.console import Ui
from spadework.engine import events as ev
from spadework.engine.display import (
    LinesDisplay,
    LiveDisplay,
    LiveView,
    NdjsonDisplay,
    _Progress,
    elapsed,
)
from spadework.plan import build_plan
from spadework.project import load_project
from tests.conftest import MakeProject

STARTED = ev.RunStarted(
    run_id="20260927T210411Z-3f9a",
    project="acme-fabrication",
    playbook="discovery-website",
    playbook_version="0.1.0",
    executor="fake",
    executor_version="0.1.0",
    budget_usd=6.0,
    max_time_s=7200,
    rounds=2,
    tasks=("scope", "references", "ia"),
)
SESSION = ev.SessionStarted(
    n=2,
    task="references",
    kind="research",
    model="sonnet",
    round=0,
    attempt=1,
    max_budget_usd=1.125,
    timeout_s=1500,
)
DENIED = ev.ToolDenied(
    n=2,
    task="references",
    kind="research",
    name="WebFetch",
    target="http://127.0.0.1/",
    reason="private address",
    possible_injection=True,
)


def ui_with_buffers() -> tuple[Ui, io.StringIO, io.StringIO]:
    ui = Ui(color=False)
    out, err = io.StringIO(), io.StringIO()
    ui.__dict__["out"] = Console(file=out, width=100, no_color=True, highlight=False)
    ui.__dict__["err"] = Console(file=err, width=100, no_color=True, highlight=False)
    return ui, out, err


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(0, "0m00s"), (58.9, "0m58s"), (72, "1m12s"), (3720, "1h02m"), (-1, "0m00s")],
)
def test_elapsed(seconds: float, text: str) -> None:
    assert elapsed(seconds) == text


def test_lines_are_timestamped_one_per_event() -> None:
    ui, _, err = ui_with_buffers()
    display = LinesDisplay(ui)

    for event in [
        STARTED,
        SESSION,
        ev.ToolUsed(2, "references", "research", "WebSearch", "peers in Portugal"),
        DENIED,
        ev.SessionFinished(
            n=2,
            task="references",
            kind="research",
            status="error",
            cost_usd=0.05,
            duration_s=61,
            guard_denials=1,
            spent_usd=0.26,
            error="Claude Code exited without a result",
            retry=True,
        ),
        ev.TaskFinished("references", "failed", "research failed"),
        ev.Notice("reclaimed the project lock", "a hint"),
        ev.Notice("waiting", level="info"),
    ]:
        display(event)

    lines = err.getvalue().splitlines()
    assert all(re.match(r"\d\d:\d\d:\d\d ", line) for line in lines)
    assert [line[9:] for line in lines] == [
        "run 20260927T210411Z-3f9a started: 3 tasks, budget $6.00, max time 2h",
        "references research: started (sonnet, cap $1.12, timeout 25m)",
        "references research: WebSearch peers in Portugal",
        "warning: references research: the sandbox blocked WebFetch "
        "http://127.0.0.1/: private address (possible prompt injection encountered)",
        "references research: error, $0.05, 1m01s (spent $0.26 of $6.00): Claude "
        "Code exited without a result; trying again",
        "references: failed: research failed",
        "warning: reclaimed the project lock",
        "a hint",
        "waiting",
    ]


def test_quiet_lines_keep_only_warnings() -> None:
    ui, _, err = ui_with_buffers()
    ui.quiet = True
    display = LinesDisplay(ui)

    for event in [STARTED, SESSION, DENIED, ev.Notice("careful")]:
        display(event)

    lines = [line[9:] for line in err.getvalue().splitlines()]
    assert lines == [
        "warning: references research: the sandbox blocked WebFetch "
        "http://127.0.0.1/: private address (possible prompt injection encountered)",
        "warning: careful",
    ]


def test_the_live_view() -> None:
    progress = _Progress()
    for event in [
        STARTED,
        ev.SessionStarted(1, "scope", "draft", "sonnet", 0, 1, 1.5, 1500),
        ev.SessionFinished(1, "scope", "draft", "ok", 0.21, 72, 0, 0.21),
        ev.TaskFinished("scope", "fresh"),
        SESSION,
        ev.ToolUsed(2, "references", "research", "WebFetch", "peer-site.example"),
        DENIED,
        DENIED,
    ]:
        progress.update(event)
    console = Console(width=140, record=True, no_color=True)

    console.print(LiveView(progress))

    text = console.export_text()
    lines = [line.rstrip() for line in text.splitlines()]
    assert lines[0] == "spadework · acme-fabrication · discovery-website 0.1.0"
    assert re.fullmatch(r"✓  scope\s+draft\s+sonnet\s+\$0\.21\s+1m12s", lines[2]), (
        lines[2]
    )
    assert re.fullmatch(
        r"▸  references\s+research\s+sonnet\s+\$0\.00\s+0m\d\ds\s+→ WebFetch "
        r"peer-site\.example",
        lines[3],
    ), lines[3]
    assert re.fullmatch(r"·  ia\s+pending", lines[4])
    assert lines[5] == (
        "⚠ references  possible prompt injection: 2 tool calls blocked by the "
        "sandbox (see: spadework logs references)"
    )
    assert re.fullmatch(r"spent \$0\.21 of \$6\.00 · 0m\d\ds of 2h", lines[7])


def test_the_live_display(make_project: MakeProject) -> None:
    ui, out, err = ui_with_buffers()
    ui.__dict__["err"] = Console(
        file=err, width=120, force_terminal=True, no_color=True, highlight=False
    )
    display = LiveDisplay(ui)
    plan = build_plan(load_project(make_project()))

    display(ev.PlanReady(plan, (("pinned", "a hint"),)))
    display(STARTED)
    assert display.live is not None
    display(SESSION)
    display(DENIED)
    display(ev.Notice("reclaimed a lock"))
    display(ev.Notice("waiting until 22:15", level="info"))
    display(
        ev.RunFinished(
            run_id=STARTED.run_id,
            outcome="rate_limited",
            exit_code=6,
            cost_usd=0.26,
            budget_usd=6.0,
            sessions=2,
            guard_denials=1,
            duration_s=75,
            max_time_s=7200,
            message="the usage limit was reached",
            hint="Run again later.",
            resume_after="22:15",
        )
    )

    assert display.live is None
    assert out.getvalue().splitlines()[0] == "playbook  fixture 1.0.0"
    assert out.getvalue().endswith(
        f"run {STARTED.run_id} · rate_limited · $0.26 of $6.00 · 2 sessions · 1m15s\n"
    )
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", err.getvalue())
    assert "warning: pinned" in text
    assert "warning: reclaimed a lock" in text
    assert "waiting until 22:15" in text
    assert "references  research  sonnet" in text
    assert "warning: the sandbox blocked 1 tool call in this run" in text
    assert "warning: stopped: the usage limit was reached; resume after 22:15" in text


def test_the_live_display_says_up_to_date() -> None:
    ui, out, _ = ui_with_buffers()
    display = LiveDisplay(ui)

    display(ev.UpToDate(4, ("alpha",)))
    display.close()

    assert out.getvalue() == "up to date\n"


def test_ndjson() -> None:
    stream = io.StringIO()
    display = NdjsonDisplay(stream)

    display(DENIED)
    display.error({"schema_version": 1, "ok": False, "error": {"exit_code": 7}})

    first, second = (json.loads(line) for line in stream.getvalue().splitlines())
    assert first == {
        "schema_version": 1,
        "ts": first["ts"],
        "event": "denied",
        "n": 2,
        "task": "references",
        "kind": "research",
        "name": "WebFetch",
        "target": "http://127.0.0.1/",
        "reason": "private address",
        "possible_injection": True,
    }
    assert second["event"] == "error"
    assert second["error"] == {"exit_code": 7}


def test_a_rate_limit_without_a_reset_time_prints_no_resume_time() -> None:
    ui, _, err = ui_with_buffers()
    display = LinesDisplay(ui)

    display(
        ev.RunFinished(
            run_id=STARTED.run_id,
            outcome="rate_limited",
            exit_code=6,
            cost_usd=0.0,
            budget_usd=6.0,
            sessions=1,
            guard_denials=0,
            duration_s=1,
            max_time_s=7200,
            message="the usage limit was reached",
            hint="Its reset time is not known.",
        )
    )

    assert err.getvalue() == (
        "warning: stopped: the usage limit was reached\nIts reset time is not known.\n"
    )


class ClosedPipe(io.StringIO):
    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")


def test_ndjson_stops_quietly_when_the_reader_goes_away() -> None:
    display = NdjsonDisplay(ClosedPipe())

    display(DENIED)
    display(DENIED)

    assert display.closed


def test_a_console_whose_reader_went_away_goes_quiet() -> None:
    console = Ui(color=False).out
    console.file = ClosedPipe()

    console.print("still running")  # Rich's default would raise SystemExit
    console.print("and still")

    assert console.quiet
