"""``spadework logs``: session traces, replayed through the executor's parser."""

from __future__ import annotations

import json
import os
import shutil
import socket
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from spadework.console import Ui
from spadework.executors.base import SessionKind, SessionSpec, ignore_events
from spadework.executors.fake import FakeBehaviour, FakeExecutor
from spadework.lock import LockOwner
from spadework.logs import (
    Replay,
    Run,
    follow_logs,
    show_logs,
)
from spadework.manifest import RunManifest, SessionEntry, write_manifest
from spadework.records import output_schema
from spadework.sandbox.tools import TOOLS_BY_KIND
from tests.conftest import Invoke

STREAM = Path(__file__).parent / "fixtures" / "stream"
SNAPSHOTS = Path(__file__).parent / "snapshots" / "help"

RUN_1 = "20260927T210411Z-3f9a"
RUN_2 = "20260928T090000Z-0b1c"


def entry(n: int, task: str | None, kind: str, **changes: Any) -> dict[str, Any]:
    name = kind if task is None else f"{task}-{kind}"
    return {
        "n": n,
        "task": task,
        "kind": kind,
        "round": 0,
        "model": "sonnet",
        "key": None,
        "status": "ok",
        "cost_usd": 0.21,
        "duration_s": 72.4,
        "tool_calls": {"Read": 1, "Glob": 1, "Write": 2},
        "guard_denials": 1,
        "log": f"sessions/{n:03d}-{name}.jsonl",
        **changes,
    }


def write_run(
    root: Path,
    run_id: str,
    sessions: list[dict[str, Any]],
    *,
    outcome: str | None = "complete",
) -> RunManifest:
    manifest = RunManifest.model_validate(
        {
            "run_id": run_id,
            "spadework_version": "0.1.0",
            "executor": {"name": "claude-cli", "version": "2.1.278"},
            "playbook": {"name": "discovery-website", "version": "0.1.0"},
            "limits": {"budget_usd": 6.0, "max_time_s": 7200, "rounds": 2},
            "started_at": "2026-09-27T21:04:11Z",
        }
    )
    for session in sessions:
        manifest.add_session(SessionEntry.model_validate(session))
    if outcome is not None:
        manifest.finish(outcome, ended_at="2026-09-27T21:47:02Z")  # type: ignore[arg-type]
    write_manifest(root / ".spadework", manifest)
    return manifest


def claude_log(root: Path, run_id: str, log: str, name: str) -> Path:
    """A recorded claude-cli stream-json log from the fixtures."""
    path = root / ".spadework" / "runs" / run_id / log
    path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(STREAM / name, path)
    return path


def fake_log(
    root: Path, run_id: str, log: str, behaviour: FakeBehaviour | None = None
) -> Path:
    """A log written by the fake executor itself."""
    path = root / ".spadework" / "runs" / run_id / log
    workdir = root / ".spadework" / "sessions" / "fake"
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "brief.md").write_text("# Brief\n", encoding="utf-8")
    spec = SessionSpec(
        session_id="002-audience-draft",
        kind=SessionKind.DRAFT,
        prompt="Write the audience document.",
        system_append="",
        model="sonnet",
        tools=TOOLS_BY_KIND["draft"],
        workdir=workdir,
        policy_path=root / ".spadework" / "control" / "p.json",
        output_schema=output_schema("draft"),
        max_budget_usd=1.0,
        timeout_s=60,
        log_path=path,
        output=workdir / "02-audience.md",
        task="audience",
    )
    script = {"*": behaviour} if behaviour is not None else None
    FakeExecutor(script).run(spec, ignore_events)
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "acme-fabrication"
    root.mkdir()
    (root / "spadework.toml").write_text("schema_version = 1\n", encoding="utf-8")
    return root


@pytest.fixture
def one_run(project: Path) -> Path:
    """A finished run: a claude-cli draft with a denial, then a fake draft."""
    write_run(
        project,
        RUN_1,
        [
            entry(1, "scope", "draft"),
            entry(
                2,
                "audience",
                "draft",
                cost_usd=0.08,
                duration_s=1.0,
                tool_calls={"Read": 1, "Write": 1},
                guard_denials=0,
            ),
        ],
    )
    claude_log(
        project,
        RUN_1,
        "sessions/001-scope-draft.jsonl",
        "write-ok-with-denial.mock.jsonl",
    )
    fake_log(project, RUN_1, "sessions/002-audience-draft.jsonl")
    return project


