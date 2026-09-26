"""The claude-cli stream parser, fed events one line at a time.

The recorded fixtures in tests/fixtures/stream/ are real Claude Code 2.1.278
output against a scripted mock API, so their costs and text are made up.
They were recorded before the guard ran as a SessionStart hook, so where a
test needs the canary to pass it puts the two SessionStart lines from
session-start-canary.mock.jsonl in front.
"""

from __future__ import annotations

import datetime as dt
import io
import json
from pathlib import Path
from typing import Any

import pytest

from spadework.executors.base import Denied, RateLimit, Text, ToolCall, Usage
from spadework.executors.claude_stream import StreamParser, describe_target, replay
from tests.executors.conftest import (
    Recorder,
    fixture_lines,
    init,
    json_deny,
    lines,
    pre_tool_use,
    result,
    session_start,
    text,
    tool_result,
    tool_use,
)

WRITE_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit", "StructuredOutput"]
RESEARCH_TOOLS = ["WebSearch", "WebFetch", "StructuredOutput"]
SESSION_START = fixture_lines("session-start-canary.mock.jsonl")[:2]
WORKDIR = Path("/WORKDIR")


def parse(
    *events: dict[str, Any] | str,
    expected: list[str] | None = WRITE_TOOLS,
    recorder: Recorder | None = None,
    **options: Any,
) -> StreamParser:
    parser = StreamParser(
        expected_tools=expected,
        events=recorder if recorder is not None else Recorder(),
        workdir=WORKDIR,
        **options,
    )
    for line in lines(*events):
        parser.feed(line)
    return parser


def started(*events: dict[str, Any] | str) -> tuple[dict[str, Any] | str, ...]:
    return (session_start(), init(WRITE_TOOLS), *events)


def test_a_successful_session(recorder: Recorder) -> None:
    parser = parse(
        *started(
            text("Reading the brief.\nThen writing."),
            tool_use("t1", "Read", {"file_path": "/WORKDIR/brief.md"}),
            pre_tool_use("Read"),
            tool_result("t1"),
            tool_use("t2", "StructuredOutput", {"status": "ok"}),
            pre_tool_use("StructuredOutput"),
            tool_result("t2"),
            result(),
        ),
        recorder=recorder,
    )
    outcome = parser.result(duration_s=9.0)

    assert parser.violation is None
    assert outcome.status == "ok"
    assert outcome.structured == {"status": "ok"}
    assert outcome.error is None
    assert outcome.cost_usd == 0.0123
    assert outcome.cost_known is True
    assert outcome.duration_s == 4.5  # from the result, not the wall clock
    assert outcome.usage == Usage(100, 20, 3000, 400, 2, 3)
    assert outcome.tool_calls == {"Read": 1}  # StructuredOutput is not counted
    assert outcome.guard_denials == 0
    assert recorder.events == [Text("Reading the brief."), ToolCall("Read", "brief.md")]


def test_every_line_is_logged() -> None:
    log = io.StringIO()
    raw = [*lines(session_start(), init(WRITE_TOOLS)), "not json\n", "\n", "[1, 2]"]
    parser = StreamParser(expected_tools=WRITE_TOOLS, log=log)
    for line in raw:
        parser.feed(line)

    assert log.getvalue() == "".join(raw) + "\n"
    assert parser.malformed_lines == 2
    assert parser.violation is None


def test_unknown_events_and_fields_are_ignored(recorder: Recorder) -> None:
    parser = parse(
        *started(
            {"type": "stream_event", "event": {"type": "message_start"}},
            {"type": "system", "subtype": "compact_boundary"},
            {"type": "assistant", "message": "not a dict"},
            {"type": "assistant", "message": {"content": ["text", {"type": "image"}]}},
            {"type": "user", "message": {"content": "plain text"}},
            result(new_field={"anything": True}),
        ),
        recorder=recorder,
    )

    assert parser.result().status == "ok"
    assert recorder.events == []


# --- the liveness canary ----------------------------------------------------


def test_no_session_start_hook_means_no_guard() -> None:
    parser = parse(init(WRITE_TOOLS), text("hello"), result())
    outcome = parser.result()

    assert parser.violation == "the sandbox guard did not start"
    assert (outcome.status, outcome.error_kind) == ("error", "sandbox")
    assert outcome.error == "the sandbox guard did not start"
    assert outcome.structured is None
    # Stopped at init, before any model call.
    assert (outcome.cost_usd, outcome.cost_known) == (0.0, True)


