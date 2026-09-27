"""Draft sessions run by the real ``claude`` through ``ClaudeCliExecutor``,
with the real staging directory and the real guard, against a scripted
model that tries to leave the sandbox."""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import pytest

from spadework.executors import claude_cli, process
from spadework.executors.base import Denied, RateLimit, SessionSpec, ToolCall
from spadework.records import DraftResult, validate_result
from tests.integration.conftest import (
    BRIEF,
    DRAFT_TOOLS,
    SECRET,
    Isolation,
    MakeSession,
    Session,
    draft_result,
)
from tests.integration.mock_api import ApiRequest, MockApi, Step, steps, text, tool_use

EXPENSIVE = {"input_tokens": 20_000, "output_tokens": 100}
"""About two US cents a call at haiku's list price."""


def denied(session: Session) -> list[tuple[str, str]]:
    """The guard's own log: (tool, target) for every call it denied."""
    return [
        (str(record["tool_name"]), str(record["target"]))
        for record in session.staging.denials()
    ]


def reasons(session: Session) -> list[str]:
    return [str(record["reason"]) for record in session.staging.denials()]


def tool_results(api: MockApi) -> list[dict[str, Any]]:
    """Every tool result the model was sent."""
    return [block for request in api.main_requests for block in request.tool_results()]


def seen_by_model(api: MockApi) -> str:
    return "\n".join(request.everything for request in api.message_requests)


def test_a_draft_session_writes_its_output_and_returns_an_ok_result(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    session = make_session()
    document = "# Scope\n\nA catalogue website for Acme Fabrication.\n"
    api.respond = steps(
        {
            "content": [
                {"type": "text", "text": "Reading the brief first."},
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": str(session.workdir / "brief.md")},
                },
            ]
        },
        tool_use("Write", {"file_path": str(session.output), "content": document}),
        tool_use("StructuredOutput", draft_result()),
        text("Done."),
    )

    result = session.run()

    assert result.status == "ok", result.error
    assert isinstance(validate_result("draft", result.structured), DraftResult)
    assert session.output.read_text(encoding="utf-8") == document
    assert session.staging.collect().extras == ()
    assert result.tool_calls == {"Read": 1, "Write": 1}
    assert result.guard_denials == 0
    assert session.staging.denials() == []
    assert session.staging.counters() == {"Read": 1, "Write": 1, "total": 2}
    assert result.cost_known
    assert 0 < result.cost_usd < 0.01
    assert result.usage.input_tokens > 0
    assert [e for e in session.events if isinstance(e, ToolCall)] == [
        ToolCall("Read", "brief.md"),
        ToolCall("Write", "01-scope.md"),
    ]
    # The Read really ran: the model got the staged brief back.
    assert BRIEF.splitlines()[0] in json.dumps(tool_results(api))
    # The session's instructions reached the system prompt.
    system = json.dumps(api.main_requests[0].body.get("system"))
    assert "Cite nothing you did not read." in system
    # The raw log holds every line, starting with the guard's SessionStart.
    first = json.loads(session.log().splitlines()[0])
    assert first.get("hook_event") == "SessionStart" or first.get("subtype") in (
        "hook_started",
        "hook_response",
    )
    assert claude_cli.guard_marker(session.spec).read_text() == session.spec.session_id
    isolation.check(api, expected_tools=DRAFT_TOOLS)