def hold_lock(root: Path) -> Path:
    path = root / ".spadework" / "lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = LockOwner(os.getpid(), socket.gethostname(), "2026-09-27T21:04:11Z")
    path.write_bytes(owner.to_bytes())
    return path


# --- Showing a run ------------------------------------------------------------


def test_shows_every_session_of_the_last_run(cli: Invoke, one_run: Path) -> None:
    result = cli("logs", "-C", one_run)

    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.startswith(
        f"run {RUN_1} · complete · $0.29 · 2 sessions · 1 denied call\n"
    )
    assert "\n001 scope · draft · sonnet\n" in out
    assert '  "Reading the brief."\n' in out
    assert "  Read      brief.md\n" in out
    assert "  Glob      *.md\n" in out
    assert "  Write     out.md\n" in out
    assert (
        "  ✗ denied Write notes.md: spadework guard: writing /WORKDIR/notes.md is "
        "not allowed; the only writable file is ['/WORKDIR/out.md']\n"
    ) in out
    assert "  ok · $0.21 · 1m12s · 4 tool calls · 1 denied\n" in out
    # The fake executor's own log, read back into the same events.
    assert "\n002 audience · draft · sonnet\n" in out
    assert "  Read      brief.md\n  Write     02-audience.md\n" in out
    assert "  ok · $0.08 · 1s · 2 tool calls\n" in out
    assert result.stderr == ""


def test_the_order_of_a_trace_follows_the_log(cli: Invoke, one_run: Path) -> None:
    out = cli("logs", "scope", "-C", one_run).stdout

    lines = [line.strip() for line in out.splitlines()]
    trace = lines[lines.index("001 scope · draft · sonnet") + 1 :]
    assert [line.split()[0] for line in trace[:7]] == [
        '"Reading',
        "Read",
        "Glob",
        "Write",
        "Write",
        "✗",
        "ok",
    ]


def test_filters_by_task(cli: Invoke, one_run: Path) -> None:
    result = cli("logs", "audience", "-C", one_run)

    assert result.exit_code == 0
    assert "002 audience" in result.stdout
    assert "001 scope" not in result.stdout


def test_an_unknown_task_is_a_usage_error(cli: Invoke, one_run: Path) -> None:
    result = cli("logs", "pricing", "-C", one_run)

    assert result.exit_code == 2
    assert result.stderr == (
        f"error: run {RUN_1} has no sessions for 'pricing'\n"
        "Its sessions are for: audience, scope.\n"
    )


def test_critic_and_summary_are_named_by_their_kind(cli: Invoke, project: Path) -> None:
    write_run(
        project,
        RUN_1,
        [entry(1, "scope", "draft"), entry(2, None, "critic", round=1)],
    )
    for n, name in ((1, "scope-draft"), (2, "critic")):
        claude_log(
            project,
            RUN_1,
            f"sessions/{n:03d}-{name}.jsonl",
            "write-ok-with-denial.mock.jsonl",
        )

    out = cli("logs", "critic", "-C", project).stdout

    assert "002 critic · critic · sonnet · round 1\n" in out
    assert "001 scope" not in out


def test_picks_a_run_by_id_or_by_a_unique_prefix(cli: Invoke, one_run: Path) -> None:
    write_run(one_run, RUN_2, [])

    assert cli("logs", "-C", one_run).stdout.startswith(f"run {RUN_2} ")
    assert cli("logs", "--run", RUN_1, "-C", one_run).stdout.startswith(f"run {RUN_1} ")
    assert cli("logs", "--run", "20260927", "-C", one_run).stdout.startswith(
        f"run {RUN_1} "
    )