def test_a_failed_session_start_hook_is_reported() -> None:
    parser = parse(
        session_start(exit_code=2, stderr="spadework guard: the policy is missing\n"),
        init(WRITE_TOOLS),
    )

    assert parser.violation == (
        "the sandbox guard did not start (spadework guard: the policy is missing)"
    )


def test_a_session_start_hook_after_init_is_too_late() -> None:
    parser = parse(init(WRITE_TOOLS), session_start())

    assert parser.violation == "the sandbox guard did not start"


@pytest.mark.parametrize("exit_code", [1, 127, None])
def test_a_crashed_guard_stops_the_session(exit_code: int | None) -> None:
    parser = parse(
        *started(
            tool_use("t1", "Write", {"file_path": "/WORKDIR/out.md"}),
            pre_tool_use("Write", exit_code=exit_code or 0, stderr="Traceback\nboom")
            | {"exit_code": exit_code},
            tool_result("t1"),
            result(),
        )
    )
    outcome = parser.result()

    assert parser.violation == (
        f"the sandbox guard failed on a Write call (exit code {exit_code}): Traceback"
    )
    assert (outcome.status, outcome.error_kind) == ("error", "sandbox")
    assert outcome.structured is None


def test_a_tool_that_runs_without_the_guard_stops_the_session() -> None:
    parser = parse(
        *started(
            tool_use("t1", "Read", {"file_path": "/WORKDIR/brief.md"}),
            tool_result("t1"),
        )
    )

    assert parser.violation == "a Read call ran without an answer from the guard"
    assert parser.result().cost_known is False  # the model was already running


def test_a_call_that_never_ran_needs_no_guard() -> None:
    parser = parse(
        *started(
            tool_use("t1", "Bash", {"command": "ls"}),
            tool_result("t1", is_error=True),
            result(),
        )
    )

    assert parser.violation is None
    assert parser.result().tool_calls == {"Bash": 1}


def test_without_guard_checks_nothing_is_required() -> None:
    parser = parse(
        init(WRITE_TOOLS),
        tool_use("t1", "Read", {"file_path": "/WORKDIR/brief.md"}),
        tool_result("t1"),
        result(),
        require_guard=False,
    )

    assert parser.violation is None
    assert parser.result().status == "ok"


def test_unexpected_tools_stop_the_session() -> None:
    parser = parse(
        session_start(), init(["Read", "Write", "Bash", "StructuredOutput"]), result()
    )

    assert parser.violation == (
        "Claude Code did not load the expected tools "
        "(missing Edit, Glob, Grep; unexpected Bash)"
    )
    assert parser.result().error_kind == "sandbox"


def test_a_missing_tool_list_is_unexpected() -> None:
    parser = parse(session_start(), {"type": "system", "subtype": "init"})

    assert parser.violation is not None
    assert parser.violation.startswith("Claude Code did not load the expected tools")


def test_the_tool_set_is_not_checked_when_not_given() -> None:
    parser = parse(session_start(), init(["Anything"]), result(), expected=None)

    assert parser.violation is None


def test_events_after_a_violation_are_only_logged(recorder: Recorder) -> None:
    log = io.StringIO()
    parser = parse(
        init(WRITE_TOOLS), text("after"), result(), recorder=recorder, log=log
    )

    assert recorder.events == []
    assert log.getvalue().count("\n") == 3
    assert parser.result().status == "error"


# --- denials ----------------------------------------------------------------


def test_denials_are_reported(recorder: Recorder) -> None:
    parser = parse(
        *started(
            tool_use("t1", "Write", {"file_path": "/WORKDIR/notes.md"}),
            pre_tool_use(
                "Write", exit_code=2, stderr="spadework guard: not writable\n"
            ),
            tool_result("t1", is_error=True),
            tool_use("t2", "Read", {"file_path": "/etc/hosts"}),
            pre_tool_use("Read", stdout=json_deny("spadework guard: outside")),
            tool_result("t2", is_error=True),
            tool_use("t3", "Edit", {"file_path": "/WORKDIR/out.md"}),
            pre_tool_use("Edit", stdout="{}"),
            {
                "type": "system",
                "subtype": "permission_denied",
                "tool_name": "Edit",
                "tool_use_id": "t3",
            },
            tool_result("t3", is_error=True),
            result(
                permission_denials=[{"tool_name": name} for name in "abc"],
            ),
        ),
        recorder=recorder,
    )
    outcome = parser.result()

    assert parser.violation is None
    assert recorder.of(Denied) == [
        Denied("Write", "notes.md", "spadework guard: not writable"),
        Denied("Read", "/etc/hosts", "spadework guard: outside"),
        Denied("Edit", "out.md", "denied by the permission mode"),
    ]
    assert outcome.guard_denials == 3
    assert outcome.tool_calls == {"Write": 1, "Read": 1, "Edit": 1}


