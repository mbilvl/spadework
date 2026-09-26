from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from spadework import __version__
from spadework.errors import UsageError
from spadework.executors import get_executor
from spadework.executors.base import (
    Denied,
    Finished,
    RateLimit,
    SessionKind,
    Text,
    ToolCall,
)
from spadework.executors.claude_cli import ClaudeCliExecutor
from spadework.executors.fake import (
    COSTS,
    DEFAULT_HEADINGS,
    FakeBehaviour,
    FakeExecutor,
    headings_in,
    load_script,
)
from spadework.records import (
    CriticResult,
    DraftResult,
    FixResult,
    ResearchResult,
    SummaryResult,
    validate_result,
)
from tests.executors.conftest import STREAM, MakeSpec, Recorder

# How the built-in playbooks ask for their sections.
TASK_BODY = """\
A quick first look at what already exists around this project.

Write these sections as `##` headings, in this order, with exactly these names:

- **Comparables**: a table with one row per comparable: what it is, who it
  serves, how it presents or prices itself.
- **What they show**: three to five bullets.
- **Gaps**: what the research could not find.

Keep the document under 90 lines.

Sources: S1 Example Directory, S2 Sample Benchmarks, S3 Placeholder Journal.
"""


def play(
    executor: FakeExecutor, spec: Any, recorder: Recorder | None = None
) -> tuple[Any, Recorder]:
    recorder = recorder or Recorder()
    return executor.run(spec, recorder), recorder


def test_a_draft(make_spec: MakeSpec) -> None:
    spec = make_spec(SessionKind.DRAFT, prompt=TASK_BODY)
    (spec.workdir / "brief.md").write_text("# Brief\n", encoding="utf-8")
    outcome, recorder = play(FakeExecutor(), spec)

    assert outcome.status == "ok"
    assert isinstance(validate_result("draft", outcome.structured), DraftResult)
    assert outcome.cost_usd == COSTS[SessionKind.DRAFT]
    assert outcome.cost_known is True
    assert outcome.tool_calls == {"Read": 1, "Write": 1}
    assert spec.output.read_text(encoding="utf-8") == (
        "# Tech stack\n"
        "\n"
        "Placeholder text from spadework's fake executor: no model was called.\n"
        "\n"
        "## Comparables\n\nPlaceholder content for Comparables. [S1]\n"
        "\n"
        "## What they show\n\nPlaceholder content for What they show. [S2]\n"
        "\n"
        "## Gaps\n\nPlaceholder content for Gaps. [S3]\n"
    )
    assert recorder.events[:2] == [
        ToolCall("Read", "brief.md"),
        ToolCall("Write", "06-tech-stack.md"),
    ]
    assert recorder.events[-1] == Finished(outcome)
    logged = [json.loads(line) for line in spec.log_path.read_text().splitlines()]
    assert [record["type"] for record in logged] == [
        "ToolCall",
        "ToolCall",
        "Text",
        "result",
    ]
    assert sorted(path.name for path in spec.workdir.iterdir()) == [
        "06-tech-stack.md",
        "brief.md",
    ]


def test_is_deterministic(make_spec: MakeSpec) -> None:
    spec = make_spec(SessionKind.DRAFT, prompt=TASK_BODY)
    first, _ = play(FakeExecutor(), spec)
    text = spec.output.read_text(encoding="utf-8")
    spec.output.unlink()
    second, _ = play(FakeExecutor(), spec)

    assert first == second
    assert spec.output.read_text(encoding="utf-8") == text


def test_a_repair(make_spec: MakeSpec) -> None:
    outcome, _ = play(FakeExecutor(), make_spec(SessionKind.REPAIR))

    assert isinstance(validate_result("repair", outcome.structured), DraftResult)
    assert outcome.cost_usd == COSTS[SessionKind.REPAIR]


def test_research(make_spec: MakeSpec) -> None:
    spec = make_spec(SessionKind.RESEARCH, task="landscape")
    outcome, recorder = play(FakeExecutor(), spec)

    record = validate_result("research", outcome.structured)
    assert isinstance(record, ResearchResult)
    assert [source.url for source in record.sources] == [
        "https://directory.example/landscape",
        "https://benchmarks.example/landscape",
        "https://journal.example/landscape",
    ]
    assert record.sources[0].accessed == dt.datetime.now(dt.UTC).date().isoformat()
    assert outcome.tool_calls == {"WebSearch": 1, "WebFetch": 3}
    assert recorder.of(ToolCall)[0] == ToolCall("WebSearch", "placeholder search")
    assert list(spec.workdir.iterdir()) == []  # research writes nothing


