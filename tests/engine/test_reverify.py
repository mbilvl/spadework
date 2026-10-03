"""``spadework verify`` as a library: check the pack's sources again."""

from __future__ import annotations

import json
import os
import socket
from collections.abc import Mapping, Sequence
from typing import Any

import pytest

from spadework.engine.reverify import verify_pack
from spadework.engine.verification import Found, VerificationResult
from spadework.errors import ExitCode
from spadework.executors.base import SessionSpec
from spadework.lock import LockedError, LockOwner
from spadework.records import Source
from tests.engine.conftest import Recording, Runner, leave_dead_lock


class Stub:
    """S1 verified, S2 not found, and nothing said about the rest."""

    def __init__(self, found: Mapping[str, Found] | None = None) -> None:
        self.found = found or {
            "S1": VerificationResult(
                status="verified",
                detail="the quote is on the page",
                checked_at="2026-09-28T09:00:00Z",
            ),
            "S2": "quote_not_found",
        }
        self.calls: list[tuple[list[str], float | None]] = []

    def verify_sources(
        self, sources: Sequence[Source], *, time_limit: float | None = None
    ) -> Mapping[str, Found]:
        self.calls.append(([s.id for s in sources], time_limit))
        return {s.id: self.found[s.id] for s in sources if s.id in self.found}


def research(runner: Runner) -> dict[str, Any]:
    path = runner.root / ".spadework" / "records" / "landscape.research.json"
    data: dict[str, Any] = json.loads(path.read_text())
    return data


def test_it_checks_every_source_again_and_renders_what_depends_on_them(
    runner: Runner,
) -> None:
    runner()
    stub = Stub()
    said: list[str] = []

    report = verify_pack(runner.root, verifier=stub, progress=said.append)

    assert stub.calls == [(["S1", "S2", "S3", "S4", "S5"], pytest.approx(7200, abs=5))]
    assert said == ["landscape: checking 5 sources"]
    assert report.tasks == ("landscape",)
    assert report.counts() == {
        "verified": 1,
        "quote_not_found": 1,
        "unreachable": 3,
        "unsupported": 0,
        "unchecked": 0,
    }
    assert [(s.id, s.result.status) for s in report.sources][:2] == [
        ("S1", "verified"),
        ("S2", "quote_not_found"),
    ]
    checked = research(runner)["verification"]
    assert checked["S1"] == {
        "status": "verified",
        "detail": "the quote is on the page",
        "checked_at": "2026-09-28T09:00:00Z",
        "final_url": None,
    }
    # S3 to S5 were not answered: they keep what the run found.
    assert checked["S3"]["detail"] == "blocked: special-use domain"
    assert report.written == (
        "out/00-summary.md",
        "out/02-landscape.md",
        "out/pack.json",
        "out/sources.md",
    )
    assert report.kept == ()
    assert not report.render_owed
    out = runner.root / "out"
    assert "| ✓ verified |" in (out / "02-landscape.md").read_text()
    assert (
        "| 02-landscape.md | 5 | 1 | 1 | 3 | 0 | 0 |"
        in (out / "00-summary.md").read_text()
    )
    pack = json.loads((out / "pack.json").read_text())
    assert pack["sources"][1]["verification"] == "quote_not_found"
    # Nothing is stale or pinned afterwards: the next run has nothing to do.
    again = runner()
    assert again.executor.sessions == []
    assert again.code == 0


def test_documents_a_person_edited_are_never_touched(runner: Runner) -> None:
    runner()
    path = runner.root / "out" / "02-landscape.md"
    edited = path.read_text() + "\nMy note.\n"
    path.write_text(edited)

    report = verify_pack(runner.root, verifier=Stub())

    assert path.read_text() == edited
    assert report.kept == ("out/02-landscape.md",)
    assert "out/02-landscape.md" not in report.written
    assert "✓ verified" in (runner.root / "out" / "sources.md").read_text()


def test_without_research_there_is_nothing_to_check(runner: Runner) -> None:
    report = verify_pack(runner.root, verifier=Stub())

    assert report.sources == ()
    assert report.written == ()
    assert not (runner.root / "out").exists()


def test_after_an_interrupted_run_only_the_records_change(runner: Runner) -> None:
    def interrupt(spec: SessionSpec) -> None:
        if spec.kind.value == "critic":
            raise KeyboardInterrupt

    assert runner(executor=Recording(before=interrupt)).code == ExitCode.INTERRUPTED

    landscape = runner.root / "out" / "02-landscape.md"
    rendered = landscape.read_bytes()  # the stopped run rendered what it had

    report = verify_pack(runner.root, verifier=Stub())

    assert report.render_owed
    assert report.written == ()
    assert landscape.read_bytes() == rendered
    assert research(runner)["verification"]["S1"]["status"] == "verified"
    ran = runner()
    assert "| ✓ verified |" in (runner.root / "out" / "02-landscape.md").read_text()
    assert ran.code == 0


def test_an_unreadable_research_record_is_a_warning(runner: Runner) -> None:
    runner()
    path = runner.root / ".spadework" / "records" / "landscape.research.json"
    path.write_text("{")

    report = verify_pack(runner.root, verifier=Stub())

    assert report.sources == ()
    [(message, hint)] = report.warnings
    assert message.startswith("the research record of task 'landscape' cannot be read")
    assert hint is not None


def test_it_honours_the_lock(runner: Runner) -> None:
    runner()
    lock = runner.root / ".spadework" / "lock"
    lock.write_bytes(
        LockOwner(os.getppid(), socket.gethostname(), "2026-09-27T21:04:11Z").to_bytes()
    )

    with pytest.raises(LockedError) as caught:
        verify_pack(runner.root, verifier=Stub())

    assert caught.value.exit_code == ExitCode.LOCKED


def test_it_reclaims_a_dead_runs_lock(runner: Runner) -> None:
    runner()
    leave_dead_lock(runner.root)

    report = verify_pack(runner.root, verifier=Stub())

    assert any("reclaimed" in message for message, _ in report.warnings)
    assert not (runner.root / ".spadework" / "lock").exists()


def test_the_time_limit_bounds_the_whole_check(runner: Runner) -> None:
    runner()
    stub = Stub()

    verify_pack(runner.root, verifier=stub, time_limit=30)

    [(_, limit)] = stub.calls
    assert limit is not None
    assert 29 < limit <= 30