def test_denials_are_counted_without_a_result(recorder: Recorder) -> None:
    parser = parse(
        *started(
            tool_use("t1", "Write", {"file_path": "/WORKDIR/notes.md"}),
            pre_tool_use("Write", exit_code=2),
            {
                "type": "system",
                "subtype": "permission_denied",
                "tool_name": "Write",
                "tool_use_id": "t1",
            },
        ),
        recorder=recorder,
    )

    assert recorder.of(Denied) == [Denied("Write", "notes.md", "denied by the guard")]
    assert parser.result(ended="timeout").guard_denials == 1


# --- how a session ends -----------------------------------------------------


def test_a_stream_without_a_result_is_a_transient_error() -> None:
    parser = parse(*started(text("working")))
    outcome = parser.result(returncode=1, stderr="warming up\nfatal: socket hang up\n")

    assert (outcome.status, outcome.error_kind) == ("error", "transient")
    assert outcome.error == (
        "Claude Code exited without a result (exit code 1): fatal: socket hang up"
    )
    assert outcome.cost_known is False


def test_an_empty_stream_is_a_transient_error() -> None:
    outcome = parse(expected=WRITE_TOOLS).result(returncode=0)

    assert (outcome.status, outcome.error_kind) == ("error", "transient")
    assert outcome.error == "Claude Code exited before the session started"
    assert outcome.cost_known is True  # it never got as far as init


@pytest.mark.parametrize("returncode", [1, 2])
def test_a_failure_before_init_is_not_retried(returncode: int) -> None:
    outcome = parse(session_start()).result(
        returncode=returncode, stderr="error: unknown option '--restricted'\n"
    )

    assert (outcome.status, outcome.error_kind) == ("error", "executor")
    assert outcome.error == (
        f"Claude Code exited before the session started (exit code {returncode}): "
        "error: unknown option '--restricted'"
    )


def test_a_signal_before_init_is_transient() -> None:
    outcome = parse().result(returncode=-9)

    assert outcome.error_kind == "transient"


@pytest.mark.parametrize(
    ("ended", "status", "error"),
    [
        ("timeout", "timeout", "the session did not finish within its time limit"),
        ("interrupted", "interrupted", "interrupted"),
    ],
)
def test_stopped_sessions(ended: Any, status: str, error: str) -> None:
    outcome = parse(*started(text("working"))).result(ended=ended, duration_s=30.0)

    assert (outcome.status, outcome.error) == (status, error)
    assert outcome.duration_s == 30.0
    assert (outcome.cost_usd, outcome.cost_known) == (0.0, False)


def test_a_result_beats_a_timeout() -> None:
    parser = parse(*started(result()))
    outcome = parser.result(ended="timeout")

    assert parser.finished
    assert outcome.status == "ok"
    assert outcome.structured == {"status": "ok"}
    assert (outcome.cost_usd, outcome.cost_known) == (0.0123, True)


def test_a_malformed_result_does_not_beat_a_timeout() -> None:
    outcome = parse(*started(result(total_cost_usd=None))).result(ended="timeout")

    assert outcome.status == "timeout"


def test_interrupted_beats_a_result() -> None:
    assert parse(*started(result())).result(ended="interrupted").status == (
        "interrupted"
    )


def test_budget() -> None:
    outcome = parse(
        *started(
            result(
                subtype="error_max_budget_usd",
                is_error=True,
                total_cost_usd=0.041,
                errors=["Reached maximum budget ($0.03)"],
                structured_output=...,
            )
        )
    ).result()

    assert outcome.status == "budget"
    assert outcome.cost_usd == 0.041  # overshoots the cap by one call
    assert outcome.error == "Reached maximum budget ($0.03)"