def test_critic_asks_for_one_fix_then_ships(make_spec: MakeSpec) -> None:
    spec = make_spec(SessionKind.CRITIC, prompt="Review 02-questions.md too.")
    (spec.workdir / "brief.md").write_text("# Brief\n", encoding="utf-8")
    (spec.workdir / "01-landscape.md").write_text("# L\n", encoding="utf-8")
    (spec.workdir / "00-summary.md").write_text("# S\n", encoding="utf-8")
    executor = FakeExecutor()

    first, _ = play(executor, spec)
    second, _ = play(executor, spec)

    critic = validate_result("critic", first.structured, max_findings=1)
    assert isinstance(critic, CriticResult)
    assert critic.verdict == "continue"
    assert [(f.id, f.target) for f in critic.findings] == [("F1", "01-landscape.md")]
    assert second.structured == {"verdict": "ship", "findings": [], "biggest_gap": ""}
    assert sorted(path.name for path in spec.workdir.iterdir()) == [
        "00-summary.md",
        "01-landscape.md",
        "brief.md",
    ]


def test_critic_ships_when_there_is_nothing_to_review(make_spec: MakeSpec) -> None:
    outcome, _ = play(FakeExecutor(), make_spec(SessionKind.CRITIC))

    assert outcome.structured["verdict"] == "ship"


def test_fix_applies_the_findings_it_was_given(make_spec: MakeSpec) -> None:
    spec = make_spec(SessionKind.FIX, prompt="Findings: F2 and F1.")
    spec.output.write_text("# Tech stack\n\nStaged.\n", encoding="utf-8")
    outcome, _ = play(FakeExecutor(), spec)

    fix = validate_result("fix", outcome.structured)
    assert isinstance(fix, FixResult)
    assert (fix.applied, fix.rejected) == (["F1", "F2"], [])
    assert spec.output.read_text(encoding="utf-8") == (
        "# Tech stack\n\nStaged.\n\nRevised after review (F1, F2).\n"
    )


def test_fix_writes_a_document_when_none_was_staged(make_spec: MakeSpec) -> None:
    spec = make_spec(SessionKind.FIX)
    play(FakeExecutor(), spec)

    assert spec.output.read_text(encoding="utf-8").startswith("# Tech stack\n")


def test_summary(make_spec: MakeSpec) -> None:
    workdir = make_spec(SessionKind.DRAFT).workdir
    spec = make_spec(
        SessionKind.SUMMARY,
        output=workdir / "00-summary.md",
        prompt="Questions: stack:Q1, audience:Q2, stack:Q1 and scope:Q3.",
        task=None,
    )
    outcome, _ = play(FakeExecutor(), spec)

    summary = validate_result("summary", outcome.structured)
    assert isinstance(summary, SummaryResult)
    assert [question.from_ for question in summary.open_questions] == [
        ["stack:Q1"],
        ["audience:Q2"],
        ["scope:Q3"],
    ]
    text = spec.output.read_text(encoding="utf-8")
    assert text.startswith("# Summary\n")
    assert "## Executive summary" in text
    assert "[S" not in text


def test_headings_in() -> None:
    assert headings_in(TASK_BODY) == ["Comparables", "What they show", "Gaps"]
    assert headings_in("Required headings: Phases, Calendar, Risks.") == [
        "Phases",
        "Calendar",
        "Risks",
    ]
    assert headings_in("No sections here.") == list(DEFAULT_HEADINGS)
    assert headings_in("headings:\n\n- plain bullet\n") == list(DEFAULT_HEADINGS)


# --- scripted failures ------------------------------------------------------


def scripted(**behaviour: Any) -> FakeExecutor:
    return FakeExecutor({"*": FakeBehaviour.model_validate(behaviour)})


@pytest.mark.parametrize(
    ("fail", "status", "error_kind"),
    [
        ("timeout", "timeout", None),
        ("interrupted", "interrupted", None),
        ("transient", "error", "transient"),
        ("error", "error", "executor"),
        ("sandbox", "error", "sandbox"),
        ("no_result", "error", "schema"),
    ],
)
def test_failures_that_end_without_a_result(
    make_spec: MakeSpec, fail: str, status: str, error_kind: str | None
) -> None:
    spec = make_spec(SessionKind.DRAFT)
    outcome, recorder = play(scripted(fail=fail), spec)

    assert (outcome.status, outcome.error_kind) == (status, error_kind)
    assert outcome.structured is None
    assert outcome.error
    assert recorder.events[-1] == Finished(outcome)
    if fail in ("timeout", "interrupted"):
        assert outcome.cost_known is False


