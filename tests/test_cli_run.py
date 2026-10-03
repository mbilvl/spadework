"""``spadework run`` through the CLI, with the fake executor."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from spadework.executors.fake import research_result
from spadework.lock import LockOwner
from spadework.manifest import load_manifest
from tests.conftest import Invoke

EXAMPLE = Path(__file__).parent.parent / "examples" / "acme-fabrication"

QUICKSTART_BRIEF = """\
---
title: "Acme Fabrication — quickstart"
reader: "A developer preparing a second call with Acme Fabrication"
public_context: "A stainless-steel fabricator planning a catalogue website."
---

# The project

A catalogue website.
"""

EVENTS = {
    "plan",
    "up_to_date",
    "run_started",
    "session_started",
    "tool_call",
    "denied",
    "session_finished",
    "task_finished",
    "notice",
    "run_finished",
    "error",
}
STAMP = r"\d\d:\d\d:\d\d"


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPADEWORK_EXECUTOR", "fake")
    monkeypatch.delenv("SPADEWORK_FAKE_SCRIPT", raising=False)


@pytest.fixture
def quickstart(tmp_path: Path, fake: None) -> Path:
    root = tmp_path / "acme-quickstart"
    root.mkdir()
    (root / "spadework.toml").write_text(
        'schema_version = 1\n\n[project]\nname = "acme-quickstart"\n'
        'playbook = "quickstart"\n'
    )
    (root / "brief.md").write_text(QUICKSTART_BRIEF)
    (root / "inputs").mkdir()
    return root


def script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: Any) -> None:
    path = tmp_path / "fake-script.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("SPADEWORK_FAKE_SCRIPT", str(path))


def ndjson(stdout: str) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stdout.splitlines()]


def test_quickstart_end_to_end(cli: Invoke, quickstart: Path) -> None:
    result = cli("run", "-C", quickstart)

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "playbook  quickstart 0.1.0"
    assert "  landscape  pending          $1.15" in lines
    # Three drafting sessions, then the critic, a fix, the critic, the summary.
    assert re.fullmatch(
        r"run \S+ · complete · \$0\.42 of \$6\.00 \(simulated\) · 7 sessions · "
        r"0m\d\ds",
        lines[-6],
    )
    assert lines[-5:] == [
        "  wrote out/00-summary.md",
        "  wrote out/01-landscape.md",
        "  wrote out/02-questions.md",
        "  wrote out/pack.json",
        "  wrote out/sources.md",
    ]
    assert (
        (quickstart / "out" / "01-landscape.md").read_text().startswith("# Landscape\n")
    )
    assert (quickstart / "out" / "02-questions.md").is_file()
    progress = result.stderr.splitlines()
    assert re.fullmatch(
        rf"{STAMP} run \S+ started: 2 tasks, executor fake \(no model is called; "
        r"costs are simulated\), budget \$6\.00, max time 2h",
        progress[0],
    )
    assert any(line.endswith(", simulated)") for line in progress)
    assert re.fullmatch(
        rf"{STAMP} landscape research: started \(sonnet, cap \$1\.12, timeout 25m\)",
        progress[1],
    )
    assert any(re.fullmatch(rf"{STAMP} landscape: fresh", line) for line in progress)
    assert any(
        re.fullmatch(
            rf"{STAMP} critic: started \(opus, cap \$1\.50, timeout 25m, round 1\)",
            line,
        )
        for line in progress
    )
    assert any(
        re.fullmatch(rf"{STAMP} review round 2: the critic's verdict is ship", line)
        for line in progress
    )
    # The project is not the current folder, so the next command names it.
    assert progress[-1] == (
        "Next: read the pack in out/. "
        f"`spadework status -C {quickstart.resolve().as_posix()}` shows each task."
    )


def test_logs_show_the_review_by_kind(cli: Invoke, quickstart: Path) -> None:
    cli("run", "-q", "-C", quickstart)

    critic = cli("logs", "critic", "-C", quickstart)
    summary = cli("logs", "summary", "-C", quickstart)

    assert critic.exit_code == 0
    assert re.search(r"\n004 critic · critic · opus · round 1\n", critic.stdout)
    assert re.search(r"\n006 critic · critic · opus · round 2\n", critic.stdout)
    assert "landscape research" not in critic.stdout
    assert "· fix ·" not in critic.stdout
    assert re.search(r"\n007 summary · summary · opus\n", summary.stdout)
    assert "  Write     00-summary.md\n" in summary.stdout


def test_a_second_run_is_up_to_date(cli: Invoke, quickstart: Path) -> None:
    cli("run", "-C", quickstart)

    result = cli("run", "-C", quickstart)

    assert result.exit_code == 0
    assert result.stdout.endswith("up to date\n")
    assert "  landscape  fresh" in result.stdout


def test_json_is_ndjson_events(cli: Invoke, quickstart: Path) -> None:
    result = cli("run", "--json", "-C", quickstart)

    assert result.exit_code == 0
    events = ndjson(result.stdout)
    assert {event["event"] for event in events} <= EVENTS
    assert all(event["schema_version"] == 1 for event in events)
    assert all(
        re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", event["ts"])
        for event in events
    )
    names = [event["event"] for event in events]
    assert names[:3] == ["plan", "run_started", "session_started"]
    assert names[-1] == "run_finished"
    plan = events[0]
    assert plan["tasks"][0]["id"] == "landscape"
    assert plan["tasks"][0]["will_run"] is True
    assert plan["limits"] == {"budget_usd": 6.0, "max_time_s": 7200, "rounds": 2}
    started = events[1]
    assert started["tasks"] == ["landscape", "questions"]
    assert started["executor"] == {
        "name": "fake",
        "version": started["executor"]["version"],
        "simulated": True,
    }
    session = events[2]
    assert {key: session[key] for key in ("n", "task", "kind", "model", "attempt")} == {
        "n": 1,
        "task": "landscape",
        "kind": "research",
        "model": "sonnet",
        "attempt": 1,
    }
    tool = next(event for event in events if event["event"] == "tool_call")
    assert set(tool) == {
        "schema_version",
        "ts",
        "event",
        "n",
        "task",
        "kind",
        "name",
        "target",
    }
    finished = [event for event in events if event["event"] == "task_finished"]
    # The last is the review's fix of 01-landscape.md.
    assert [(e["task"], e["state"]) for e in finished] == [
        ("landscape", "fresh"),
        ("questions", "fresh"),
        ("landscape", "fresh"),
    ]
    assert finished[0]["validators"][0] == {"name": "max_lines", "ok": True}
    review = [
        (event["task"], event["kind"], event["round"])
        for event in events
        if event["event"] == "session_started" and event["round"]
    ]
    assert review == [
        (None, "critic", 1),
        ("landscape", "fix", 1),
        (None, "critic", 2),
    ]
    end = events[-1]
    assert end["outcome"] == "complete"
    assert end["exit_code"] == 0
    assert end["simulated"] is True
    assert end["sessions"] == 7
    assert end["rendered"] == [
        "out/00-summary.md",
        "out/01-landscape.md",
        "out/02-questions.md",
        "out/pack.json",
        "out/sources.md",
    ]
    assert end["tasks"] == {"landscape": "fresh", "questions": "fresh"}
    manifest = load_manifest(quickstart / end["manifest"])
    assert manifest.run_id == end["run_id"]
    assert result.stderr == ""


def test_json_up_to_date(cli: Invoke, quickstart: Path) -> None:
    cli("run", "-C", quickstart)

    events = ndjson(cli("run", "--json", "-C", quickstart).stdout)

    assert [event["event"] for event in events] == ["plan", "up_to_date"]
    assert events[1]["exit_code"] == 0


def test_json_errors_are_an_event(cli: Invoke, tmp_path: Path, fake: None) -> None:
    result = cli("run", "--json", "-C", tmp_path)

    assert result.exit_code == 3
    [event] = ndjson(result.stdout)
    assert event["event"] == "error"
    assert event["ok"] is False
    assert event["error"]["exit_code"] == 3
    assert result.stderr.startswith("error: there is no spadework.toml")


def test_discovery_website_on_the_example_project(
    cli: Invoke, tmp_path: Path, fake: None
) -> None:
    root = tmp_path / "acme-fabrication"
    shutil.copytree(EXAMPLE, root)

    result = cli("run", "--json", "-C", root)

    assert result.exit_code == 0, result.stdout
    end = ndjson(result.stdout)[-1]
    assert end["outcome"] == "complete"
    # 9 drafts and 3 research sessions, then the critic, a fix of
    # 01-scope.md, the critic again, and the summary.
    assert end["sessions"] == 16
    assert end["cost_usd"] < 6.0
    assert set(end["tasks"].values()) == {"fresh"}
    out = root / "out"
    documents = sorted(path.name for path in out.glob("0[1-9]-*.md"))
    assert len(documents) == 9
    assert sorted(end["rendered"]) == sorted(
        [f"out/{name}" for name in documents]
        + ["out/00-summary.md", "out/pack.json", "out/sources.md"]
    )
    for name in documents:
        text = (out / name).read_text()
        assert text.count("\n## Sources\n") == 1, name
    summary = (out / "00-summary.md").read_text()
    for section in (
        "## Executive summary",
        "## Documents",
        "## Source verification",
        "## How this pack was produced",
    ):
        assert f"\n{section}\n" in summary
    assert "| 06-tech-stack.md | 5 | 0 | 0 | 5 | 0 | 0 |" in summary
    sources = (out / "sources.md").read_text()
    assert sources.count("\n### S") == 15
    pack = json.loads((out / "pack.json").read_text())
    assert [d["file"] for d in pack["documents"]] == documents
    assert {d["state"] for d in pack["documents"]} == {"fresh"}
    assert pack["versions"]["playbook"] == "discovery-website@0.1.0"
    assert "scope:D1" in {decision["id"] for decision in pack["decisions"]}
    assert len(pack["sources"]) == 15
    assert len(pack["consolidated_questions"]) == 9


def test_discovery_website_reports_an_invalid_task(
    cli: Invoke, tmp_path: Path, fake: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "acme-fabrication"
    shutil.copytree(EXAMPLE, root)
    # references needs four sources; a repair cannot add them.
    three = research_result("references")
    three["sources"] = three["sources"][:3]
    script(tmp_path, monkeypatch, {"references:research": {"structured": three}})

    result = cli("run", "-C", root)

    assert result.exit_code == 4
    assert "· partial ·" in result.stdout
    assert "warning: 1 of 9 tasks is not done: references (invalid)" in result.stderr
    assert (root / "out" / "03-references.md").is_file()


def test_budget_stop_exits_5(cli: Invoke, quickstart: Path) -> None:
    result = cli("run", "--budget-usd", "1.2", "-C", quickstart)

    # landscape research: 0.75 + 0.50 reserve is over $1.20.
    assert result.exit_code == 5
    assert "· budget ·" in result.stdout
    assert "warning: stopped: the run budget ($1.20) cannot cover the next" in (
        result.stderr
    )
    [manifest] = (quickstart / ".spadework" / "runs").glob("*/manifest.json")
    assert load_manifest(manifest).outcome == "budget"


def test_rate_limit_exits_6_and_says_when_to_resume(
    cli: Invoke, quickstart: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    reset = dt.datetime(2030, 1, 2, 3, 4, tzinfo=dt.UTC)
    script(
        tmp_path,
        monkeypatch,
        {"landscape:draft": {"fail": "rate_limit", "resets_at": reset.isoformat()}},
    )

    result = cli("run", "-C", quickstart)

    assert result.exit_code == 6
    local = reset.astimezone().strftime("%H:%M")
    assert (
        f"warning: stopped: the usage limit was reached; resume after {local}"
        in result.stderr
    )


def test_an_interrupt_exits_130(
    cli: Invoke, quickstart: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script(tmp_path, monkeypatch, {"questions": {"fail": "interrupted"}})

    result = cli("run", "-C", quickstart)

    assert result.exit_code == 130
    assert "\ninterrupted\n" in result.stderr
    [manifest] = (quickstart / ".spadework" / "runs").glob("*/manifest.json")
    assert load_manifest(manifest).outcome == "interrupted"


def test_a_held_lock_exits_7(cli: Invoke, quickstart: Path) -> None:
    lock = quickstart / ".spadework" / "lock"
    lock.parent.mkdir()
    lock.write_bytes(
        LockOwner(os.getppid(), socket.gethostname(), "2026-09-27T21:04:11Z").to_bytes()
    )

    result = cli("run", "-C", quickstart)

    assert result.exit_code == 7
    assert "error: another run holds the project lock" in result.stderr


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--only", "nope"], "error: --only nope: there is no task 'nope'"),
        (["--only", "questions"], "its dependency 'landscape' has never produced"),
        (["--max-time", "soon"], "error: --max-time: 'soon' is not a duration"),
        (["--rounds", "-1"], "Invalid value for '--rounds'"),
    ],
)
def test_usage_errors_exit_2(
    cli: Invoke, quickstart: Path, args: list[str], message: str
) -> None:
    result = cli("run", *args, "-C", quickstart)

    assert result.exit_code == 2
    assert message in result.stderr


def test_an_unknown_executor_exits_2(
    cli: Invoke, quickstart: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPADEWORK_EXECUTOR", "nope")

    result = cli("run", "-C", quickstart)

    assert result.exit_code == 2
    assert "error: unknown executor 'nope'" in result.stderr


def test_an_invalid_project_exits_3_without_a_run(
    cli: Invoke, quickstart: Path
) -> None:
    (quickstart / "brief.md").write_text("---\ntitle: x\n---\n")

    result = cli("run", "-C", quickstart)

    assert result.exit_code == 3
    assert not (quickstart / ".spadework").exists()


def test_quiet_prints_only_results_and_warnings(cli: Invoke, quickstart: Path) -> None:
    result = cli("run", "-q", "-C", quickstart)

    assert result.exit_code == 0
    assert result.stderr == ""


def test_a_killed_run_resumes_without_redrafting(cli: Invoke, quickstart: Path) -> None:
    # A real process, killed without cleanup during its third session: no
    # finally blocks run, and the lock is left behind.
    code = f"""