def test_schema_retries_exhausted() -> None:
    outcome = parse(
        *started(
            result(
                subtype="error_max_structured_output_retries",
                is_error=True,
                errors=["Failed to provide valid structured output after 5 attempts"],
                structured_output=...,
            )
        )
    ).result()

    assert (outcome.status, outcome.error_kind) == ("error", "schema")
    assert outcome.error == "Failed to provide valid structured output after 5 attempts"


def test_a_success_without_a_structured_result_is_a_schema_failure() -> None:
    outcome = parse(*started(result(structured_output=..., result="Done."))).result()

    assert (outcome.status, outcome.error_kind) == ("error", "schema")
    assert outcome.error == "the session ended without a structured result"


def test_a_structured_result_that_is_not_an_object_is_a_schema_failure() -> None:
    outcome = parse(*started(result(structured_output=["ok"]))).result()

    assert outcome.error_kind == "schema"


def test_a_success_marked_as_an_error_is_not_ok() -> None:
    outcome = parse(
        *started(result(is_error=True, result="Invalid API key · Please run /login"))
    ).result()

    assert (outcome.status, outcome.error_kind) == ("error", "executor")
    assert outcome.error == "Invalid API key · Please run /login"
    assert outcome.structured is None


def test_other_errors() -> None:
    outcome = parse(
        *started(result(subtype="error_during_execution", is_error=True, result=None))
    ).result()

    assert (outcome.status, outcome.error_kind) == ("error", "executor")
    assert outcome.error == "Claude Code reported an error (error_during_execution)"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("subtype", ...),
        ("subtype", None),
        ("subtype", 1),
        ("is_error", ...),
        ("is_error", "false"),
        ("total_cost_usd", ...),
        ("total_cost_usd", None),
        ("total_cost_usd", "0.01"),
        ("total_cost_usd", True),
        ("total_cost_usd", -1),
    ],
)
def test_a_result_without_the_fields_we_need_is_an_error(
    field: str, value: Any
) -> None:
    outcome = parse(*started(result(**{field: value}))).result()

    assert (outcome.status, outcome.error_kind) == ("error", "executor")
    assert outcome.error == (
        f"Claude Code's result has no valid '{field}'; "
        "this version may not be supported"
    )


def test_usage_is_read_tolerantly() -> None:
    outcome = parse(
        *started(
            result(usage={"input_tokens": 5, "output_tokens": "x"}, duration_ms=None)
        )
    ).result(duration_s=2.0)

    assert outcome.usage == Usage(input_tokens=5)
    assert outcome.duration_s == 2.0


# --- rate limits ------------------------------------------------------------

RESET_AT = dt.datetime(2026, 9, 27, 22, 40, tzinfo=dt.UTC)
RESET = int(RESET_AT.timestamp())


def rate_limit_event(status: str, **info: Any) -> dict[str, Any]:
    return {
        "type": "rate_limit_event",
        "rate_limit_info": {"status": status, **info},
    }


def test_a_rate_limit_warning_is_only_an_event(recorder: Recorder) -> None:
    parser = parse(
        *started(rate_limit_event("allowed_warning", resetsAt=RESET), result()),
        recorder=recorder,
    )

    assert recorder.of(RateLimit) == [RateLimit("allowed_warning", RESET_AT)]
    assert parser.result().status == "ok"
    assert parser.result().rate_limit_reset is None


def test_a_rejected_rate_limit(recorder: Recorder) -> None:
    parser = parse(
        *started(
            rate_limit_event("rejected", resetsAt=RESET, rateLimitType="five_hour"),
            result(is_error=True, api_error_status=429, structured_output=...),
        ),
        recorder=recorder,
    )
    outcome = parser.result()

    assert outcome.status == "rate_limited"
    assert outcome.rate_limit_reset == RESET_AT
    assert recorder.of(RateLimit)[0].status == "rejected"


def test_a_finished_session_keeps_its_result_after_a_rate_limit() -> None:
    outcome = parse(
        *started(rate_limit_event("rejected", resetsAt=RESET), result())
    ).result()

    assert outcome.status == "ok"
    assert outcome.structured == {"status": "ok"}
    assert outcome.rate_limit_reset == RESET_AT


def test_a_rejected_rate_limit_without_a_result() -> None:
    outcome = parse(*started(rate_limit_event("rejected"))).result()

    assert outcome.status == "rate_limited"
    assert outcome.rate_limit_reset is None


