"""``spadework doctor``: the environment checks and the sandbox canary.

No test here runs the real ``claude``: the checks use a stand-in binary, and
the canary is judged with stand-in executors. The canary against the real
CLI is in tests/integration/test_canary.py.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from spadework import doctor
from spadework.doctor import (
    OUTPUT_FILE,
    SECOND_FILE,
    CanaryReport,
    choose_executor,
    python_check,
    run_canary,
)
from spadework.executors.base import (
    Denied,
    Diagnostic,
    EventSink,
    SessionResult,
    SessionSpec,
    ToolCall,
)
from spadework.sandbox.policy import load
from tests.conftest import Invoke, MakeProject

SNAPSHOTS = Path(__file__).parent / "snapshots" / "help"

STAND_IN = """\
#!/bin/sh
case "$1" in
  --version) echo "${STAND_IN_VERSION:-2.1.278} (Claude Code)" ;;
  --help)
    echo "Usage: claude [options]"
    [ -z "$STAND_IN_NO_RESTRICTED" ] && echo "  --restricted  confine the file tools"
    ;;
  auth) echo "{\\"loggedIn\\": ${STAND_IN_LOGGED_IN:-true}}" ;;
  *) exit 64 ;;
esac
exit 0
"""


@pytest.fixture(autouse=True)
def stand_in_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A ``claude`` that answers the probes and never runs a session."""
    binary = tmp_path / "bin" / "claude"
    binary.parent.mkdir()
    binary.write_text(STAND_IN, encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("SPADEWORK_CLAUDE_BIN", str(binary))
    # Wide enough that no detail wraps onto a second line.
    monkeypatch.setenv("COLUMNS", "400")
    for name in ("CLAUDECODE", "SPADEWORK_EXECUTOR", "SPADEWORK_FAKE_SCRIPT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(tmp_path)
    return binary


def lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines()]


def row(text: str, name: str) -> str:
    """The line of the check called ``name``."""
    for line in text.splitlines():
        if re.match(rf"^[✓✗!] {re.escape(name)}  ", line):
            return re.sub(r"\s{2,}", "  ", line.rstrip())
    raise AssertionError(f"no {name!r} line in:\n{text}")


# --- The environment checks -------------------------------------------------


def test_everything_in_place(cli: Invoke, stand_in_claude: Path) -> None:
    result = cli("doctor")

    assert result.exit_code == 0, result.output
    assert row(result.stdout, "Python").startswith("✓ Python  3.")
    assert row(result.stdout, "spadework") == "✓ spadework  0.1.0.dev0"
    assert row(result.stdout, "Executor") == "✓ Executor  claude-cli (default)"
    assert row(result.stdout, "Claude Code") == f"✓ Claude Code  {stand_in_claude}"
    assert row(result.stdout, "Claude Code version") == (
        "✓ Claude Code version  2.1.278 (minimum 2.1.278)"
    )
    assert row(result.stdout, "Restricted mode") == "✓ Restricted mode  available"
    assert row(result.stdout, "Claude Code login") == "✓ Claude Code login  logged in"
    assert row(result.stdout, "Project") == (
        "✓ Project  none here (run doctor in a project, or pass -C)"
    )
    assert result.stderr == "Everything spadework needs is in place.\n"


def test_a_failed_check_prints_its_hint_and_exits_1(
    cli: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STAND_IN_LOGGED_IN", "false")

    result = cli("doctor")

    assert result.exit_code == 1
    assert row(result.stdout, "Claude Code login") == (
        "✗ Claude Code login  not logged in"
    )
    assert "Run `claude` and log in." in result.stdout
    assert result.stderr == "1 problem found.\n"


def test_an_old_claude_code_fails(cli: Invoke, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAND_IN_VERSION", "2.0.1")

    result = cli("doctor")

    assert result.exit_code == 1
    assert row(result.stdout, "Claude Code version") == (
        "✗ Claude Code version  2.0.1, older than 2.1.278"
    )
    assert "claude update" in result.stdout


def test_no_restricted_mode_is_a_warning(
    cli: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STAND_IN_NO_RESTRICTED", "1")

    result = cli("doctor")

    assert result.exit_code == 0
    assert row(result.stdout, "Restricted mode") == (
        "! Restricted mode  this Claude Code has no --restricted"
    )


def test_a_nested_session_is_a_warning_with_a_hint(
    cli: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")

    result = cli("doctor")

    assert result.exit_code == 0
    assert row(result.stdout, "Nested session") == (
        "! Nested session  spadework is running inside a Claude Code session"
    )
    assert "Run spadework from a normal terminal." in result.stdout


def test_claude_code_not_found(
    cli: Invoke, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPADEWORK_CLAUDE_BIN", str(tmp_path / "missing"))

    result = cli("doctor")

    assert result.exit_code == 1
    assert row(result.stdout, "Claude Code") == "✗ Claude Code  not found"
    assert "Claude Code version" not in result.stdout


def test_the_executor_comes_from_the_environment_first(
    cli: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPADEWORK_EXECUTOR", "fake")

    result = cli("doctor")

    assert result.exit_code == 0
    assert row(result.stdout, "Executor") == (
        "✓ Executor  fake (from SPADEWORK_EXECUTOR): no model is called and "
        "nothing is spent"
    )
    assert result.stdout.count("Executor") == 1
    assert "Claude Code" not in result.stdout


def test_an_unknown_executor_fails(
    cli: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPADEWORK_EXECUTOR", "gpt")

    result = cli("doctor")

    assert result.exit_code == 1
    assert row(result.stdout, "Executor") == (
        "✗ Executor  unknown executor 'gpt' (from SPADEWORK_EXECUTOR)"
    )
    assert "Use one of: claude-cli, fake." in result.stdout


def test_an_unusable_fake_script_fails(
    cli: Invoke, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SPADEWORK_EXECUTOR", "fake")
    monkeypatch.setenv("SPADEWORK_FAKE_SCRIPT", str(tmp_path / "missing.json"))

    result = cli("doctor")

    assert result.exit_code == 1
    assert row(result.stdout, "Executor").startswith(
        "✗ Executor  fake: cannot read the fake executor script"
    )


def test_a_valid_project_is_checked(cli: Invoke, make_project: MakeProject) -> None:
    result = cli("doctor", "-C", make_project())

    assert result.exit_code == 0
    assert row(result.stdout, "Project") == (
        "✓ Project  acme-fabrication is valid (fixture 1.0.0)"
    )


def test_the_project_config_chooses_the_executor(
    cli: Invoke, make_project: MakeProject
) -> None:
    config = (
        'schema_version = 1\n[project]\nname = "acme-fabrication"\n'
        'playbook = "./playbooks/valid"\n[executor]\nname = "fake"\n'
    )

    result = cli("doctor", "-C", make_project(config=config))

    assert row(result.stdout, "Executor").startswith(
        "✓ Executor  fake (from spadework.toml): "
    )


def test_a_project_that_fails_check_fails(
    cli: Invoke, make_project: MakeProject
) -> None:
    result = cli("doctor", "-C", make_project(brief="---\ntitle: x\n---\n"))

    assert result.exit_code == 1
    assert row(result.stdout, "Project").startswith(
        "✗ Project  `spadework check` fails: brief.md"
    )


def test_a_folder_given_with_C_must_be_a_project(cli: Invoke, tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()

    result = cli("doctor", "-C", folder)

    assert result.exit_code == 1
    assert row(result.stdout, "Project") == (
        f"✗ Project  there is no spadework.toml in {folder}"
    )


def test_json_output(cli: Invoke, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STAND_IN_LOGGED_IN", "false")

    result = cli("doctor", "--json")

    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["schema_version"] == 1
    assert document["ok"] is False
    assert document["canary"] is None
    login = next(c for c in document["checks"] if c["name"] == "Claude Code login")
    assert login == {
        "name": "Claude Code login",
        "status": "fail",
        "ok": False,
        "required": True,
        "detail": "not logged in",
        "hint": login["hint"],
    }
    assert login["hint"].startswith("Run `claude` and log in.")
    assert result.stderr == ""


def test_python_check() -> None:
    assert python_check((3, 11, 0)).ok
    old = python_check((3, 10, 14))
    assert not old.ok
    assert old.detail == "3.10.14, older than 3.11"


def test_choose_executor_precedence(make_project: MakeProject) -> None:
    from spadework.config import load_config

    config = load_config(make_project() / "spadework.toml")

    assert choose_executor(None, {}).name == "claude-cli"
    assert choose_executor(config, {}).source == "spadework.toml"
    chosen = choose_executor(config, {"SPADEWORK_EXECUTOR": "fake"})
    assert (chosen.name, chosen.source) == ("fake", "SPADEWORK_EXECUTOR")


# --- The canary -------------------------------------------------------------


def canary_paths(spec: SessionSpec) -> tuple[Path, Path, Path]:
    """The outside file, the second file and the output the prompt names."""
    (outside,) = re.findall(r"Read tool to read (/\S+)", spec.prompt)
    second, output = re.findall(r"Write tool to create (/\S+) with", spec.prompt)
    return Path(outside), Path(second), Path(output)


Behaviour = Callable[[SessionSpec, EventSink], SessionResult]


class StandInExecutor:
    """Plays a session the way a behaviour says, without any model."""

    name = "claude-cli"

    def __init__(self, behaviour: Behaviour) -> None:
        self.behaviour = behaviour
        self.specs: list[SessionSpec] = []

    def version(self) -> str | None:
        return "2.1.278"

    def diagnose(self) -> list[Diagnostic]:
        return [Diagnostic("Claude Code", True, "a stand-in")]

    def run(self, spec: SessionSpec, events: EventSink) -> SessionResult:
        self.specs.append(spec)
        return self.behaviour(spec, events)


def log(spec: SessionSpec, *calls: tuple[str, str]) -> None:
    """Write the session's raw log: one line per tool call."""
    spec.log_path.parent.mkdir(parents=True, exist_ok=True)
    with spec.log_path.open("a", encoding="utf-8") as file:
        for name, path in calls:
            block = {"type": "tool_use", "name": name, "input": {"file_path": path}}
            file.write(json.dumps({"type": "assistant", "content": [block]}) + "\n")


def guard_denies(spec: SessionSpec, *calls: tuple[str, str]) -> None:
    """Write denials to the guard's log, as the guard does."""
    policy = load(spec.policy_path)
    with open(policy.denials_path, "a", encoding="utf-8") as file:
        for name, target in calls:
            record = {"ts": "", "tool_name": name, "reason": "denied", "target": target}
            file.write(json.dumps(record) + "\n")


def ok(**changes: Any) -> SessionResult:
    fields: dict[str, Any] = {
        "structured": {"read_outside": False, "outside_text": ""},
        "cost_usd": 0.0123,
        **changes,
    }
    return SessionResult(status="ok", **fields)


def sandbox_holds(spec: SessionSpec, events: EventSink) -> SessionResult:
    outside, second, output = canary_paths(spec)
    log(spec, ("Read", str(outside)), ("Write", str(second)), ("Write", str(output)))
    guard_denies(spec, ("Read", str(outside)), ("Write", str(second)))
    events(ToolCall("Read", str(outside)))
    events(Denied("Read", str(outside), "denied"))
    output.write_text("spadework canary\n", encoding="utf-8")
    return ok(guard_denials=2)


def sandbox_leaks(spec: SessionSpec, events: EventSink) -> SessionResult:
    outside, second, output = canary_paths(spec)
    content = outside.read_text(encoding="utf-8")
    log(spec, ("Read", str(outside)), ("Write", str(second)))
    second.write_text("second file\n", encoding="utf-8")
    output.write_text("spadework canary\n", encoding="utf-8")
    return ok(structured={"read_outside": True, "outside_text": content})


def lazy(spec: SessionSpec, events: EventSink) -> SessionResult:
    _, _, output = canary_paths(spec)
    output.write_text("spadework canary\n", encoding="utf-8")
    return ok()


def blocked_elsewhere(spec: SessionSpec, events: EventSink) -> SessionResult:
    outside, second, output = canary_paths(spec)
    log(spec, ("Read", str(outside)), ("Write", str(second)))
    output.write_text("spadework canary\n", encoding="utf-8")
    return ok()


def over_budget(spec: SessionSpec, events: EventSink) -> SessionResult:
    return SessionResult(status="budget", cost_usd=0.06, error="Reached the cap")


def by_name(report: CanaryReport) -> dict[str, tuple[bool, str]]:
    return {check.name: (check.ok, check.detail) for check in report.checks}


@pytest.fixture
def temp(tmp_path: Path) -> Path:
    folder = tmp_path / "tmp"
    folder.mkdir()
    return folder


def test_the_canary_passes_when_both_attempts_are_denied(temp: Path) -> None:
    executor = StandInExecutor(sandbox_holds)
    seen: list[Any] = []

    report = run_canary(executor, temp_root=temp, events=seen.append)

    assert report.ok
    assert by_name(report) == {
        "Session": (True, "finished with a structured result"),
        "Read outside": (True, "denied by the guard"),
        "Second file": (True, "denied by the guard"),
        "Outside content": (True, "never reached the session"),
        "Allowed output": (True, "written"),
    }
    assert report.cost_usd == 0.0123
    assert [type(event).__name__ for event in seen] == ["ToolCall", "Denied"]
    assert os.listdir(temp) == []


def test_the_canary_session_is_a_small_sandboxed_draft(temp: Path) -> None:
    executor = StandInExecutor(sandbox_holds)

    run_canary(executor, temp_root=temp)

    (spec,) = executor.specs
    assert spec.model == "haiku"
    assert spec.max_budget_usd == 0.05
    assert spec.kind == "draft"
    assert spec.output is not None
    assert spec.output.name == OUTPUT_FILE
    outside, second, output = canary_paths(spec)
    assert output == spec.output
    assert second.parent == spec.workdir
    assert second.name == SECOND_FILE
    assert not outside.is_relative_to(spec.workdir)
    # The secret's content is never in what the session is told.
    assert "spadework-canary-" not in spec.prompt.replace(str(outside.parent), "")


def test_the_canary_fails_when_the_sandbox_leaks(temp: Path) -> None:
    report = run_canary(StandInExecutor(sandbox_leaks), temp_root=temp)

    assert not report.ok
    checks = by_name(report)
    assert checks["Read outside"] == (
        False,
        "ALLOWED: the file's content reached the session",
    )
    assert checks["Second file"] == (False, f"ALLOWED: {SECOND_FILE} was written")
    assert checks["Outside content"][0] is False
    assert checks["Allowed output"] == (
        False,
        f"the session left other files: {SECOND_FILE}",
    )


def test_the_canary_is_inconclusive_when_the_model_does_not_try(temp: Path) -> None:
    report = run_canary(StandInExecutor(lazy), temp_root=temp)

    assert not report.ok
    checks = by_name(report)
    assert checks["Read outside"] == (
        False,
        "the model never tried it, so it was not tested",
    )
    assert checks["Allowed output"] == (True, "written")


def test_a_denial_that_is_not_the_guard_s_does_not_count(temp: Path) -> None:
    report = run_canary(StandInExecutor(blocked_elsewhere), temp_root=temp)

    checks = by_name(report)
    assert checks["Read outside"] == (
        False,
        "blocked, but not by the guard, so the guard was not tested",
    )


def test_a_canary_over_its_budget_fails(temp: Path) -> None:
    report = run_canary(StandInExecutor(over_budget), temp_root=temp)

    checks = {check.name: check for check in report.checks}
    assert checks["Session"].detail == "budget: Reached the cap"
    assert "Run it again." in checks["Session"].hint
    assert checks["Allowed output"].detail == (
        "the session could not write its one allowed file"
    )


def test_debug_keeps_the_canary_folder(
    temp: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPADEWORK_DEBUG", "1")

    run_canary(StandInExecutor(sandbox_holds), temp_root=temp)

    (folder,) = os.listdir(temp)
    assert folder.startswith("spadework-canary-")


@pytest.mark.parametrize(
    ("result", "hint"),
    [
        (SessionResult(status="rate_limited"), "usage limit"),
        (
            SessionResult(status="error", error_kind="sandbox", error="no guard"),
            "sessions are refused",
        ),
        (SessionResult(status="error", error_kind="executor"), "claude -p"),
    ],
)
def test_a_failed_canary_session_says_what_to_do(
    temp: Path, result: SessionResult, hint: str
) -> None:
    report = run_canary(StandInExecutor(lambda spec, events: result), temp_root=temp)

    session = report.checks[0]
    assert not session.ok
    assert hint in session.hint


# --- The canary from the command line ----------------------------------------


@pytest.fixture
def stand_in_executor(monkeypatch: pytest.MonkeyPatch) -> StandInExecutor:
    executor = StandInExecutor(sandbox_holds)

    def get_executor(name: str, *, claude_bin: str | None = None) -> Any:
        return executor

    monkeypatch.setattr("spadework.executors.get_executor", get_executor)
    return executor


def test_doctor_canary(cli: Invoke, stand_in_executor: StandInExecutor) -> None:
    result = cli("doctor", "--canary")

    assert result.exit_code == 0, result.output
    assert "\nSandbox canary:\n" in result.stdout
    assert row(result.stdout, "Read outside") == "✓ Read outside  denied by the guard"
    assert result.stderr == (
        "Running the sandbox canary: one haiku session, capped at $0.05.\n"
        "The canary spent $0.0123.\n"
        "Everything spadework needs is in place.\n"
    )
    assert len(stand_in_executor.specs) == 1


def test_doctor_canary_verbose_shows_the_session(
    cli: Invoke, stand_in_executor: StandInExecutor
) -> None:
    result = cli("doctor", "--canary", "-v")

    assert "  denied: Read /" in result.stderr


def test_doctor_canary_json(cli: Invoke, stand_in_executor: StandInExecutor) -> None:
    stand_in_executor.behaviour = sandbox_leaks

    result = cli("doctor", "--canary", "--json")

    assert result.exit_code == 1
    document = json.loads(result.stdout)
    assert document["ok"] is False
    assert document["canary"]["ok"] is False
    assert document["canary"]["model"] == "haiku"
    assert document["canary"]["cost_usd"] == 0.0123
    statuses = {c["name"]: c["status"] for c in document["canary"]["checks"]}
    assert statuses["Read outside"] == "fail"
    assert statuses["Session"] == "ok"


def test_doctor_canary_with_unknown_cost(
    cli: Invoke, stand_in_executor: StandInExecutor
) -> None:
    stand_in_executor.behaviour = lambda spec, events: SessionResult(
        status="timeout", cost_known=False
    )

    result = cli("doctor", "--canary")

    assert result.exit_code == 1
    assert (
        "Claude Code did not report what the canary cost; its cap was $0.05."
        in result.stderr
    )


def test_the_canary_needs_the_claude_cli_executor(
    cli: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPADEWORK_EXECUTOR", "fake")

    result = cli("doctor", "--canary")

    assert result.exit_code == 1
    assert row(result.stdout, "Canary") == (
        "✗ Canary  not run: it needs the claude-cli executor"
    )


def test_the_canary_does_not_run_when_a_check_failed(
    cli: Invoke, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STAND_IN_LOGGED_IN", "false")
    ran: list[Any] = []
    monkeypatch.setattr(doctor, "run_canary", lambda *a, **k: ran.append(a))

    result = cli("doctor", "--canary")

    assert result.exit_code == 1
    assert row(result.stdout, "Canary") == (
        "✗ Canary  not run, because of the problems above"
    )
    assert ran == []
    assert result.stderr == "2 problems found.\n"


# --- Help ---------------------------------------------------------------------


def test_help_matches_the_snapshot(cli: Invoke) -> None:
    result = cli("doctor", "--help")

    assert result.exit_code == 0
    snapshot = SNAPSHOTS / "spadework-doctor.txt"
    if os.environ.get("SPADEWORK_UPDATE_SNAPSHOTS"):
        snapshot.write_text(result.stdout, encoding="utf-8")
    assert result.stdout == snapshot.read_text(encoding="utf-8")
    examples = lines(result.stdout)[lines(result.stdout).index("  Examples:") + 1 :]
    assert all(line.strip().startswith("spadework doctor") for line in examples if line)
    for option in ("-C, --project PATH", "-q, --quiet", "-v, --verbose", "--no-color"):
        assert option in result.stdout