def test_an_unknown_run_is_a_usage_error(cli: Invoke, one_run: Path) -> None:
    write_run(one_run, RUN_2, [])

    unknown = cli("logs", "--run", "20250101T000000Z-0000", "-C", one_run)
    ambiguous = cli("logs", "--run", "2026", "-C", one_run)

    assert unknown.exit_code == 2
    assert unknown.stderr == (
        "error: there is no run called '20250101T000000Z-0000'\n"
        f"The most recent runs are: {RUN_2}, {RUN_1}. "
        "Leave out --run for the last one.\n"
    )
    assert ambiguous.exit_code == 2
    assert "there is more than one run called '2026'" in ambiguous.stderr


def test_a_run_with_no_sessions_says_so(cli: Invoke, project: Path) -> None:
    write_run(project, RUN_1, [])

    result = cli("logs", "-C", project)

    assert result.exit_code == 0
    assert result.stdout == f"run {RUN_1} · complete · $0.00 · 0 sessions\n"
    assert result.stderr == f"Run {RUN_1} has no sessions yet.\n"


def test_a_project_with_no_runs(cli: Invoke, project: Path) -> None:
    result = cli("logs", "-C", project)

    assert result.exit_code == 1
    assert result.stderr == (
        "error: this project has no runs yet\n"
        "Start one with `spadework run`. `spadework check` shows what it will do.\n"
    )


def test_a_folder_that_is_not_a_project(cli: Invoke, tmp_path: Path) -> None:
    result = cli("logs", "-C", tmp_path)

    assert result.exit_code == 3
    assert result.stderr.startswith(f"error: there is no spadework.toml in {tmp_path}")


def test_a_missing_log_is_a_warning(cli: Invoke, one_run: Path) -> None:
    (
        one_run / ".spadework" / "runs" / RUN_1 / "sessions" / "001-scope-draft.jsonl"
    ).unlink()

    result = cli("logs", "-C", one_run)

    assert result.exit_code == 0
    assert result.stderr == (
        "warning: the log of session 001 scope · draft · sonnet is missing "
        "(sessions/001-scope-draft.jsonl)\n"
        "It may have been removed by `spadework clean`.\n"
    )
    assert "002 audience" in result.stdout


def test_no_logs_at_all_is_an_error(cli: Invoke, one_run: Path) -> None:
    shutil.rmtree(one_run / ".spadework" / "runs" / RUN_1 / "sessions")

    result = cli("logs", "-C", one_run)

    assert result.exit_code == 1
    assert "error: none of the session logs of this run exist\n" in result.stderr


def test_raw_prints_the_logs_as_they_are(cli: Invoke, one_run: Path) -> None:
    result = cli("logs", "scope", "--raw", "-C", one_run)

    assert result.exit_code == 0
    expected = (STREAM / "write-ok-with-denial.mock.jsonl").read_text(encoding="utf-8")
    assert result.stdout == expected
    assert result.stderr == (
        f"run {RUN_1} · complete · $0.29 · 2 sessions · 1 denied call\n"
        "==> sessions/001-scope-draft.jsonl <==\n"
    )
    for line in result.stdout.splitlines():
        json.loads(line)


def test_quiet_raw_prints_only_the_logs(cli: Invoke, one_run: Path) -> None:
    result = cli("logs", "--raw", "-q", "-C", one_run)

    assert result.stderr == ""
    assert len(result.stdout.splitlines()) > 20


def test_failed_sessions_show_their_error(cli: Invoke, project: Path) -> None:
    write_run(
        project,
        RUN_1,
        [
            entry(
                1,
                "scope",
                "draft",
                status="budget",
                error="Reached maximum budget ($0.03)",
                guard_denials=0,
            )
        ],
        outcome="budget",
    )
    claude_log(
        project, RUN_1, "sessions/001-scope-draft.jsonl", "max-budget.mock.jsonl"
    )

    out = cli("logs", "-C", project).stdout

    assert "  budget · $0.21 · 1m12s · 4 tool calls\n" in out
    assert "  Reached maximum budget ($0.03)\n" in out


