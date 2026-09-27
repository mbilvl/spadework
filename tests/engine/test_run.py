"""The pipeline, end to end, with the fake executor and a clock that never waits."""

from __future__ import annotations

import datetime as dt
import json
import os
import socket
from pathlib import Path

import pytest

from spadework.engine import events as ev
from spadework.engine.planning import plan_project
from spadework.engine.run import RunOptions, run_project
from spadework.errors import ExitCode, UsageError
from spadework.executors.base import Notice, SessionResult, SessionSpec, Usage
from spadework.keys import hash_file
from spadework.lock import LockedError, LockOwner
from spadework.manifest import exit_code
from spadework.project import load_project
from tests.conftest import MakeProject
from tests.engine.conftest import (
    Recording,
    Runner,
    edit_config,
    task_file,
    write_playbook,
)


def test_a_first_run_drafts_every_task_in_order(runner: Runner) -> None:
    ran = runner()

    assert ran.code == 0
    assert ran.result.outcome == "complete"
    assert ran.executor.kinds() == [
        "overview:draft",
        "landscape:research",
        "landscape:draft",
        "plan:draft",
    ]
    assert ran.tasks() == {"overview": "fresh", "landscape": "fresh", "plan": "fresh"}
    manifest = ran.manifest
    assert manifest.outcome == "complete"
    assert manifest.ended_at is not None
    assert [s.kind for s in manifest.sessions] == [
        "draft",
        "research",
        "draft",
        "draft",
    ]
    assert manifest.totals.sessions == 4
    assert manifest.totals.cost_usd == pytest.approx(0.08 * 3 + 0.05)
    assert manifest.executor.version == "test"
    assert (manifest.limits.budget_usd, manifest.limits.rounds) == (6.0, 2)
    assert [s.log for s in manifest.sessions] == [
        "sessions/001-overview-draft.jsonl",
        "sessions/002-landscape-research.jsonl",
        "sessions/003-landscape-draft.jsonl",
        "sessions/004-plan-draft.jsonl",
    ]
    run_dir = ran.root / ".spadework" / "runs" / manifest.run_id
    assert all((run_dir / s.log).is_file() for s in manifest.sessions)
    assert ran.finished.rendered == (
        "out/01-overview.md",
        "out/02-landscape.md",
        "out/03-plan.md",
    )


def test_outputs_state_and_records(runner: Runner) -> None:
    ran = runner()
    root = ran.root

    state = ran.state
    assert state.review is None
    materials = plan_project(load_project(root)).materials
    for task_id, output in [
        ("overview", "01-overview.md"),
        ("landscape", "02-landscape.md"),
        ("plan", "03-plan.md"),
    ]:
        entry = state.tasks[task_id]
        assert entry.status == "fresh"
        assert entry.material == materials[task_id]
        work = root / ".spadework" / "work" / output
        assert entry.output is not None
        assert entry.output.hash == hash_file(work)
        assert entry.rendered is not None
        assert entry.rendered.hash == hash_file(root / "out" / output)
        assert (root / "out" / output).read_bytes() == work.read_bytes()
        record = json.loads(
            (root / ".spadework" / "records" / f"{task_id}.draft.json").read_text()
        )
        assert record["key"] == materials[task_id].key
        assert record["result"]["status"] == "ok"
    research = json.loads(
        (root / ".spadework" / "records" / "landscape.research.json").read_text()
    )
    assert research["key"] == materials["landscape"].research_key
    assert research["verification"] == {f"S{n}": "unchecked" for n in range(1, 6)}
    assert state.research["landscape"].record == "records/landscape.research.json"
    # Staging directories and control files are removed after every session.
    assert list((root / ".spadework" / "sessions").iterdir()) == []
    assert list((root / ".spadework" / "control").iterdir()) == []
    assert list(runner.tmp.iterdir()) == []


def test_a_second_run_is_up_to_date(runner: Runner) -> None:
    runner()

    again = runner()

    assert again.code == 0
    assert again.result.outcome is None
    assert again.executor.sessions == []
    assert again.of(ev.UpToDate) == [ev.UpToDate(0, ())]
    assert not again.of(ev.RunStarted)
    assert len(list((runner.root / ".spadework" / "runs").iterdir())) == 1


def test_the_plan_comes_first(runner: Runner) -> None:
    ran = runner()

    assert isinstance(ran.events[0], ev.PlanReady)
    assert isinstance(ran.events[1], ev.RunStarted)
    assert ran.events[1].tasks == ("overview", "landscape", "plan")
    assert isinstance(ran.events[-1], ev.RunFinished)


def test_sessions_get_the_right_model_tools_and_limits(runner: Runner) -> None:
    ran = runner()

    research = ran.executor.of("landscape", "research")[0].spec
    plan = ran.executor.of("plan", "draft")[0].spec
    assert (research.model, plan.model) == ("sonnet", "opus")
    assert research.tools == ("WebSearch", "WebFetch")
    assert plan.tools == ("Read", "Glob", "Grep", "Write", "Edit")
    assert research.output is None
    assert plan.output is not None
    assert plan.output.name == "03-plan.md"
    # Estimates: research $0.75, plan's budget_usd $1.25 → caps of 1.5x.
    assert research.max_budget_usd == pytest.approx(1.125)
    assert plan.max_budget_usd == pytest.approx(1.875)
    assert plan.timeout_s == 1500
    assert research.log_path.name == "002-landscape-research.jsonl"
    assert research.session_id == "002-landscape-research"


def test_research_sessions_are_staged_empty_outside_the_project(
    runner: Runner,
) -> None:
    ran = runner()

    research = ran.executor.of("landscape", "research")[0]
    assert research.files == {}
    assert research.spec.workdir.is_relative_to(runner.tmp)
    assert not research.spec.workdir.is_relative_to(ran.root)


