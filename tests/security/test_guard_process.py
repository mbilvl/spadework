"""The guard as the CLI runs it: a subprocess, stdin in, decision out.

I6: every failure blocks the call (exit 2 with one line on stderr, or a deny).
I7: caps are counted under a lock, also when calls run in parallel.
I11: every denial is logged.
SessionStart: a liveness check that prints nothing on stdout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from spadework.sandbox import guard
from spadework.sandbox.policy import Policy, guard_command
from tests.security.conftest import (
    NOW,
    OUTPUT,
    Box,
    MakeBox,
    fake_resolve,
    future_deadline,
    hook_decision,
    run_guard,
)


@pytest.fixture
def live(make_box: MakeBox) -> Box:
    """A draft session whose deadline is in the future on the real clock."""
    return make_box("draft", deadline=future_deadline())


def call(
    box: Box, tool: str, tool_input: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    return run_guard(box.staging.paths.policy, box.payload(tool, tool_input))


def assert_blocked(completed: subprocess.CompletedProcess[str], fragment: str) -> None:
    """Exit 2, nothing on stdout, and one line on stderr without a traceback."""
    assert completed.returncode == 2
    assert completed.stdout == ""
    assert completed.stderr.startswith("spadework guard: ")
    assert completed.stderr.count("\n") == 1
    assert "Traceback" not in completed.stderr
    assert fragment in completed.stderr


# The real entry point


def test_allows_a_legitimate_call(live: Box) -> None:
    decision, reason = hook_decision(call(live, "Read", {"file_path": "brief.md"}))

    assert decision == "allow"
    assert reason.startswith("spadework: ")


def test_denies_with_json_and_exit_0(live: Box) -> None:
    decision, reason = hook_decision(
        call(
            live,
            "Write",
            {"file_path": "<c>/001-scope-draft.policy.json", "content": ""},
        )
    )

    assert decision == "deny"
    assert "inside the working directory" in reason


def test_accepts_the_captured_cli_payloads(live: Box) -> None:
    """The payload shapes recorded from Claude Code 2.1.278 in the M2 spike."""
    calls: list[tuple[str, dict[str, Any], str]] = [
        ("Read", {"file_path": "<w>/brief.md"}, "allow"),
        ("Glob", {"pattern": "*.md"}, "allow"),
        ("Grep", {"pattern": "hello", "path": "<w>"}, "allow"),
        ("Write", {"file_path": f"<w>/{OUTPUT}", "content": "hello\n"}, "allow"),
        (
            "Edit",
            {
                "file_path": f"<w>/{OUTPUT}",
                "old_string": "hello",
                "new_string": "hello world",
                "replace_all": False,
            },
            "allow",
        ),
        ("StructuredOutput", {"status": "ok", "summary": "Wrote hello."}, "allow"),
        ("WebFetch", {"url": "https://example.com/", "prompt": "Summarise"}, "deny"),
        ("WebSearch", {"query": "static site generators comparison"}, "deny"),
    ]
    for tool, tool_input, expected in calls:
        decision, _ = hook_decision(call(live, tool, tool_input))
        assert decision == expected, tool


# I6: fail closed


@pytest.mark.parametrize(
    ("stdin", "fragment"),
    [
        pytest.param(b"{not json", "not valid JSON", id="invalid-json"),
        pytest.param(b"\xff\xfe", "not valid JSON", id="not-utf8"),
        pytest.param(b"", "not valid JSON", id="empty"),
        pytest.param(b"[]", "not a JSON object", id="array"),
        pytest.param(
            json.dumps(
                {"hook_event_name": "PostToolUse", "tool_name": "Read"}
            ).encode(),
            "expected a PreToolUse hook payload",
            id="wrong-event",
        ),
        pytest.param(
            json.dumps({"hook_event_name": "PreToolUse", "tool_input": {}}).encode(),
            "no tool_name",
            id="no-tool-name",
        ),
        pytest.param(
            json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Read"}).encode(),
            "no tool_input",
            id="no-tool-input",
        ),
    ],
)
def test_unreadable_payloads_block(live: Box, stdin: bytes, fragment: str) -> None:
    completed = run_guard(live.staging.paths.policy, stdin)

    assert_blocked(completed, fragment)
    denials = live.staging.denials()
    assert len(denials) == 1
    assert denials[0]["tool_name"] == "(unknown)"


def test_missing_policy_blocks_without_revealing_its_path(live: Box) -> None:
    live.staging.paths.policy.unlink()

    completed = call(live, "Read", {"file_path": "brief.md"})

    assert_blocked(completed, "policy is unusable")
    assert str(live.root) not in completed.stderr
    assert "acme" not in completed.stderr


@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        pytest.param(
            lambda data: {**data, "extra": 1}, "unknown policy field", id="unknown"
        ),
        pytest.param(
            lambda data: {k: v for k, v in data.items() if k != "caps"},
            "missing policy field 'caps'",
            id="missing",
        ),
        pytest.param(
            lambda data: {**data, "allowed_tools": ["Read", "Bash"]},
            "unsupported tool 'Bash'",
            id="bash-allowed",
        ),
        pytest.param(
            lambda data: {**data, "allowed_tools": ["Read", "mcp__x__y"]},
            "unsupported tool",
            id="mcp-allowed",
        ),
        pytest.param(
            lambda data: {**data, "writable": [data["counters_path"]]},
            "inside the workdir",
            id="writable-control-file",
        ),
        pytest.param(
            lambda data: {**data, "caps": {"WebFetch": 3}},
            "must set 'total'",
            id="no-total-cap",
        ),
        pytest.param(
            lambda data: {**data, "deadline_epoch": "soon"},
            "must be a number",
            id="deadline",
        ),
        pytest.param(lambda data: [data], "must be a JSON object", id="not-an-object"),
    ],
)
def test_invalid_policy_blocks(live: Box, change: Any, fragment: str) -> None:
    path = live.staging.paths.policy
    path.write_text(json.dumps(change(json.loads(path.read_text()))), encoding="utf-8")

    completed = call(live, "Read", {"file_path": "brief.md"})

    assert_blocked(completed, fragment)
    assert str(live.root) not in completed.stderr


def test_policy_that_is_not_json_blocks(live: Box) -> None:
    live.staging.paths.policy.write_text("{", encoding="utf-8")

    assert_blocked(call(live, "Read", {"file_path": "brief.md"}), "not valid JSON")


def test_policy_inside_the_workdir_blocks(live: Box) -> None:
    inside = live.workdir / "policy.json"
    inside.write_bytes(live.staging.paths.policy.read_bytes())

    completed = run_guard(inside, live.payload("Read", {"file_path": "brief.md"}))

    assert_blocked(completed, "outside the workdir")


def test_unknown_tool_is_denied(live: Box) -> None:
    decision, reason = hook_decision(call(live, "Frobnicate", {"x": 1}))

    assert decision == "deny"
    assert "not available" in reason


def test_passed_deadline_is_denied(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=1.0)  # long gone on the real clock

    decision, reason = hook_decision(call(box, "Read", {"file_path": "brief.md"}))

    assert decision == "deny"
    assert "time is up" in reason


def test_corrupt_counters_block(live: Box) -> None:
    live.staging.paths.counters.write_text("{broken", encoding="utf-8")

    assert_blocked(call(live, "Read", {"file_path": "brief.md"}), "counters")
    assert "counters" in live.staging.denials()[0]["reason"]


def test_internal_error_blocks(live: Box) -> None:
    live.staging.paths.lock.mkdir()  # the lock file cannot be opened

    assert_blocked(call(live, "Read", {"file_path": "brief.md"}), "internal error")
    assert live.staging.denials()[0]["reason"].startswith("guard error")


def test_internal_error_in_process(
    writer: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(*args: Any, **kwargs: Any) -> guard.Decision:
        raise RuntimeError("boom /secret/path")

    monkeypatch.setattr(guard, "check_offline", explode)
    payload = json.dumps(writer.payload("Read", {"file_path": "brief.md"})).encode()

    code, out, err = guard.run(
        ["--policy", str(writer.staging.paths.policy), "--event", "pre-tool-use"],
        payload,
        now=NOW,
    )

    assert (code, out) == (2, "")
    assert err == "spadework guard: internal error\n"
    assert writer.staging.denials()[0]["reason"] == "guard error: RuntimeError"


@pytest.mark.parametrize(
    ("argv", "fragment"),
    [
        pytest.param([], "usage", id="none"),
        pytest.param(["--policy", "/x.json"], "usage", id="no-event"),
        pytest.param(
            ["--policy", "/x.json", "--evnt", "pre-tool-use"], "usage", id="typo"
        ),
        pytest.param(
            ["--policy", "x.json", "--event", "pre-tool-use"], "absolute", id="rel"
        ),
        pytest.param(
            ["--policy", "/x.json", "--event", "post-tool-use"], "--event", id="ev"
        ),
    ],
)
def test_bad_arguments_block(argv: list[str], fragment: str) -> None:
    command = [sys.executable, "-I", "-m", "spadework.sandbox.guard", *argv]

    completed = run_guard("/unused", b"{}", argv=command)

    assert_blocked(completed, fragment)


def test_main_reads_stdin_and_writes_stdout(
    writer: Box, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = json.dumps(writer.payload("Bash", {"command": "ls"})).encode()
    monkeypatch.setattr(sys, "stdin", _Stdin(payload))
    argv = ["--policy", str(writer.staging.paths.policy), "--event", "pre-tool-use"]

    assert guard.main(argv) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


class _Stdin:
    def __init__(self, data: bytes) -> None:
        self.buffer = _Buffer(data)


class _Buffer:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self, size: int = -1) -> bytes:
        return self.data


# SessionStart


def session_start(box: Box, cwd: str | Path) -> subprocess.CompletedProcess[str]:
    payload = {
        "session_id": "00000000-0000-0000-0000-000000000000",
        "cwd": str(cwd),
        "hook_event_name": "SessionStart",
        "source": "startup",
    }
    return run_guard(box.staging.paths.policy, payload, event="session-start")


def test_session_start_is_silent_when_all_is_well(live: Box) -> None:
    completed = session_start(live, live.workdir)

    assert (completed.returncode, completed.stdout, completed.stderr) == (0, "", "")


def test_session_start_accepts_a_symlinked_cwd(live: Box, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(live.workdir)

    assert session_start(live, alias).returncode == 0


def test_session_start_rejects_another_cwd(live: Box) -> None:
    completed = session_start(live, live.root)

    assert_blocked(completed, "not running in its staging directory")


def test_session_start_rejects_a_missing_policy(live: Box) -> None:
    live.staging.paths.policy.unlink()

    assert_blocked(session_start(live, live.workdir), "policy is unusable")


def test_session_start_rejects_a_passed_deadline(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=1.0)

    assert_blocked(session_start(box, box.workdir), "deadline has already passed")


def test_session_start_rejects_an_unresolved_workdir(live: Box, tmp_path: Path) -> None:
    alias = tmp_path / "alias"
    alias.symlink_to(live.workdir)
    data = live.policy.to_dict()
    data["workdir"] = str(alias)
    data["writable"] = [str(alias / OUTPUT)]
    live.staging.paths.policy.write_text(json.dumps(data), encoding="utf-8")

    assert_blocked(session_start(live, alias), "not a fully resolved path")


def test_session_start_rejects_a_missing_workdir(live: Box) -> None:
    shutil.rmtree(live.workdir)

    assert_blocked(session_start(live, live.workdir), "not a directory")


def test_session_start_rejects_a_writable_directory(live: Box) -> None:
    live.staging.output_path.mkdir()

    assert_blocked(session_start(live, live.workdir), "not a regular file")


def test_session_start_rejects_a_tool_payload(live: Box) -> None:
    payload = live.payload("Read", {"file_path": "brief.md"})
    completed = run_guard(live.staging.paths.policy, payload, event="session-start")

    assert_blocked(completed, "expected a SessionStart hook payload")


def test_session_start_rejects_a_payload_without_cwd(live: Box) -> None:
    payload = {"hook_event_name": "SessionStart"}
    completed = run_guard(live.staging.paths.policy, payload, event="session-start")

    assert_blocked(completed, "no cwd")


def test_session_start_checks_control_files_are_outside(
    writer: Box, tmp_path: Path
) -> None:
    """A control directory reached through a symlink into the workdir."""
    sneaky = tmp_path / "sneaky"
    sneaky.symlink_to(writer.workdir)
    data = writer.policy.to_dict()
    data["counters_path"] = str(sneaky / "counters.json")
    policy = Policy(**data)

    problem = guard.check_session_start(
        {"cwd": str(writer.workdir)},
        policy,
        str(writer.staging.paths.policy),
        now=NOW,
    )

    assert problem == "the control files must be outside the staging directory"


# Stdlib only


def test_guard_imports_only_the_standard_library() -> None:
    script = (
        "import json, sys\n"
        "before = set(sys.modules)\n"
        "import spadework.sandbox.guard\n"
        "print(json.dumps(sorted(set(sys.modules) - before)))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    imported = json.loads(completed.stdout)
    ours = {"spadework", "spadework.sandbox"}
    foreign = [
        name
        for name in imported
        if name not in ours
        and not name.startswith("spadework.sandbox.")
        and name.split(".")[0] not in sys.stdlib_module_names
    ]

    assert "spadework.sandbox.guard" in imported
    assert foreign == []
    assert not any(
        name.startswith("spadework.") and ".sandbox" not in name for name in imported
    )


def test_guard_command_runs_in_isolated_mode(writer: Box) -> None:
    command = guard_command(writer.staging.paths.policy, "pre-tool-use")

    assert command[:4] == [sys.executable, "-I", "-m", "spadework.sandbox.guard"]


def test_isolated_mode_ignores_the_session_cwd_and_pythonpath(live: Box) -> None:
    """A staged `json.py` must not be imported by the guard."""
    trap = live.workdir / "json.py"
    trap.write_text("raise SystemExit('imported from the workdir')\n", encoding="utf-8")
    payload = json.dumps(live.payload("Read", {"file_path": "brief.md"})).encode()

    completed = subprocess.run(
        guard_command(live.staging.paths.policy, "pre-tool-use"),
        input=payload,
        capture_output=True,
        cwd=live.workdir,
        env={**os.environ, "PYTHONPATH": str(live.workdir)},
        check=False,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert b'"allow"' in completed.stdout


# I11: denials are logged


def test_every_denial_is_logged_and_allowed_calls_are_not(live: Box) -> None:
    call(live, "Read", {"file_path": "brief.md"})
    call(live, "Read", {"file_path": "<outside>/secret.txt"})
    call(live, "mcp__x__y", {})

    denials = live.staging.denials()

    assert [denial["tool_name"] for denial in denials] == ["Read", "mcp__x__y"]
    first = denials[0]
    assert set(first) == {"ts", "tool_name", "reason", "target"}
    assert first["target"] == f"{live.outside}/secret.txt"
    assert "inside the working directory" in first["reason"]
    assert first["ts"].endswith("Z")


def test_logged_urls_have_no_credentials_or_query(make_box: MakeBox) -> None:
    box = make_box("research", deadline=future_deadline())

    call(
        box,
        "WebFetch",
        {"url": "http://bob:hunter2@127.0.0.1/a?token=t0k", "prompt": "x"},
    )

    (denial,) = box.staging.denials()
    assert denial["target"] == "http://127.0.0.1/a"
    assert "hunter2" not in json.dumps(denial)
    assert "t0k" not in json.dumps(denial)


def test_logging_failure_still_denies(writer: Box) -> None:
    writer.staging.paths.denials.mkdir()  # cannot be appended to
    payload = json.dumps(writer.payload("Bash", {"command": "ls"})).encode()

    code, out, _ = guard.run(
        ["--policy", str(writer.staging.paths.policy), "--event", "pre-tool-use"],
        payload,
        now=NOW,
    )

    assert code == 0
    assert json.loads(out)["hookSpecificOutput"]["permissionDecision"] == "deny"


# I7: caps


def in_process(box: Box, tool: str, tool_input: dict[str, Any]) -> str:
    payload = json.dumps(box.payload(tool, tool_input)).encode()
    code, out, err = guard.run(
        ["--policy", str(box.staging.paths.policy), "--event", "pre-tool-use"],
        payload,
        now=NOW,
        resolve=fake_resolve,
    )
    assert code == 0, err
    decision: str = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    return decision


def test_per_tool_caps(make_box: MakeBox) -> None:
    box = make_box("research", max_searches=2, max_fetches=1)
    fetch = {"url": "https://example.com/", "prompt": "x"}

    searches = [in_process(box, "WebSearch", {"query": "q"}) for _ in range(3)]
    fetches = [in_process(box, "WebFetch", fetch) for _ in range(2)]

    assert searches == ["allow", "allow", "deny"]
    assert fetches == ["allow", "deny"]
    assert box.staging.counters() == {"WebSearch": 2, "WebFetch": 1, "total": 3}
    reasons = [denial["reason"] for denial in box.staging.denials()]
    assert "limit of 2 WebSearch calls" in reasons[0]
    assert "limit of 1 WebFetch calls" in reasons[1]


def test_total_cap(make_box: MakeBox) -> None:
    box = make_box("draft", total_calls=3)

    decisions = [in_process(box, "Read", {"file_path": "brief.md"}) for _ in range(4)]

    assert decisions == ["allow", "allow", "allow", "deny"]
    assert "limit of 3 tool calls" in box.staging.denials()[0]["reason"]


def test_denied_calls_are_not_counted(make_box: MakeBox) -> None:
    box = make_box("draft", total_calls=2)

    in_process(box, "Read", {"file_path": "/etc/passwd"})
    in_process(box, "Bash", {"command": "ls"})

    assert box.staging.counters() == {}
    assert in_process(box, "Read", {"file_path": "brief.md"}) == "allow"
    assert box.staging.counters() == {"Read": 1, "total": 1}


def test_structured_output_is_never_capped(make_box: MakeBox) -> None:
    box = make_box("draft", total_calls=1)

    assert in_process(box, "Read", {"file_path": "brief.md"}) == "allow"
    assert in_process(box, "Read", {"file_path": "brief.md"}) == "deny"
    assert in_process(box, "StructuredOutput", {"status": "ok"}) == "allow"
    assert box.staging.counters() == {"Read": 1, "total": 1}


def test_zero_cap_denies_the_first_call(make_box: MakeBox) -> None:
    box = make_box("research", max_searches=0, max_fetches=1)

    assert in_process(box, "WebSearch", {"query": "q"}) == "deny"


def test_concurrent_charges_are_serialised(writer: Box) -> None:
    """Threads open the lock file separately, so flock serialises them."""
    object.__setattr__(writer.policy, "caps", {"total": 25})
    barrier = threading.Barrier(8)

    def charge() -> list[str | None]:
        barrier.wait()
        return [guard.charge(writer.policy, "Read") for _ in range(5)]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = [r for batch in pool.map(lambda _: charge(), range(8)) for r in batch]

    assert results.count(None) == 25
    assert writer.staging.counters() == {"Read": 25, "total": 25}


def test_concurrent_guard_processes_respect_the_cap(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=future_deadline(), total_calls=6)
    payload = json.dumps(box.payload("Read", {"file_path": "brief.md"})).encode()
    command = guard_command(box.staging.paths.policy, "pre-tool-use")

    processes = [
        subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(12)
    ]
    outputs = [process.communicate(payload, timeout=60) for process in processes]

    decisions = [
        json.loads(out)["hookSpecificOutput"]["permissionDecision"]
        for out, _ in outputs
    ]
    assert decisions.count("allow") == 6
    assert decisions.count("deny") == 6
    assert box.staging.counters() == {"Read": 6, "total": 6}
    assert len(box.staging.denials()) == 6


@pytest.mark.parametrize(
    "content",
    [
        pytest.param('{"total": -1}', id="negative"),
        pytest.param('{"total": 1.5}', id="float"),
        pytest.param('{"total": true}', id="bool"),
        pytest.param("[1]", id="list"),
    ],
)
def test_invalid_counters_are_errors(writer: Box, content: str) -> None:
    writer.staging.paths.counters.write_text(content, encoding="utf-8")

    with pytest.raises(guard.GuardError, match="counters are invalid"):
        guard.charge(writer.policy, "Read")


def test_counters_are_written_atomically(writer: Box) -> None:
    guard.charge(writer.policy, "Read")

    leftovers = [p.name for p in writer.control.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []
    mode = writer.staging.paths.counters.stat().st_mode & 0o777
    assert mode == 0o600


# The same paths in process, where coverage can see them


def run_in_process(
    box: Box, stdin: bytes | dict[str, Any], event: str = "pre-tool-use"
) -> tuple[int, str, str]:
    data = stdin if isinstance(stdin, bytes) else json.dumps(stdin).encode()
    argv = ["--policy", str(box.staging.paths.policy), "--event", event]
    return guard.run(argv, data, now=NOW, resolve=fake_resolve)


def test_session_start_in_process(writer: Box) -> None:
    payload = {"hook_event_name": "SessionStart", "cwd": str(writer.workdir)}

    assert run_in_process(writer, payload, "session-start") == (0, "", "")


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        pytest.param({"hook_event_name": "SessionStart"}, "no cwd", id="no-cwd"),
        pytest.param(
            {"hook_event_name": "SessionStart", "cwd": "/"},
            "not running in its staging directory",
            id="wrong-cwd",
        ),
        pytest.param(
            {"hook_event_name": "PreToolUse"}, "SessionStart", id="wrong-event"
        ),
    ],
)
def test_session_start_problems_in_process(
    writer: Box, payload: dict[str, Any], message: str
) -> None:
    code, out, err = run_in_process(writer, payload, "session-start")

    assert (code, out) == (2, "")
    assert message in err


def test_session_start_with_a_missing_policy_in_process(writer: Box) -> None:
    writer.staging.paths.policy.unlink()
    payload = {"hook_event_name": "SessionStart", "cwd": str(writer.workdir)}

    code, _, err = run_in_process(writer, payload, "session-start")

    assert code == 2
    assert "policy is unusable" in err


def test_payload_problems_are_reported_before_policy_problems(writer: Box) -> None:
    writer.staging.paths.policy.unlink()

    code, _, err = run_in_process(writer, b"{bad")

    assert code == 2
    assert "not valid JSON" in err


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        pytest.param(b"[]", "not a JSON object", id="array"),
        pytest.param({"hook_event_name": "PreToolUse"}, "no tool_name", id="no-tool"),
        pytest.param(
            {"hook_event_name": "PreToolUse", "tool_name": "Read"},
            "no tool_input",
            id="input",
        ),
    ],
)
def test_payload_problems_in_process(
    writer: Box, payload: bytes | dict[str, Any], message: str
) -> None:
    code, out, err = run_in_process(writer, payload)

    assert (code, out) == (2, "")
    assert message in err
    assert writer.staging.denials()[0]["tool_name"] == "(unknown)"


def test_oversized_payload_blocks(writer: Box, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(guard, "MAX_PAYLOAD_BYTES", 10)

    code, _, err = run_in_process(writer, writer.payload("Read", {"file_path": "x"}))

    assert code == 2
    assert "too large" in err


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([], id="none"),
        pytest.param(["--policy", "/p", "--event"], id="odd"),
        pytest.param(["--policy", "/p", "--policy", "/q"], id="repeated"),
    ],
)
def test_bad_arguments_in_process(argv: list[str]) -> None:
    code, out, err = guard.run(argv, b"{}")

    assert (code, out) == (2, "")
    assert err.startswith("spadework guard: usage:")


def test_main_blocks_when_stdin_cannot_be_read(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", None)

    assert guard.main(["--policy", "/p", "--event", "pre-tool-use"]) == 2
    assert capsys.readouterr().err == "spadework guard: cannot read the hook payload\n"


def test_main_blocks_when_stdout_is_gone(
    writer: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Closed:
        def write(self, text: str) -> int:
            raise BrokenPipeError(32, "Broken pipe")

        def flush(self) -> None:
            raise BrokenPipeError(32, "Broken pipe")

    payload = json.dumps(writer.payload("Read", {"file_path": "brief.md"})).encode()
    monkeypatch.setattr(sys, "stdin", _Stdin(payload))
    monkeypatch.setattr(sys, "stdout", Closed())

    argv = ["--policy", str(writer.staging.paths.policy), "--event", "pre-tool-use"]
    assert guard.main(argv) == 2


def test_unreadable_counters_in_process(writer: Box) -> None:
    writer.staging.paths.counters.write_text("{broken", encoding="utf-8")

    with pytest.raises(guard.GuardError, match="counters are unreadable"):
        guard.charge(writer.policy, "Read")