@pytest.mark.parametrize(
    "info",
    [
        None,
        {"resetsAt": RESET},
        {"status": 3},
        {"status": "rejected", "resetsAt": "soon"},
    ],
)
def test_odd_rate_limit_events_are_tolerated(info: Any) -> None:
    parser = parse(*started({"type": "rate_limit_event", "rate_limit_info": info}))

    assert parser.violation is None
    assert parser.result().rate_limit_reset is None


def test_an_api_429_is_a_rate_limit() -> None:
    outcome = parse(
        *started(
            result(
                is_error=True,
                api_error_status=429,
                result="API Error: Request rejected (429)",
                structured_output=...,
            )
        )
    ).result()

    assert outcome.status == "rate_limited"
    assert outcome.error == "API Error: Request rejected (429)"


def test_a_rate_limit_error_on_the_assistant_message() -> None:
    event = text("API Error: Request rejected")
    event["error"] = "rate_limit"
    outcome = parse(*started(event, result(is_error=True, result="failed"))).result()

    assert outcome.status == "rate_limited"


def test_rate_limit_text_is_only_a_fallback() -> None:
    limited = parse(
        *started(result(is_error=True, result="Claude usage limit reached"))
    ).result()
    other = parse(*started(result(is_error=True, result="Overloaded"))).result()

    assert limited.status == "rate_limited"
    assert other.status == "error"


# --- the recorded fixtures --------------------------------------------------


def replayed(name: str, expected: list[str] | None, **options: Any) -> Any:
    recorder = Recorder()
    head = (
        [] if name.startswith(("session-start", "invalid-settings")) else SESSION_START
    )
    outcome = replay(
        [*head, *fixture_lines(name)],
        expected_tools=expected,
        events=recorder,
        workdir=WORKDIR,
        **options,
    )
    return outcome, recorder


def test_replay_research() -> None:
    outcome, recorder = replayed("research-ok.mock.jsonl", RESEARCH_TOOLS)

    assert outcome.status == "ok"
    assert outcome.structured == {"notes": "nothing found", "sources": []}
    assert outcome.cost_usd == 0.0004
    assert outcome.usage == Usage(input_tokens=150, output_tokens=30)
    assert outcome.duration_s == 0.153
    # Read is not in this session's tool set: the CLI refused it unguarded.
    assert outcome.tool_calls == {"WebSearch": 1, "Read": 1}
    assert outcome.guard_denials == 0
    assert recorder.of(ToolCall) == [
        ToolCall("WebSearch", "static site generators comparison"),
        ToolCall("Read", "/etc/hosts"),
    ]


def test_replay_write_with_a_denial() -> None:
    outcome, recorder = replayed("write-ok-with-denial.mock.jsonl", WRITE_TOOLS)

    assert outcome.status == "ok"
    assert outcome.structured == {"status": "ok", "summary": "Wrote hello into out.md."}
    assert outcome.cost_usd == 0.0145
    assert outcome.usage == Usage(4500, 300, 60000, 2000, 0, 0)
    assert outcome.tool_calls == {"Read": 1, "Glob": 1, "Write": 2}
    assert outcome.guard_denials == 1
    assert [event.target for event in recorder.of(ToolCall)] == [
        "brief.md",
        "*.md",
        "out.md",
        "notes.md",
    ]
    [denied] = recorder.of(Denied)
    assert (denied.name, denied.target) == ("Write", "notes.md")
    assert denied.reason.startswith("spadework guard: writing /WORKDIR/notes.md")
    assert recorder.of(Text) == [Text("Reading the brief.")]


def test_replay_api_429() -> None:
    outcome, recorder = replayed("api-429.mock.jsonl", [])

    assert outcome.status == "rate_limited"
    assert outcome.cost_usd == 0
    assert outcome.error == "API Error: Request rejected (429) · mock: rate limited"
    assert recorder.of(Text) == [
        Text("API Error: Request rejected (429) · mock: rate limited")
    ]


def test_replay_max_budget() -> None:
    # Recorded without the guard hooks, so only the result is checked.
    outcome, _ = replayed("max-budget.mock.jsonl", WRITE_TOOLS, require_guard=False)

    assert outcome.status == "budget"
    assert outcome.cost_usd == 0.041
    assert outcome.error == "Reached maximum budget ($0.03)"
    assert outcome.tool_calls == {"Read": 2}


def test_replay_max_budget_with_guard_checks_catches_the_unguarded_read() -> None:
    outcome, _ = replayed("max-budget.mock.jsonl", WRITE_TOOLS)

    assert outcome.error == "a Read call ran without an answer from the guard"