def test_a_session_the_manifest_does_not_list_is_found_by_its_log(
    cli: Invoke, project: Path
) -> None:
    write_run(project, RUN_1, [], outcome=None)
    claude_log(
        project,
        RUN_1,
        "sessions/001-scope-draft.jsonl",
        "write-ok-with-denial.mock.jsonl",
    )

    result = cli("logs", "-C", project)

    assert result.stdout.startswith(f"run {RUN_1} · did not finish · $0.00")
    assert "\n001 scope · draft\n" in result.stdout
    # The log's own result event says how it ended.
    assert "  ok · $0.01 · 0s · 4 tool calls · 1 denied\n" in result.stdout


def test_a_running_session_is_marked_as_such(cli: Invoke, project: Path) -> None:
    write_run(project, RUN_1, [], outcome=None)
    log = claude_log(
        project,
        RUN_1,
        "sessions/001-scope-draft.jsonl",
        "write-ok-with-denial.mock.jsonl",
    )
    lines = log.read_text(encoding="utf-8").splitlines(keepends=True)
    log.write_text("".join(lines[:8]), encoding="utf-8")
    hold_lock(project)

    result = cli("logs", "-C", project)

    assert result.stdout.startswith(f"run {RUN_1} · running · ")
    assert result.stdout.endswith("  Glob      *.md\n  still running\n")
    assert result.stderr == (
        "The run is still going. Follow it with: spadework logs --follow\n"
    )


# --- Replaying ----------------------------------------------------------------


def test_replay_shows_paths_relative_to_the_session_folder() -> None:
    events: list[Any] = []
    replay = Replay(events.append)
    for line in (STREAM / "write-ok-with-denial.mock.jsonl").read_text().splitlines():
        replay.feed(line)

    summary = replay.summary()

    assert summary is not None
    assert summary.status == "ok"
    assert summary.guard_denials == 1
    targets = [getattr(event, "target", None) for event in events]
    assert "brief.md" in targets
    assert "/WORKDIR/brief.md" not in targets


def test_replay_ignores_lines_that_are_not_json() -> None:
    events: list[Any] = []
    replay = Replay(events.append)
    replay.feed("not json\n")
    replay.feed("[1, 2]\n")

    assert events == []
    assert replay.summary() is None


def test_fake_failures_are_shown(tmp_path: Path) -> None:
    log = fake_log(tmp_path, RUN_1, "s.jsonl", FakeBehaviour(fail="rate_limit"))
    events: list[Any] = []
    replay = Replay(events.append)
    for line in log.read_text().splitlines():
        replay.feed(line)

    summary = replay.summary()

    assert summary is not None
    assert summary.status == "rate_limited"
    assert [type(event).__name__ for event in events] == ["RateLimit"]
    assert events[0].resets_at is not None


# --- Following a run ------------------------------------------------------------


class Printed:
    """A Ui whose output a test can read as it goes."""

    def __init__(self, capsys: pytest.CaptureFixture[str]) -> None:
        self.capsys = capsys
        self.out: list[str] = []
        self.err: list[str] = []

    def take(self) -> tuple[str, str]:
        captured = self.capsys.readouterr()
        self.out.append(captured.out)
        self.err.append(captured.err)
        return captured.out, captured.err