def test_draft_sessions_see_the_brief_inputs_dependencies_and_research(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project(
        inputs={"calls/one.md": "# Call one\n", "two.txt": "two", "other.md": "x"}
    )
    ran = Runner(root, research_tmp)()

    overview = ran.executor.of("overview", "draft")[0]
    assert sorted(overview.files) == [
        "brief.md",
        "inputs/calls/one.md",
        "inputs/other.md",
        "inputs/two.txt",
    ]
    landscape = ran.executor.of("landscape", "draft")[0]
    assert sorted(landscape.files) == [
        "01-overview.md",
        "brief.md",
        "inputs/calls/one.md",
        "inputs/two.txt",
        "research/notes.md",
        "research/sources.md",
    ]
    assert "Verification: unchecked" in landscape.files["research/sources.md"]
    prompt = landscape.spec.prompt
    assert "## Key facts from the documents this one builds on" in prompt
    assert "### 01-overview.md\n\n- Overview: placeholder content" in prompt
    assert "| S1 | Example Directory: landscape |" in prompt
    assert "| unchecked |" in prompt
    assert '- `inputs/calls/one.md`: an input ("Call one")' in prompt
    assert "Write the document to `02-landscape.md`" in prompt
    plan = ran.executor.of("plan", "draft")[0]
    assert sorted(plan.files) == ["01-overview.md", "02-landscape.md", "brief.md"]


def test_the_system_prompt_carries_the_rules_the_reader_and_the_appendix(
    runner: Runner,
) -> None:
    ran = runner()

    system = ran.executor.of("overview", "draft")[0].spec.system_append
    assert system.startswith("# House rules")
    assert "## The reader\n\nA developer scoping a catalogue website" in system
    assert "# Writing a document" in system
    # The fixture playbook's prompts/rules.md, appended as written.
    assert "Prefer tables to long prose.\n\n## The reader" in system


def test_the_draft_prompt_lists_the_checks(runner: Runner) -> None:
    ran = runner()

    prompt = ran.executor.of("overview", "draft")[0].spec.prompt
    assert "Required headings: Summary, Open questions\n" in prompt
    assert "- At most 120 lines." in prompt
    assert "- At most 6 open questions in your result." in prompt


# Staleness.


def test_adding_an_input_makes_exactly_the_matching_tasks_stale(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project()
    write_playbook(
        root,
        {
            "alpha": task_file("01-alpha.md", inputs=("alpha/**",)),
            "beta": task_file("02-beta.md", inputs=("beta/**",)),
            "gamma": task_file("03-gamma.md", inputs=()),
        },
    )
    run = Runner(root, research_tmp)
    assert run().code == 0

    (root / "inputs" / "alpha").mkdir()
    (root / "inputs" / "alpha" / "2026-10-01-call.md").write_text("# Call\n")
    planned = plan_project(load_project(root))

    assert {t.id: (t.state, t.reason) for t in planned.plan.tasks} == {
        "alpha": ("stale", "inputs/alpha/2026-10-01-call.md added"),
        "beta": ("fresh", None),
        "gamma": ("fresh", None),
    }
    again = run()
    assert again.executor.kinds() == ["alpha:draft"]


def test_research_is_reused_while_its_key_matches(runner: Runner) -> None:
    runner()
    (runner.root / "inputs" / "calls").mkdir()
    (runner.root / "inputs" / "calls" / "new.md").write_text("# New\n")

    ran = runner()

    # Inputs change the draft key, not the research key.
    assert ran.executor.kinds() == ["overview:draft", "landscape:draft", "plan:draft"]


def test_refresh_research(runner: Runner) -> None:
    runner()

    ran = runner(force=True, only=["landscape"], refresh_research=True)

    assert ran.executor.kinds() == ["landscape:research", "landscape:draft"]


def test_a_brief_change_redoes_research_only_if_public_context_changed(
    runner: Runner,
) -> None:
    runner()
    brief = runner.root / "brief.md"
    brief.write_text(brief.read_text().replace("makes stainless", "makes fine"))

    ran = runner()

    assert ran.executor.kinds() == ["overview:draft", "landscape:draft", "plan:draft"]
    brief.write_text(brief.read_text().replace("planning a catalogue", "planning a"))
    assert "landscape:research" in runner().executor.kinds()


def test_force_redrafts_fresh_tasks(runner: Runner) -> None:
    runner()

    ran = runner(force=True)

    assert ran.executor.kinds() == ["overview:draft", "landscape:draft", "plan:draft"]
    assert ran.code == 0


def test_model_overrides(runner: Runner) -> None:
    runner()

    ran = runner(draft_model="haiku")

    # The draft model is part of every draft-tier key, and of research's.
    assert ran.executor.kinds() == [
        "overview:draft",
        "landscape:research",
        "landscape:draft",
        "plan:draft",
    ]
    models = {f"{s.spec.task}:{s.kind}": s.spec.model for s in ran.executor.sessions}
    assert models["overview:draft"] == "haiku"
    assert models["landscape:research"] == "haiku"
    assert models["plan:draft"] == "opus"


# --only.


def test_only_runs_the_named_tasks_with_existing_dependency_outputs(
    runner: Runner,
) -> None:
    runner()
    (runner.root / "inputs" / "note.md").write_text("# Note\n")

    ran = runner(only=["landscape"])

    assert ran.executor.kinds() == ["landscape:draft"]
    staged = ran.executor.sessions[0].files
    assert "01-overview.md" in staged
    # overview is stale and was not redrafted: landscape read its older
    # output, so it stays stale until overview is redrafted.
    states = {
        t.id: (t.state, t.reason)
        for t in plan_project(load_project(runner.root)).plan.tasks
    }
    assert states["overview"] == ("stale", "inputs/note.md added")
    assert states["landscape"] == ("stale", "dependency overview is stale")
    assert ran.code == 0


def test_only_fails_when_a_dependency_never_produced_an_output(
    runner: Runner,
) -> None:
    with pytest.raises(
        UsageError, match="its dependency 'overview' has never produced"
    ):
        runner(only=["landscape"])
    assert not (runner.root / ".spadework" / "runs").exists()
    assert not (runner.root / ".spadework" / "lock").exists()


def test_only_names_an_unknown_task(runner: Runner) -> None:
    with pytest.raises(UsageError, match="there is no task 'nope'"):
        runner(only=["nope"])


def test_only_with_its_dependency_also_selected(runner: Runner) -> None:
    ran = runner(only=["overview", "landscape"])

    assert ran.executor.kinds() == [
        "overview:draft",
        "landscape:research",
        "landscape:draft",
    ]
    assert ran.code == 0
    states = {t.id: t.state for t in plan_project(load_project(runner.root)).plan.tasks}
    assert states == {"overview": "fresh", "landscape": "fresh", "plan": "pending"}


# Pinned outputs and rendering.


def test_an_edited_output_is_pinned_and_used_as_it_stands(runner: Runner) -> None:
    runner()
    out = runner.root / "out" / "01-overview.md"
    out.write_text("# Overview\n\nEdited by a person.\n")
    brief = runner.root / "brief.md"
    brief.write_text(brief.read_text() + "\nMore.\n")

    ran = runner()

    assert ran.executor.kinds() == ["landscape:draft", "plan:draft"]
    assert out.read_text() == "# Overview\n\nEdited by a person.\n"
    staged = ran.executor.of("landscape", "draft")[0].files
    assert staged["01-overview.md"] == "# Overview\n\nEdited by a person.\n"
    assert "edited by a person: use it as it stands" in (
        ran.executor.of("landscape", "draft")[0].spec.prompt
    )
    planned = plan_project(load_project(runner.root)).plan.tasks
    assert {t.id: t.state for t in planned} == {
        "overview": "pinned",
        "landscape": "fresh",
        "plan": "fresh",
    }


def test_force_alone_leaves_pinned_tasks(runner: Runner) -> None:
    runner()
    out = runner.root / "out" / "01-overview.md"
    out.write_text("# Mine\n")

    ran = runner(force=True)

    assert "overview:draft" not in ran.executor.kinds()
    assert out.read_text() == "# Mine\n"
    [plan] = ran.of(ev.PlanReady)
    assert plan.warnings == (
        (
            "task 'overview' is pinned: out/01-overview.md was edited, so it is not "
            "redrafted",
            "To replace your edits: spadework run --force --only overview",
        ),
    )


def test_force_only_replaces_a_pinned_output(runner: Runner) -> None:
    runner()
    out = runner.root / "out" / "01-overview.md"
    out.write_text("# Mine\n")

    ran = runner(force=True, only=["overview"])

    assert ran.executor.kinds() == ["overview:draft"]
    assert out.read_text().startswith("# Overview\n")
    [plan] = ran.of(ev.PlanReady)
    assert plan.warnings == (
        ("--force replaces your edits to out/01-overview.md", None),
    )
    states = {t.id: t.state for t in plan_project(load_project(runner.root)).plan.tasks}
    assert states["overview"] == "fresh"


def test_a_deleted_output_is_rendered_again(runner: Runner) -> None:
    runner()
    out = runner.root / "out" / "02-landscape.md"
    out.unlink()

    ran = runner()

    assert ran.executor.sessions == []
    assert ran.code == 0
    assert ran.result.outcome == "complete"
    assert ran.finished.rendered == ("out/02-landscape.md",)
    assert out.is_file()


def test_a_file_the_engine_did_not_write_is_left_alone(runner: Runner) -> None:
    (runner.root / "out").mkdir()
    mine = runner.root / "out" / "01-overview.md"
    mine.write_text("# Written by hand before any run\n")

    ran = runner()

    assert mine.read_text() == "# Written by hand before any run\n"
    notices = [n.message for n in ran.of(ev.Notice)]
    assert (
        "out/01-overview.md was not written by spadework, so it was left as it is"
        in notices
    )
    assert "out/01-overview.md" not in ran.finished.rendered


# Durability.


def test_a_crash_between_sessions_resumes_without_redrafting(
    runner: Runner,
) -> None:
    class Crash(BaseException):
        pass

    def crash_on_plan(spec: SessionSpec) -> None:
        if spec.task == "plan":
            raise Crash

    with pytest.raises(Crash):
        runner(executor=Recording(before=crash_on_plan))
    # kill -9 leaves the lock behind, owned by a process that has gone.
    lock = runner.root / ".spadework" / "lock"
    lock.write_bytes(LockOwner(_dead_pid(), socket.gethostname(), "x").to_bytes())

    ran = runner()

    assert ran.executor.kinds() == ["plan:draft"]
    assert ran.code == 0
    assert any("reclaimed the project lock" in n.message for n in ran.of(ev.Notice))


def test_state_is_saved_after_every_session(runner: Runner) -> None:
    seen: list[set[str]] = []

    def snapshot(spec: SessionSpec) -> None:
        state = json.loads((runner.root / ".spadework" / "state.json").read_text())
        seen.append(set(state["tasks"]) | {f"research:{t}" for t in state["research"]})

    runner()  # the state file exists from the first session on
    runner(executor=Recording(before=snapshot), force=True, refresh_research=True)

    assert len(seen) == 4


def test_the_manifest_is_written_after_every_session(runner: Runner) -> None:
    counts: list[int] = []

    def snapshot(spec: SessionSpec) -> None:
        runs = sorted((runner.root / ".spadework" / "runs").iterdir())
        manifest = json.loads((runs[-1] / "manifest.json").read_text())
        counts.append(len(manifest["sessions"]))
        assert manifest["outcome"] is None

    runner(executor=Recording(before=snapshot))

    assert counts == [0, 1, 2, 3]


def test_a_held_lock_stops_the_run_before_it_starts(runner: Runner) -> None:
    lock = runner.root / ".spadework" / "lock"
    lock.parent.mkdir()
    owner = LockOwner(os.getppid(), socket.gethostname(), "2026-09-27T21:04:11Z")
    lock.write_bytes(owner.to_bytes())

    with pytest.raises(LockedError) as caught:
        runner()

    assert caught.value.exit_code == ExitCode.LOCKED
    assert lock.read_bytes() == owner.to_bytes()
    assert not (runner.root / ".spadework" / "runs").exists()


# Budgets and limits.


def test_the_budget_stops_the_run_and_it_resumes(runner: Runner) -> None:
    ran = runner({"*": {"cost_usd": 0.6}}, budget_usd=2.0)

    # overview $0.60; landscape research: 0.60 + 0.75 + 0.50 reserve fits in
    # $2.00; landscape draft: 1.20 + 0.50 + 0.50 does not.
    assert ran.code == ExitCode.LIMIT
    assert ran.result.outcome == "budget"
    assert ran.executor.kinds() == ["overview:draft", "landscape:research"]
    manifest = ran.manifest
    assert manifest.outcome == "budget"
    assert manifest.exit_code == ExitCode.LIMIT
    assert manifest.totals.cost_usd == pytest.approx(1.2)
    assert ran.finished.message is not None
    assert "cannot cover the next session: landscape draft" in ran.finished.message
    assert ran.finished.rendered == ()

    resumed = runner()

    assert resumed.executor.kinds() == ["landscape:draft", "plan:draft"]
    assert resumed.code == 0


def test_the_summary_reserve_is_kept_from_the_start(runner: Runner) -> None:
    ran = runner(budget_usd=0.9)

    # overview's estimate is $0.50: 0.50 + 0.50 reserve > 0.90.
    assert ran.code == ExitCode.LIMIT
    assert ran.executor.sessions == []
    assert ran.manifest.sessions == []


def test_a_session_cap_is_limited_by_what_is_left(runner: Runner) -> None:
    ran = runner({"*": {"cost_usd": 0.2}}, budget_usd=1.5)

    # After overview ($0.20), research may use min(1.125, 1.5 - 0.2 - 0.5).
    research = ran.executor.of("landscape", "research")[0].spec
    assert research.max_budget_usd == pytest.approx(0.8)


def test_a_session_that_uses_up_the_run_budget_stops_the_run(
    runner: Runner,
) -> None:
    ran = runner({"landscape:research": {"fail": "budget"}}, budget_usd=1.6)

    assert ran.result.outcome == "budget"
    assert ran.code == ExitCode.LIMIT


def test_a_session_over_its_own_cap_fails_the_task(runner: Runner) -> None:
    ran = runner({"landscape:research": {"fail": "budget"}})

    assert ran.result.outcome == "partial"
    assert ran.code == ExitCode.PARTIAL
    assert ran.tasks()["landscape"] == "failed"
    assert ran.tasks()["plan"] == "skipped"


def test_an_unknown_cost_is_charged_at_the_cap(runner: Runner) -> None:
    def unknown(spec: SessionSpec, result: SessionResult) -> SessionResult:
        if spec.task == "overview":
            result.cost_known = False
        return result

    ran = runner(executor=Recording(after=unknown))

    assert ran.manifest.sessions[0].cost_usd == pytest.approx(0.75)


def test_the_time_limit_stops_the_run(runner: Runner) -> None:
    def slow(spec: SessionSpec) -> None:
        runner.clock.advance(1500)

    ran = runner(executor=Recording(before=slow), max_time="1h")

    # 3600 s: two sessions of 1500 s leave 600 s, then 30 s is kept back and
    # the next timeout would be 570 s; a third leaves too little.
    assert ran.result.outcome == "time"
    assert ran.code == ExitCode.LIMIT
    assert len(ran.executor.sessions) == 3
    assert ran.executor.sessions[2].spec.timeout_s == 570
    assert ran.manifest.outcome == "time"


def test_a_timeout_cut_short_by_the_run_time_stops_the_run(runner: Runner) -> None:
    edit_config(runner.root, '[limits]\nsession_timeout = "2h"')

    ran = runner({"overview:draft": {"fail": "timeout"}}, max_time="1h")

    assert ran.executor.sessions[0].spec.timeout_s == 3570
    assert ran.result.outcome == "time"
    assert ran.code == ExitCode.LIMIT
    assert "overview" not in ran.state.tasks


# Rate limits.


def test_a_rate_limit_stops_the_run(runner: Runner) -> None:
    reset = int(runner.clock.time()) + 3600
    ran = runner({"landscape:research": {"fail": "rate_limit", "resets_at": reset}})

    assert ran.code == ExitCode.RATE_LIMITED
    assert ran.result.outcome == "rate_limited"
    moment = dt.datetime.fromtimestamp(reset, dt.UTC)
    assert ran.manifest.rate_limit_reset == moment.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert ran.finished.resume_after == moment.astimezone().strftime("%H:%M")
    assert ran.executor.kinds() == ["overview:draft", "landscape:research"]
    assert ran.manifest.sessions[-1].status == "rate_limited"


def test_wait_on_rate_limit_waits_for_the_reset(runner: Runner) -> None:
    reset = int(runner.clock.time()) + 600
    ran = runner(
        {"landscape:research": {"fail": "rate_limit", "resets_at": reset, "times": 1}},
        wait_on_rate_limit=True,
    )

    assert ran.code == 0
    assert ran.executor.kinds() == [
        "overview:draft",
        "landscape:research",
        "landscape:research",
        "landscape:draft",
        "plan:draft",
    ]
    assert len(runner.clock.sleeps) == 1
    assert runner.clock.sleeps[0] == pytest.approx(660, abs=1)


def test_wait_on_rate_limit_stops_when_the_reset_is_too_late(runner: Runner) -> None:
    reset = int(runner.clock.time()) + 3 * 3600
    ran = runner(
        {"landscape:research": {"fail": "rate_limit", "resets_at": reset}},
        wait_on_rate_limit=True,
    )

    assert ran.code == ExitCode.RATE_LIMITED
    assert runner.clock.sleeps == []
    assert ran.finished.message is not None
    assert "would pass the run's time limit" in ran.finished.message


def test_a_rate_limit_on_a_successful_session_stops_before_the_next(
    runner: Runner,
) -> None:
    reset = dt.datetime.fromtimestamp(runner.clock.time() + 900, dt.UTC)

    def limited(spec: SessionSpec, result: SessionResult) -> SessionResult:
        if spec.task == "overview":
            result.rate_limit_reset = reset
        return result

    ran = runner(executor=Recording(after=limited))

    assert ran.code == ExitCode.RATE_LIMITED
    assert ran.executor.kinds() == ["overview:draft"]
    assert ran.tasks() == {"overview": "fresh"}
    assert ran.state.tasks["overview"].status == "fresh"


# Interrupts and errors.


def test_an_interrupt_ends_the_run_with_a_manifest(runner: Runner) -> None:
    ran = runner({"landscape:draft": {"fail": "interrupted"}})

    assert ran.code == ExitCode.INTERRUPTED
    assert ran.manifest.outcome == "interrupted"
    assert ran.state.tasks["overview"].status == "fresh"
    assert "landscape" not in ran.state.tasks
    # The research done before the interrupt is kept.
    assert "landscape" in ran.state.research
    assert runner().executor.kinds() == ["landscape:draft", "plan:draft"]


def test_ctrl_c_outside_a_session_ends_the_run_with_a_manifest(
    runner: Runner,
) -> None:
    def interrupt(spec: SessionSpec) -> None:
        if spec.task == "plan":
            raise KeyboardInterrupt

    ran = runner(executor=Recording(before=interrupt))

    assert ran.code == ExitCode.INTERRUPTED
    assert ran.manifest.outcome == "interrupted"
    assert not (runner.root / ".spadework" / "lock").exists()


def test_an_unexpected_error_ends_the_run_with_a_manifest(runner: Runner) -> None:
    def boom(spec: SessionSpec) -> None:
        raise RuntimeError("boom")

    ran = runner(executor=Recording(before=boom))

    assert ran.code == ExitCode.ERROR
    assert ran.manifest.outcome == "error"
    assert ran.manifest.error == "unexpected error: RuntimeError: boom"
    assert isinstance(ran.result.exception, RuntimeError)


@pytest.mark.parametrize("fail", ["sandbox", "error"])
def test_sandbox_and_executor_errors_stop_the_run(runner: Runner, fail: str) -> None:
    ran = runner({"overview:draft": {"fail": fail}})

    assert ran.code == ExitCode.ERROR
    assert ran.result.outcome == "error"
    assert len(ran.executor.sessions) == 1
    assert ran.manifest.error is not None
    assert ran.manifest.error.startswith("overview draft: ")
    assert "overview" not in ran.state.tasks


def test_every_outcome_matches_its_exit_code(runner: Runner) -> None:
    ran = runner({"overview:draft": {"fail": "interrupted"}})

    assert ran.manifest.exit_code == exit_code("interrupted") == ran.code


# Retries.


def test_a_result_that_fails_its_schema_is_retried_once(runner: Runner) -> None:
    ran = runner({"overview:draft": {"fail": "invalid_schema", "times": 1}})

    assert ran.executor.kinds()[:2] == ["overview:draft", "overview:draft"]
    first, second = ran.manifest.sessions[:2]
    assert first.status == "error"
    assert first.error is not None
    assert first.error.startswith("its result does not match the schema")
    assert second.status == "ok"
    retry = ran.executor.sessions[1].spec.prompt
    assert "## Your last attempt was rejected" in retry
    assert ran.code == 0


def test_a_second_schema_failure_fails_the_task(runner: Runner) -> None:
    ran = runner({"overview:draft": {"fail": "invalid_schema"}})

    assert ran.executor.kinds() == ["overview:draft", "overview:draft"]
    assert ran.tasks() == {
        "overview": "failed",
        "landscape": "skipped",
        "plan": "skipped",
    }
    assert ran.code == ExitCode.PARTIAL
    entry = ran.state.tasks["overview"]
    assert entry.status == "failed"
    assert entry.error is not None
    assert entry.error.endswith("(after 2 attempts)")
    # A failed task is retried by the next run.
    assert runner().executor.kinds()[0] == "overview:draft"


def test_a_missing_output_is_retried_once(runner: Runner) -> None:
    ran = runner({"overview:draft": {"fail": "missing_output", "times": 1}})

    assert ran.executor.kinds()[:2] == ["overview:draft", "overview:draft"]
    assert ran.manifest.sessions[0].error == "it did not write 01-overview.md"
    assert ran.code == 0


def test_transient_errors_are_retried_twice_with_waits(runner: Runner) -> None:
    ran = runner({"overview:draft": {"fail": "transient", "times": 2}})

    assert ran.executor.kinds()[:3] == ["overview:draft"] * 3
    assert runner.clock.sleeps == [10.0, 30.0]
    assert ran.code == 0


def test_a_third_transient_error_fails_the_task(runner: Runner) -> None:
    ran = runner({"overview:draft": {"fail": "transient"}})

    assert ran.executor.kinds() == ["overview:draft"] * 3
    assert ran.tasks()["overview"] == "failed"
    assert [e.retry for e in ran.of(ev.SessionFinished)] == [True, True, False]


def test_a_timeout_fails_only_the_task(runner: Runner) -> None:
    ran = runner({"overview:draft": {"fail": "timeout"}})

    assert ran.executor.kinds() == ["overview:draft"]
    assert ran.tasks()["overview"] == "failed"
    error = ran.state.tasks["overview"].error
    assert error == "the session did not finish within session_timeout (25m)"


# Validators and repair.


def test_a_repairable_failure_is_repaired(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project()
    write_playbook(
        root,
        {"alpha": task_file("01-alpha.md", extra="validate:\n  max_open_questions: 0")},
    )
    fixed = {
        "status": "ok",
        "summary": "Repaired.",
        "key_facts": [],
        "decisions": [],
        "open_questions": [],
        "assumptions": [],
        "risks": [],
        "gaps": [],
    }

    ran = Runner(root, research_tmp)({"alpha:repair": {"structured": fixed}})

    assert ran.executor.kinds() == ["alpha:draft", "alpha:repair"]
    repair = ran.executor.sessions[1]
    assert repair.files["01-alpha.md"].startswith("# Alpha")
    assert "## What to repair" in repair.spec.prompt
    assert "`max_open_questions`: The result lists 1 open question" in (
        repair.spec.prompt
    )
    assert ran.tasks() == {"alpha": "fresh"}
    draft, repaired = ran.manifest.sessions
    assert [(v.name, v.ok) for v in draft.validators if not v.ok] == [
        ("max_open_questions", False)
    ]
    assert all(v.ok for v in repaired.validators)
    record = json.loads((root / ".spadework/records/alpha.draft.json").read_text())
    assert record["kind"] == "repair"
    assert ran.code == 0


def test_a_failed_repair_leaves_the_task_invalid_until_its_key_changes(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project()
    write_playbook(
        root,
        {
            "alpha": task_file(
                "01-alpha.md", extra="validate:\n  forbidden_phrases: [placeholder]"
            ),
            "beta": task_file("02-beta.md", depends_on=("alpha",)),
        },
    )
    run = Runner(root, research_tmp)

    ran = run()

    assert ran.executor.kinds() == ["alpha:draft", "alpha:repair", "beta:draft"]
    assert ran.tasks() == {"alpha": "invalid", "beta": "fresh"}
    assert ran.code == ExitCode.PARTIAL
    assert (root / "out" / "01-alpha.md").is_file()  # the pack is still produced
    assert ran.finished.message == "1 of 2 tasks is not done: alpha (invalid)"

    again = run()
    assert again.executor.sessions == []
    assert again.of(ev.UpToDate) == [ev.UpToDate(int(ExitCode.PARTIAL), ("alpha",))]
    assert again.code == ExitCode.PARTIAL
    assert run(force=True, only=["alpha"]).executor.kinds() == [
        "alpha:draft",
        "alpha:repair",
    ]


def test_an_unrepairable_failure_skips_the_repair(runner: Runner) -> None:
    task = runner.root / "playbooks" / "valid" / "tasks" / "landscape.md"
    # The fake finds five sources.
    task.write_text(task.read_text().replace("min_sources: 2", "min_sources: 6"))

    ran = runner()

    assert "landscape:repair" not in ran.executor.kinds()
    assert ran.tasks()["landscape"] == "invalid"
    [session] = [
        s for s in ran.manifest.sessions if s.task == "landscape" and s.kind == "draft"
    ]
    [failure] = [v for v in session.validators if not v.ok]
    assert (failure.name, failure.repairable) == ("min_sources", False)


def test_a_failed_dependency_skips_its_dependents(runner: Runner) -> None:
    ran = runner({"landscape:research": {"fail": "timeout"}})

    assert ran.tasks() == {
        "overview": "fresh",
        "landscape": "failed",
        "plan": "skipped",
    }
    [finished] = [e for e in ran.of(ev.TaskFinished) if e.task == "plan"]
    assert finished.reason == "dependency landscape failed"
    assert "plan" not in ran.state.tasks
    assert ran.finished.tasks["plan"] == "skipped"


# Guard denials and other notices.


def test_denials_in_research_are_flagged_as_possible_prompt_injection(
    runner: Runner,
) -> None:
    ran = runner({"landscape:research": {"fail": "guard_denial", "denials": 2}})

    denied = ran.of(ev.ToolDenied)
    assert len(denied) == 2
    assert all(d.possible_injection for d in denied)
    session = next(s for s in ran.manifest.sessions if s.kind == "research")
    assert session.guard_denials == 2
    assert ran.manifest.totals.guard_denials == 2
    assert ran.finished.possible_injection == ("landscape",)


def test_denials_elsewhere_are_not_flagged_as_injection(runner: Runner) -> None:
    ran = runner({"overview:draft": {"fail": "guard_denial"}})

    [denied] = ran.of(ev.ToolDenied)
    assert not denied.possible_injection
    assert ran.finished.possible_injection == ()


def test_extra_files_are_reported_and_never_kept(runner: Runner) -> None:
    ran = runner({"overview:draft": {"fail": "extra_file"}})

    messages = [n.message for n in ran.of(ev.Notice)]
    assert (
        "overview draft: ignored 1 file the session wrote besides its output: notes.md"
        in messages
    )
    assert not (runner.root / ".spadework" / "work" / "notes.md").exists()


def test_the_review_limits_are_recorded(runner: Runner) -> None:
    assert runner(rounds=3).manifest.limits.rounds == 3
    ran = runner(force=True, review=False)
    assert ran.manifest.limits.rounds == 0


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"max_time": "soon"}, "--max-time: 'soon' is not a duration"),
        ({"budget_usd": 0.0}, "--budget-usd 0.0 is not a positive amount"),
        ({"rounds": -1}, "--rounds cannot be negative"),
        ({"draft_model": " "}, "--draft-model is empty"),
    ],
)
def test_bad_options(runner: Runner, options: dict[str, object], message: str) -> None:
    with pytest.raises(UsageError, match=message.replace("(", r"\(")):
        runner(**options)