def test_replay_schema_retries_exhausted() -> None:
    outcome, recorder = replayed("schema-retries-exhausted.mock.jsonl", WRITE_TOOLS)

    assert (outcome.status, outcome.error_kind) == ("error", "schema")
    assert outcome.error.startswith(
        "Failed to provide valid structured output after 5 attempts"
    )
    assert outcome.cost_usd == 0.0005
    assert outcome.tool_calls == {}
    assert recorder.of(ToolCall) == []


def test_replay_guard_spawn_failure() -> None:
    outcome, _ = replayed(
        "guard-spawn-failure.mock.jsonl", ["Edit", "Glob", "Grep", "Read", "Write"]
    )

    assert (outcome.status, outcome.error_kind) == ("error", "sandbox")
    assert outcome.error == (
        "the sandbox guard failed on a Write call (exit code 127): "
        "/bin/sh: /nonexistent/python3: No such file or directory"
    )
    assert outcome.cost_known is False


def test_replay_session_start_canary() -> None:
    outcome, recorder = replayed("session-start-canary.mock.jsonl", ["Read"])

    # The canary passes; this session was run without --json-schema.
    assert (outcome.status, outcome.error_kind) == ("error", "schema")
    assert outcome.cost_usd == 0.0001
    assert recorder.of(Text) == [Text("hello")]


def test_replay_invalid_settings_is_caught_at_init() -> None:
    log = io.StringIO()
    outcome, recorder = replayed(
        "invalid-settings-no-guard.mock.jsonl", ["Read"], log=log
    )

    assert (outcome.status, outcome.error) == (
        "error",
        "the sandbox guard did not start",
    )
    assert outcome.cost_known is True
    assert recorder.events == []
    assert log.getvalue().count("\n") == 1  # stopped at init


def test_fixtures_are_one_json_object_per_line() -> None:
    names = sorted(path.name for path in fixture_dir().glob("*.jsonl"))
    assert len(names) == 8
    for name in names:
        for line in fixture_lines(name):
            assert isinstance(json.loads(line), dict)


def fixture_dir() -> Path:
    return Path(__file__).parent.parent / "fixtures" / "stream"


# --- targets ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "tool_input", "target"),
    [
        ("Read", {"file_path": "/WORKDIR/inputs/notes.md"}, "inputs/notes.md"),
        ("Write", {"file_path": "/elsewhere/x.md"}, "/elsewhere/x.md"),
        ("Grep", {"pattern": "price", "path": "/WORKDIR"}, "price"),
        ("Glob", {"pattern": "**/*.md"}, "**/*.md"),
        ("WebSearch", {"query": "headless  CMS\npricing"}, "headless CMS pricing"),
        ("WebFetch", {"url": "https://cms.example/" + "p" * 100}, None),
        ("Read", {"file_path": 3}, ""),
        ("Mystery", {}, ""),
    ],
)
def test_describe_target(
    name: str, tool_input: dict[str, Any], target: str | None
) -> None:
    described = describe_target(name, tool_input, WORKDIR)

    if target is None:
        assert len(described) == 80
        assert described.endswith("…")
    else:
        assert described == target


def test_describe_target_without_a_workdir() -> None:
    assert describe_target("Read", {"file_path": "/WORKDIR/a.md"}) == "/WORKDIR/a.md"


# --- odd but survivable input -----------------------------------------------