def test_a_rate_limit(make_spec: MakeSpec) -> None:
    reset = dt.datetime(2026, 9, 27, 23, 0, tzinfo=dt.UTC)
    by_time, recorder = play(scripted(fail="rate_limit", resets_at=reset), make_spec())
    by_epoch, _ = play(
        scripted(fail="rate_limit", resets_at=int(reset.timestamp())), make_spec()
    )
    naive, _ = play(
        scripted(fail="rate_limit", resets_at=reset.replace(tzinfo=None)), make_spec()
    )
    default, _ = play(scripted(fail="rate_limit"), make_spec())

    assert by_time.status == "rate_limited"
    assert by_time.rate_limit_reset == by_epoch.rate_limit_reset == reset
    assert naive.rate_limit_reset == reset
    assert recorder.of(RateLimit) == [RateLimit("rejected", reset)]
    assert default.rate_limit_reset > dt.datetime.now(dt.UTC)


def test_budget(make_spec: MakeSpec) -> None:
    forced, _ = play(scripted(fail="budget"), make_spec(max_budget_usd=0.5))
    too_dear, _ = play(scripted(cost_usd=0.9), make_spec(max_budget_usd=0.5))
    cheap, _ = play(scripted(cost_usd=0.4), make_spec(max_budget_usd=0.5))

    assert (forced.status, forced.cost_usd) == ("budget", 0.5)
    assert (too_dear.status, too_dear.cost_usd) == ("budget", 0.9)
    assert (cheap.status, cheap.cost_usd) == ("ok", 0.4)


def test_an_invalid_structured_result(make_spec: MakeSpec) -> None:
    outcome, _ = play(scripted(fail="invalid_schema"), make_spec())

    assert outcome.status == "ok"
    with pytest.raises(ValidationError):
        validate_result("draft", outcome.structured)


def test_a_given_structured_result(make_spec: MakeSpec) -> None:
    outcome, _ = play(scripted(structured={"status": "odd"}), make_spec())

    assert outcome.structured == {"status": "odd"}


def test_a_missing_output(make_spec: MakeSpec) -> None:
    spec = make_spec(SessionKind.FIX)
    spec.output.write_text("# Staged\n", encoding="utf-8")
    outcome, _ = play(scripted(fail="missing_output"), spec)

    assert outcome.status == "ok"
    assert not spec.output.exists()


def test_an_extra_file(make_spec: MakeSpec) -> None:
    spec = make_spec()
    outcome, _ = play(scripted(fail="extra_file"), spec)

    assert outcome.status == "ok"
    assert sorted(path.name for path in spec.workdir.iterdir()) == [
        "06-tech-stack.md",
        "notes.md",
    ]


@pytest.mark.parametrize(
    ("kind", "name", "target"),
    [
        (SessionKind.DRAFT, "Write", "notes.md"),
        (SessionKind.RESEARCH, "WebFetch", "http://127.0.0.1/"),
    ],
)
def test_guard_denials(
    make_spec: MakeSpec, kind: SessionKind, name: str, target: str
) -> None:
    spec = make_spec(kind)
    outcome, recorder = play(scripted(fail="guard_denial", denials=2), spec)

    assert outcome.status == "ok"
    assert outcome.guard_denials == 2
    assert (
        recorder.of(Denied)
        == [Denied(name, target, "spadework guard: denied (fake)")] * 2
    )
    assert not (spec.workdir / "notes.md").exists()


# --- scripts ----------------------------------------------------------------


def test_the_most_specific_entry_wins_and_times_run_out(make_spec: MakeSpec) -> None:
    executor = FakeExecutor(
        {
            "stack:draft": FakeBehaviour(fail="timeout", times=1),
            "stack": FakeBehaviour(fail="transient", times=1),
            "draft": FakeBehaviour(cost_usd=0.01),
            "*": FakeBehaviour(fail="error"),
        }
    )
    draft = make_spec(SessionKind.DRAFT)
    statuses = [play(executor, draft)[0] for _ in range(4)]

    assert [(o.status, o.error_kind) for o in statuses] == [
        ("timeout", None),
        ("error", "transient"),
        ("ok", None),
        ("ok", None),
    ]
    assert statuses[3].cost_usd == 0.01
    assert play(executor, make_spec(SessionKind.CRITIC, task=None))[0].error_kind == (
        "executor"
    )


def test_the_critic_verdict_can_be_scripted(make_spec: MakeSpec) -> None:
    spec = make_spec(SessionKind.CRITIC)
    (spec.workdir / "01-landscape.md").write_text("# L\n", encoding="utf-8")
    shipped, _ = play(FakeExecutor({"critic": FakeBehaviour(verdict="ship")}), spec)

    assert shipped.structured["verdict"] == "ship"


def test_load_a_toml_script(tmp_path: Path) -> None:
    path = tmp_path / "script.toml"
    path.write_text(
        '["stack:draft"]\nfail = "rate_limit"\nresets_at = 2026-09-27T23:00:00Z\n'
        'times = 2\n\n[research]\nreplay = "logs/research.jsonl"\n',
        encoding="utf-8",
    )
    script = load_script(path)

    assert script["stack:draft"].fail == "rate_limit"
    assert script["stack:draft"].resets_at == dt.datetime(
        2026, 9, 27, 23, 0, tzinfo=dt.UTC
    )
    assert script["research"].replay == str(tmp_path / "logs" / "research.jsonl")


