"""The verification step between research and draft, and what reaches the pack."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from spadework.engine import events as ev
from spadework.engine.verification import (
    Found,
    NetworkVerifier,
    UncheckedVerifier,
    VerificationResult,
)
from spadework.records import Source
from spadework.verify.reader import read_in_process
from tests.engine.conftest import Runner, edit_config
from tests.verify.conftest import NOW, Resolver, Web, probe, respond


class Verifier:
    """Marks S1 verified and S2 not found; leaves the rest unchecked."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], float | None]] = []

    def verify_sources(
        self, sources: Sequence[Source], *, time_limit: float | None = None
    ) -> Mapping[str, Found]:
        self.calls.append(([source.id for source in sources], time_limit))
        found: dict[str, Found] = {
            "S1": VerificationResult(
                status="verified",
                detail="the quote is on the page",
                checked_at="2026-09-27T21:47:02Z",
                final_url="https://www.example.com/a",
            ),
            "S2": "quote_not_found",
        }
        return {s.id: found.get(s.id, "unchecked") for s in sources}


ALL = ["S1", "S2", "S3", "S4", "S5"]
"""The fake executor's sources."""
TIME = 7200.0 - 30 - 60
"""The run's time, less what a draft needs to start after the checks."""


def record(runner: Runner) -> dict[str, Any]:
    path = runner.root / ".spadework" / "records" / "landscape.research.json"
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def statuses(runner: Runner) -> dict[str, str]:
    return {
        key: value["status"] for key, value in record(runner)["verification"].items()
    }


def source(id: str, url: str, quote: str) -> Source:
    return Source(
        id=id,
        url=url,
        title="A",
        publisher="A",
        accessed="2026-09-27",
        quote=quote,
        claim="c",
    )


def test_the_unchecked_verifier_checks_nothing() -> None:
    sources = [source("S1", "https://a.example/", "q")]

    assert UncheckedVerifier().verify_sources(sources) == {"S1": "unchecked"}


def test_by_default_sources_are_checked_over_the_network_offline(
    runner: Runner,
) -> None:
    # The fake's sources are on .example hosts: refused without a lookup,
    # so the default verifier runs without touching the network.
    ran = runner()

    checked = record(runner)["verification"]
    assert {key: value["status"] for key, value in checked.items()} == dict.fromkeys(
        ALL, "unreachable"
    )
    assert {value["detail"] for value in checked.values()} == {
        "blocked: special-use domain"
    }
    assert all(value["checked_at"].endswith("Z") for value in checked.values())
    [notice] = [
        n for n in ran.of(ev.Notice) if n.message.startswith("landscape: checked")
    ]
    assert notice.message == "landscape: checked 5 of 5 sources: 5 unreachable"
    assert notice.level == "info"


def test_results_reach_the_record_and_the_draft(runner: Runner) -> None:
    verifier = Verifier()

    ran = runner(verifier=verifier)

    assert verifier.calls == [(ALL, TIME)]
    assert statuses(runner) == {
        "S1": "verified",
        "S2": "quote_not_found",
        "S3": "unchecked",
        "S4": "unchecked",
        "S5": "unchecked",
    }
    assert record(runner)["verification"]["S1"] == {
        "status": "verified",
        "detail": "the quote is on the page",
        "checked_at": "2026-09-27T21:47:02Z",
        "final_url": "https://www.example.com/a",
    }
    draft = ran.executor.of("landscape", "draft")[0]
    assert "| verified |" in draft.spec.prompt
    assert "| quote_not_found |" in draft.spec.prompt
    sources = draft.files["research/sources.md"]
    assert "- Verification: verified (the quote is on the page)" in sources
    assert "- Verification: unchecked\n" in sources
    # The rule for claims that rest on sources not verified.
    assert "[unverified]" in draft.spec.system_append


def test_no_verify_skips_it(runner: Runner) -> None:
    verifier = Verifier()

    runner(verifier=verifier, verify=False)

    assert verifier.calls == []
    assert set(statuses(runner).values()) == {"unchecked"}


def test_the_config_can_turn_it_off(runner: Runner) -> None:
    edit_config(runner.root, "[verify]\nenabled = false")
    verifier = Verifier()

    runner(verifier=verifier)

    assert verifier.calls == []


def test_reused_research_is_checked_when_it_was_not_before(runner: Runner) -> None:
    runner(verify=False)
    verifier = Verifier()

    ran = runner(verifier=verifier, force=True, only=["landscape"])

    assert ran.executor.drafting() == ["landscape:draft"]
    assert verifier.calls == [(ALL, TIME)]
    assert statuses(runner)["S1"] == "verified"
    # Only what is still unchecked is checked again.
    again = Verifier()
    runner(verifier=again, force=True, only=["landscape"])
    assert again.calls == [(["S3", "S4", "S5"], TIME)]


