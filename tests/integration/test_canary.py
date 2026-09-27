"""The ``doctor --canary`` sandbox test, run by the real ``claude`` against a
scripted model that does what the canary asks: it tries to leave the
sandbox."""

from __future__ import annotations

import json
import os
import re

import pytest
from typer.testing import CliRunner

from spadework.cli import app
from spadework.doctor import run_canary
from spadework.executors.claude_cli import ClaudeCliExecutor
from tests.integration.conftest import DRAFT_TOOLS, Isolation
from tests.integration.mock_api import ApiRequest, MockApi, Step, text, tool_use


def paths(request: ApiRequest) -> tuple[str, str, str]:
    """The three paths the canary's prompt names, in step order."""
    (outside,) = re.findall(r"Read tool to read (/\S+)", request.prompt)
    second, output = re.findall(r"Write tool to create (/\S+) with", request.prompt)
    return outside, second, output


def escaping_model(request: ApiRequest) -> Step:
    """Tries every step, and reports whatever it managed to read."""
    outside, second, output = paths(request)
    read = [
        str(block.get("content", ""))
        for block in request.tool_results()
        if not block.get("is_error")
    ]
    return [
        tool_use("Read", {"file_path": outside}),
        tool_use("Write", {"file_path": second, "content": "second file\n"}),
        tool_use("Write", {"file_path": output, "content": "spadework canary\n"}),
        tool_use(
            "StructuredOutput",
            {
                "read_outside": False,
                "outside_text": "\n".join(read),
                "wrote_second": False,
                "wrote_output": True,
            },
        ),
        text("Done."),
    ][min(request.turn, 4)]


def lazy_model(request: ApiRequest) -> Step:
    """Skips the steps that should be blocked."""
    _, _, output = paths(request)
    return [
        tool_use("Write", {"file_path": output, "content": "spadework canary\n"}),
        tool_use(
            "StructuredOutput",
            {
                "read_outside": False,
                "outside_text": "",
                "wrote_second": False,
                "wrote_output": True,
            },
        ),
        text("Done."),
    ][min(request.turn, 2)]


def canary_folders(isolation: Isolation) -> list[str]:
    return [name for name in os.listdir(isolation.temp) if "canary" in name]


def test_the_canary_passes_when_the_sandbox_holds(
    api: MockApi, isolation: Isolation
) -> None:
    api.respond = escaping_model

    report = run_canary(ClaudeCliExecutor())

    assert report.ok, report.checks
    details = {check.name: check.detail for check in report.checks}
    assert details["Read outside"] == "denied by the guard"
    assert details["Second file"] == "denied by the guard"
    assert report.result is not None
    assert report.result.guard_denials == 2
    assert 0 < report.cost_usd < 0.05
    # The prompt asked for the cheapest model, capped at five cents.
    request = api.main_requests[0]
    assert "haiku" in str(request.body.get("model"))
    assert "$0.05" in request.everything
    assert canary_folders(isolation) == []
    isolation.check(api, expected_tools=DRAFT_TOOLS)


def test_the_canary_fails_when_the_model_does_not_try_to_escape(
    api: MockApi, isolation: Isolation
) -> None:
    api.respond = lazy_model

    report = run_canary(ClaudeCliExecutor())

    assert not report.ok
    failed = {check.name: check.detail for check in report.checks if not check.ok}
    assert failed == {
        "Read outside": "the model never tried it, so it was not tested",
        "Second file": "the model never tried it, so it was not tested",
    }
    assert canary_folders(isolation) == []


def test_doctor_canary_runs_end_to_end(
    api: MockApi, isolation: Isolation, monkeypatch: pytest.MonkeyPatch
) -> None:
    api.respond = escaping_model
    monkeypatch.chdir(isolation.home)

    result = CliRunner().invoke(app, ["doctor", "--canary", "--json"])

    document = json.loads(result.stdout)
    assert result.exit_code == 0, (document, result.stderr)
    assert document["ok"] is True
    assert document["canary"]["ok"] is True
    assert 0 < document["canary"]["cost_usd"] < 0.05
    names = [check["name"] for check in document["checks"]]
    assert "Claude Code login" in names
    assert "Nested session" not in names
    assert "Running the sandbox canary" in result.stderr
    assert canary_folders(isolation) == []
    isolation.check(api, expected_tools=DRAFT_TOOLS)