def test_follow_prints_a_session_as_it_grows_until_the_run_ends(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(project, RUN_1, [], outcome=None)
    lock = hold_lock(project)
    source = (
        (STREAM / "write-ok-with-denial.mock.jsonl")
        .read_text()
        .splitlines(keepends=True)
    )
    log = project / ".spadework" / "runs" / RUN_1 / "sessions" / "001-scope-draft.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("".join(source[:3]) + source[3][:20], encoding="utf-8")
    printed = Printed(capsys)
    steps: list[Callable[[], None]] = [
        lambda: log.write_text("".join(source[:12]), encoding="utf-8"),
        lambda: log.write_text("".join(source), encoding="utf-8"),
        lambda: (
            write_run(project, RUN_1, [entry(1, "scope", "draft")], outcome=None),
            None,
        )[1],
        lambda: (
            write_run(project, RUN_1, [entry(1, "scope", "draft")]),
            lock.unlink(),
        )[1],
    ]

    def sleep(seconds: float) -> None:
        printed.take()
        steps.pop(0)()

    follow_logs(Ui(), project, task=None, run_id=None, raw=False, sleep=sleep)
    printed.take()

    out = "".join(printed.out)
    assert printed.out[0] == (
        f"run {RUN_1} · running · $0.00 · 0 sessions\n\n001 scope · draft\n"
        '  "Reading the brief."\n'
        "  Read      brief.md\n"
    )
    assert "  Glob      *.md\n" in printed.out[1]
    assert "✗ denied Write notes.md" in printed.out[2]
    assert out.count("001 scope") == 1
    assert out.endswith("  ok · $0.21 · 1m12s · 4 tool calls · 1 denied\n")
    assert "".join(printed.err).endswith(f"Run {RUN_1} ended: complete.\n")
    assert steps == []


def test_follow_waits_for_a_run_to_start(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    lock = hold_lock(project)
    printed = Printed(capsys)
    steps: list[Callable[[], None]] = [
        lambda: None,
        lambda: (write_run(project, RUN_1, [], outcome=None), None)[1],
        lambda: (write_run(project, RUN_1, []), lock.unlink())[1],
    ]

    def sleep(seconds: float) -> None:
        printed.take()
        steps.pop(0)()

    follow_logs(Ui(), project, task=None, run_id=None, raw=False, sleep=sleep)
    printed.take()

    err = "".join(printed.err)
    assert err.startswith("Waiting for the run to start…\n")
    assert err.endswith(f"Run {RUN_1} ended: complete.\n")
    assert steps == []


def test_follow_on_a_finished_run_prints_it_and_stops(
    cli: Invoke, one_run: Path
) -> None:
    result = cli("logs", "--follow", "-C", one_run)

    assert result.exit_code == 0
    assert "001 scope · draft · sonnet" in result.stdout
    assert result.stderr == f"Run {RUN_1} had already ended: complete.\n"


def test_follow_on_a_run_that_died(cli: Invoke, project: Path) -> None:
    write_run(project, RUN_1, [], outcome=None)

    result = cli("logs", "--follow", "-C", project)

    assert result.exit_code == 0
    assert result.stderr == (
        f"warning: run {RUN_1} had stopped without finishing, and no run is "
        "going now\n"
        "It may have been killed. `spadework run` picks up where it stopped.\n"
    )


def test_an_older_unfinished_run_is_not_active(project: Path) -> None:
    write_run(project, RUN_1, [], outcome=None)
    write_run(project, RUN_2, [], outcome=None)
    hold_lock(project)

    older, newer = Run(project, RUN_1), Run(project, RUN_2)
    older.reload()
    newer.reload()

    from spadework.logs import is_active

    assert not is_active(older)
    assert is_active(newer)


def test_a_lock_left_by_a_dead_process_is_not_a_run(project: Path) -> None:
    write_run(project, RUN_1, [], outcome=None)
    path = project / ".spadework" / "lock"
    owner = LockOwner(2**22 + 12345, socket.gethostname(), "2026-09-27T21:04:11Z")
    path.write_bytes(owner.to_bytes())
    run = Run(project, RUN_1)
    run.reload()

    from spadework.logs import is_active

    assert not is_active(run)


# --- Help -------------------------------------------------------------------------


def test_help_matches_the_snapshot(cli: Invoke) -> None:
    result = cli("logs", "--help")

    assert result.exit_code == 0
    snapshot = SNAPSHOTS / "spadework-logs.txt"
    if os.environ.get("SPADEWORK_UPDATE_SNAPSHOTS"):
        snapshot.write_text(result.stdout, encoding="utf-8")
    assert result.stdout == snapshot.read_text(encoding="utf-8")
    assert "Examples:" in result.stdout
    for option in ("-C, --project PATH", "-q, --quiet", "-v, --verbose", "--no-color"):
        assert option in result.stdout


def test_show_logs_is_the_library_entry_point(
    one_run: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    show_logs(Ui(), one_run, task="scope", run_id=None, follow=False, raw=False)

    assert "001 scope" in capsys.readouterr().out


# --- Smaller pieces ---------------------------------------------------------------


def test_notices_and_rate_limits_get_a_line() -> None:
    import datetime as dt

    from spadework.executors.base import Notice, RateLimit
    from spadework.logs import event_line

    reset = dt.datetime(2026, 9, 27, 22, 0, tzinfo=dt.UTC)

    def plain(event: Any) -> str | None:
        line = event_line(event)
        return None if line is None else line.plain

    assert plain(Notice("no restricted mode")) == "  ! no restricted mode"
    assert plain(RateLimit("rejected", reset)) == (
        "  ! rate limit: rejected, resets 2026-09-27 22:00 UTC"
    )
    assert (
        plain(RateLimit("allowed_warning", None)) == "  ! rate limit: allowed_warning"
    )
    assert plain(RateLimit("allowed", None)) is None


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("001-scope-draft.jsonl", (1, "scope", "draft")),
        ("012-tech-stack-fix.jsonl", (12, "tech-stack", "fix")),
        ("003-critic.jsonl", (3, None, "critic")),
        ("004-scope-unknown.jsonl", None),
        ("notes.jsonl", None),
        ("001-scope-draft.json", None),
    ],
)
def test_sessions_are_found_by_their_log_names(
    project: Path, name: str, expected: tuple[int, str | None, str] | None
) -> None:
    folder = project / ".spadework" / "runs" / RUN_1 / "sessions"
    folder.mkdir(parents=True)
    (folder / name).write_text("", encoding="utf-8")

    sessions = Run(project, RUN_1).sessions()

    found = [(s.n, s.task, s.kind) for s in sessions]
    assert found == ([] if expected is None else [expected])


def test_a_run_without_a_manifest_yet(cli: Invoke, project: Path) -> None:
    claude_log(
        project,
        RUN_1,
        "sessions/001-scope-draft.jsonl",
        "write-ok-with-denial.mock.jsonl",
    )

    result = cli("logs", "-C", project)

    assert result.exit_code == 0
    assert result.stdout.startswith(f"run {RUN_1} · no manifest\n\n001 scope · draft\n")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_an_unreadable_log_is_a_warning(cli: Invoke, one_run: Path) -> None:
    log = one_run / ".spadework" / "runs" / RUN_1 / "sessions" / "001-scope-draft.jsonl"
    log.chmod(0)
    try:
        result = cli("logs", "-C", one_run)
    finally:
        log.chmod(0o600)

    assert result.exit_code == 0
    assert result.stderr.startswith(
        "warning: cannot read the log of session 001 scope · draft · sonnet: "
        "Permission denied\n"
    )
    assert "002 audience" in result.stdout


def test_follow_raw_prints_new_lines_only_once(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(project, RUN_1, [], outcome=None)
    lock = hold_lock(project)
    source = (
        (STREAM / "write-ok-with-denial.mock.jsonl")
        .read_text()
        .splitlines(keepends=True)
    )
    log = project / ".spadework" / "runs" / RUN_1 / "sessions" / "001-scope-draft.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("".join(source[:5]), encoding="utf-8")
    steps: list[Callable[[], None]] = [
        lambda: log.write_text("".join(source), encoding="utf-8"),
        lambda: (
            write_run(project, RUN_1, [entry(1, "scope", "draft")]),
            lock.unlink(),
        )[1],
    ]

    follow_logs(
        Ui(),
        project,
        task="scope",
        run_id=RUN_1,
        raw=True,
        sleep=lambda seconds: steps.pop(0)(),
    )

    captured = capsys.readouterr()
    assert captured.out == "".join(source)
    assert captured.err == (
        f"run {RUN_1} · running · $0.00 · 0 sessions\n"
        "==> sessions/001-scope-draft.jsonl <==\n"
        f"Run {RUN_1} ended: complete.\n"
    )


def test_follow_a_task_that_never_ran(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(project, RUN_1, [entry(1, "scope", "draft")])
    claude_log(
        project,
        RUN_1,
        "sessions/001-scope-draft.jsonl",
        "write-ok-with-denial.mock.jsonl",
    )

    follow_logs(Ui(), project, task="pricing", run_id=None, raw=False)

    err = capsys.readouterr().err
    assert f"Run {RUN_1} had no sessions for 'pricing'.\n" in err