import os, sys
from spadework.executors.fake import FakeExecutor

calls = []
original = FakeExecutor.run

def run(self, spec, events):
    calls.append(spec.task)
    if len(calls) == 3:
        os._exit(137)
    return original(self, spec, events)

FakeExecutor.run = run
from spadework.cli import app
sys.argv = ["spadework", "run", "-q", "-C", {str(quickstart)!r}]
app()
"""
    env = {**os.environ, "SPADEWORK_EXECUTOR": "fake"}
    killed = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, check=False
    )
    assert killed.returncode == 137
    assert (quickstart / ".spadework" / "lock").exists()

    status = cli("status", "-C", quickstart)
    assert "· did not finish ·" in status.stdout
    assert "  landscape  fresh" in status.stdout
    assert "  questions  pending" in status.stdout

    resumed = cli("run", "--json", "-C", quickstart)

    assert resumed.exit_code == 0
    events = ndjson(resumed.stdout)
    sessions = [e for e in events if e["event"] == "session_started"]
    assert [(e["task"], e["kind"]) for e in sessions] == [
        ("questions", "draft"),
        (None, "critic"),
        ("landscape", "fix"),
        (None, "critic"),
        (None, "summary"),
    ]
    notices = [e["message"] for e in events if e["event"] == "notice"]
    assert any("reclaimed the project lock" in message for message in notices)


# Regressions from the engine review.

SLOW_FIRST_SESSION = """
import sys, time
from spadework.executors.fake import FakeExecutor