def _dead_pid() -> int:
    import subprocess
    import sys

    code = "import os; print(os.getpid())"
    process = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    return int(process.stdout)


def test_executor_notices_are_passed_on(runner: Runner) -> None:
    notice = Notice("Claude Code has no --restricted")

    ran = runner(executor=Recording(emit=(notice,)))

    notices = [n for n in ran.of(ev.Notice) if n.message == notice.message]
    assert len(notices) == 4
    assert notices[0].task == "overview"


def test_the_guards_denial_log_is_kept_with_the_session_log(runner: Runner) -> None:
    def deny(spec: SessionSpec) -> None:
        if spec.task == "overview":
            log = spec.policy_path.parent / f"{spec.session_id}.denials.jsonl"
            log.write_text('{"tool_name": "Write", "reason": "not writable"}\n')

    ran = runner(executor=Recording(before=deny))

    session = ran.manifest.sessions[0]
    assert session.guard_denials == 1
    run_dir = ran.root / ".spadework" / "runs" / ran.manifest.run_id
    kept = run_dir / "sessions" / "001-overview-draft.denials.jsonl"
    assert json.loads(kept.read_text()) == {
        "tool_name": "Write",
        "reason": "not writable",
    }


def test_a_file_that_cannot_be_staged_fails_the_task(runner: Runner) -> None:
    runner()
    out = runner.root / "out" / "01-overview.md"
    elsewhere = runner.root / "elsewhere.md"
    elsewhere.write_text("# Overview\n\nMoved.\n")
    out.unlink()
    out.symlink_to(elsewhere)

    ran = runner(force=True, only=["landscape"])

    assert ran.executor.sessions == []
    assert ran.tasks() == {"landscape": "failed"}
    error = ran.state.tasks["landscape"].error
    assert error is not None
    assert error.startswith("cannot stage the session: ")


