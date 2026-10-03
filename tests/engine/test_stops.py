"""A run that stops before it finishes still renders what it finished.

Spec 04: the pack always gets a summary. On a budget stop the summary session
runs on its reserve if there is time; on any other stop, or without time,
00-summary.md has only what code writes, and the next run that finishes
writes the prose summary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from spadework.engine import events as ev
from spadework.errors import ExitCode
from spadework.executors.base import SessionSpec
from tests.engine.conftest import Recording, Runner

CODE_ONLY = "The prose summary is written when a run finishes."


def out(runner: Runner, name: str) -> Path:
    return runner.root / "out" / name


def stop_at_plan(runner: Runner, raised: BaseException) -> Recording:
    def before(spec: SessionSpec) -> None:
        if spec.task == "plan":
            raise raised

    return Recording(before=before)


def assert_code_only_pack(runner: Runner) -> None:
    summary = out(runner, "00-summary.md").read_text()
    assert CODE_ONLY in summary
    assert "## Documents" in summary
    assert "## How this pack was produced" in summary
    assert "| [01-overview.md](01-overview.md): Overview | fresh |" in summary
    assert "| 03-plan.md: Plan | not produced |" in summary
    assert out(runner, "01-overview.md").is_file()
    assert out(runner, "02-landscape.md").is_file()
    assert not out(runner, "03-plan.md").exists()
    pack = json.loads(out(runner, "pack.json").read_text())
    assert [d["state"] for d in pack["documents"]] == ["fresh", "fresh", "pending"]
    assert pack["consolidated_questions"] == []
    assert "## Sources" in out(runner, "02-landscape.md").read_text()


def test_an_interrupt_renders_what_was_finished(runner: Runner) -> None:
    ran = runner(executor=stop_at_plan(runner, KeyboardInterrupt()))

    assert ran.code == ExitCode.INTERRUPTED
    assert ran.executor.kinds() == [
        "overview:draft",
        "landscape:research",
        "landscape:draft",
        "plan:draft",
    ]
    assert_code_only_pack(runner)
    assert ran.finished.rendered == (
        "out/00-summary.md",
        "out/01-overview.md",
        "out/02-landscape.md",
        "out/pack.json",
        "out/sources.md",
    )
    assert ran.finished.tasks["plan"] == "pending"


def test_the_next_run_that_finishes_writes_the_prose_summary(runner: Runner) -> None:
    runner(executor=stop_at_plan(runner, KeyboardInterrupt()))

    ran = runner()

    assert ran.code == 0
    assert ran.executor.drafting() == ["plan:draft"]
    assert ran.executor.reviewing()[-1] == "summary"
    summary = out(runner, "00-summary.md").read_text()
    assert CODE_ONLY not in summary
    assert "## Executive summary" in summary
    assert ran.state.review is None


@pytest.mark.parametrize(
    ("script", "code", "outcome"),
    [
        ({"plan:draft": {"fail": "error"}}, ExitCode.ERROR, "error"),
        ({"plan:draft": {"fail": "sandbox"}}, ExitCode.ERROR, "error"),
        ({"plan:draft": {"fail": "rate_limit"}}, ExitCode.RATE_LIMITED, "rate_limited"),
    ],
)
def test_other_stops_render_with_a_summary_code_writes(
    runner: Runner, script: dict[str, dict[str, str]], code: int, outcome: str
) -> None:
    ran = runner(script)

    assert (ran.code, ran.result.outcome) == (code, outcome)
    assert "summary" not in ran.executor.kinds()
    assert_code_only_pack(runner)
    record = json.loads(
        (runner.root / ".spadework" / "records" / "summary.json").read_text()
    )
    assert (record["pending"], record["result"]) == (True, None)


def test_an_unexpected_error_still_renders(runner: Runner) -> None:
    ran = runner(executor=stop_at_plan(runner, RuntimeError("boom")))

    assert ran.code == ExitCode.ERROR
    assert ran.manifest.error == "unexpected error: RuntimeError: boom"
    assert_code_only_pack(runner)


def test_a_time_stop_from_the_minimum_session_time_renders(runner: Runner) -> None:
    def slow(spec: SessionSpec) -> None:
        if spec.task == "landscape" and spec.kind.value == "draft":
            runner.clock.advance(7200 - 80)  # 80 s left: no session can start

    ran = runner(executor=Recording(before=slow))

    assert (ran.code, ran.result.outcome) == (ExitCode.LIMIT, "time")
    assert ran.finished.message is not None
    assert ran.finished.message.startswith(
        "there is not enough time left to start a session"
    )
    assert "summary" not in ran.executor.kinds()
    assert_code_only_pack(runner)


def test_a_budget_stop_runs_the_summary_on_its_reserve(runner: Runner) -> None:
    # The plan's draft ($1.25) cannot start: 0.21 spent, 0.50 kept back.
    ran = runner(budget_usd=1.9)

    assert (ran.code, ran.result.outcome) == (ExitCode.LIMIT, "budget")
    assert ran.executor.kinds() == [
        "overview:draft",
        "landscape:research",
        "landscape:draft",
        "summary",
    ]
    summary_session = ran.executor.sessions[-1].spec
    assert summary_session.max_budget_usd == pytest.approx(0.75)
    text = out(runner, "00-summary.md").read_text()
    assert "## Executive summary" in text
    assert CODE_ONLY not in text
    assert "| 03-plan.md: Plan | not produced |" in text
    assert ran.manifest.sessions[-1].kind == "summary"
    # The summary stays owed: the next run that finishes writes it again.
    resumed = runner()
    assert resumed.executor.reviewing()[-1] == "summary"
    assert resumed.code == 0


def test_a_budget_stop_without_time_for_the_summary_writes_it_by_code(
    runner: Runner,
) -> None:
    # landscape's draft may use only what is left before the reserve
    # (1.35 - 0.13 - 0.50 = 0.72), uses it up, and leaves 85 s of the run.
    def slow(spec: SessionSpec) -> None:
        if spec.task == "landscape" and spec.kind.value == "draft":
            runner.clock.advance(7200 - 85)

    executor = Recording({"landscape:draft": {"fail": "budget"}}, before=slow)

    ran = runner(executor=executor, budget_usd=1.35)

    assert ran.result.outcome == "budget"
    assert ran.finished.message is not None
    assert "used what was left of the run budget" in ran.finished.message
    assert "summary" not in ran.executor.kinds()
    summary = out(runner, "00-summary.md").read_text()
    assert CODE_ONLY in summary
    # The run's limit stopped the session, so the task did not fail.
    assert "| 02-landscape.md: Landscape | not produced |" in summary


def test_a_stop_keeps_a_persons_edits(runner: Runner) -> None:
    runner()
    edited = "# Overview\n\nMine.\n"
    out(runner, "01-overview.md").write_text(edited)
    (runner.root / "inputs" / "note.md").write_text("# Note\n")

    ran = runner(executor=stop_at_plan(runner, KeyboardInterrupt()))

    assert ran.code == ExitCode.INTERRUPTED
    assert out(runner, "01-overview.md").read_text() == edited
    assert "out/01-overview.md" not in ran.finished.rendered
    summary = out(runner, "00-summary.md").read_text()
    assert CODE_ONLY in summary
    assert "edited by a person" in summary


def test_a_stop_that_changed_nothing_keeps_the_summary(runner: Runner) -> None:
    runner()
    before = out(runner, "00-summary.md").read_text()
    (runner.root / "inputs" / "note.md").write_text("# Note\n")

    # The first session cannot start: nothing changes.
    ran = runner(budget_usd=0.9)

    assert ran.code == ExitCode.LIMIT
    assert ran.executor.sessions == []
    text = out(runner, "00-summary.md").read_text()
    assert "## Executive summary" in text
    assert CODE_ONLY not in text
    assert text.split("## Documents")[0] == before.split("## Documents")[0]


def test_a_render_that_failed_is_not_tried_again(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def broken(*args: object, **kwargs: object) -> None:
        calls.append(1)
        raise RuntimeError("disk full")

    monkeypatch.setattr("spadework.engine.run.render_pack", broken)

    ran = runner()

    assert ran.code == ExitCode.ERROR
    assert calls == [1]
    assert not (runner.root / "out").exists()
    assert ran.state.review is not None


def test_a_render_that_fails_on_the_way_out_is_reported(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("spadework.engine.run.render_pack", broken)

    ran = runner(executor=stop_at_plan(runner, KeyboardInterrupt()))

    # The run's own outcome stands; the failed render is a notice.
    assert ran.code == ExitCode.INTERRUPTED
    assert any(
        n.message.startswith("the pack could not be rendered: OSError")
        for n in ran.of(ev.Notice)
    )