def test_load_a_json_script(tmp_path: Path) -> None:
    path = tmp_path / "script.json"
    path.write_text(
        json.dumps({"critic": {"fail": "guard_denial", "denials": 3, "cost_usd": 1}}),
        encoding="utf-8",
    )

    assert load_script(path)["critic"] == FakeBehaviour(
        fail="guard_denial", denials=3, cost_usd=1.0
    )


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ('{"draft": {"fail": "explode"}}', "is not a valid fake executor script"),
        ('{"draft": {"typo": 1}}', "is not a valid fake executor script"),
        ('{"draft": {"times": 0}}', "is not a valid fake executor script"),
        ("not json", "is not a valid fake executor script"),
        ('["draft"]', "is not a valid fake executor script"),
    ],
)
def test_bad_scripts(tmp_path: Path, text: str, error: str) -> None:
    path = tmp_path / "script.json"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(UsageError, match=error) as caught:
        load_script(path)
    assert caught.value.hint == "Fix the script, or unset SPADEWORK_FAKE_SCRIPT."


def test_a_missing_script(tmp_path: Path) -> None:
    with pytest.raises(UsageError, match="cannot read the fake executor script"):
        load_script(tmp_path / "missing.json")


def test_from_env(tmp_path: Path, make_spec: MakeSpec) -> None:
    path = tmp_path / "script.json"
    path.write_text('{"draft": {"fail": "timeout"}}', encoding="utf-8")

    scripted_executor = FakeExecutor.from_env({"SPADEWORK_FAKE_SCRIPT": str(path)})
    plain = FakeExecutor.from_env({})

    assert play(scripted_executor, make_spec())[0].status == "timeout"
    assert plain.script == {}


# --- replaying a recorded log -----------------------------------------------


def test_replay_through_the_claude_cli_parser(make_spec: MakeSpec) -> None:
    replaying = FakeExecutor(
        {
            "research": FakeBehaviour(
                replay=str(STREAM / "research-ok.mock.jsonl"), guard_checks=False
            )
        }
    )
    spec = make_spec(SessionKind.RESEARCH)
    outcome, recorder = play(replaying, spec)

    assert outcome.status == "ok"
    assert outcome.structured == {"notes": "nothing found", "sources": []}
    assert outcome.cost_usd == 0.0004
    assert recorder.of(ToolCall)[0] == ToolCall(
        "WebSearch", "static site generators comparison"
    )
    assert recorder.events[-1] == Finished(outcome)
    assert spec.log_path.read_text(encoding="utf-8") == (
        STREAM / "research-ok.mock.jsonl"
    ).read_text(encoding="utf-8")


def test_replay_keeps_the_guard_checks(make_spec: MakeSpec) -> None:
    replaying = FakeExecutor(
        {
            "critic": FakeBehaviour(
                replay=str(STREAM / "invalid-settings-no-guard.mock.jsonl")
            )
        }
    )
    critic = make_spec(SessionKind.CRITIC, expected_tools=("Read",))
    outcome, _ = play(replaying, critic)

    assert (outcome.status, outcome.error) == (
        "error",
        "the sandbox guard did not start",
    )


# --- identity ---------------------------------------------------------------


def test_identity() -> None:
    executor = FakeExecutor()

    assert executor.name == "fake"
    assert executor.version() == __version__
    [check] = executor.diagnose()
    assert check.ok


def test_get_executor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("SPADEWORK_FAKE_SCRIPT", raising=False)

    assert isinstance(get_executor("fake"), FakeExecutor)
    claude = get_executor("claude-cli", claude_bin="/opt/claude")
    assert isinstance(claude, ClaudeCliExecutor)
    assert claude.name == "claude-cli"
    with pytest.raises(UsageError, match="unknown executor 'openai'") as caught:
        get_executor("openai")
    assert caught.value.hint == "Use one of: claude-cli, fake."


def test_get_executor_reads_the_fake_script(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "script.json"
    path.write_text('{"*": {"fail": "error"}}', encoding="utf-8")
    monkeypatch.setenv("SPADEWORK_FAKE_SCRIPT", str(path))
    executor = get_executor("fake")

    assert isinstance(executor, FakeExecutor)
    assert executor.script["*"].fail == "error"


def test_text_events(make_spec: MakeSpec) -> None:
    _, recorder = play(FakeExecutor(), make_spec())

    assert recorder.of(Text) == [Text("Finished the draft session (fake executor).")]
