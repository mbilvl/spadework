"""The claude-cli executor's command line, environment, settings and process
control, run against a stand-in ``claude`` that never calls a model."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from spadework.executors import claude_cli, process
from spadework.executors.base import (
    Finished,
    Notice,
    SessionKind,
    SessionResult,
    Text,
)
from spadework.executors.claude_cli import (
    ClaudeCliExecutor,
    build_argv,
    child_env,
    expected_tools,
    find_claude,
    format_usd,
    guard_marker,
    parse_version,
    settings_for,
    settings_path,
    write_settings,
)
from spadework.executors.process import LineAction, run_process
from spadework.sandbox.policy import guard_command
from tests.executors.conftest import (
    MakeSpec,
    Recorder,
    init,
    result,
    session_start,
    text,
    write_stream,
)

WRITE_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit", "StructuredOutput"]


@pytest.fixture(autouse=True)
def quick_grace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(process, "TERM_GRACE_S", 0.5)
    monkeypatch.setattr(process, "EXIT_GRACE_S", 2.0)
    for name in ("CLAUDECODE", "SPADEWORK_CLAUDE_BIN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_CLAUDE_RECORD", str(path))
    return path


def stream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *events: Any) -> Path:
    path = write_stream(tmp_path / "stream.jsonl", events)
    monkeypatch.setenv("FAKE_CLAUDE_STREAM", str(path))
    return path


def ok_events(tools: list[str] = WRITE_TOOLS) -> list[Any]:
    return [session_start(), init(tools), text("Done."), result()]


def gone(pid: int) -> bool:
    """True once ``pid`` has exited (a zombie waiting to be reaped counts)."""
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    return state == "" or state.startswith("Z")


def wait_until_gone(pid: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gone(pid):
            return True
        time.sleep(0.05)
    return False


# --- finding the binary -----------------------------------------------------


def executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_the_environment_variable_beats_the_config_which_beats_path(
    fake_claude: Path, tmp_path: Path
) -> None:
    from_env = executable(tmp_path / "from-env")
    configured = executable(tmp_path / "configured")
    path = {"PATH": str(fake_claude.parent)}

    assert (
        find_claude(str(configured), {**path, "SPADEWORK_CLAUDE_BIN": str(from_env)})
        == from_env
    )
    assert find_claude(str(configured), path) == configured
    assert find_claude(None, path) == fake_claude
    assert find_claude("claude", path) == fake_claude
    assert find_claude(None, {"PATH": str(tmp_path)}) is None


def test_a_bin_that_was_given_is_not_replaced_when_missing(
    fake_claude: Path, tmp_path: Path
) -> None:
    environ = {"PATH": str(fake_claude.parent)}
    not_executable = tmp_path / "plain"
    not_executable.write_text("", encoding="utf-8")

    assert find_claude(str(tmp_path / "missing"), environ) is None
    assert find_claude(str(not_executable), environ) is None
    assert find_claude("no-such-claude", environ) is None
    assert find_claude("~/no-such-claude", environ) is None
    assert (
        find_claude(None, {**environ, "SPADEWORK_CLAUDE_BIN": "nowhere/claude"}) is None
    )
    assert (
        find_claude(str(fake_claude), {**environ, "SPADEWORK_CLAUDE_BIN": "gone"})
        is None
    )


def test_find_claude_returns_absolute_paths(
    fake_claude: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(fake_claude.parent.parent)
    relative = f"./{fake_claude.parent.name}/claude"
    link = tmp_path / "link-to-claude"
    link.symlink_to(fake_claude)

    assert find_claude(relative, {}) == fake_claude
    assert find_claude(None, {"SPADEWORK_CLAUDE_BIN": relative}) == fake_claude
    assert find_claude(None, {"PATH": fake_claude.parent.name}) == fake_claude
    for found in (find_claude(relative, {}), find_claude(str(link), {})):
        assert found is not None
        assert found.is_absolute()
    # Symlinks are kept, so an update that moves the target still works.
    assert find_claude(str(link), {}) == link


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("2.1.278 (Claude Code)\n", (2, 1, 278)),
        ("claude 10.0.1", (10, 0, 1)),
        ("unknown", None),
    ],
)
def test_parse_version(text: str, version: tuple[int, int, int] | None) -> None:
    assert parse_version(text) == version


# --- the command line, environment and settings -----------------------------


def test_the_command_line(make_spec: MakeSpec, tmp_path: Path) -> None:
    spec = make_spec(SessionKind.DRAFT, max_budget_usd=1.5)
    argv = build_argv(Path("/opt/claude"), spec, tmp_path / "s.json", restricted=True)

    assert argv == [
        "/opt/claude",
        "-p",
        "--model",
        "sonnet",
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--json-schema",
        json.dumps(spec.output_schema, separators=(",", ":")),
        "--tools",
        "Read,Glob,Grep,Write,Edit",
        "--restricted",
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--settings",
        str(tmp_path / "s.json"),
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--exclude-dynamic-system-prompt-sections",
        "--append-system-prompt",
        "Follow the house rules.",
        "--max-budget-usd",
        "1.5000",
    ]


def test_the_command_line_without_restricted_mode_or_a_system_prompt(
    make_spec: MakeSpec, tmp_path: Path
) -> None:
    spec = make_spec(SessionKind.RESEARCH, system_append="")
    argv = build_argv(Path("claude"), spec, tmp_path / "s.json", restricted=False)

    assert "--restricted" not in argv
    assert "--append-system-prompt" not in argv
    assert argv[argv.index("--tools") + 1] == "WebSearch,WebFetch"


@pytest.mark.parametrize(
    ("amount", "flag"),
    [
        (1.5, "1.5000"),
        (0.29, "0.2900"),
        (0.0001, "0.0001"),
        (0.00019, "0.0001"),  # rounded down, never above the cap
        (2.99999, "2.9999"),
        (1e-4 * 3, "0.0003"),
    ],
)
def test_the_budget_flag_is_never_above_the_cap_or_zero(
    make_spec: MakeSpec, amount: float, flag: str
) -> None:
    argv = build_argv(
        Path("c"), make_spec(max_budget_usd=amount), Path("s"), restricted=True
    )

    assert argv[argv.index("--max-budget-usd") + 1] == flag
    assert format_usd(amount) == flag


def test_the_command_line_never_has_unsafe_flags(make_spec: MakeSpec) -> None:
    for kind in SessionKind:
        argv = build_argv(Path("c"), make_spec(kind), Path("s"), restricted=True)
        for flag in (
            "--bare",
            "--safe-mode",
            "--allowedTools",
            "--add-dir",
            "--mcp-config",
            "--agents",
            "--fallback-model",
            "--dangerously-skip-permissions",
            "bypassPermissions",
            "acceptEdits",
        ):
            assert flag not in argv
        assert not any(arg.startswith("--plugin") for arg in argv)


def test_the_environment() -> None:
    environ = {
        "HOME": "/home/someone",
        "ANTHROPIC_API_KEY": "placeholder",
        "HTTPS_PROXY": "http://proxy.example:8080",
        "CLAUDE_CODE_SIMPLE": "1",
        "CLAUDE_CODE_SAFE_MODE": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "0",
    }

    assert child_env(environ) == {
        "HOME": "/home/someone",
        "ANTHROPIC_API_KEY": "placeholder",
        "HTTPS_PROXY": "http://proxy.example:8080",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    }


def test_the_settings_file(make_spec: MakeSpec) -> None:
    spec = make_spec(SessionKind.FIX)
    path = write_settings(spec)

    assert path == spec.policy_path.parent / "001-stack-fix.settings.json"
    assert path == settings_path(spec)
    assert not path.is_relative_to(spec.workdir)
    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings == settings_for(spec.policy_path)
    start = settings["hooks"]["SessionStart"][0]["hooks"][0]
    pre = settings["hooks"]["PreToolUse"][0]
    assert settings["hooks"].keys() == {"SessionStart", "PreToolUse"}
    assert start == {
        "type": "command",
        "command": shlex.join(guard_command(spec.policy_path, "session-start")),
        "timeout": 600,
    }
    assert pre["matcher"] == "*"
    assert pre["hooks"][0]["command"] == shlex.join(
        guard_command(spec.policy_path, "pre-tool-use")
    )
    assert settings["autoMemoryEnabled"] is False
    assert settings["includeGitInstructions"] is False


def test_the_hook_command_is_quoted(tmp_path: Path) -> None:
    policy = tmp_path / "a dir; rm -rf x" / "p.json"
    command = settings_for(policy)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    assert shlex.split(command) == guard_command(policy, "pre-tool-use")


def test_expected_tools(make_spec: MakeSpec) -> None:
    assert expected_tools(make_spec(SessionKind.CRITIC)) == {
        "Read",
        "Glob",
        "Grep",
        "StructuredOutput",
    }
    custom = make_spec(SessionKind.CRITIC, expected_tools=("Read", "Other"))
    assert expected_tools(custom) == {"Read", "Other"}


def test_a_tool_list_that_does_not_match_the_kind_is_refused(
    make_spec: MakeSpec, fake_claude: Path
) -> None:
    executor = ClaudeCliExecutor(str(fake_claude))
    spec = make_spec(SessionKind.CRITIC, tools=("Read", "Glob", "Grep", "Bash"))

    with pytest.raises(ValueError, match="a critic session gets the tools Read"):
        executor.run(spec, Recorder())


# --- probing and doctor -----------------------------------------------------


def test_version_and_restricted_mode_are_probed_once(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    real_run = subprocess.run

    def counting_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting_run)
    executor = ClaudeCliExecutor(str(fake_claude))

    assert executor.version() == "2.1.278"
    assert executor.version() == "2.1.278"
    assert executor.restricted_supported is True
    assert executor.restricted_supported is True
    assert [argv[1:] for argv in calls] == [["--version"], ["--help"]]


def test_doctor_when_all_is_well(fake_claude: Path) -> None:
    checks = ClaudeCliExecutor(str(fake_claude)).diagnose()

    assert [(check.name, check.ok) for check in checks] == [
        ("Claude Code", True),
        ("Claude Code version", True),
        ("Restricted mode", True),
        ("Claude Code login", True),
    ]
    assert checks[0].detail == str(fake_claude)
    assert checks[1].detail == "2.1.278 (minimum 2.1.278)"


def test_doctor_when_claude_is_missing(tmp_path: Path) -> None:
    [check] = ClaudeCliExecutor(str(tmp_path / "claude")).diagnose()

    assert (check.name, check.ok, check.detail) == ("Claude Code", False, "not found")
    assert "SPADEWORK_CLAUDE_BIN" in check.hint


def test_doctor_finds_problems(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_VERSION", "2.0.9 (Claude Code)")
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESTRICTED", "1")
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", json.dumps({"loggedIn": False}))
    monkeypatch.setenv("CLAUDECODE", "1")
    checks = {
        check.name: check for check in ClaudeCliExecutor(str(fake_claude)).diagnose()
    }

    version = checks["Claude Code version"]
    assert (version.ok, version.detail) == (False, "2.0.9, older than 2.1.278")
    assert version.hint == "Update Claude Code: claude update"
    restricted = checks["Restricted mode"]
    assert (restricted.ok, restricted.required) == (False, False)
    login = checks["Claude Code login"]
    assert (login.ok, login.detail) == (False, "not logged in")
    assert "inside another Claude Code session" in login.hint
    nested = checks["Nested session"]
    assert (nested.ok, nested.required) == (False, False)


@pytest.mark.parametrize("output", ["not json", "[]", '{"loggedIn": "yes"}'])
def test_doctor_reads_auth_status_strictly(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch, output: str
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_AUTH", output)
    checks = {
        check.name: check for check in ClaudeCliExecutor(str(fake_claude)).diagnose()
    }

    assert checks["Claude Code login"].ok is False


def test_doctor_when_the_binary_does_not_run(tmp_path: Path) -> None:
    broken = tmp_path / "claude"
    broken.write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")
    broken.chmod(0o755)
    executor = ClaudeCliExecutor(str(broken))
    checks = {check.name: check for check in executor.diagnose()}

    assert executor.version() is None
    assert executor.restricted_supported is None
    assert checks["Claude Code version"].ok is False
    restricted = checks["Restricted mode"]
    assert (restricted.ok, restricted.required) == (False, True)
    assert "Sessions are refused until it does" in restricted.hint
    assert checks["Claude Code login"].detail == "not logged in"


def test_probes_that_hang_or_fail_to_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hanging = tmp_path / "claude"
    hanging.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    hanging.chmod(0o755)
    monkeypatch.setattr(claude_cli, "PROBE_TIMEOUT_S", 0.2)

    assert ClaudeCliExecutor(str(hanging)).version() is None
    assert ClaudeCliExecutor(str(hanging)).restricted_supported is None
    login = ClaudeCliExecutor(str(hanging))._login_check()
    assert login.detail == "`claude auth status` did not run"
    assert ClaudeCliExecutor(str(tmp_path / "missing")).version() is None


# --- running a session ------------------------------------------------------


def test_run_passes_the_session_to_claude(
    make_spec: MakeSpec,
    fake_claude: Path,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, *ok_events())
    monkeypatch.setenv("CLAUDE_CODE_SIMPLE", "1")
    monkeypatch.setenv("CLAUDE_CODE_SAFE_MODE", "1")
    monkeypatch.setenv("FAKE_CLAUDE_INHERITED", "inherited")
    spec = make_spec(SessionKind.DRAFT, prompt="Write it ✓\n" * 3)
    recorder = Recorder()

    outcome = ClaudeCliExecutor(str(fake_claude)).run(spec, recorder)

    assert outcome.status == "ok"
    assert outcome.structured == {"status": "ok"}
    seen = json.loads(record.read_text(encoding="utf-8"))
    assert (
        seen["argv"]
        == build_argv(fake_claude, spec, settings_path(spec), restricted=True)[1:]
    )
    assert Path(seen["cwd"]).resolve() == spec.workdir.resolve()
    assert seen["stdin"] == "Write it ✓\n" * 3
    assert seen["env"] == {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
        "FAKE_CLAUDE_INHERITED": "inherited",
    }
    assert seen["pgid"] == seen["pid"]  # its own process group
    assert os.getpgid(0) != seen["pgid"]
    assert settings_path(spec).is_file()
    assert recorder.events == [Text("Done."), Finished(outcome)]
    logged = spec.log_path.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["type"] for line in logged] == [
        "system",
        "system",
        "assistant",
        "result",
    ]


def test_run_without_restricted_mode(
    make_spec: MakeSpec,
    fake_claude: Path,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, *ok_events())
    monkeypatch.setenv("FAKE_CLAUDE_NO_RESTRICTED", "1")

    recorder = Recorder()
    outcome = ClaudeCliExecutor(str(fake_claude)).run(make_spec(), recorder)

    assert outcome.status == "ok"
    assert "--restricted" not in json.loads(record.read_text(encoding="utf-8"))["argv"]
    [notice] = recorder.of(Notice)
    assert notice.message.startswith("Claude Code has no --restricted")


@pytest.mark.parametrize("failure", ["exit", "timeout"])
def test_a_failed_help_probe_refuses_the_session(
    make_spec: MakeSpec,
    fake_claude: Path,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    stream(tmp_path, monkeypatch, *ok_events())
    if failure == "exit":
        monkeypatch.setenv("FAKE_CLAUDE_HELP_EXIT", "1")
    else:
        monkeypatch.setattr(claude_cli, "PROBE_TIMEOUT_S", 0.0)
    recorder = Recorder()

    outcome = ClaudeCliExecutor(str(fake_claude)).run(make_spec(), recorder)

    assert (outcome.status, outcome.error_kind) == ("error", "executor")
    assert outcome.error is not None
    assert "could not tell whether Claude Code supports --restricted" in outcome.error
    assert not record.exists()  # no session was started
    assert recorder.events == [Finished(outcome)]


def test_a_failed_help_probe_is_tried_again(
    fake_claude: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = ClaudeCliExecutor(str(fake_claude))
    monkeypatch.setenv("FAKE_CLAUDE_HELP_EXIT", "1")
    assert executor.restricted_supported is None

    monkeypatch.delenv("FAKE_CLAUDE_HELP_EXIT")
    assert executor.restricted_supported is True


def test_only_the_whole_flag_counts(tmp_path: Path) -> None:
    lookalike = tmp_path / "claude"
    lookalike.write_text(
        "#!/bin/sh\necho '  --restricted-tools <names>  something else'\n",
        encoding="utf-8",
    )
    lookalike.chmod(0o755)

    assert ClaudeCliExecutor(str(lookalike)).restricted_supported is False


def test_run_writes_a_large_prompt_without_blocking(
    make_spec: MakeSpec,
    fake_claude: Path,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, *ok_events())
    prompt = "x" * 3_000_000

    outcome = ClaudeCliExecutor(str(fake_claude)).run(
        make_spec(prompt=prompt), Recorder()
    )

    assert outcome.status == "ok"
    assert len(json.loads(record.read_text(encoding="utf-8"))["stdin"]) == len(prompt)


def test_run_when_claude_is_missing(make_spec: MakeSpec, tmp_path: Path) -> None:
    recorder = Recorder()
    outcome = ClaudeCliExecutor(str(tmp_path / "none")).run(make_spec(), recorder)

    assert (outcome.status, outcome.error_kind) == ("error", "executor")
    assert outcome.error == (
        "Claude Code was not found; set SPADEWORK_CLAUDE_BIN or [executor].bin"
    )
    assert recorder.events == [Finished(outcome)]


def test_run_when_claude_cannot_start(
    make_spec: MakeSpec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = tmp_path / "claude"
    broken.write_text("#!/nonexistent/interpreter\n", encoding="utf-8")
    broken.chmod(0o755)
    executor = ClaudeCliExecutor(str(broken))
    monkeypatch.setattr(ClaudeCliExecutor, "restricted_supported", False)

    outcome = executor.run(make_spec(), Recorder())

    assert (outcome.status, outcome.error_kind) == ("error", "executor")
    assert outcome.error is not None
    assert outcome.error.startswith("could not start Claude Code:")


def test_a_crash_without_a_result(
    make_spec: MakeSpec,
    fake_claude: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, session_start(), init(WRITE_TOOLS))
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "Error: connection reset\n")
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "1")

    outcome = ClaudeCliExecutor(str(fake_claude)).run(make_spec(), Recorder())

    assert (outcome.status, outcome.error_kind) == ("error", "transient")
    assert outcome.error == (
        "Claude Code exited without a result (exit code 1): Error: connection reset"
    )


def test_a_timeout_kills_the_whole_process_group(
    make_spec: MakeSpec,
    fake_claude: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, session_start(), init(WRITE_TOOLS), text("thinking"))
    child = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CLAUDE_CHILD", str(child))
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    monkeypatch.setenv("FAKE_CLAUDE_IGNORE_TERM", "1")  # SIGKILL is needed
    recorder = Recorder()

    started = time.monotonic()
    outcome = ClaudeCliExecutor(str(fake_claude)).run(make_spec(timeout_s=1), recorder)

    assert time.monotonic() - started < 10
    assert outcome.status == "timeout"
    assert outcome.cost_known is False
    assert recorder.of(Text) == [Text("thinking")]
    assert wait_until_gone(int(child.read_text(encoding="utf-8")))


def test_a_sandbox_violation_stops_the_session_at_once(
    make_spec: MakeSpec,
    fake_claude: Path,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No SessionStart hook: the settings file was ignored.
    stream(tmp_path, monkeypatch, init(WRITE_TOOLS), text("I will now read"))
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")

    started = time.monotonic()
    outcome = ClaudeCliExecutor(str(fake_claude)).run(make_spec(), Recorder())

    assert time.monotonic() - started < 10
    assert (outcome.status, outcome.error_kind) == ("error", "sandbox")
    assert outcome.error == "the sandbox guard did not start"
    assert wait_until_gone(json.loads(record.read_text(encoding="utf-8"))["pid"])


def test_ctrl_c_stops_the_session_and_reports_it(
    make_spec: MakeSpec,
    fake_claude: Path,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, session_start(), init(WRITE_TOOLS), text("working"))
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    seen: list[Any] = []

    def interrupting(event: Any) -> None:
        seen.append(event)
        if isinstance(event, Text):
            raise KeyboardInterrupt

    outcome = ClaudeCliExecutor(str(fake_claude)).run(make_spec(), interrupting)

    assert outcome.status == "interrupted"
    assert seen[-1] == Finished(outcome)
    assert wait_until_gone(json.loads(record.read_text(encoding="utf-8"))["pid"])


def test_an_error_in_the_event_sink_stops_the_process(
    make_spec: MakeSpec,
    fake_claude: Path,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, session_start(), init(WRITE_TOOLS), text("working"))
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")

    def broken(event: Any) -> None:
        raise RuntimeError("the live view broke")

    with pytest.raises(RuntimeError, match="the live view broke"):
        ClaudeCliExecutor(str(fake_claude)).run(make_spec(), broken)
    assert wait_until_gone(json.loads(record.read_text(encoding="utf-8"))["pid"])


def test_an_exit_before_init_is_not_worth_retrying(
    make_spec: MakeSpec,
    fake_claude: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FAKE_CLAUDE_STDERR", "error: unknown option '--tools'\n")
    monkeypatch.setenv("FAKE_CLAUDE_EXIT", "2")

    outcome = ClaudeCliExecutor(str(fake_claude)).run(make_spec(), Recorder())

    assert (outcome.status, outcome.error_kind) == ("error", "executor")
    assert outcome.error == (
        "Claude Code exited before the session started (exit code 2): "
        "error: unknown option '--tools'"
    )


# --- the guard marker -------------------------------------------------------


def test_the_guard_marker_is_checked(
    make_spec: MakeSpec,
    fake_claude: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, *ok_events())
    spec = make_spec()

    outcome = ClaudeCliExecutor(str(fake_claude)).run(spec, Recorder())

    assert outcome.status == "ok"
    assert guard_marker(spec) == spec.policy_path.parent / f"{spec.session_id}.alive"
    assert guard_marker(spec).read_text(encoding="utf-8").strip() == spec.session_id


@pytest.mark.parametrize("guard", ["missing", "002-other-session"])
def test_another_session_start_hook_cannot_stand_in_for_the_guard(
    make_spec: MakeSpec,
    fake_claude: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
) -> None:
    # A SessionStart hook answered (say, from managed settings), but our
    # guard wrote no marker, or someone else's.
    stream(tmp_path, monkeypatch, *ok_events())
    monkeypatch.setenv("FAKE_CLAUDE_GUARD", guard)

    outcome = ClaudeCliExecutor(str(fake_claude)).run(make_spec(), Recorder())

    assert (outcome.status, outcome.error_kind) == ("error", "sandbox")
    assert outcome.error == "the sandbox guard did not start"
    assert outcome.structured is None


def test_a_stale_marker_is_removed_before_the_session(
    make_spec: MakeSpec,
    fake_claude: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, *ok_events())
    monkeypatch.setenv("FAKE_CLAUDE_GUARD", "missing")
    spec = make_spec()
    guard_marker(spec).write_text(spec.session_id + "\n", encoding="utf-8")

    outcome = ClaudeCliExecutor(str(fake_claude)).run(spec, Recorder())

    assert outcome.error == "the sandbox guard did not start"
    assert not guard_marker(spec).exists()


# --- nothing outlives the session -------------------------------------------


def test_a_result_is_kept_when_claude_does_not_exit(
    make_spec: MakeSpec,
    fake_claude: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, *ok_events())
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    monkeypatch.setattr(process, "EXIT_GRACE_S", 0.3)

    started = time.monotonic()
    outcome = ClaudeCliExecutor(str(fake_claude)).run(
        make_spec(timeout_s=20), Recorder()
    )

    assert time.monotonic() - started < 5
    assert outcome.status == "ok"
    assert outcome.structured == {"status": "ok"}


def test_a_result_just_before_the_deadline_beats_the_timeout(
    make_spec: MakeSpec,
    fake_claude: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, *ok_events())
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    monkeypatch.setattr(process, "EXIT_GRACE_S", 1.5)

    outcome = ClaudeCliExecutor(str(fake_claude)).run(
        make_spec(timeout_s=1), Recorder()
    )

    assert outcome.status == "ok"


@pytest.mark.parametrize("detached", [True, False])
@pytest.mark.parametrize("waitid", [True, False])
def test_a_normal_exit_kills_what_claude_left_behind(
    make_spec: MakeSpec,
    fake_claude: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detached: bool,
    waitid: bool,
) -> None:
    stream(tmp_path, monkeypatch, *ok_events())
    child = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CLAUDE_CHILD", str(child))
    if detached:
        monkeypatch.setenv("FAKE_CLAUDE_CHILD_DETACHED", "1")
    if not waitid:  # as on macOS before Python 3.13
        monkeypatch.setattr(process, "_WAITID", None)
    monkeypatch.setattr(process, "EXIT_GRACE_S", 0.5)

    started = time.monotonic()
    outcome = ClaudeCliExecutor(str(fake_claude)).run(
        make_spec(timeout_s=20), Recorder()
    )

    assert time.monotonic() - started < 5
    assert outcome.status == "ok"
    assert wait_until_gone(int(child.read_text(encoding="utf-8")))


def test_a_sandbox_violation_is_killed_without_a_grace_period(
    make_spec: MakeSpec,
    fake_claude: Path,
    record: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream(tmp_path, monkeypatch, init(WRITE_TOOLS), text("I will now read"))
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    monkeypatch.setenv("FAKE_CLAUDE_IGNORE_TERM", "1")
    monkeypatch.setattr(process, "TERM_GRACE_S", 30.0)

    started = time.monotonic()
    outcome = ClaudeCliExecutor(str(fake_claude)).run(make_spec(), Recorder())

    assert time.monotonic() - started < 5
    assert outcome.error_kind == "sandbox"
    assert wait_until_gone(json.loads(record.read_text(encoding="utf-8"))["pid"])


# --- run_process on its own -------------------------------------------------


def python(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_run_process_collects_lines_and_stderr(tmp_path: Path) -> None:
    seen: list[str] = []
    code = (
        "import sys; data = sys.stdin.read(); "
        "sys.stdout.write('one\\ntwo\\n' + data + '\\nlast'); "
        "sys.stderr.write('warn\\n' * 30 + 'final')"
    )

    outcome = run_process(
        python(code),
        cwd=tmp_path,
        env=os.environ,
        stdin="from stdin",
        timeout_s=10,
        on_line=lambda line: seen.append(line) or "continue",
    )

    assert outcome.ended == "exited"
    assert outcome.returncode == 0
    assert seen == ["one\n", "two\n", "from stdin\n", "last"]
    stderr = outcome.stderr.splitlines()
    assert len(stderr) == 21  # the last 20 lines plus the unfinished one
    assert stderr[-1] == "final"
    assert outcome.start_error is None


def test_run_process_keeps_only_the_end_of_an_unfinished_stderr_line(
    tmp_path: Path,
) -> None:
    outcome = run_process(
        python("import sys; sys.stderr.write('x' * 100000 + 'END')"),
        cwd=tmp_path,
        env=os.environ,
        stdin="",
        timeout_s=10,
        on_line=lambda line: "continue",
    )

    assert len(outcome.stderr) == 4096
    assert outcome.stderr.endswith("END")


def test_run_process_with_an_empty_prompt_and_a_child_that_ignores_it(
    tmp_path: Path,
) -> None:
    outcome = run_process(
        python("print('hi')"),
        cwd=tmp_path,
        env=os.environ,
        stdin="",
        timeout_s=10,
        on_line=lambda line: "continue",
    )

    assert (outcome.ended, outcome.returncode) == ("exited", 0)


def test_run_process_survives_a_child_that_closes_stdin_early(tmp_path: Path) -> None:
    outcome = run_process(
        python("import os; os.close(0); print('closed')"),
        cwd=tmp_path,
        env=os.environ,
        stdin="y" * 2_000_000,
        timeout_s=10,
        on_line=lambda line: "continue",
    )

    assert outcome.ended == "exited"


def test_run_process_stops_when_asked(tmp_path: Path) -> None:
    seen: list[str] = []

    def on_line(line: str) -> LineAction:
        seen.append(line)
        return "kill" if line.startswith("stop") else "continue"

    started = time.monotonic()
    outcome = run_process(
        python("import time; print('go'); print('stop', flush=True); time.sleep(30)"),
        cwd=tmp_path,
        env=os.environ,
        stdin="",
        timeout_s=20,
        on_line=on_line,
    )

    assert time.monotonic() - started < 10
    assert outcome.ended == "killed"
    assert seen == ["go\n", "stop\n"]


def test_run_process_winds_down_when_the_work_is_done(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process, "EXIT_GRACE_S", 0.3)
    code = "import time; print('result', flush=True); time.sleep(30)"

    started = time.monotonic()
    outcome = run_process(
        python(code),
        cwd=tmp_path,
        env=os.environ,
        stdin="",
        timeout_s=0.2,  # the deadline passes during the wind-down
        on_line=lambda line: "done" if line.startswith("result") else "continue",
    )

    assert time.monotonic() - started < 5
    assert outcome.ended == "exited"
    assert outcome.returncode == -9


def test_run_process_reports_a_start_failure(tmp_path: Path) -> None:
    outcome = run_process(
        [str(tmp_path / "missing")],
        cwd=tmp_path,
        env=os.environ,
        stdin="",
        timeout_s=1,
        on_line=lambda line: "continue",
    )

    assert outcome.start_error is not None
    assert outcome.returncode is None


def test_run_process_times_out_a_child_that_closes_its_output_but_keeps_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process, "EXIT_GRACE_S", 30.0)
    code = "import os, time; os.close(1); os.close(2); time.sleep(30)"

    started = time.monotonic()
    outcome = run_process(
        python(code),
        cwd=tmp_path,
        env=os.environ,
        stdin="",
        timeout_s=1,
        on_line=lambda line: "continue",
    )

    assert time.monotonic() - started < 10
    assert outcome.ended == "timeout"


def test_run_process_stops_a_child_that_lingers_after_its_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(process, "EXIT_GRACE_S", 0.3)
    code = "import os, time; print('done', flush=True); os.close(1); time.sleep(30)"
    code = code.replace("os.close(1)", "os.close(1); os.close(2)")

    outcome = run_process(
        python(code),
        cwd=tmp_path,
        env=os.environ,
        stdin="",
        timeout_s=20,
        on_line=lambda line: "continue",
    )

    # The output is complete, so this is not a timeout.
    assert outcome.ended == "exited"
    assert outcome.returncode is not None


def test_a_real_session_result_type() -> None:
    assert SessionResult(status="ok").cost_known is True