def test_odd_tool_events_are_tolerated(recorder: Recorder) -> None:
    parser = parse(
        *started(
            session_start(exit_code=1),  # after the canary passed: ignored
            {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
            tool_use("t1", "Read", {"file_path": "/WORKDIR/a.md"}),
            tool_use("t1", "Read", {"file_path": "/WORKDIR/a.md"}),  # repeated
            {"type": "assistant", "message": {"content": [{"type": "text"}]}},
            pre_tool_use("Read", stdout="not json"),
            tool_result("t1"),
            tool_result("t1"),
            tool_result("unknown", is_error=True),
            {
                "type": "user",
                "message": {"content": [{"type": "tool_result", "is_error": True}]},
            },
            pre_tool_use("Glob", exit_code=2, stderr="spadework guard: late\n"),
            pre_tool_use("Grep"),
            result(),
        ),
        recorder=recorder,
    )

    assert parser.violation is None
    assert parser.result().tool_calls == {"Read": 1}
    assert recorder.of(Denied) == [Denied("Glob", "", "spadework guard: late")]


def test_a_second_failed_session_start_keeps_the_first_reason() -> None:
    parser = parse(
        session_start(exit_code=2, stderr="first\n"),
        session_start(exit_code=2, stderr="second\n"),
        init(WRITE_TOOLS),
    )

    assert parser.violation == "the sandbox guard did not start (first)"


def test_a_guard_failure_without_detail() -> None:
    parser = parse(*started(pre_tool_use("", exit_code=1, stderr="")))

    assert parser.violation == "the sandbox guard failed on a tool call (exit code 1)"


def test_only_the_first_violation_is_kept() -> None:
    parser = StreamParser(expected_tools=WRITE_TOOLS)
    parser._fail("first")
    parser._fail("second")

    assert parser.violation == "first"


@pytest.mark.parametrize("reset", [10**20, -5, 1.5e300])
def test_impossible_reset_times_are_dropped(reset: float) -> None:
    parser = parse(*started(rate_limit_event("rejected", resetsAt=reset)))

    assert parser.result().rate_limit_reset is None
    assert parser.result().status == "rate_limited"


def test_hook_stderr_that_is_not_text() -> None:
    parser = parse(*started(pre_tool_use("Read", exit_code=1) | {"stderr": 3}))

    assert parser.violation == "the sandbox guard failed on a Read call (exit code 1)"


# --- review regressions -----------------------------------------------------


@pytest.mark.parametrize(
    "first",
    [
        text("hello"),
        tool_use("t1", "Read", {"file_path": "/WORKDIR/a.md"}),
        tool_result("t1"),
        result(),
    ],
)
def test_nothing_may_come_before_init(first: dict[str, Any]) -> None:
    parser = parse(first, session_start(), init(WRITE_TOOLS), result())
    outcome = parser.result()

    assert parser.violation == (
        f"Claude Code sent '{first['type']}' before 'init', "
        "so the sandbox could not be checked"
    )
    assert (outcome.status, outcome.error_kind) == ("error", "sandbox")
    assert outcome.cost_known is False


def test_a_stream_without_init_is_never_ok() -> None:
    for require_guard in (True, False):
        outcome = parse(
            tool_use("t1", "Read", {}),
            pre_tool_use("Read"),
            tool_result("t1"),
            result(),
            require_guard=require_guard,
        ).result()
        assert outcome.status != "ok"


def test_the_cost_is_unknown_once_the_model_has_spoken() -> None:
    parser = parse(session_start(), init(WRITE_TOOLS), text("thinking"))

    assert parser.result(ended="timeout").cost_known is False


@pytest.mark.parametrize("code", [False, True, "0", None])
def test_only_an_integer_is_an_exit_code(code: Any) -> None:
    start = session_start() | {"exit_code": code}
    assert parse(start, init(WRITE_TOOLS)).violation == (
        f"the sandbox guard did not start (exit code {code!r})"
    )

    answer = pre_tool_use("Read") | {"exit_code": code}
    parser = parse(*started(tool_use("t1", "Read", {}), answer))
    assert parser.violation == (
        f"the sandbox guard failed on a Read call (exit code {code!r})"
    )


def test_a_hook_response_without_hook_event_is_still_read() -> None:
    crashed = pre_tool_use("Read", exit_code=1, stderr="boom")
    del crashed["hook_event"]
    parser = parse(*started(tool_use("t1", "Read", {}), crashed))

    assert (
        parser.violation
        == "the sandbox guard failed on a Read call (exit code 1): boom"
    )

    start = session_start()
    del start["hook_event"]
    assert parse(start, init(WRITE_TOOLS)).violation is None


def two_reads() -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
                {"type": "tool_use", "id": "b", "name": "Read", "input": {}},
            ]
        },
    }


def results(*call_ids: str) -> dict[str, Any]:
    blocks = [
        {"type": "tool_result", "tool_use_id": call_id, "content": "x"}
        for call_id in call_ids
    ]
    return {"type": "user", "message": {"content": blocks}}


def test_each_call_that_runs_needs_its_own_allow() -> None:
    parser = parse(*started(two_reads(), pre_tool_use("Read"), results("b", "a")))

    assert parser.violation == "a Read call ran without an answer from the guard"


def test_answers_are_matched_by_tool_in_any_order() -> None:
    parser = parse(
        *started(
            pre_tool_use("Read"),  # an answer may come before its tool_use event
            two_reads(),
            pre_tool_use("Read"),
            results("b", "a"),
            result(),
        )
    )

    assert parser.violation is None
    assert parser.result().status == "ok"


