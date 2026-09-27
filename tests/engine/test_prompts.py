"""The base prompts, and what each kind of session is told."""

from __future__ import annotations

import datetime as dt
import inspect
from pathlib import Path

from spadework.engine import prompts
from spadework.engine.prompts import (
    ENGINE_PROMPT_VERSION,
    base_prompts,
    draft_prompt,
    prompts_dir,
    repair_prompt,
    requirements,
    research_prompt,
    research_system_prompt,
    retry_prompt,
    writer_system_prompt,
)
from spadework.keys import hash_json
from spadework.project import load_project
from spadework.records import output_schema
from spadework.render import (
    DependencyFacts,
    ResearchFindings,
    Source,
    StagedFile,
)
from spadework.validators import ValidatorResult
from tests.conftest import MakeProject
from tests.engine.conftest import Runner

CANARIES = {
    "title": "CANARY-TITLE-7f3a",
    "reader": "CANARY-READER-91be",
    "priority": "CANARY-PRIORITY-2c4d",
    "extra": "CANARY-EXTRA-55e1",
    "nested": "CANARY-NESTED-e0f7",
    "body": "CANARY-BODY-0a9c",
    "input": "CANARY-INPUT-b7d2",
    "call": "CANARY-CALL-43aa",
}

SECRET_BRIEF = f"""\
---
title: "{CANARIES["title"]}"
reader: "{CANARIES["reader"]}"
public_context: "A stainless-steel fabricator planning a catalogue website."
priorities:
  - "{CANARIES["priority"]}"
currencies: [EUR]
budget_note: "{CANARIES["extra"]}"
contacts:
  owner: "{CANARIES["nested"]}"
---

# The client

{CANARIES["body"]}: the family that owns the business, and its margins.
"""


def test_the_version_is_a_hash_over_every_base_prompt() -> None:
    files = {path.stem: path.read_text() for path in prompts_dir().glob("*.md")}

    assert set(base_prompts()) == {
        "critic",
        "draft",
        "fix",
        "repair",
        "research",
        "rules",
        "summary",
    }
    assert hash_json(files) == ENGINE_PROMPT_VERSION
    assert ENGINE_PROMPT_VERSION.startswith("sha256:")


def test_the_base_prompts_carry_the_truth_rules() -> None:
    rules = base_prompts()["rules"]

    for phrase in ("[S1]", "[unverified]", "inference", "Never invent", "preamble"):
        assert phrase in rules
    assert "level-1 heading" in rules
    assert "Prefer tables" in rules
    assert "reader named below" in rules


def test_research_sessions_see_public_context_and_questions_only(
    make_project: MakeProject, research_tmp: Path
) -> None:
    # I5: nothing from the brief but public_context, and nothing from
    # inputs/, reaches a session with web access.
    root = make_project(
        brief=SECRET_BRIEF,
        inputs={
            "calls/2026-09-30-call.md": f"# {CANARIES['call']}\n",
            "notes.txt": CANARIES["input"],
        },
    )
    appendix = root / "playbooks" / "valid" / "prompts" / "research.md"
    appendix.write_text("Prefer the vendors' own pages.\n")

    ran = Runner(root, research_tmp)()

    [research] = ran.executor.of("landscape", "research")
    seen = research.spec.prompt + research.spec.system_append
    for name, canary in CANARIES.items():
        assert canary not in seen, name
    assert research.files == {}
    assert "A stainless-steel fabricator planning a catalogue website." in seen
    assert '- five comparable companies for "Landscape", in en' in seen
    assert research.spec.system_append.endswith("Prefer the vendors' own pages.")
    # Not the house rules, whose appendix and reader block name the reader.
    assert "# House rules" not in seen
    assert "Prefer tables to long prose." not in seen
    assert "## The reader" not in seen
    assert str(root) not in seen

    # The same brief does reach the draft session, so the check above can fail.
    [draft] = ran.executor.of("landscape", "draft")
    staged = "".join(draft.files.values()) + draft.spec.system_append
    for name in ("title", "reader", "priority", "body", "call", "input"):
        assert CANARIES[name] in staged, name


def test_the_research_builders_take_no_brief() -> None:
    assert list(inspect.signature(research_prompt).parameters) == [
        "public_context",
        "questions",
        "max_searches",
        "max_fetches",
        "today",
    ]
    assert list(inspect.signature(research_system_prompt).parameters) == ["playbook"]


