"""The review loop: critic, fixes, repairs, and how an interrupted review resumes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from spadework.engine import events as ev
from spadework.errors import ExitCode
from spadework.executors.base import SessionSpec
from spadework.executors.fake import draft_result
from spadework.keys import hash_bytes
from tests.conftest import MakeProject
from tests.engine.conftest import (
    Recording,
    Runner,
    leave_dead_lock,
    task_file,
    write_playbook,
)

DRAFTS = ["overview:draft", "landscape:research", "landscape:draft", "plan:draft"]


class Crash(BaseException):
    """Stands in for kill -9: nothing after it runs."""


def finding(id: str, target: str, instruction: str = "Say more.") -> dict[str, Any]:
    return {
        "id": id,
        "target": target,
        "kind": "specificity",
        "severity": "medium",
        "instruction": instruction,
    }


def critic(*findings: dict[str, Any], verdict: str = "continue") -> dict[str, Any]:
    return {"verdict": verdict, "findings": list(findings), "biggest_gap": ""}


def fixed(title: str, *, applied: list[str], rejected: list[Any]) -> dict[str, Any]:
    return {**draft_result(title), "applied": applied, "rejected": rejected}


def crash_on(
    kind: str,
    task: str | None = None,
    *,
    nth: int = 1,
    script: dict[str, dict[str, Any]] | None = None,
) -> Recording:
    """An executor that crashes the nth time a session of this kind starts."""
    seen: list[str] = []

    def before(spec: SessionSpec) -> None:
        if spec.kind.value == kind and (task is None or spec.task == task):
            seen.append(spec.session_id)
            if len(seen) == nth:
                raise Crash

    return Recording(script, before=before)


def critic_record(root: Path, round_: int) -> dict[str, Any]:
    path = root / ".spadework" / "records" / f"review.round-{round_}.json"
    data: dict[str, Any] = json.loads(path.read_text())
    return data


# The loop.


def test_the_critic_sees_the_whole_pack_and_the_fixer_its_document(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project(inputs={"calls/one.md": "# Call one\n", "other.md": "x"})

    ran = Runner(root, research_tmp)()

    [first, second] = ran.executor.of(None, "critic")
    assert sorted(first.files) == [
        "01-overview.md",
        "02-landscape.md",
        "03-plan.md",
        "brief.md",
        "inputs/calls/one.md",
        "inputs/other.md",
        "records/digest.md",
    ]
    assert first.spec.output is None
    assert first.spec.tools == ("Read", "Glob", "Grep")
    assert first.spec.model == "opus"
    assert first.spec.output_schema["properties"]["findings"]["maxItems"] == 4
    prompt = first.spec.prompt
    assert prompt.startswith("# Review, round 1 of 2\n")
    assert "| `02-landscape.md` | Landscape | fresh |" in prompt
    assert "- Staff can edit the catalogue unaided" in prompt
    assert "At most 4 findings." in prompt
    records = first.files["records/digest.md"]
    assert "## 02-landscape.md: Landscape" in records
    assert "overview:Q1: Is a placeholder overview enough" in records
    assert "S2 [unreachable]: Sample Benchmarks: landscape" in records
    assert "# Reviewing the pack" in first.spec.system_append
    assert "# House rules" in first.spec.system_append
    assert second.spec.prompt.startswith("# Review, round 2 of 2\n")

    [fix] = ran.executor.of("overview", "fix")
    assert sorted(fix.files) == [
        "01-overview.md",
        "brief.md",
        "inputs/calls/one.md",
        "inputs/other.md",
    ]
    assert fix.files["01-overview.md"].startswith("# Overview\n")
    assert fix.spec.output is not None
    assert fix.spec.output.name == "01-overview.md"
    assert fix.spec.model == "sonnet"  # the task's tier
    assert (
        "- **F1** (specificity, medium): Tie the first section to a fact from the "
        "brief." in fix.spec.prompt
    )
    assert "Required headings: Summary, Open questions" in fix.spec.prompt
    assert "# Fixing a document" in fix.spec.system_append


def test_a_fix_changes_the_text_and_never_the_key(runner: Runner) -> None:
    ran = runner()

    entry = ran.state.tasks["overview"]
    work = runner.root / ".spadework" / "work" / "01-overview.md"
    assert work.read_text().endswith("Revised after review (F1).\n")
    assert entry.output is not None
    [draft] = [s for s in ran.manifest.sessions if s.kind == "draft"][:1]
    assert entry.output.key == entry.material.key == draft.key
    fix = next(s for s in ran.manifest.sessions if s.kind == "fix")
    assert (fix.key, fix.round, fix.task) == (entry.output.key, 1, "overview")
    record = json.loads(
        (runner.root / ".spadework" / "records" / "overview.draft.json").read_text()
    )
    assert record["kind"] == "fix"
    assert record["key"] == entry.output.key
    assert "applied" not in record["result"]
    # Nothing is stale after a fix: the next run is up to date.
    assert runner().executor.sessions == []
    # The review's finding and its rendered document.
    assert (
        "Revised after review (F1)."
        in (runner.root / "out" / "01-overview.md").read_text()
    )
    assert [e.task for e in ran.of(ev.TaskFinished)][-1] == "overview"


def test_a_ship_verdict_ends_the_review(runner: Runner) -> None:
    ran = runner({"critic": {"verdict": "ship"}})

    assert ran.executor.reviewing() == ["critic", "summary"]
    assert any(
        n.message == "review round 1: the critic's verdict is ship"
        for n in ran.of(ev.Notice)
    )
    assert critic_record(runner.root, 1)["result"]["verdict"] == "ship"


def test_no_review_skips_the_rounds_but_not_the_summary(runner: Runner) -> None:
    ran = runner(review=False)

    assert ran.executor.kinds() == [*DRAFTS, "summary"]
    assert ran.manifest.limits.rounds == 0
    assert (runner.root / "out" / "00-summary.md").is_file()


def test_rounds_sets_the_most_rounds(runner: Runner) -> None:
    ran = runner(
        {"critic": {"structured": critic(finding("F1", "03-plan.md"))}}, rounds=1
    )

    assert ran.executor.reviewing() == ["critic", "plan:fix", "summary"]
    assert ran.manifest.limits.rounds == 1


def test_three_rounds(runner: Runner) -> None:
    ran = runner(
        {"critic": {"structured": critic(finding("F1", "03-plan.md"))}}, rounds=3
    )

    assert ran.executor.reviewing() == [
        "critic",
        "plan:fix",
        "critic",
        "plan:fix",
        "critic",
        "plan:fix",
        "summary",
    ]
    assert [s.round for s in ran.manifest.sessions if s.kind == "critic"] == [1, 2, 3]


def test_fixes_run_in_task_order(runner: Runner) -> None:
    findings = critic(
        finding("F1", "03-plan.md"),
        finding("F2", "01-overview.md"),
        finding("F3", "03-plan.md", "Shorter."),
    )

    ran = runner({"critic": {"structured": findings, "times": 1}})

    assert ran.executor.reviewing() == [
        "critic",
        "overview:fix",
        "plan:fix",
        "critic",
        "summary",
    ]
    [plan] = ran.executor.of("plan", "fix")
    assert "**F1**" in plan.spec.prompt
    assert "**F3**" in plan.spec.prompt
    assert "**F2**" not in plan.spec.prompt


def test_rejected_findings_go_to_the_manifest_and_never_the_document(
    runner: Runner,
) -> None:
    reason = "It would mean inventing a price the sources do not give."
    result = fixed(
        "Overview",
        applied=["F1"],
        rejected=[{"finding": "F2", "reason": reason}],
    )
    findings = critic(
        finding("F1", "01-overview.md"),
        finding("F2", "01-overview.md", "Give the exact monthly price."),
    )

    ran = runner(
        {
            "critic": {"structured": findings, "times": 1},
            "overview:fix": {"structured": result},
        }
    )

    fix = next(s for s in ran.manifest.sessions if s.kind == "fix")
    assert [r.model_dump() for r in fix.rejected_findings] == [
        {
            "finding": "F2",
            "reason": reason,
            "instruction": "Give the exact monthly price.",
        }
    ]
    assert reason not in (runner.root / "out" / "01-overview.md").read_text()
    assert (
        reason
        not in (runner.root / ".spadework" / "work" / "01-overview.md").read_text()
    )


def test_findings_naming_an_unknown_file_are_dropped_and_logged(
    runner: Runner,
) -> None:
    findings = critic(
        finding("F1", "99-nowhere.md"),
        finding("F2", "out/03-plan.md"),
        finding("F3", "00-summary.md"),
    )

    ran = runner({"critic": {"structured": findings, "times": 1}})

    assert ran.executor.reviewing() == ["critic", "plan:fix", "critic", "summary"]
    notices = [n.message for n in ran.of(ev.Notice)]
    assert (
        "review round 1: dropped finding F1: '99-nowhere.md' is not a document of "
        "the pack" in notices
    )
    assert any("dropped finding F3" in message for message in notices)
    record = critic_record(runner.root, 1)
    assert [f["target"] for f in record["findings"]] == ["03-plan.md"]
    assert len(record["dropped"]) == 2


def test_too_many_findings_fail_the_schema(runner: Runner) -> None:
    findings = critic(*(finding(f"F{n}", "03-plan.md") for n in range(1, 6)))

    ran = runner({"critic": {"structured": findings}})

    # Retried once with the error, then the review stops; the summary runs.
    assert ran.executor.reviewing() == ["critic", "critic", "summary"]
    first = next(s for s in ran.manifest.sessions if s.kind == "critic")
    assert first.error is not None
    assert "max_findings of 4" in first.error
    assert any(
        n.message.startswith("review round 1: the critic failed (")
        for n in ran.of(ev.Notice)
    )
    assert ran.code == 0


def test_a_failed_fix_keeps_the_document(runner: Runner) -> None:
    ran = runner({"overview:fix": {"fail": "timeout"}})

    work = runner.root / ".spadework" / "work" / "01-overview.md"
    assert "Revised after review" not in work.read_text()
    assert any(
        n.message.startswith("overview: the fix failed (") for n in ran.of(ev.Notice)
    )
    # Nothing changed, so another critic would see the same pack.
    assert ran.executor.reviewing() == ["critic", "overview:fix", "summary"]
    assert any(
        n.message == "review round 1 changed no document, so the review stopped"
        for n in ran.of(ev.Notice)
    )
    assert ran.code == 0


def forbid_the_fakes_revision(runner: Runner) -> None:
    """The fake's fix appends "Revised after review": make that fail a check.
    Its repair writes a fresh document, which passes."""
    task = runner.root / "playbooks" / "valid" / "tasks" / "overview.md"
    task.write_text(
        task.read_text().replace(
            "validate:\n", "validate:\n  forbidden_phrases: [revised after review]\n"
        )
    )


def test_a_fix_that_breaks_a_check_gets_one_repair(runner: Runner) -> None:
    forbid_the_fakes_revision(runner)

    ran = runner()

    assert ran.executor.reviewing() == [
        "critic",
        "overview:fix",
        "overview:repair",
        "critic",
        "summary",
    ]
    [repair] = ran.executor.of("overview", "repair")
    assert "## What to repair" in repair.spec.prompt
    assert "`forbidden_phrases`: Forbidden phrase found: 'revised after review'" in (
        repair.spec.prompt
    )
    assert repair.files["01-overview.md"].endswith("Revised after review (F1).\n")
    assert ran.state.tasks["overview"].status == "fresh"
    fix, repaired = [s for s in ran.manifest.sessions if s.round == 1 and s.task]
    assert (fix.kind, repaired.kind, repaired.round) == ("fix", "repair", 1)
    record = json.loads(
        (runner.root / ".spadework" / "records" / "overview.draft.json").read_text()
    )
    assert record["kind"] == "repair"


def test_validator_failures_become_findings(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project()
    write_playbook(
        root,
        {
            # The fake writes "Placeholder": draft and repair both fail.
            "alpha": task_file(
                "01-alpha.md", extra="validate:\n  forbidden_phrases: [placeholder]"
            ),
            "beta": task_file("02-beta.md"),
        },
    )

    ran = Runner(root, research_tmp)({"critic": {"verdict": "ship"}})

    # The critic ships, but a validator is failing: the fixes go on.
    assert ran.executor.kinds() == [
        "alpha:draft",
        "alpha:repair",
        "beta:draft",
        "critic",
        "alpha:fix",
        "alpha:repair",
        "critic",
        "alpha:fix",
        "alpha:repair",
        "summary",
    ]
    critic = ran.executor.of(None, "critic")[0]
    assert "## Already found by the checks in code" in critic.spec.prompt
    assert "- `01-alpha.md`: Forbidden phrase found: 'placeholder'" in (
        critic.spec.prompt
    )
    [record_finding] = critic_record(root, 1)["findings"]
    assert record_finding["id"] == "F1"
    assert record_finding["origin"] == "validator"
    assert record_finding["kind"] == "filler"
    fix = ran.executor.of("alpha", "fix")[0]
    assert "**F1** (filler, from a check in code, high): Forbidden phrase" in (
        fix.spec.prompt
    )
    # Still failing after the review: reported, never hidden.
    assert ran.code == ExitCode.PARTIAL
    summary = (root / "out" / "00-summary.md").read_text()
    assert (
        "| [01-alpha.md](01-alpha.md): Alpha | invalid | failed: forbidden_phrases |"
        in summary
    )
    pack = json.loads((root / "out" / "pack.json").read_text())
    assert [d["validators_ok"] for d in pack["documents"]] == [False, True]


def test_a_failure_no_fix_can_mend_is_not_a_finding(runner: Runner) -> None:
    task = runner.root / "playbooks" / "valid" / "tasks" / "landscape.md"
    task.write_text(task.read_text().replace("min_sources: 2", "min_sources: 6"))

    ran = runner({"critic": {"verdict": "ship"}})

    assert ran.tasks()["landscape"] == "invalid"
    assert ran.executor.reviewing() == ["critic", "summary"]
    assert critic_record(runner.root, 1)["findings"] == []


# Documents a person edited.


def test_findings_on_an_edited_document_are_kept_for_the_summary(
    runner: Runner,
) -> None:
    runner()
    out = runner.root / "out" / "01-overview.md"
    out.write_text("# Overview\n\nThe person's version.\n")
    brief = runner.root / "brief.md"
    brief.write_text(brief.read_text() + "\nMore.\n")
    findings = critic(
        finding("F1", "01-overview.md", "Name the catalogue's three product lines.")
    )

    ran = runner({"critic": {"structured": findings, "times": 1}})

    assert ran.executor.drafting() == ["landscape:draft", "plan:draft"]
    # Every finding was on the edited document: no second critic.
    assert ran.executor.reviewing() == ["critic", "summary"]
    assert out.read_text() == "# Overview\n\nThe person's version.\n"
    critic_session = ran.executor.of(None, "critic")[0]
    assert critic_session.files["01-overview.md"] == out.read_text()
    assert (
        "| `01-overview.md` | Overview | edited by a person |"
        in critic_session.spec.prompt
    )
    assert any(
        n.message == "overview: 1 review finding kept for the summary: a person "
        "edited 01-overview.md"
        for n in ran.of(ev.Notice)
    )
    summary = (runner.root / "out" / "00-summary.md").read_text()
    assert "## Review findings for a person" in summary
    assert (
        "| 01-overview.md | specificity | medium | Name the catalogue's three "
        "product lines. |" in summary
    )
    assert "| [01-overview.md](01-overview.md): Overview | edited by a person |" in (
        summary
    )
    # The pinned document's findings are not validator findings.
    assert critic_record(runner.root, 1)["validators_failing"] is False


# Money and time.


def test_the_review_stops_when_the_budget_cannot_cover_a_critic_and_a_fix(
    runner: Runner,
) -> None:
    # Drafts: 0.08 x 3 + 0.05 = 0.29. Left for sessions other than the
    # summary: 2.30 - 0.29 - 0.50 = 1.51, and a critic and a fix need 1.50.
    ran = runner(budget_usd=2.30)

    assert ran.executor.reviewing() == ["critic", "overview:fix", "summary"]
    notices = [n.message for n in ran.of(ev.Notice)]
    # After the critic ($0.06) and the fix ($0.04): 2.30 - 0.39 - 0.50.
    assert any(
        m.startswith("the review stopped before round 2: the budget left ($1.41")
        and "with $0.50 kept for the summary) cannot cover $1.50" in m
        for m in notices
    ), notices
    assert ran.code == 0
    assert ran.result.outcome == "complete"
    assert (runner.root / "out" / "00-summary.md").is_file()


def test_the_summary_runs_on_its_reserve(runner: Runner) -> None:
    # The drafts spend 0.21 + 1.00: 2.00 - 1.21 - 0.50 = 0.29 is left for
    # other sessions, too little for anything. The summary has its $0.50.
    ran = runner({"plan:draft": {"cost_usd": 1.0}}, budget_usd=2.0)

    assert ran.executor.reviewing() == ["summary"]
    summary = next(s for s in ran.manifest.sessions if s.kind == "summary")
    assert ran.executor.sessions[-1].spec.max_budget_usd == pytest.approx(0.75)
    assert summary.status == "ok"
    assert ran.code == 0


def test_a_fix_that_uses_what_is_left_of_the_budget_ends_the_review(
    runner: Runner,
) -> None:
    # Before the critic, 2.35 - 0.29 - 0.50 = 1.56 covers it and a fix. The
    # critic costs 0.90, so the fix's cap is the 0.66 left, and it spends it.
    ran = runner(
        {"critic": {"cost_usd": 0.9}, "overview:fix": {"fail": "budget"}},
        budget_usd=2.35,
    )

    assert ran.executor.reviewing() == ["critic", "overview:fix", "summary"]
    assert any(
        n.message.startswith("the review stopped: overview fix used what was left")
        for n in ran.of(ev.Notice)
    )
    assert ran.result.outcome == "complete"
    assert (runner.root / "out" / "00-summary.md").is_file()


def test_the_review_stops_when_the_time_left_is_too_short(runner: Runner) -> None:
    def slow(spec: SessionSpec) -> None:
        if spec.task == "plan" and spec.kind.value == "draft":
            runner.clock.advance(7200 - 200)

    ran = runner(executor=Recording(before=slow))

    assert ran.executor.reviewing() == ["summary"]
    assert any(
        "the review stopped before round 1: the time left" in n.message
        for n in ran.of(ev.Notice)
    )
    assert ran.code == 0


# Resuming an interrupted review.


def test_a_crash_before_the_critic_finishes_runs_it_again(runner: Runner) -> None:
    with pytest.raises(Crash):
        runner(executor=crash_on("critic"))
    leave_dead_lock(runner.root)
    assert runner.root.joinpath("out").exists() is False

    ran = runner()

    assert ran.executor.kinds() == ["critic", "overview:fix", "critic", "summary"]
    assert [s.round for s in ran.manifest.sessions] == [1, 1, 2, 0]
    assert ran.code == 0
    assert ran.state.review is None
    assert (runner.root / "out" / "00-summary.md").is_file()


def test_a_crash_during_the_fixes_resumes_the_round_without_its_critic(
    runner: Runner,
) -> None:
    findings = critic(finding("F1", "01-overview.md"), finding("F2", "03-plan.md"))
    script = {"critic": {"structured": findings, "times": 1}}
    with pytest.raises(Crash):
        runner(executor=crash_on("fix", "plan", script=script))
    leave_dead_lock(runner.root)
    progress = json.loads((runner.root / ".spadework" / "state.json").read_text())
    assert progress["review"]["round"] == 1
    assert progress["review"]["fixed"] == ["overview"]

    ran = runner()

    # Round 1's findings come from its record; overview's fix is not redone.
    assert ran.executor.kinds() == ["plan:fix", "critic", "overview:fix", "summary"]
    assert [s.round for s in ran.manifest.sessions] == [1, 2, 2, 0]
    [plan] = ran.executor.of("plan", "fix")
    assert "**F2**" in plan.spec.prompt
    assert "**F1**" not in plan.spec.prompt
    work = runner.root / ".spadework" / "work" / "01-overview.md"
    assert work.read_text().count("Revised after review") == 2  # rounds 1 and 2


def test_a_crash_before_a_repair_resumes_with_the_repair_alone(
    runner: Runner,
) -> None:
    forbid_the_fakes_revision(runner)
    with pytest.raises(Crash):
        runner(executor=crash_on("repair", "overview"))
    leave_dead_lock(runner.root)
    state = json.loads((runner.root / ".spadework" / "state.json").read_text())
    assert state["review"]["repairs"] == ["overview"]
    assert state["tasks"]["overview"]["status"] == "invalid"

    ran = runner({"critic": {"verdict": "ship"}})

    assert ran.executor.kinds() == ["overview:repair", "critic", "summary"]
    [repair] = ran.executor.of("overview", "repair")
    assert "`forbidden_phrases`: Forbidden phrase found: 'revised after review'" in (
        repair.spec.prompt
    )
    assert repair.spec.prompt.startswith("# Fix: Overview\n")
    assert ran.state.tasks["overview"].status == "fresh"


def test_a_crash_during_the_summary_resumes_with_the_summary(runner: Runner) -> None:
    with pytest.raises(Crash):
        runner(executor=crash_on("summary"))
    leave_dead_lock(runner.root)

    ran = runner()

    assert ran.executor.kinds() == ["summary"]
    assert ran.code == 0
    assert (runner.root / "out" / "00-summary.md").is_file()


def test_a_crash_after_the_summary_only_renders(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def interrupted(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("spadework.engine.run.render_pack", interrupted)
    assert runner().code == ExitCode.INTERRUPTED
    monkeypatch.undo()

    ran = runner()

    assert ran.executor.sessions == []
    assert ran.code == 0
    assert (
        "Placeholder content for Executive summary."
        in (runner.root / "out" / "00-summary.md").read_text()
    )


def test_no_review_on_resume_skips_what_is_left_of_it(runner: Runner) -> None:
    with pytest.raises(Crash):
        runner(executor=crash_on("critic"))
    leave_dead_lock(runner.root)

    ran = runner(review=False)

    assert ran.executor.kinds() == ["summary"]


def test_new_drafts_start_a_new_review(runner: Runner) -> None:
    with pytest.raises(Crash):
        runner(executor=crash_on("critic", nth=2))
    leave_dead_lock(runner.root)
    (runner.root / "inputs" / "note.md").write_text("# Note\n")

    ran = runner()

    assert ran.executor.drafting() == [
        "overview:draft",
        "landscape:draft",
        "plan:draft",
    ]
    assert ran.executor.reviewing() == ["critic", "overview:fix", "critic", "summary"]
    assert [s.round for s in ran.manifest.sessions if s.kind == "critic"] == [1, 2]


def test_a_ship_verdict_still_keeps_findings_for_a_person(runner: Runner) -> None:
    runner()
    (runner.root / "out" / "03-plan.md").write_text("# Plan\n\nMine.\n")
    (runner.root / "inputs" / "calls").mkdir()
    (runner.root / "inputs" / "calls" / "new.md").write_text("# New\n")
    findings = critic(finding("F1", "03-plan.md", "Date each phase."), verdict="ship")

    ran = runner({"critic": {"structured": findings}})

    assert ran.executor.reviewing() == ["critic", "summary"]
    assert (
        "| 03-plan.md | specificity | medium | Date each phase. |"
        in (runner.root / "out" / "00-summary.md").read_text()
    )


def test_a_rate_limit_in_the_review_stops_the_run_and_it_resumes(
    runner: Runner,
) -> None:
    ran = runner({"critic": {"fail": "rate_limit", "times": 1}})

    assert ran.code == ExitCode.RATE_LIMITED
    assert not (runner.root / "out").exists()
    assert ran.state.review is not None

    resumed = runner()

    assert resumed.executor.kinds() == ["critic", "overview:fix", "critic", "summary"]
    assert resumed.code == 0


def test_a_finding_on_a_task_with_no_document_is_dropped(runner: Runner) -> None:
    findings = critic(finding("F1", "02-landscape.md"))

    ran = runner(
        {
            "landscape:research": {"fail": "timeout"},
            "critic": {"structured": findings, "times": 1},
        }
    )

    # Nothing left to fix in round 1, so no second critic.
    assert ran.executor.reviewing() == ["critic", "summary"]
    assert any(
        n.message == "review round 1: dropped finding F1: 02-landscape.md has no "
        "document to fix"
        for n in ran.of(ev.Notice)
    )


def test_a_failed_repair_after_a_fix_keeps_the_fix(runner: Runner) -> None:
    forbid_the_fakes_revision(runner)

    ran = runner({"overview:repair": {"fail": "timeout"}})

    assert ran.executor.reviewing()[:3] == [
        "critic",
        "overview:fix",
        "overview:repair",
    ]
    assert any(
        n.message.startswith("overview: the repair after the fix failed (")
        for n in ran.of(ev.Notice)
    )
    work = runner.root / ".spadework" / "work" / "01-overview.md"
    assert work.read_text().endswith("Revised after review (F1).\n")
    assert ran.state.tasks["overview"].status == "invalid"
    assert ran.code == ExitCode.PARTIAL


def test_findings_for_a_person_survive_a_summary_retried_later(
    runner: Runner,
) -> None:
    runner()
    (runner.root / "out" / "03-plan.md").write_text("# Plan\n\nMine.\n")
    (runner.root / "inputs" / "calls").mkdir()
    (runner.root / "inputs" / "calls" / "new.md").write_text("# New\n")
    findings = critic(finding("F1", "03-plan.md", "Date each phase."))
    failed = runner(
        {
            "critic": {"structured": findings, "times": 1},
            "summary": {"fail": "invalid_schema"},
        }
    )
    assert failed.code == ExitCode.PARTIAL
    row = "| 03-plan.md | specificity | medium | Date each phase. |"
    assert row in (runner.root / "out" / "00-summary.md").read_text()

    retried = runner()

    assert retried.executor.kinds() == ["summary"]
    assert retried.code == 0
    text = (runner.root / "out" / "00-summary.md").read_text()
    assert "## Executive summary" in text
    assert row in text


def test_findings_for_a_document_no_longer_edited_are_not_carried_over(
    runner: Runner,
) -> None:
    runner()
    plan = runner.root / "out" / "03-plan.md"
    original = plan.read_text()
    plan.write_text("# Plan\n\nMine.\n")
    (runner.root / "inputs" / "calls").mkdir()
    (runner.root / "inputs" / "calls" / "new.md").write_text("# New\n")
    findings = critic(finding("F1", "03-plan.md", "Date each phase."))
    runner(
        {
            "critic": {"structured": findings, "times": 1},
            "summary": {"fail": "invalid_schema"},
        }
    )
    plan.write_text(original)  # the person's edits are undone

    runner()

    assert "Date each phase." not in (runner.root / "out" / "00-summary.md").read_text()


# The summary's slot of time.


def spend_until(runner: Runner, left: float) -> Recording:
    """An executor whose last draft leaves ``left`` seconds of a 1h run."""

    def before(spec: SessionSpec) -> None:
        if spec.task == "plan" and spec.kind.value == "draft":
            runner.clock.advance(3600 - left)

    return Recording(before=before)


def test_a_review_session_cannot_use_the_summarys_time(runner: Runner) -> None:
    executor = spend_until(runner, 400)

    def full_timeout(spec: SessionSpec) -> None:
        if spec.kind.value == "critic":
            runner.clock.advance(spec.timeout_s)

    plan_hook = executor.before
    assert plan_hook is not None
    executor.before = lambda spec: (plan_hook(spec), full_timeout(spec))[0]

    ran = runner(executor=executor, max_time="1h")

    # 400 s left: the critic gets 400 - 30 - 60, and 90 s are left after it.
    [critic] = ran.executor.of(None, "critic")
    assert critic.spec.timeout_s == 310
    assert ran.executor.reviewing() == ["critic", "summary"]
    assert ran.code == 0
    assert (runner.root / "out" / "00-summary.md").is_file()


def test_a_review_session_stopped_by_the_time_still_leaves_the_summary(
    runner: Runner,
) -> None:
    executor = spend_until(runner, 400)
    executor.inner.script.update(
        Recording({"critic": {"fail": "timeout"}}).inner.script
    )
    plan_hook = executor.before
    assert plan_hook is not None

    def hooks(spec: SessionSpec) -> None:
        plan_hook(spec)
        if spec.kind.value == "critic":
            runner.clock.advance(spec.timeout_s)

    executor.before = hooks

    ran = runner(executor=executor, max_time="1h")

    assert ran.executor.reviewing() == ["critic", "summary"]
    assert any(
        n.message == "the review stopped: critic used the time left before the summary"
        for n in ran.of(ev.Notice)
    )
    assert ran.code == 0
    assert ran.result.outcome == "complete"


def test_a_repair_with_no_time_left_is_skipped_not_the_run(runner: Runner) -> None:
    forbid_the_fakes_revision(runner)
    executor = spend_until(runner, 600)
    plan_hook = executor.before
    assert plan_hook is not None

    def hooks(spec: SessionSpec) -> None:
        plan_hook(spec)
        if spec.kind.value == "fix":
            runner.clock.advance(spec.timeout_s)

    executor.before = hooks

    ran = runner(executor=executor, max_time="1h")

    assert ran.executor.reviewing() == ["critic", "overview:fix", "summary"]
    assert any(
        n.message.startswith(
            "overview: the repair after the fix was skipped: the time left"
        )
        for n in ran.of(ev.Notice)
    )
    assert ran.state.tasks["overview"].status == "invalid"
    assert ran.code == ExitCode.PARTIAL
    assert (runner.root / "out" / "00-summary.md").is_file()


def test_without_time_for_the_summary_the_run_stops_and_resumes(
    runner: Runner,
) -> None:
    executor = spend_until(runner, 60)

    ran = runner(executor=executor, max_time="1h")

    assert ran.code == ExitCode.LIMIT
    assert ran.result.outcome == "time"
    assert ran.executor.reviewing() == []
    resumed = runner()
    assert resumed.executor.kinds() == ["summary"]
    assert resumed.code == 0


# A run that stops between a session's work and its record.


def test_a_fix_written_but_not_recorded_is_never_done_again(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spadework.engine import review

    write = review.write_work

    def write_then_crash(root: Path, output: str, data: bytes) -> str:
        write(root, output, data)
        raise Crash

    monkeypatch.setattr(review, "write_work", write_then_crash)
    with pytest.raises(Crash):
        runner()
    monkeypatch.undo()
    leave_dead_lock(runner.root)

    state = json.loads((runner.root / ".spadework" / "state.json").read_text())
    assert state["review"]["fixed"] == ["overview"]
    assert state["review"]["writing"]["task"] == "overview"

    ran = runner(rounds=1)

    # Round 1's fix is recorded, not redone, and it counts as a change.
    assert ran.executor.kinds() == ["summary"]
    work = runner.root / ".spadework" / "work" / "01-overview.md"
    assert work.read_text().count("Revised after review") == 1
    entry = ran.state.tasks["overview"]
    assert entry.output is not None
    assert entry.output.hash == hash_bytes(work.read_bytes())
    assert ran.state.review is None
    record = json.loads(
        (runner.root / ".spadework" / "records" / "overview.draft.json").read_text()
    )
    assert record["kind"] == "fix"
    assert [e.task for e in ran.of(ev.TaskFinished)] == ["overview"]
    assert (
        "Revised after review (F1)."
        in (runner.root / "out" / "01-overview.md").read_text()
    )


def test_a_fix_stopped_before_it_was_written_is_not_done_again(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spadework.engine import review

    def crash(root: Path, output: str, data: bytes) -> str:
        raise Crash

    monkeypatch.setattr(review, "write_work", crash)
    with pytest.raises(Crash):
        runner()
    monkeypatch.undo()
    leave_dead_lock(runner.root)

    ran = runner()

    assert ran.executor.kinds() == ["summary"]
    assert any(
        n.message == "overview: the last run stopped before it saved the fix, so "
        "the document stays as it was"
        for n in ran.of(ev.Notice)
    )
    work = runner.root / ".spadework" / "work" / "01-overview.md"
    assert "Revised after review" not in work.read_text()
    assert ran.code == 0


def crash_writing(monkeypatch: pytest.MonkeyPatch, name: str, *, after: bool) -> None:
    """Stop the run as a record called ``name`` is saved, before or after."""
    from spadework.engine import review

    write = review.write_record

    def write_record(root: Path, record_name: str, record: Any) -> str:
        if record_name == name and not after:
            raise Crash
        written = write(root, record_name, record)
        if record_name == name:
            raise Crash
        return written

    monkeypatch.setattr(review, "write_record", write_record)


def test_a_critic_whose_record_was_not_saved_runs_again(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash_writing(monkeypatch, "records/review.round-1.json", after=False)
    with pytest.raises(Crash):
        runner()
    monkeypatch.undo()
    leave_dead_lock(runner.root)

    ran = runner()

    assert ran.executor.kinds() == ["critic", "overview:fix", "critic", "summary"]
    assert ran.code == 0


def test_a_critic_whose_record_was_saved_is_not_run_again(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash_writing(monkeypatch, "records/review.round-1.json", after=True)
    with pytest.raises(Crash):
        runner()
    monkeypatch.undo()
    leave_dead_lock(runner.root)

    ran = runner()

    # Round 1 uses the saved findings; the fake critic of round 2 asks again.
    assert ran.executor.kinds() == ["overview:fix", "critic", "overview:fix", "summary"]
    assert [s.round for s in ran.manifest.sessions] == [1, 2, 2, 0]


def test_a_summary_whose_record_was_not_saved_runs_again(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash_writing(monkeypatch, "records/summary.json", after=False)
    with pytest.raises(Crash):
        runner()
    monkeypatch.undo()
    leave_dead_lock(runner.root)

    ran = runner()

    assert ran.executor.kinds() == ["summary"]
    assert ran.code == 0


def test_a_summary_whose_record_was_saved_is_not_written_again(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    crash_writing(monkeypatch, "records/summary.json", after=True)
    with pytest.raises(Crash):
        runner()
    monkeypatch.undo()
    leave_dead_lock(runner.root)

    ran = runner()

    assert ran.executor.sessions == []
    assert ran.code == 0
    assert "## Executive summary" in (runner.root / "out" / "00-summary.md").read_text()
