"""The verification step between research and draft."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from spadework.engine.verification import UncheckedVerifier, VerificationStatus
from spadework.records import Source
from tests.engine.conftest import Runner, edit_config


class Verifier:
    """Marks S1 verified and S2 not found; leaves the rest unchecked."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], float | None]] = []

    def verify_sources(
        self, sources: Sequence[Source], *, time_limit: float | None = None
    ) -> Mapping[str, VerificationStatus]:
        self.calls.append(([source.id for source in sources], time_limit))
        found: dict[str, VerificationStatus] = {
            "S1": "verified",
            "S2": "quote_not_found",
        }
        return {s.id: found.get(s.id, "unchecked") for s in sources}


ALL = ["S1", "S2", "S3", "S4", "S5"]
"""The fake executor's sources."""


def statuses(runner: Runner) -> dict[str, str]:
    path = runner.root / ".spadework" / "records" / "landscape.research.json"
    verification: dict[str, str] = json.loads(path.read_text())["verification"]
    return verification


def test_without_a_verifier_every_source_is_unchecked(runner: Runner) -> None:
    sources = [
        Source(
            id="S1",
            url="https://a.example/",
            title="A",
            publisher="A",
            accessed="2026-09-27",
            quote="q",
            claim="c",
        )
    ]
    assert UncheckedVerifier().verify_sources(sources) == {"S1": "unchecked"}

    runner()

    assert set(statuses(runner).values()) == {"unchecked"}


def test_statuses_reach_the_record_and_the_draft(runner: Runner) -> None:
    verifier = Verifier()

    ran = runner(verifier=verifier)

    assert verifier.calls == [(ALL, 7200.0)]
    assert statuses(runner) == {
        "S1": "verified",
        "S2": "quote_not_found",
        "S3": "unchecked",
        "S4": "unchecked",
        "S5": "unchecked",
    }
    draft = ran.executor.of("landscape", "draft")[0]
    assert "| verified |" in draft.spec.prompt
    assert "| quote_not_found |" in draft.spec.prompt
    assert "- Verification: verified" in draft.files["research/sources.md"]


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

    assert ran.executor.kinds() == ["landscape:draft"]
    assert verifier.calls == [(ALL, 7200.0)]
    assert statuses(runner)["S1"] == "verified"
    # Only what is still unchecked is checked again.
    again = Verifier()
    runner(verifier=again, force=True, only=["landscape"])
    assert again.calls == [(["S3", "S4", "S5"], 7200.0)]