def test_a_template_that_fails_at_run_time_fails_the_task(runner: Runner) -> None:
    task = runner.root / "playbooks" / "valid" / "tasks" / "landscape.md"
    task.write_text(
        task.read_text() + "First source: {{ research.sources[0].title }}\n"
    )
    nothing = {"sources": [], "notes": "Nothing found.", "unanswered": ["All"]}

    ran = runner({"landscape:research": {"structured": nothing}})

    assert ran.tasks()["landscape"] == "failed"
    error = ran.state.tasks["landscape"].error
    assert error is not None
    assert error.startswith("the task template failed: ")
    assert ran.code == ExitCode.PARTIAL


def test_a_repair_that_fails_keeps_the_draft(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project()
    write_playbook(
        root,
        {"alpha": task_file("01-alpha.md", extra="validate:\n  max_open_questions: 0")},
    )

    ran = Runner(root, research_tmp)({"alpha:repair": {"fail": "invalid_schema"}})

    assert ran.executor.kinds() == ["alpha:draft", "alpha:repair", "alpha:repair"]
    assert ran.tasks() == {"alpha": "invalid"}
    messages = [n.message for n in ran.of(ev.Notice)]
    assert any(m.startswith("alpha: the repair failed (") for m in messages)
    record = json.loads((root / ".spadework/records/alpha.draft.json").read_text())
    assert record["kind"] == "draft"


def test_a_stop_before_the_repair_keeps_the_draft(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project()
    write_playbook(
        root,
        {"alpha": task_file("01-alpha.md", extra="validate:\n  max_open_questions: 0")},
    )

    # The draft costs $0.50; the repair's $0.40 and the reserve then do not fit.
    ran = Runner(root, research_tmp)({"alpha:draft": {"cost_usd": 0.5}}, budget_usd=1.2)

    assert ran.result.outcome == "budget"
    assert ran.executor.kinds() == ["alpha:draft"]
    assert ran.state.tasks["alpha"].status == "invalid"
    assert (root / ".spadework" / "work" / "01-alpha.md").is_file()


def test_a_run_stopped_before_its_render_is_finished_by_the_next(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("spadework.engine.run.render_pack", interrupted)
    first = runner()
    assert first.code == ExitCode.INTERRUPTED
    assert first.state.review is not None
    assert not (runner.root / "out").exists()
    monkeypatch.undo()

    ran = runner()

    # Nothing is stale, but the render the last run owed is done now.
    assert ran.executor.sessions == []
    assert ran.result.outcome == "complete"
    assert len(ran.finished.rendered) == 3
    assert ran.state.review is None


def test_a_rate_limit_without_a_reset_time(runner: Runner) -> None:
    def limited(spec: SessionSpec, result: SessionResult) -> SessionResult:
        return SessionResult(status="rate_limited", error="API error 429")

    ran = runner(executor=Recording(after=limited))

    assert ran.code == ExitCode.RATE_LIMITED
    assert ran.manifest.rate_limit_reset is None
    assert ran.finished.resume_after is None
    assert ran.finished.hint is not None
    assert ran.finished.hint.startswith("Its reset time is not known.")


def test_wait_on_rate_limit_backs_off_without_a_reset_time(runner: Runner) -> None:
    calls: list[str] = []

    def limited_once(spec: SessionSpec, result: SessionResult) -> SessionResult:
        calls.append(spec.session_id)
        if len(calls) == 1:
            return SessionResult(status="rate_limited", error="API error 429")
        return result

    ran = runner(executor=Recording(after=limited_once), wait_on_rate_limit=True)

    assert ran.code == 0
    assert runner.clock.sleeps == [300.0]
    assert ran.executor.kinds()[:2] == ["overview:draft", "overview:draft"]
    waiting = [n for n in ran.of(ev.Notice) if n.level == "info"]
    assert waiting[0].message.startswith("the usage limit was reached; waiting until ")


def test_wait_on_rate_limit_stops_when_backing_off_runs_out_of_time(
    runner: Runner,
) -> None:
    def limited(spec: SessionSpec, result: SessionResult) -> SessionResult:
        return SessionResult(status="rate_limited", error="API error 429")

    ran = runner(
        executor=Recording(after=limited), wait_on_rate_limit=True, max_time="20m"
    )

    # 1200 s: waits of 300 s at 0, 300 and 600 s. At 900 s, waiting would
    # leave no time for a session after it.
    assert ran.code == ExitCode.RATE_LIMITED
    assert runner.clock.sleeps == [300.0, 300.0, 300.0]
    assert len(ran.executor.sessions) == 4


# Regressions from the engine review.


def test_a_failed_force_only_redraft_keeps_the_persons_edits(runner: Runner) -> None:
    runner()
    out = runner.root / "out" / "01-overview.md"
    edited = out.read_text() + "\nMy edits.\n"
    out.write_text(edited)

    ran = runner({"overview:draft": {"fail": "timeout"}}, force=True, only=["overview"])

    assert ran.tasks() == {"overview": "failed"}
    assert ran.result.outcome == "partial"
    assert out.read_text() == edited
    assert ran.finished.rendered == ()
    states = {t.id: t.state for t in plan_project(load_project(runner.root)).plan.tasks}
    assert states["overview"] == "pinned"


def test_a_redraft_that_ends_invalid_still_replaces_the_pinned_file(
    runner: Runner,
) -> None:
    runner()
    out = runner.root / "out" / "01-overview.md"
    out.write_text("# Mine\n")
    # The fake writes "Placeholder text": the draft and its repair both fail.
    task = runner.root / "playbooks" / "valid" / "tasks" / "overview.md"
    task.write_text(
        task.read_text().replace(
            "validate:\n", "validate:\n  forbidden_phrases: [placeholder]\n"
        )
    )

    ran = runner(force=True, only=["overview"])

    assert ran.executor.kinds() == ["overview:draft", "overview:repair"]
    assert ran.tasks() == {"overview": "invalid"}
    assert out.read_text().startswith("# Overview\n")
    assert ran.finished.rendered == ("out/01-overview.md",)


def test_a_reset_in_the_past_backs_off_instead_of_spinning(runner: Runner) -> None:
    past = int(runner.clock.time()) - 3600
    ran = runner(
        {"overview:draft": {"fail": "rate_limit", "resets_at": past, "times": 2}},
        wait_on_rate_limit=True,
    )

    assert runner.clock.sleeps == [300.0, 300.0]
    assert ran.code == 0


def test_a_reset_less_than_the_backoff_away_waits_the_backoff(runner: Runner) -> None:
    soon = int(runner.clock.time()) + 20
    ran = runner(
        {"overview:draft": {"fail": "rate_limit", "resets_at": soon, "times": 1}},
        wait_on_rate_limit=True,
    )

    assert runner.clock.sleeps == [300.0]
    assert ran.code == 0


def test_a_reset_in_the_past_is_not_printed_as_a_resume_time(runner: Runner) -> None:
    past = int(runner.clock.time()) - 3600
    ran = runner({"overview:draft": {"fail": "rate_limit", "resets_at": past}})

    assert ran.code == ExitCode.RATE_LIMITED
    assert ran.finished.resume_after is None
    assert ran.manifest.rate_limit_reset is None


def test_a_session_waits_for_a_rate_limit_at_most_six_times(runner: Runner) -> None:
    ran = runner(
        {"overview:draft": {"fail": "rate_limit"}},
        wait_on_rate_limit=True,
        max_time="24h",
    )

    assert ran.code == ExitCode.RATE_LIMITED
    assert len(runner.clock.sleeps) == 6
    assert len(ran.executor.sessions) == 7
    assert ran.finished.message == "the usage limit was still reached after 6 waits"
    assert [e.retry for e in ran.of(ev.SessionFinished)] == [True] * 6 + [False]


def test_a_failing_reporter_does_not_change_how_the_run_ends(runner: Runner) -> None:
    class Failing:
        calls = 0

        def __call__(self, event: ev.Event) -> None:
            if isinstance(event, ev.ToolUsed):
                self.calls += 1
                if self.calls == 3:
                    raise BrokenPipeError(32, "Broken pipe")

    executor = Recording()
    result = run_project(
        runner.root,
        RunOptions(),
        reporter=Failing(),
        executor=executor,
        clock=runner.clock,
        temp_root=runner.tmp,
    )

    assert result.outcome == "complete"
    assert int(result.exit_code) == 0
    assert isinstance(result.reporter_error, BrokenPipeError)
    assert result.manifest is not None
    assert result.manifest.outcome == "complete"
    assert len(result.manifest.sessions) == len(executor.sessions) == 4


def test_a_session_an_exception_ends_is_still_recorded_at_its_cap(
    runner: Runner,
) -> None:
    def boom(spec: SessionSpec) -> None:
        if spec.task == "landscape":
            raise RuntimeError("boom")

    ran = runner(executor=Recording(before=boom))

    assert ran.code == ExitCode.ERROR
    manifest = ran.manifest
    assert manifest.exit_code == ran.code
    last = manifest.sessions[-1]
    assert (last.task, last.kind, last.status) == ("landscape", "research", "error")
    assert last.cost_usd == pytest.approx(1.125)  # its cap: the cost is unknown
    assert last.error == "stopped by an error: RuntimeError: boom"
    assert manifest.totals.cost_usd == pytest.approx(0.08 + 1.125)


def test_an_input_removed_during_the_run_is_a_stale_reason(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project(inputs={"notes.txt": "hello\n"})

    def remove(spec: SessionSpec) -> None:
        if spec.task == "plan":
            (root / "inputs" / "notes.txt").unlink()

    ran = Runner(root, research_tmp)(executor=Recording(before=remove))

    assert ran.code == 0
    assert ran.result.outcome == "complete"
    assert ran.finished.tasks == {
        "overview": "stale",
        "landscape": "stale",
        "plan": "stale",
    }
    planned = plan_project(load_project(root)).plan.tasks
    assert planned[0].reason == "inputs/notes.txt removed"


def test_a_pinned_dependency_passes_no_key_facts(runner: Runner) -> None:
    runner()
    out = runner.root / "out" / "01-overview.md"
    out.write_text("# Overview\n\nThe person chose option B.\n")

    ran = runner(force=True, only=["landscape"])

    [draft] = ran.executor.of("landscape", "draft")
    prompt = draft.spec.prompt
    # The fixture's template lists deps.overview.key_facts: now none.
    assert "Overview: placeholder content" not in prompt
    assert "## Key facts" not in prompt
    assert "Build on 01-overview.md:\n\n" in prompt
    assert draft.files["01-overview.md"] == "# Overview\n\nThe person chose option B.\n"


def test_cache_creation_tokens_reach_the_manifest(runner: Runner) -> None:
    def usage(spec: SessionSpec, result: SessionResult) -> SessionResult:
        result.usage = Usage(input_tokens=10, cache_creation_tokens=7)
        return result

    ran = runner(executor=Recording(after=usage))

    first = ran.manifest.sessions[0].usage
    assert (first.input_tokens, first.cache_creation_tokens) == (10, 7)


def test_a_render_cut_short_does_not_pin_the_engines_own_file(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner()
    task = runner.root / "playbooks" / "valid" / "tasks" / "overview.md"
    task.write_text(
        task.read_text().replace("Open questions]", "Open questions, Notes]")
    )
    from spadework.engine import pack

    write = pack.write_file

    def write_then_stop(path: Path, data: bytes) -> None:
        write(path, data)
        raise KeyboardInterrupt  # the run stops before recording the hash

    monkeypatch.setattr(pack, "write_file", write_then_stop)
    stopped = runner()
    assert stopped.code == ExitCode.INTERRUPTED
    monkeypatch.undo()

    planned = plan_project(load_project(runner.root)).plan.tasks
    assert {t.id: t.state for t in planned}["overview"] == "fresh"
    ran = runner()
    assert ran.executor.sessions == []
    assert ran.result.outcome == "complete"
    entry = ran.state.tasks["overview"]
    assert entry.rendered is not None
    assert entry.output is not None
    assert entry.rendered.hash == entry.output.hash


def test_a_brief_linked_inside_the_project_is_staged_by_its_target(
    runner: Runner,
) -> None:
    brief = runner.root / "brief.md"
    (runner.root / "docs").mkdir()
    target = runner.root / "docs" / "brief.md"
    target.write_text(brief.read_text())
    brief.unlink()
    brief.symlink_to(Path("docs") / "brief.md")

    ran = runner()

    assert ran.code == 0
    staged = ran.executor.of("overview", "draft")[0].files["brief.md"]
    assert staged == target.read_text()


def test_a_task_without_research_may_cite_no_sources(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project()
    # The fake cites every source id its prompt mentions.
    write_playbook(root, {"alpha": task_file("01-alpha.md", body="Like S1 said.\n")})

    ran = Runner(root, research_tmp)()

    assert ran.executor.kinds() == ["alpha:draft", "alpha:repair"]
    assert "- No `[S#]` citations" in ran.executor.sessions[0].spec.prompt
    [failure] = [v for v in ran.manifest.sessions[0].validators if not v.ok]
    assert failure.name == "citations_resolve"
    assert failure.message is not None
    assert "this task has no sources" in failure.message
    assert ran.tasks() == {"alpha": "invalid"}