def test_an_allow_for_one_tool_does_not_cover_another() -> None:
    parser = parse(
        *started(
            tool_use("t1", "Write", {"file_path": "/WORKDIR/out.md"}),
            pre_tool_use("Read"),
            tool_result("t1"),
        )
    )

    assert parser.violation == "a Write call ran without an answer from the guard"


def test_a_denial_does_not_count_as_an_allow() -> None:
    parser = parse(
        *started(
            tool_use("t1", "Read", {}),
            pre_tool_use("Read", exit_code=2, stderr="no"),
            tool_result("t1"),
        )
    )

    assert parser.violation == "a Read call ran without an answer from the guard"


def test_an_unannounced_tool_that_runs_is_a_violation() -> None:
    parser = parse(*started(pre_tool_use("Read"), tool_result("zzz")))

    assert parser.violation == "a tool call that Claude Code did not announce ran"


def test_denials_are_matched_to_their_calls(recorder: Recorder) -> None:
    reason_a = "spadework guard: writing /WORKDIR/a.md is not allowed"
    reason_b = "spadework guard: writing /WORKDIR/b.md is not allowed"
    blocks = [
        {
            "type": "tool_use",
            "id": call_id,
            "name": "Write",
            "input": {"file_path": path},
        }
        for call_id, path in (("a", "/WORKDIR/a.md"), ("b", "/WORKDIR/b.md"))
    ]
    parser = parse(
        *started(
            {"type": "assistant", "message": {"content": blocks}},
            pre_tool_use("Write", exit_code=2, stderr=reason_a),
            pre_tool_use("Write", stdout=json_deny(reason_b)),
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "b",
                            "is_error": True,
                            "content": [{"type": "text", "text": reason_b}],
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "a",
                            "is_error": True,
                            "content": f"PreToolUse:Write hook error: {reason_a}",
                        },
                    ]
                },
            },
            result(),
        ),
        recorder=recorder,
    )

    assert parser.violation is None
    assert recorder.of(Denied) == [
        Denied("Write", "b.md", reason_b),
        Denied("Write", "a.md", reason_a),
    ]


def test_a_denial_without_a_result_is_reported_at_the_end(recorder: Recorder) -> None:
    parser = parse(
        *started(
            tool_use("t1", "WebFetch", {"url": "http://127.0.0.1/"}),
            pre_tool_use("WebFetch", exit_code=2, stderr="spadework guard: private"),
        ),
        recorder=recorder,
    )
    outcome = parser.result(ended="timeout")

    assert recorder.of(Denied) == [
        Denied("WebFetch", "http://127.0.0.1/", "spadework guard: private")
    ]
    assert outcome.guard_denials == 1
    parser.result()
    assert len(recorder.of(Denied)) == 1  # reported once


def test_reset_times_in_milliseconds() -> None:
    parser = parse(*started(rate_limit_event("rejected", resetsAt=RESET * 1000)))

    assert parser.result().rate_limit_reset == RESET_AT


@pytest.mark.parametrize("status", [500, 502, 529])
def test_server_errors_are_transient(status: int) -> None:
    outcome = parse(
        *started(
            result(
                is_error=True,
                api_error_status=status,
                result=f"API Error: {status}",
                structured_output=...,
            )
        )
    ).result()

    assert (outcome.status, outcome.error_kind) == ("error", "transient")
    assert outcome.error == f"API Error: {status}"


def test_a_server_error_without_text() -> None:
    outcome = parse(
        *started(
            result(
                subtype="error_during_execution",
                is_error=True,
                api_error_status=503,
                structured_output=...,
            )
        )
    ).result()

    assert outcome.error == "API error 503"


def test_the_guard_marker_is_checked_at_init() -> None:
    checks: list[bool] = []

    def alive(answer: bool) -> Any:
        def check() -> bool:
            checks.append(answer)
            return answer

        return check

    missing = parse(session_start(), init(WRITE_TOOLS), guard_alive=alive(False))
    present = parse(session_start(), init(WRITE_TOOLS), guard_alive=alive(True))
    unchecked = parse(init(WRITE_TOOLS), guard_alive=alive(False), require_guard=False)

    assert missing.violation == "the sandbox guard did not start"
    assert present.violation is None
    assert unchecked.violation is None
    assert checks == [False, True]
