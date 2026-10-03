"""The summary, and the pack rendered into out/: Sources sections, sources.md,
pack.json and 00-summary.md."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from spadework.engine import events as ev
from spadework.engine.documents import (
    Document,
    link,
    md,
    sources_section,
)
from spadework.engine.pack import write_file
from spadework.engine.planning import plan_project
from spadework.engine.store import ResearchRecord
from spadework.engine.verification import VerificationResult
from spadework.errors import ExitCode
from spadework.executors.base import SessionResult, SessionSpec
from spadework.executors.fake import draft_result, research_result
from spadework.keys import hash_file
from spadework.project import load_project
from spadework.records import ResearchResult
from tests.conftest import MakeProject
from tests.engine.conftest import Recording, Runner, task_file, write_playbook


def out(runner: Runner, name: str) -> str:
    return (runner.root / "out" / name).read_text()


def pack(runner: Runner) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(out(runner, "pack.json"))
    return data


# The summary session.


def test_the_summary_sees_the_pack_and_writes_only_its_file(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project(inputs={"notes.md": "# Notes\n"})

    ran = Runner(root, research_tmp)()

    [summary] = ran.executor.of(None, "summary")
    assert sorted(summary.files) == [
        "01-overview.md",
        "02-landscape.md",
        "03-plan.md",
        "brief.md",
        "inputs/notes.md",
        "records/digest.md",
    ]
    spec = summary.spec
    assert spec.tools == ("Read", "Glob", "Grep", "Write")
    assert spec.output is not None
    assert spec.output.name == "00-summary.md"
    assert spec.model == "opus"
    assert "# Writing the summary" in spec.system_append
    prompt = spec.prompt
    assert prompt.startswith("# Summary: Acme Fabrication — test pack\n")
    assert "| `03-plan.md` | Plan | fresh | pass |" in prompt
    assert (
        "- `overview:Q1` (01-overview.md): Is a placeholder overview enough to "
        "start with?" in prompt
    )
    assert "Required headings: Executive summary, Key decisions, Open questions" in (
        prompt
    )
    entry = next(s for s in ran.manifest.sessions if s.kind == "summary")
    assert (entry.task, entry.key, entry.round) == (None, None, 0)
    record = json.loads((root / ".spadework" / "records" / "summary.json").read_text())
    assert record["error"] is None
    assert [q["from"] for q in record["result"]["open_questions"]] == [
        ["overview:Q1"],
        ["landscape:Q1"],
        ["plan:Q1"],
    ]


def test_00_summary_has_the_prose_then_what_code_writes(runner: Runner) -> None:
    ran = runner()

    text = out(runner, "00-summary.md")
    headings = [line for line in text.splitlines() if line.startswith("#")]
    assert headings == [
        "# Summary",
        "## Executive summary",
        "## Key decisions",
        "## Open questions",
        "## Documents",
        "## Source verification",
        "## How this pack was produced",
    ]
    assert "| [01-overview.md](01-overview.md): Overview | fresh | pass | — |" in text
    assert "| 02-landscape.md | 5 | 0 | 0 | 5 | 0 | 0 |" in text
    today = dt.datetime.now(dt.UTC).date().isoformat()
    cost = ran.manifest.totals.cost_usd
    assert f"- Date: {today} (UTC)" in text
    assert "playbook fixture 1.0.0, executor fake test" in text
    assert f"- Cost: ${cost:.2f} in total over 1 run, ${cost:.2f} of it" in text
    assert "This pack is automated research. A person must review it" in text
    assert "Possible prompt injection" not in text
    # The tests' executor is the fake: its costs are simulated.
    assert (
        "of it in the run that wrote this summary, all simulated by the fake "
        "executor: no model was called and nothing was spent" in text
    )


def test_real_costs_are_not_called_simulated() -> None:
    from spadework.engine.documents import produced_section
    from spadework.engine.store import Produced

    produced = Produced(
        date="2026-10-03",
        spadework="0.1.0",
        playbook="quickstart@0.1.0",
        executor="claude-cli@2.1.278",
        cost_usd=0.42,
        runs=1,
        run_cost_usd=0.42,
    )

    text = produced_section(produced)

    assert "- Cost: $0.42 in total over 1 run, $0.42 of it in the run that " in text
    assert "simulated" not in text
    assert "simulated by the fake executor" in produced_section(
        produced.model_copy(update={"simulated": True})
    )


def test_the_cost_counts_every_run(runner: Runner) -> None:
    first = runner()
    (runner.root / "inputs" / "note.md").write_text("# Note\n")

    second = runner()

    total = first.manifest.totals.cost_usd + second.manifest.totals.cost_usd
    run = second.manifest.totals.cost_usd
    assert (
        f"- Cost: ${total:.2f} in total over 2 runs, ${run:.2f} of it in the run "
        "that wrote this summary" in out(runner, "00-summary.md")
    )


def test_a_failed_summary_is_written_again_by_the_next_run(runner: Runner) -> None:
    ran = runner({"summary": {"fail": "invalid_schema"}})

    assert ran.code == ExitCode.PARTIAL
    assert ran.result.outcome == "partial"
    assert ran.finished.message is not None
    assert ran.finished.message.startswith("the summary failed: ")
    text = out(runner, "00-summary.md")
    assert text.startswith("# Acme Fabrication — test pack\n\nThe summary of this")
    assert "The summary session did not finish (its result does not match" in text
    assert "## Documents" in text
    assert "## How this pack was produced" in text
    pack_data = pack(runner)
    assert pack_data["consolidated_questions"] == []

    again = runner()

    assert again.executor.kinds() == ["summary"]
    assert again.code == 0
    assert "## Executive summary" in out(runner, "00-summary.md")
    assert runner().executor.sessions == []  # now up to date


def test_a_pack_without_a_summary_gets_one(runner: Runner) -> None:
    runner()
    (runner.root / ".spadework" / "records" / "summary.json").unlink()

    ran = runner()

    assert ran.executor.kinds() == ["summary"]
    assert ran.code == 0


def test_questions_the_summary_invents_are_left_out_of_pack_json(
    runner: Runner,
) -> None:
    result = {
        "status": "ok",
        "open_questions": [
            {
                "question": "Is Portuguese needed at launch?",
                "why": "It changes the build.",
                "from": ["overview:Q1", "stack:Q7"],
            }
        ],
    }

    runner({"summary": {"structured": result}})

    assert pack(runner)["consolidated_questions"] == [
        {
            "question": "Is Portuguese needed at launch?",
            "why": "It changes the build.",
            "from": ["overview:Q1"],
        }
    ]


def test_research_that_met_blocked_calls_is_flagged_in_the_summary(
    runner: Runner,
) -> None:
    runner({"landscape:research": {"fail": "guard_denial", "denials": 2}})

    text = out(runner, "00-summary.md")
    assert (
        "**Possible prompt injection encountered** in the research for "
        "02-landscape.md" in text
    )
    record = json.loads(
        (runner.root / ".spadework" / "records" / "landscape.research.json").read_text()
    )
    assert record["guard_denials"] == 2


# Each document's Sources section.


def test_each_document_lists_the_sources_it_cites(runner: Runner) -> None:
    runner()

    landscape = out(runner, "02-landscape.md")
    section = landscape[landscape.index("\n## Sources\n") :]
    assert (
        "| S1 | [Example Directory: landscape](<https://directory.example/landscape>)"
        ", Example Directory | " in section
    )
    # The fake cites a source in each of its three sections.
    assert section.count("| ? unreachable |") == 3
    assert "| S4 |" not in section
    assert "[sources.md](sources.md)" in section
    overview = out(runner, "01-overview.md")
    assert overview.endswith(
        "## Sources\n\nThis document has no web research of its own. Its facts "
        "come from the brief, the inputs and the other documents of the pack, "
        "which it names.\n"
    )


def document(
    tmp_path: Path, text: str, *, research: ResearchRecord | None, has_research: bool
) -> Document:
    project_task = type(
        "T",
        (),
        {
            "id": "stack",
            "title": "Stack",
            "output": "06-stack.md",
            "research": object(),
        },
    )
    if not has_research:
        project_task.research = None  # type: ignore[attr-defined]
    path = tmp_path / "06-stack.md"
    path.write_text(text)
    return Document(
        task=project_task,  # type: ignore[arg-type]
        state="fresh",
        entry=None,
        record=None,
        research=research,
        path=path,
        pinned=False,
    )


def research_record(**statuses: str) -> ResearchRecord:
    result = ResearchResult.model_validate(research_result("stack"))
    return ResearchRecord(
        task="stack",
        key="k",
        run_id=None,
        result=result,
        verification={
            source_id: VerificationResult(status=status)  # type: ignore[arg-type]
            for source_id, status in statuses.items()
        },
    )


def test_a_sources_section_lists_only_what_is_cited(tmp_path: Path) -> None:
    research = research_record(S1="verified", S3="quote_not_found")
    doc = document(tmp_path, "x", research=research, has_research=True)

    section = sources_section(doc, ["S3", "S1", "S9"])

    rows = [line for line in section.splitlines() if line.startswith("| S")]
    assert [row.split(" | ")[0] for row in rows] == ["| S1", "| S3"]
    assert rows[0].endswith("| ✓ verified |")
    assert rows[1].endswith("| ✗ quote not found |")
    assert "Cited but not among the research's sources: S9." in section


def test_a_document_that_cites_none_of_its_sources(tmp_path: Path) -> None:
    doc = document(tmp_path, "x", research=research_record(), has_research=True)

    assert "cites none of the 5 sources its research found" in sources_section(doc, [])


def test_a_missing_research_record_is_said_plainly(tmp_path: Path) -> None:
    doc = document(tmp_path, "x", research=None, has_research=True)

    assert "The record of this document's research is missing" in sources_section(
        doc, ["S1"]
    )


def test_text_from_the_web_cannot_add_markdown() -> None:
    assert md("Evil](javascript:alert(1)) <img src=x> | *x* `y`\nz") == (
        "Evil\\](javascript:alert(1)) \\<img src=x\\> \\| \\*x\\* \\`y\\` z"
    )
    assert link("A [b]", "https://x.example.com/a<b>c\\d") == (
        "[A \\[b\\]](<https://x.example.com/a%3Cb%3Ec%5Cd>)"
    )


# sources.md and pack.json.


def test_sources_md_lists_every_source_by_document(runner: Runner) -> None:
    runner()

    text = out(runner, "sources.md")
    assert text.startswith("# Sources: Acme Fabrication — test pack\n")
    assert "\n## 02-landscape.md: Landscape\n" in text
    assert text.count("\n### S") == 5
    assert "- <https://directory.example/landscape>" in text
    assert (
        "- Verification: ? unreachable (blocked: special-use domain), checked " in text
    )
    assert "- Cited in 02-landscape.md: yes" in text
    assert "- Quote: “Most of the listed providers offer a free tier" in text


def test_pack_json(runner: Runner) -> None:
    ran = runner()

    data = pack(runner)
    assert list(data) == [
        "schema_version",
        "project",
        "generated_at",
        "versions",
        "summary_pending",
        "documents",
        "decisions",
        "open_questions",
        "consolidated_questions",
        "risks",
        "assumptions",
        "gaps",
        "sources",
    ]
    assert data["schema_version"] == 1
    assert data["summary_pending"] is False
    assert data["project"] == "acme-fabrication"
    assert data["generated_at"] == ran.manifest.ended_at or data["generated_at"]
    assert data["versions"] == {
        "spadework": data["versions"]["spadework"],
        "playbook": "fixture@1.0.0",
        "executor": "fake@test",
    }
    assert data["documents"][0] == {
        "task": "overview",
        "title": "Overview",
        "file": "01-overview.md",
        "state": "fresh",
        "status": "ok",
        "validators_ok": True,
        "summary": "Placeholder overview from the fake executor.",
        "key_facts": ["Overview: placeholder content, no model was called"],
    }
    assert [d["id"] for d in data["decisions"]] == [
        "overview:D1",
        "landscape:D1",
        "plan:D1",
    ]
    assert data["decisions"][0]["reversibility"] == "easy"
    assert data["open_questions"][0]["id"] == "overview:Q1"
    assert data["open_questions"][0]["answer_format"] == "yes_no"
    assert data["risks"][0]["task"] == "overview"
    assert data["assumptions"][0] == {
        "task": "overview",
        "assumption": "No model was called for this document",
    }
    assert data["gaps"] == []
    assert len(data["consolidated_questions"]) == 3
    source = data["sources"][0]
    assert source["id"] == "landscape:S1"
    assert source["task"] == "landscape"
    assert source["cited"] is True
    assert source["verification"] == "unreachable"
    assert source["verification_detail"] == "blocked: special-use domain"
    assert source["final_url"] is None
    assert source["quote"].startswith("Most of the listed providers")


def test_pack_json_keeps_its_time_until_its_data_changes(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner()
    first = pack(runner)["generated_at"]
    (runner.root / "out" / "02-landscape.md").unlink()
    monkeypatch.setattr(
        "spadework.engine.run.utc_timestamp", lambda *args: "2030-01-01T00:00:00Z"
    )

    ran = runner()

    # Only the deleted document is written: pack.json's data is the same.
    assert ran.finished.rendered == ("out/02-landscape.md",)
    assert pack(runner)["generated_at"] == first
    (runner.root / "inputs" / "note.md").write_text("# Note\n")
    # No review: the fake's fix would restore the original record.
    runner(
        {"overview:draft": {"structured": draft_result("Other overview")}},
        review=False,
    )
    assert pack(runner)["generated_at"] == "2030-01-01T00:00:00Z"


def test_documents_not_produced_are_listed_with_their_state(
    runner: Runner,
) -> None:
    ran = runner({"landscape:research": {"fail": "timeout"}})

    assert ran.code == ExitCode.PARTIAL
    data = pack(runner)
    assert [(d["task"], d["state"], d["validators_ok"]) for d in data["documents"]] == [
        ("overview", "fresh", True),
        ("landscape", "failed", None),
        ("plan", "skipped", None),
    ]
    text = out(runner, "00-summary.md")
    assert "| 02-landscape.md: Landscape | failed, no document | — | — |" in text
    assert "| 03-plan.md: Plan | skipped, no document | — | — |" in text


# Files a person edited, deleted, or never had.


def test_an_edited_summary_is_kept(runner: Runner) -> None:
    runner()
    summary = runner.root / "out" / "00-summary.md"
    summary.write_text("# My summary\n")
    (runner.root / "inputs" / "note.md").write_text("# Note\n")

    ran = runner()

    assert summary.read_text() == "# My summary\n"
    assert "out/00-summary.md" not in ran.finished.rendered
    assert any(
        n.message == "out/00-summary.md was edited, so it was not updated"
        for n in ran.of(ev.Notice)
    )
    # Not a task: it pins nothing.
    states = {t.id: t.state for t in plan_project(load_project(runner.root)).plan.tasks}
    assert set(states.values()) == {"fresh"}


def test_a_deleted_pack_file_is_rendered_again_without_a_session(
    runner: Runner,
) -> None:
    runner()
    for name in ("00-summary.md", "sources.md", "pack.json"):
        (runner.root / "out" / name).unlink()

    ran = runner()

    assert ran.executor.sessions == []
    assert ran.finished.rendered == (
        "out/00-summary.md",
        "out/pack.json",
        "out/sources.md",
    )
    assert "## Executive summary" in out(runner, "00-summary.md")


def test_a_pack_file_the_engine_did_not_write_is_left_alone(runner: Runner) -> None:
    (runner.root / "out").mkdir()
    (runner.root / "out" / "sources.md").write_text("# Mine\n")

    ran = runner()

    assert out(runner, "sources.md") == "# Mine\n"
    assert any(
        n.message == "out/sources.md was not written by spadework, so it was left "
        "as it is"
        for n in ran.of(ev.Notice)
    )


def test_an_edited_document_keeps_its_sources_section(runner: Runner) -> None:
    runner()
    path = runner.root / "out" / "02-landscape.md"
    edited = path.read_text().replace("# Landscape", "# Landscape, edited")
    path.write_text(edited)
    (runner.root / "brief.md").write_text(
        (runner.root / "brief.md").read_text() + "\nMore.\n"
    )

    ran = runner()

    assert path.read_text() == edited
    assert "out/02-landscape.md" not in ran.finished.rendered
    [summary] = ran.executor.of(None, "summary")
    assert summary.files["02-landscape.md"] == edited
    text = out(runner, "00-summary.md")
    assert "| edited by a person | — (edited by a person) |" in text


def test_a_render_cut_short_leaves_nothing_pinned(
    runner: Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    written: list[str] = []

    def write_two_then_stop(path: Path, data: bytes) -> None:
        write_file(path, data)
        written.append(path.name)
        if len(written) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("spadework.engine.pack.write_file", write_two_then_stop)
    assert runner().code == ExitCode.INTERRUPTED
    monkeypatch.undo()
    state = json.loads((runner.root / ".spadework" / "state.json").read_text())
    assert len(state["rendering"]) == 6
    assert written == ["01-overview.md", "02-landscape.md"]

    ran = runner()

    assert ran.executor.sessions == []
    assert ran.code == 0
    after = ran.state
    assert after.rendering == {}
    for task_id, name in [("overview", "01-overview.md"), ("plan", "03-plan.md")]:
        entry = after.tasks[task_id]
        assert entry.rendered is not None
        assert entry.rendered.hash == hash_file(runner.root / "out" / name)
    assert after.files["sources.md"] == hash_file(runner.root / "out" / "sources.md")


def test_a_research_task_without_citations_says_so(
    make_project: MakeProject, research_tmp: Path
) -> None:
    root = make_project()
    write_playbook(
        root,
        {"alpha": task_file("01-alpha.md", extra="research:\n  questions: q")},
    )

    def no_citations(spec: SessionSpec, result: SessionResult) -> None:
        if spec.kind.value == "draft" and spec.output is not None:
            spec.output.write_text("# Alpha\n\nNothing cited.\n")

    Runner(root, research_tmp)(executor=Recording(after=no_citations), review=False)

    text = (root / "out" / "01-alpha.md").read_text()
    assert text == (
        "# Alpha\n\nNothing cited.\n\n## Sources\n\nThis document cites none of "
        "the 5 sources its research found. They are listed in "
        "[sources.md](sources.md).\n"
    )


def test_a_partial_draft_is_marked_but_keeps_its_state(runner: Runner) -> None:
    partial = {**draft_result("Plan"), "status": "partial", "gaps": ["No dates"]}

    ran = runner({"plan:draft": {"structured": partial}}, review=False)

    # The state comes from the validators; the session's own status is shown.
    assert ran.tasks()["plan"] == "fresh"
    assert ran.code == 0
    data = pack(runner)
    plan = next(d for d in data["documents"] if d["task"] == "plan")
    assert (plan["state"], plan["status"]) == ("fresh", "partial")
    text = out(runner, "00-summary.md")
    assert "| [03-plan.md](03-plan.md): Plan | fresh, partial | pass | 1 |" in text
    [summary] = ran.executor.of(None, "summary")
    assert (
        "| `03-plan.md` | Plan | fresh; partial: part of its task could not be done"
        in summary.spec.prompt
    )