def test_the_checks_count_against_the_run_time(runner: Runner) -> None:
    verifier = Verifier()

    def slow(spec: Any) -> None:
        if spec.task == "overview":
            runner.clock.advance(3600)

    from tests.engine.conftest import Recording

    runner(verifier=verifier, executor=Recording(before=slow))

    assert verifier.calls == [(ALL, TIME - 3600)]


def test_without_time_to_draft_after_them_the_checks_wait(runner: Runner) -> None:
    verifier = Verifier()

    def slow(spec: Any) -> None:
        if spec.kind == "research":
            runner.clock.advance(7200 - 90)

    from tests.engine.conftest import Recording

    ran = runner(verifier=verifier, executor=Recording(before=slow))

    # 90 s were left: the draft could start, but not after any checking.
    assert verifier.calls == []
    assert set(statuses(runner).values()) == {"unchecked"}
    assert ran.executor.drafting()[:3] == [
        "overview:draft",
        "landscape:research",
        "landscape:draft",
    ]


# The M5 done criterion: a planted fake quote is reported as quote_not_found.

PAGE = "<html><body><h1>Pricing</h1><p>The Free plan includes up to 20 editors.</p>"
STORY = "<html><body><p>Acme Fabrication opened a second workshop in 2024.</p>"


@pytest.fixture
def web() -> Web:
    web = Web()
    web.add("https://cms.example.com/pricing", respond(PAGE))
    web.add("https://news.example.com/story", respond(STORY))
    web.add("https://example.com/gone", respond("Not here", status=404))
    return web


def planted_research() -> dict[str, Any]:
    return {
        "sources": [
            source(
                "S1", "https://cms.example.com/pricing", "up to 20 editors"
            ).model_dump(),
            source(
                "S2",
                "https://news.example.com/story",
                "Acme Fabrication won the 2025 export award",  # not on the page
            ).model_dump(),
            source(
                "S3", "https://example.com/gone", "anything at all here"
            ).model_dump(),
        ],
        "notes": "Findings [S1] [S2] [S3].",
        "unanswered": [],
    }


def test_a_planted_quote_is_reported_as_quote_not_found(
    runner: Runner, web: Web, tmp_path: Path
) -> None:
    resolver = Resolver()
    verifier = NetworkVerifier(
        transport=web.transport,
        resolver=resolver,
        probe=probe,
        reader=read_in_process,
        now=lambda: NOW,
    )

    ran = runner(
        {"landscape:research": {"structured": planted_research()}},
        verifier=verifier,
    )

    assert ran.code == 0
    checked = record(runner)["verification"]
    assert checked["S1"]["status"] == "verified"
    assert checked["S2"] == {
        "status": "quote_not_found",
        "detail": "the page loaded, but the quote is not in its text",
        "checked_at": "2026-09-27T21:47:02Z",
        "final_url": "https://news.example.com/story",
    }
    assert checked["S3"]["status"] == "unreachable"
    assert checked["S3"]["detail"] == "HTTP 404 Not Found"
    # Only the source hosts were looked up, never anything else.
    assert sorted(resolver.calls) == [
        "cms.example.com.",
        "example.com.",
        "news.example.com.",
    ]
    # The writer is told each result.
    draft = ran.executor.of("landscape", "draft")[0]
    assert "| S2 | A | A | 2026-09-27 | quote_not_found |" in draft.spec.prompt
    # The pack reports it: the document's Sources section, sources.md,
    # pack.json and the summary's counts.
    out = runner.root / "out"
    document = (out / "02-landscape.md").read_text()
    sources = document[document.index("## Sources") :]
    assert (
        "| S1 | [A](<https://cms.example.com/pricing>), A | 2026-09-27 | ✓ verified |"
        in sources
    )
    assert (
        "| S2 | [A](<https://news.example.com/story>), A | 2026-09-27 | "
        "✗ quote not found |" in sources
    )
    assert "? unreachable" in sources
    listed = (out / "sources.md").read_text()
    assert (
        "- Verification: ✗ quote not found (the page loaded, but the quote is not "
        "in its text), checked 2026-09-27T21:47:02Z"
    ) in listed
    pack = json.loads((out / "pack.json").read_text())
    assert {s["id"]: s["verification"] for s in pack["sources"]} == {
        "landscape:S1": "verified",
        "landscape:S2": "quote_not_found",
        "landscape:S3": "unreachable",
    }
    summary = (out / "00-summary.md").read_text()
    assert "| 02-landscape.md | 3 | 1 | 1 | 1 | 0 | 0 |" in summary