def test_research_prompt() -> None:
    text = research_prompt(
        public_context="  A bakery in Lisbon.\n",
        questions="- five bakeries\n",
        max_searches=4,
        max_fetches=1,
        today=dt.date(2026, 9, 27),
    )

    assert text == (
        "## The project, as publicly described\n\n"
        "A bakery in Lisbon.\n\n"
        "## Research questions\n\n"
        "- five bakeries\n\n"
        "## Limits\n\n"
        "You can make up to 4 web searches and 1 page fetch in this session. "
        "Today is 2026-09-27.\n"
    )
    empty = research_prompt(
        public_context="",
        questions="q",
        max_searches=0,
        max_fetches=3,
        today=dt.date(2026, 9, 27),
    )
    assert "(No public description was given.)" in empty
    assert "up to 3 page fetches in this session" in empty


def test_writer_system_prompt(make_project: MakeProject) -> None:
    project = load_project(make_project())
    brief = project.brief

    draft = writer_system_prompt("draft", project.playbook, brief)
    repair = writer_system_prompt("repair", project.playbook, brief)

    assert draft.startswith(base_prompts()["rules"].rstrip("\n"))
    assert "\n\nPrefer tables to long prose.\n\n## The reader\n\n" in draft
    assert "Write the pack in the language with the tag `en`." in draft
    assert "Give every amount" not in draft  # the fixture brief has no currencies
    assert draft.endswith(base_prompts()["draft"].rstrip("\n"))
    assert repair.endswith(base_prompts()["repair"].rstrip("\n"))


def test_the_review_base_prompts_carry_their_contracts() -> None:
    critic = base_prompts()["critic"]
    fix = base_prompts()["fix"]
    summary = base_prompts()["summary"]

    # The critic's generic rubric (docs/spec/03-playbooks.md).
    for kind in ("fabrication", "contradiction", "filler", "length"):
        assert f"(`{kind}`)" in critic
    for name in ("Specificity", "Priorities", "Process narration"):
        assert f"**{name}**" in critic
    assert "Never ask for facts the pack does not support" in critic
    assert "Code checks each document's validators" in critic
    # A fixer may reject a finding if it says why, and never invents facts.
    assert "You may reject a finding" in fix
    assert "Never invent names, numbers, prices, dates, quotes or URLs" in fix
    assert "Never write a rejection, or anything about the review, into the" in fix
    # The summary's structure.
    assert "`## Executive summary`: at most 12 bullets" in summary
    assert "`## Key decisions`: the decisions the pack recommends, each with its\n" in (
        summary
    )
    assert "`## Open questions`: at most 10 questions" in summary
    assert "Code appends more sections after yours" in summary
    # No writer adds a Sources section of its own: code renders it.
    assert "Do not add a Sources section" in base_prompts()["rules"]


def test_playbook_appendices_reach_the_critic_and_the_summary(
    make_project: MakeProject,
) -> None:
    from spadework.playbook import builtin_root

    root = make_project()
    (root / "spadework.toml").write_text(
        (root / "spadework.toml")
        .read_text()
        .replace('"./playbooks/valid"', '"discovery-website"')
    )
    project = load_project(root)
    assert project.playbook.ref.root == builtin_root() / "discovery-website"

    critic = writer_system_prompt("critic", project.playbook, project.brief)
    summary = writer_system_prompt("summary", project.playbook, project.brief)
    fix = writer_system_prompt("fix", project.playbook, project.brief)

    assert critic.endswith(project.playbook.prompts["critic"].rstrip("\n"))
    assert base_prompts()["critic"].rstrip("\n") + "\n\n## Extra tests" in critic
    assert summary.endswith(project.playbook.prompts["summary"].rstrip("\n"))
    assert fix.endswith(base_prompts()["fix"].rstrip("\n"))
    assert "## House style for a website discovery pack" in fix


def test_requirements_follow_the_validators(make_project: MakeProject) -> None:
    project = load_project(make_project())
    landscape = project.playbook.task("landscape")
    plan = project.playbook.task("plan")

    assert requirements(landscape.validators, has_research=True) == [
        "- The first line is a level-1 heading (`# Title`).",
        "- At most 120 lines.",
        "- Every source id you cite is one of this document's sources, listed below.",
        "- At least 2 distinct sources cited in the text.",
        "- At most 6 open questions in your result.",
        '- None of these phrases: "lorem ipsum", "as an AI", "in this document '
        'we will", "this session", "session 2".',
    ]
    assert requirements(plan.validators, has_research=False)[0] == "- At most 80 lines."


