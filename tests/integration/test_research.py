"""A research session run by the real ``claude``: web tools only, staged in
an empty folder under the OS temporary directory, and never able to reach a
private address."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from spadework.executors.base import SessionKind, SessionSpec
from spadework.executors.claude_cli import ClaudeCliExecutor
from spadework.records import ResearchResult, output_schema, validate_result
from spadework.sandbox.staging import Staging
from spadework.sandbox.tools import STRUCTURED_OUTPUT, TOOLS_BY_KIND
from tests.integration.conftest import Isolation
from tests.integration.mock_api import MockApi, steps, text, tool_use

RESEARCH_TOOLS = {*TOOLS_BY_KIND["research"], STRUCTURED_OUTPUT}
FINDINGS: dict[str, Any] = {
    "sources": [],
    "notes": "Nothing could be verified.",
    "unanswered": ["Which catalogue platforms suit a small fabricator?"],
}


def research(
    isolation: Isolation, *, max_searches: int = 2
) -> tuple[Staging, SessionSpec]:
    staging = Staging.create(isolation.project, "003-landscape-research", "research")
    staging.write_policy(
        deadline_epoch=time.time() + 120, max_searches=max_searches, max_fetches=6
    )
    spec = SessionSpec(
        session_id="003-landscape-research",
        kind=SessionKind.RESEARCH,
        prompt="Which catalogue platforms suit a small stainless-steel fabricator?",
        system_append="Cite every claim.",
        model="haiku",
        tools=TOOLS_BY_KIND["research"],
        workdir=staging.paths.workdir,
        policy_path=staging.paths.policy,
        output_schema=output_schema("research"),
        max_budget_usd=0.5,
        timeout_s=120,
        log_path=isolation.project / ".spadework" / "runs" / "r" / "003.jsonl",
        task="landscape",
    )
    return staging, spec


@pytest.fixture
def cleanup() -> Any:
    made: list[Staging] = []
    yield made.append
    for staging in made:
        staging.cleanup()


def test_a_research_session_is_staged_empty_outside_the_project(
    api: MockApi, isolation: Isolation, cleanup: Any
) -> None:
    staging, spec = research(isolation)
    cleanup(staging)
    workdir = staging.paths.workdir
    api.respond = steps(tool_use("StructuredOutput", FINDINGS), text("Done."))

    result = ClaudeCliExecutor().run(spec, lambda event: None)

    assert result.status == "ok", result.error
    assert isinstance(validate_result("research", result.structured), ResearchResult)
    assert workdir.is_relative_to(Path(isolation.temp).resolve())
    assert workdir.name.startswith("spadework-")
    assert list(workdir.iterdir()) == []
    # The model is told its working directory; it names no project.
    assert isolation.project.name not in api.main_requests[0].everything
    isolation.check(api, expected_tools=RESEARCH_TOOLS)


def test_fetches_of_private_addresses_are_denied_and_never_made(
    api: MockApi, isolation: Isolation, cleanup: Any
) -> None:
    staging, spec = research(isolation)
    cleanup(staging)
    port = api.url.rsplit(":", 1)[1]
    urls = [
        f"http://127.0.0.1:{port}/page",
        f"http://localhost:{port}/page",
        f"http://[::1]:{port}/page",
        f"http://2130706433:{port}/page",
        "http://169.254.169.254/latest/meta-data/",
        "https://user:secret@example.com/",
        "file:///etc/passwd",
    ]
    prompt = "Summarise the page."
    api.respond = steps(
        *(tool_use("WebFetch", {"url": url, "prompt": prompt}) for url in urls),
        tool_use("Read", {"file_path": "/etc/passwd"}),
        tool_use("StructuredOutput", FINDINGS),
    )

    result = ClaudeCliExecutor().run(spec, lambda event: None)

    assert result.status == "ok", result.error
    denied = [record["tool_name"] for record in staging.denials()]
    assert denied == ["WebFetch"] * len(urls)
    assert result.guard_denials == len(urls)
    # Not one request reached the mock's web page.
    assert [r.path for r in api.requests if r.method == "GET"] == []
    reasons = [record["reason"] for record in staging.denials()]
    assert (
        reasons[:5]
        == ["spadework: WebFetch cannot fetch local or private network addresses."] * 5
    )
    assert staging.counters() == {}
    isolation.check(api, expected_tools=RESEARCH_TOOLS)


def test_searches_beyond_the_cap_are_denied(
    api: MockApi, isolation: Isolation, cleanup: Any
) -> None:
    staging, spec = research(isolation, max_searches=1)
    cleanup(staging)
    api.respond = steps(
        tool_use("WebSearch", {"query": "catalogue website platforms"}),
        tool_use("WebSearch", {"query": "stainless steel fabricators websites"}),
        tool_use("StructuredOutput", FINDINGS),
    )

    result = ClaudeCliExecutor().run(spec, lambda event: None)

    assert result.status == "ok", result.error
    assert staging.counters() == {"WebSearch": 1, "total": 1}
    records = staging.denials()
    assert [record["tool_name"] for record in records] == ["WebSearch"]
    assert "WebSearch" in records[0]["reason"]
    assert result.tool_calls == {"WebSearch": 2}
    isolation.check(api, expected_tools=RESEARCH_TOOLS)