original = FakeExecutor.run
calls = []

def run(self, spec, events):
    calls.append(spec.task)
    if len(calls) == 1:
        time.sleep(0.5)  # `head` reads its lines and exits meanwhile
    return original(self, spec, events)

FakeExecutor.run = run
from spadework.cli import app
sys.argv = ["spadework", *sys.argv[1:]]
app()
"""


@pytest.mark.parametrize("json_output", [True, False])
def test_a_reader_that_goes_away_does_not_change_the_exit_code(
    quickstart: Path, tmp_path: Path, json_output: bool
) -> None:
    # A real pipe: `head` exits after two lines, and later writes fail.
    wrapper = tmp_path / "spadework_slow.py"
    wrapper.write_text(SLOW_FIRST_SESSION)
    args = ["run", "-C", str(quickstart)] + (["--json"] if json_output else [])
    command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(wrapper))} "
        f"{' '.join(shlex.quote(arg) for arg in args)} 2>{tmp_path / 'err'}"
        " | head -n 2 >/dev/null; echo ${PIPESTATUS[0]}"
    )
    env = {**os.environ, "SPADEWORK_EXECUTOR": "fake"}

    status = subprocess.run(
        ["bash", "-c", command], env=env, capture_output=True, text=True, check=True
    )

    [path] = (quickstart / ".spadework" / "runs").glob("*/manifest.json")
    manifest = load_manifest(path)
    assert manifest.outcome == "complete"
    assert len(manifest.sessions) == 7
    assert int(status.stdout) == manifest.exit_code == 0
    errors = (tmp_path / "err").read_text()
    assert "Exception ignored" not in errors
    assert "BrokenPipeError" not in errors
    assert (quickstart / "out" / "02-questions.md").is_file()


@pytest.mark.parametrize(
    ("raised", "message", "code"),
    [
        (RuntimeError("boom"), "unexpected error: RuntimeError: boom", 1),
        (
            PermissionError(13, "Permission denied", "brief.md"),
            "cannot use brief.md: Permission denied",
            1,
        ),
    ],
)
def test_json_reports_any_failure_before_the_run_as_an_error_event(
    cli: Invoke,
    quickstart: Path,
    monkeypatch: pytest.MonkeyPatch,
    raised: Exception,
    message: str,
    code: int,
) -> None:
    def fail(root: Path) -> None:
        raise raised

    monkeypatch.setattr("spadework.engine.run.load_project", fail)
    monkeypatch.chdir(quickstart)

    result = cli("run", "--json")

    assert result.exit_code == code
    [event] = ndjson(result.stdout)
    assert event["event"] == "error"
    assert event["ok"] is False
    assert event["error"]["message"] == message
    assert event["error"]["exit_code"] == code


def test_force_only_replaces_a_persons_edits_end_to_end(
    cli: Invoke, quickstart: Path
) -> None:
    assert cli("run", "-q", "-C", quickstart).exit_code == 0
    path = quickstart / "out" / "01-landscape.md"
    path.write_text("# Landscape\n\nMy own notes.\n")
    assert "  landscape  pinned" in cli("status", "-C", quickstart).stdout

    # Without --force, the edits stand and nothing runs.
    kept = cli("run", "--only", "landscape", "-C", quickstart)
    assert kept.exit_code == 0
    assert kept.stdout.endswith("up to date\n")
    assert "warning: task 'landscape' is pinned" in kept.stderr
    assert path.read_text() == "# Landscape\n\nMy own notes.\n"

    replaced = cli("run", "--force", "--only", "landscape", "--json", "-C", quickstart)

    assert replaced.exit_code == 0
    events = ndjson(replaced.stdout)
    assert events[0]["warnings"] == [
        {"message": "--force replaces your edits to out/01-landscape.md", "hint": None}
    ]
    sessions = [
        (e["task"], e["kind"]) for e in events if e["event"] == "session_started"
    ]
    # The research is reused; the review sees the new document.
    assert sessions[0] == ("landscape", "draft")
    assert (None, "summary") in sessions
    end = events[-1]
    assert end["tasks"]["landscape"] == "fresh"
    assert "out/01-landscape.md" in end["rendered"]
    text = path.read_text()
    assert "My own notes." not in text
    assert text.startswith("# Landscape\n")
    assert "\n## Sources\n" in text
    pack = json.loads((quickstart / "out" / "pack.json").read_text())
    assert pack["documents"][0]["state"] == "fresh"
    summary = (quickstart / "out" / "00-summary.md").read_text()
    assert (
        "| [01-landscape.md](01-landscape.md): Landscape of comparables | fresh |"
        in summary
    )
    assert "edited by a person" not in summary
    assert "  landscape  fresh" in cli("status", "-C", quickstart).stdout
    assert cli("run", "-C", quickstart).stdout.endswith("up to date\n")


def test_clean_cache_makes_the_next_run_research_again(
    cli: Invoke, quickstart: Path
) -> None:
    assert cli("run", "-q", "-C", quickstart).exit_code == 0
    assert cli("clean", "--cache", "--yes", "-C", quickstart).exit_code == 0
    assert not (
        quickstart / ".spadework" / "records" / "landscape.research.json"
    ).exists()

    check = cli("check", "--json", "-C", quickstart)

    tasks = {t["id"]: t for t in json.loads(check.stdout)["tasks"]}
    assert (tasks["landscape"]["state"], tasks["landscape"]["reason"]) == (
        "stale",
        "research record missing",
    )
    # Its research is estimated again: there is nothing to reuse.
    assert tasks["landscape"]["research_reused"] is False
    assert tasks["landscape"]["estimate_usd"] == 1.15
    assert tasks["questions"]["state"] == "fresh"
    text = cli("check", "-C", quickstart).stdout
    assert re.search(r"\n  landscape +stale +research record missing +\$1\.15\n", text)

    ran = cli("run", "--json", "-C", quickstart)

    assert ran.exit_code == 0
    sessions = [
        (e["task"], e["kind"])
        for e in ndjson(ran.stdout)
        if e["event"] == "session_started"
    ]
    assert sessions[:2] == [("landscape", "research"), ("landscape", "draft")]
    assert (quickstart / ".spadework" / "records" / "landscape.research.json").is_file()
    assert cli("run", "-C", quickstart).stdout.endswith("up to date\n")