def test_reads_outside_the_workdir_are_denied_and_counted(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    session = make_session()
    project = str(isolation.project)
    api.respond = steps(
        tool_use("Read", {"file_path": str(session.secret)}),
        tool_use("Read", {"file_path": "../../../secret.txt"}),
        tool_use("Glob", {"pattern": "*.txt", "path": project}),
        tool_use("Grep", {"pattern": "SECRET", "path": project}),
        tool_use("Glob", {"pattern": "../../../*.txt"}),
        tool_use("StructuredOutput", draft_result()),
    )

    result = session.run()

    assert result.status == "ok", result.error
    expected = ["Read", "Read", "Glob", "Grep", "Glob"]
    assert [tool for tool, _ in denied(session)] == expected
    assert result.guard_denials == 5
    assert [e.name for e in session.events if isinstance(e, Denied)] == expected
    assert all(block.get("is_error") for block in tool_results(api)[:5])
    assert SECRET not in seen_by_model(api)
    assert SECRET not in session.log()
    assert session.staging.counters() == {}
    isolation.check(api, expected_tools=DRAFT_TOOLS)


def test_writes_to_any_file_but_the_output_are_denied(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    session = make_session()
    workdir = session.workdir
    brief = workdir / "brief.md"
    api.respond = steps(
        tool_use("Write", {"file_path": str(workdir / "notes.md"), "content": "x"}),
        # A case variant of the output, which is the same file on macOS.
        tool_use("Write", {"file_path": str(workdir / "01-Scope.md"), "content": "x"}),
        tool_use("Read", {"file_path": str(brief)}),
        tool_use(
            "Edit",
            {
                "file_path": str(brief),
                "old_string": "Staged brief.",
                "new_string": "Edited brief.",
            },
        ),
        tool_use("Write", {"file_path": str(session.output), "content": "# Scope\n"}),
        tool_use("StructuredOutput", draft_result()),
    )

    result = session.run()

    assert result.status == "ok", result.error
    assert [tool for tool, _ in denied(session)] == ["Write", "Write", "Edit"]
    assert result.guard_denials == 3
    assert not (workdir / "notes.md").exists()
    assert brief.read_text(encoding="utf-8") == BRIEF
    assert session.output.read_text(encoding="utf-8") == "# Scope\n"
    assert session.staging.collect().extras == ()
    assert session.staging.counters() == {"Read": 1, "Write": 1, "total": 2}
    isolation.check(api, expected_tools=DRAFT_TOOLS)


def test_the_control_plane_cannot_be_written_or_read(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    session = make_session()
    paths = session.staging.paths
    policy_before = paths.policy.read_bytes()
    settings = claude_cli.settings_path(session.spec)
    api.respond = steps(
        tool_use(
            "Write", {"file_path": str(paths.control_dir / "x.json"), "content": "{}"}
        ),
        tool_use("Read", {"file_path": str(paths.policy)}),
        # Overwriting a file needs a Read of it first, which Claude Code
        # checks before any hook: blocked, but not by the guard.
        tool_use("Write", {"file_path": str(paths.policy), "content": "{}"}),
        tool_use(
            "Edit",
            {"file_path": str(settings), "old_string": "hooks", "new_string": "x"},
        ),
        tool_use("Glob", {"pattern": "*", "path": str(paths.control_dir)}),
        tool_use("StructuredOutput", draft_result()),
    )

    result = session.run()

    assert result.status == "ok", result.error
    assert ("Write", str(paths.control_dir / "x.json")) in denied(session)
    assert ("Read", str(paths.policy)) in denied(session)
    assert ("Glob", "*") in denied(session) or ("Glob", str(paths.control_dir)) in (
        denied(session)
    )
    assert not (paths.control_dir / "x.json").exists()
    assert paths.policy.read_bytes() == policy_before
    assert json.loads(settings.read_text()) == claude_cli.settings_for(paths.policy)
    assert all(block.get("is_error") for block in tool_results(api)[:5])
    assert '"session_id"' not in json.dumps(tool_results(api))
    assert session.staging.counters() == {}
    isolation.check(api, expected_tools=DRAFT_TOOLS)


@pytest.mark.parametrize(
    "call",
    [
        ("Grep", {"pattern": "root", "path": " /etc", "output_mode": "content"}),
        ("Glob", {"pattern": "*", "path": " /etc"}),
        ("Glob", {"pattern": "*", "path": "/etc "}),
        ("Grep", {"pattern": "root", "path": "\t/etc", "output_mode": "content"}),
    ],
    ids=["grep-leading-space", "glob-leading-space", "glob-trailing", "grep-tab"],
)
def test_paths_with_surrounding_whitespace_are_denied(
    api: MockApi,
    isolation: Isolation,
    make_session: MakeSession,
    call: tuple[str, dict[str, Any]],
) -> None:
    session = make_session()
    name, tool_input = call
    api.respond = steps(
        tool_use(name, tool_input), tool_use("StructuredOutput", draft_result())
    )

    result = session.run()

    assert result.status == "ok", result.error
    assert [tool for tool, _ in denied(session)] == [name]
    assert "whitespace" in reasons(session)[0]
    assert result.guard_denials == 1
    first = tool_results(api)[0]
    assert first.get("is_error") is True
    for listed in ("passwd", "hosts", "root:"):
        assert listed not in json.dumps(first)
    isolation.check(api, expected_tools=DRAFT_TOOLS)


def test_a_session_whose_guard_cannot_start_is_stopped_before_any_model_call(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    # No policy: the guard's SessionStart check fails, as it would for a
    # settings file that points it at a policy that is missing.
    session = make_session(write_policy=False)
    api.respond = steps(tool_use("StructuredOutput", draft_result()))

    result = session.run()

    assert result.status == "error"
    assert result.error_kind == "sandbox"
    assert result.error is not None
    assert result.error.startswith("the sandbox guard did not start")
    assert api.message_requests == []
    assert result.cost_known
    assert result.cost_usd == 0
    assert not claude_cli.guard_marker(session.spec).exists()
    assert not session.output.exists()


def test_a_settings_file_claude_code_cannot_parse_is_caught(
    api: MockApi,
    isolation: Isolation,
    make_session: MakeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Claude Code silently ignores a settings file it cannot parse, so the
    # guard's hooks never load. The executor must notice.
    session = make_session()
    write_settings = claude_cli.write_settings

    def unparseable(spec: SessionSpec) -> Any:
        path = write_settings(spec)
        path.write_text("{ this is not JSON", encoding="utf-8")
        return path

    monkeypatch.setattr(claude_cli, "write_settings", unparseable)
    api.respond = steps(
        tool_use("Write", {"file_path": str(session.output), "content": "x"}),
        tool_use("StructuredOutput", draft_result()),
    )

    result = session.run()

    assert result.status == "error"
    assert result.error_kind == "sandbox"
    assert result.error == "the sandbox guard did not start"
    assert api.main_requests == []
    assert not session.output.exists()


def test_a_session_over_its_budget_ends_as_budget(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    session = make_session(max_budget_usd=0.03)
    read = {"file_path": str(session.workdir / "brief.md")}
    api.respond = steps(
        tool_use("Read", read, usage=EXPENSIVE),
        tool_use("Read", read, usage=EXPENSIVE),
        tool_use("Read", read, usage=EXPENSIVE),
        tool_use("StructuredOutput", draft_result(), usage=EXPENSIVE),
    )

    result = session.run()

    assert result.status == "budget", result.error
    assert result.cost_known
    # The cap is checked after each call, so it can be passed by one call.
    assert 0.03 < result.cost_usd < 0.03 + 0.025
    assert len(api.main_requests) == 2
    assert result.structured is None
    isolation.check(api, expected_tools=DRAFT_TOOLS)


def test_a_result_that_never_matches_the_schema_is_a_schema_error(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    session = make_session()
    api.respond = steps(
        tool_use("StructuredOutput", {"status": "finished", "extra": True}),
        repeat_last=True,
    )

    result = session.run()

    assert result.status == "error"
    assert result.error_kind == "schema"
    assert result.structured is None
    assert result.cost_known
    assert result.error is not None
    assert "structured output" in result.error.lower()
    # Claude Code gives the model 5 attempts in all, then gives up.
    assert len(api.main_requests) == 5
    isolation.check(api, expected_tools=DRAFT_TOOLS)


def test_a_session_that_never_returns_a_result_is_a_schema_error(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    session = make_session()
    api.respond = steps(text("I have nothing to report."))

    result = session.run()

    assert result.status == "error"
    assert result.error_kind == "schema"
    assert result.error == "the session ended without a structured result"
    isolation.check(api, expected_tools=DRAFT_TOOLS)


def test_tools_outside_the_allowlist_never_run(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    session = make_session()
    marker = isolation.home / "ran.txt"
    api.respond = steps(
        tool_use("Bash", {"command": f"touch {marker}"}),
        tool_use("WebFetch", {"url": "https://example.com/", "prompt": "Read it."}),
        tool_use("Task", {"description": "x", "prompt": f"touch {marker}"}),
        tool_use("Agent", {"description": "x", "prompt": f"touch {marker}"}),
        tool_use("mcp__planted__run", {"command": f"touch {marker}"}),
        tool_use("NotebookEdit", {"notebook_path": str(marker), "new_source": "x"}),
        tool_use("StructuredOutput", draft_result()),
    )

    result = session.run()

    assert result.status == "ok", result.error
    assert not marker.exists()
    # The last result, StructuredOutput's, ends the session and is never sent.
    results = tool_results(api)
    assert len(results) == 6
    assert all(block.get("is_error") for block in results)
    assert session.staging.counters() == {}
    isolation.check(api, expected_tools=DRAFT_TOOLS)


def test_the_guard_allows_the_arguments_claude_code_really_sends(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    # The guard denies arguments it does not know, so a CLI that sends a new
    # one would break sessions. These are the options the tools document.
    session = make_session()
    session.staging.seed_output("# Scope\n\nOld text.\n")
    output = str(session.output)
    api.respond = steps(
        tool_use("Glob", {"pattern": "*.md"}),
        tool_use("Glob", {"pattern": "**/*.md", "path": str(session.workdir)}),
        tool_use(
            "Grep",
            {
                "pattern": "Acme",
                "path": str(session.workdir),
                "glob": "*.md",
                "output_mode": "content",
                "-n": True,
                "-i": True,
                "-C": 1,
                "head_limit": 5,
            },
        ),
        tool_use("Grep", {"pattern": "brief", "output_mode": "files_with_matches"}),
        tool_use(
            "Read",
            {"file_path": str(session.workdir / "brief.md"), "offset": 1, "limit": 2},
        ),
        tool_use("Read", {"file_path": output}),
        tool_use(
            "Edit",
            {
                "file_path": output,
                "old_string": "Old text.",
                "new_string": "New text.",
                "replace_all": False,
            },
        ),
        tool_use("StructuredOutput", draft_result()),
    )

    result = session.run()

    assert result.status == "ok", result.error
    assert session.staging.denials() == []
    assert result.guard_denials == 0
    assert not any(block.get("is_error") for block in tool_results(api))
    assert session.output.read_text(encoding="utf-8") == "# Scope\n\nNew text.\n"
    assert session.staging.counters() == {
        "Glob": 2,
        "Grep": 2,
        "Read": 2,
        "Edit": 1,
        "total": 7,
    }
    isolation.check(api, expected_tools=DRAFT_TOOLS)


def test_a_session_past_its_time_limit_is_stopped(
    api: MockApi,
    isolation: Isolation,
    make_session: MakeSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "TERM_GRACE_S", 1.0)
    session = make_session(timeout_s=4)
    release = threading.Event()

    def slow(request: ApiRequest) -> Step:
        release.wait(60)
        return tool_use("StructuredOutput", draft_result())

    api.respond = slow
    started = time.monotonic()
    try:
        result = session.run()
    finally:
        release.set()

    assert result.status == "timeout"
    assert not result.cost_known
    assert time.monotonic() - started < 15
    assert result.error == "the session did not finish within its time limit"


RESET = int(time.time()) + 3600
LIMITED = {
    "anthropic-ratelimit-unified-status": "rejected",
    "anthropic-ratelimit-unified-reset": str(RESET),
    "anthropic-ratelimit-unified-representative-claim": "five_hour",
    "anthropic-ratelimit-unified-5h-utilization": "1.0",
    "anthropic-ratelimit-unified-5h-reset": str(RESET),
    "retry-after": "3600",
}


def test_a_rejected_rate_limit_stops_the_session_with_its_reset_time(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    session = make_session()
    api.respond = steps(
        {
            "status": 429,
            "headers": LIMITED,
            "error": {
                "type": "error",
                "error": {"type": "rate_limit_error", "message": "rate limited"},
            },
        }
    )

    result = session.run()

    assert result.status == "rate_limited", result.error
    assert result.error is not None
    assert "429" in result.error
    # With an API key, Claude Code reports the 429 but no rate_limit_event,
    # so the reset time is not known (its headers and retry-after are not
    # passed on). It does not retry either.
    assert result.rate_limit_reset is None
    assert [e for e in session.events if isinstance(e, RateLimit)] == []
    assert len(api.message_requests) == 1


def test_a_rate_limit_warning_does_not_stop_the_session(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    session = make_session()
    warning = {
        **LIMITED,
        "anthropic-ratelimit-unified-status": "allowed_warning",
        "anthropic-ratelimit-unified-5h-utilization": "0.92",
        "anthropic-ratelimit-unified-5h-surpassed-threshold": "0.9",
    }
    warning.pop("retry-after")
    api.respond = steps(tool_use("StructuredOutput", draft_result(), headers=warning))

    result = session.run()

    assert result.status == "ok", result.error
    assert result.rate_limit_reset is None
    # As above: with an API key, Claude Code passes no rate-limit events on.
    assert [e for e in session.events if isinstance(e, RateLimit)] == []
    isolation.check(api, expected_tools=DRAFT_TOOLS)


def test_logs_replays_a_real_session_as_the_live_view_saw_it(
    api: MockApi, isolation: Isolation, make_session: MakeSession
) -> None:
    from spadework.executors.base import Finished
    from spadework.logs import Replay

    session = make_session()
    api.respond = steps(
        {
            "content": [
                {"type": "text", "text": "Reading the brief."},
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": str(session.workdir / "brief.md")},
                },
            ]
        },
        tool_use("Write", {"file_path": str(session.workdir / "x.md"), "content": "x"}),
        tool_use("Write", {"file_path": str(session.output), "content": "# Scope\n"}),
        tool_use("StructuredOutput", draft_result()),
    )
    result = session.run()
    replayed: list[Any] = []

    replay = Replay(replayed.append)
    for line in session.log().splitlines():
        replay.feed(line)
    summary = replay.summary()

    live = [event for event in session.events if not isinstance(event, Finished)]
    assert replayed == live
    assert [type(event).__name__ for event in live] == [
        "Text",
        "ToolCall",
        "ToolCall",
        "Denied",
        "ToolCall",
    ]
    assert summary is not None
    assert (summary.status, summary.guard_denials) == ("ok", 1)
    assert summary.cost_usd == result.cost_usd