def test_draft_and_repair_prompts(make_project: MakeProject) -> None:
    project = load_project(make_project())
    landscape = project.playbook.task("landscape")
    overview = project.playbook.task("overview")
    findings = ResearchFindings(
        notes="Most offer a free tier [S1].",
        sources=(
            Source(
                "S1",
                "https://a.example/",
                "A | B",
                "A",
                "2026-09-27",
                "quote",
                "claim",
                "verified",
            ),
        ),
        unanswered=("Prices in EUR",),
    )

    text = draft_prompt(
        landscape,
        "The body.\n",
        files=[
            StagedFile("brief.md", "The client"),
            StagedFile("01-overview.md", "Overview"),
            StagedFile("research/notes.md", "Research notes"),
        ],
        deps=[DependencyFacts(overview, ("Fact one",))],
        research=findings,
        pinned={"overview"},
    )

    assert text.startswith("# Landscape\n\nThe body.\n\n## Checked in code")
    assert '- `brief.md`: the brief ("The client")' in text
    assert (
        "- `01-overview.md`: the Overview document, edited by a person: use it as "
        'it stands ("Overview")'
    ) in text
    # A pinned dependency's key facts describe the engine's version, not the
    # person's: they are left out, and its file is read instead.
    assert "Fact one" not in text
    assert "## Key facts" not in text
    unpinned = draft_prompt(
        landscape,
        "The body.\n",
        files=[],
        deps=[DependencyFacts(overview, ("Fact one",))],
        research=findings,
    )
    assert "### 01-overview.md\n\n- Fact one" in unpinned
    assert "| S1 | A \\| B | A | 2026-09-27 | verified |" in text
    assert "### Not answered by the research\n\n- Prices in EUR" in text
    assert text.endswith(
        "Write the document to `02-landscape.md` in your working directory. It is "
        "the only file you can write. Then return your result.\n"
    )

    repair = repair_prompt(
        landscape,
        text,
        [ValidatorResult(name="max_lines", ok=False, message="Too long.")],
    )
    assert repair.startswith(text.rstrip("\n"))
    assert "- `max_lines`: Too long.\n" in repair
    assert retry_prompt("Do it.", "it did not write x.md").endswith(
        "it did not write x.md. Do the task again from the start, and return a "
        "result that matches the schema exactly.\n"
    )


def test_staged_research_files() -> None:
    findings = ResearchFindings(notes="Notes.", sources=(), unanswered=("Q",))

    assert prompts.notes_file(findings) == (
        "# Research notes\n\nNotes.\n\n## Not answered by the research\n\n- Q\n"
    )
    assert prompts.sources_file(findings) == (
        "# Sources\n\nThe research found no sources.\n"
    )


def test_citations_stay_local_to_their_document() -> None:
    rules = base_prompts()["rules"]
    draft = base_prompts()["draft"]

    assert "never copy that document's `[S#]` citations" in rules
    assert '"(see\n  01-landscape.md)"' in rules
    assert "cites no `[S#]` at all" in rules
    assert "refer to them by file name" in draft


def test_citations_are_checked_in_tasks_without_research(
    make_project: MakeProject,
) -> None:
    project = load_project(make_project())
    overview = project.playbook.task("overview")

    assert overview.validators.citations_resolve is True
    assert (
        "- No `[S#]` citations: this document has no sources of its own. Attribute "
        "a fact from another document by its file name."
    ) in requirements(overview.validators, has_research=False)


def test_the_quote_rule_is_in_the_prompt_and_the_schema() -> None:
    research = base_prompts()["research"]
    schema = output_schema("research")
    quote = schema["properties"]["sources"]["items"]["properties"]["quote"]

    assert "ask WebFetch to quote the passages you need\n  verbatim" in research
    assert "one\ncontiguous passage" in research.replace("  ", "")
    assert "at most 4 fragments of at\nleast 3 words each" in research.replace("  ", "")
    assert quote["description"] == (
        "Copied verbatim from the page, at most 300 characters; code checks it "
        "against the page. Give one contiguous passage if you can. Otherwise give "
        "at most 4 fragments of at least 3 words each, in page order, joined by …. "
        "Anything else fails the check."
    )
