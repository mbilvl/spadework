import dataclasses
import json
from typing import Any

import pytest

from spadework.playbook import ENGINE_FORBIDDEN_PHRASES, Validators
from spadework.project import load_project
from spadework.records import DraftResult, Source
from spadework.validators import (
    ValidatorResult,
    can_repair,
    cited_sources,
    failures,
    run_validators,
)
from tests.conftest import MakeProject

OFF = Validators(
    max_lines=None,
    min_lines=None,
    starts_with_h1=False,
    required_headings=(),
    forbidden_phrases=(),
    max_open_questions=None,
    citations_resolve=False,
    min_sources=None,
)


def _record(open_questions: int = 0) -> DraftResult:
    return DraftResult.model_validate(
        {
            "status": "ok",
            "summary": "A summary.",
            "key_facts": [],
            "decisions": [],
            "open_questions": [
                {
                    "id": f"Q{n}",
                    "question": f"Question {n}?",
                    "why": "It matters.",
                    "answer_format": "text",
                }
                for n in range(1, open_questions + 1)
            ],
            "assumptions": [],
            "risks": [],
            "gaps": [],
        }
    )


def _sources(count: int) -> list[Source]:
    return [
        Source.model_validate(
            {
                "id": f"S{n}",
                "url": f"https://example.com/{n}",
                "title": f"Page {n}",
                "publisher": "Example",
                "accessed": "2026-09-27",
                "quote": "A quote.",
                "claim": "A claim.",
            }
        )
        for n in range(1, count + 1)
    ]


def check(
    text: str,
    *,
    record: DraftResult | None = None,
    sources: int = 0,
    **settings: Any,
) -> dict[str, ValidatorResult]:
    validators = dataclasses.replace(OFF, **settings)
    results = run_validators(
        text, validators, result=record or _record(), sources=_sources(sources)
    )
    return {result.name: result for result in results}


def only(text: str, name: str, **kwargs: Any) -> ValidatorResult:
    return check(text, **kwargs)[name]


# Line counts.


def test_max_lines() -> None:
    assert only("a\nb\nc\n", "max_lines", max_lines=3).ok
    result = only("a\nb\nc\nd\n", "max_lines", max_lines=3)
    assert not result.ok
    assert result.message is not None
    assert "4 lines; the limit is 3" in result.message
    assert "Cut at least 1 line:" in result.message


def test_min_lines() -> None:
    assert only("a\nb\n", "min_lines", min_lines=2).ok
    result = only("a\n", "min_lines", min_lines=40)
    assert not result.ok
    assert result.message is not None
    assert "1 line; it needs at least 40" in result.message


def test_a_trailing_newline_is_not_a_line() -> None:
    assert only("a\nb", "max_lines", max_lines=2).ok
    assert only("a\nb\n", "max_lines", max_lines=2).ok
    assert not only("a\nb\n\n", "max_lines", max_lines=2).ok


# starts_with_h1.


@pytest.mark.parametrize(
    "text",
    ["# Tech stack\n\nBody", "\n\n  # Tech stack\n", "﻿# Title\n", "# Title #\n"],
)
def test_starts_with_h1_passes(text: str) -> None:
    assert only(text, "starts_with_h1", starts_with_h1=True).ok


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        ("## Tech stack\n", "'## Tech stack'"),
        ("Intro line\n# Title\n", "'Intro line'"),
        ("#Title\n", "'#Title'"),
        ("```\n# not a heading\n```\n", "'```'"),
        ("    # indented code\n", "'    # indented code'"),
    ],
)
def test_starts_with_h1_fails(text: str, shown: str) -> None:
    result = only(text, "starts_with_h1", starts_with_h1=True)
    assert not result.ok
    assert result.message is not None
    assert f"line 1 is {shown}." in result.message


def test_starts_with_h1_names_the_line() -> None:
    result = only("\n\nSome text\n", "starts_with_h1", starts_with_h1=True)
    assert result.message is not None
    assert "line 3 is 'Some text'" in result.message


def test_an_empty_document_has_no_h1() -> None:
    result = only("  \n\n", "starts_with_h1", starts_with_h1=True)
    assert not result.ok
    assert result.message is not None
    assert "empty" in result.message


def test_starts_with_h1_off() -> None:
    assert "starts_with_h1" not in check("No heading", starts_with_h1=False)


# required_headings.


DOC = """\
# Tech stack

## 1. Recommendation

### options:

## Cost (EUR)

Risks
-----

```markdown
## Open questions
```
"""


def test_required_headings_at_any_level_and_in_any_case() -> None:
    result = only(
        DOC, "required_headings", required_headings=("RECOMMENDATION", "Options")
    )
    assert result.ok


def test_required_headings_accept_numbering_and_qualifiers() -> None:
    assert only(DOC, "required_headings", required_headings=("Cost",)).ok
    assert only(
        "# T\n## Cost — yearly\n## Risks - top 3\n## *Next steps*\n",
        "required_headings",
        required_headings=("Cost", "Risks", "Next steps"),
    ).ok


def test_required_headings_report_what_is_missing() -> None:
    result = only(
        DOC,
        "required_headings",
        required_headings=("Recommendation", "Risks", "Open questions", "Costs"),
    )
    assert not result.ok
    # Setext headings and headings inside code fences do not count.
    assert result.message == (
        "Missing required headings: 'Risks', 'Open questions', 'Costs'. Add each "
        "as a Markdown heading with that name, such as '## Risks'. Any level "
        "counts, and case is ignored."
    )


def test_a_fence_closes_only_with_its_own_marker() -> None:
    text = "# T\n~~~\n```\n## Inside\n~~~\n##\n## Cost\n````\n## Also inside\n```\n"
    result = only(
        text, "required_headings", required_headings=("Cost", "Inside", "Also inside")
    )
    assert result.message is not None
    assert result.message.startswith(
        "Missing required headings: 'Inside', 'Also inside'."
    )


def test_a_heading_must_be_the_name_not_contain_it() -> None:
    result = only(
        "# T\n## Cost model\n", "required_headings", required_headings=("Cost",)
    )
    assert not result.ok
    assert result.message is not None
    assert result.message.startswith("Missing required heading: 'Cost'.")


# forbidden_phrases.


def test_forbidden_phrases_engine_defaults() -> None:
    text = "# T\n\nAs an ai model, I think.\nIn this document we will cover.\n"
    result = only(text, "forbidden_phrases", forbidden_phrases=ENGINE_FORBIDDEN_PHRASES)
    assert not result.ok
    assert result.message is not None
    assert result.message.startswith(
        "Forbidden phrases found: 'as an AI' (line 3); "
        "'in this document we will' (line 4). Rewrite those sentences"
    )


def test_forbidden_phrases_match_whole_words_across_spaces() -> None:
    phrases = ("as an AI", "session 2")
    assert only(
        "As an AIM member.\nSee session 20.\n",
        "forbidden_phrases",
        forbidden_phrases=phrases,
    ).ok
    result = only(
        "This was covered in Session\n2 and in session  2.",
        "forbidden_phrases",
        forbidden_phrases=phrases,
    )
    assert result.message is not None
    assert "'session 2' (lines 1, 2)" in result.message


def test_forbidden_phrases_list_at_most_five_lines() -> None:
    text = "\n".join(["lorem ipsum"] * 7)
    result = only(text, "forbidden_phrases", forbidden_phrases=("Lorem Ipsum",))
    assert result.message is not None
    assert "'Lorem Ipsum' (lines 1, 2, 3, 4, 5, …)" in result.message


def test_forbidden_phrases_repeated_in_the_playbook_are_listed_once() -> None:
    result = only(
        "lorem ipsum",
        "forbidden_phrases",
        forbidden_phrases=(*ENGINE_FORBIDDEN_PHRASES, "LOREM IPSUM", "  "),
    )
    assert result.message is not None
    assert result.message.count("(line 1)") == 1


def test_forbidden_phrases_with_punctuation() -> None:
    result = only(
        "Price: $5 (approx.)", "forbidden_phrases", forbidden_phrases=("(approx.)",)
    )
    assert not result.ok


def test_forbidden_phrases_always_run() -> None:
    assert check("# Fine\n")["forbidden_phrases"].ok


# max_open_questions.


def test_max_open_questions_counts_the_record() -> None:
    text = "Q? Q? Q? Q? Q?"
    assert only(text, "max_open_questions", record=_record(3), max_open_questions=3).ok
    result = only(text, "max_open_questions", record=_record(4), max_open_questions=3)
    assert not result.ok
    assert result.message is not None
    assert result.message.startswith(
        "The result lists 4 open questions; the limit for this task is 3."
    )


def test_max_open_questions_off() -> None:
    assert "max_open_questions" not in check("", record=_record(6))


# Citations.


def test_cited_sources_in_order_of_first_use() -> None:
    text = "A [S2]. B [S1, S3]. C [S2][S4]. D [ S5 ; S6 ]. E [s7] [S] [see S8]."
    assert cited_sources(text) == ["S2", "S1", "S3", "S4", "S5", "S6"]


def test_citations_resolve() -> None:
    assert only(
        "A [S1]. B [S2, S3].", "citations_resolve", sources=3, citations_resolve=True
    ).ok
    result = only(
        "A [S1]. B [S7]. C [S9, S1].",
        "citations_resolve",
        sources=3,
        citations_resolve=True,
    )
    assert not result.ok
    assert result.message is not None
    assert result.message.startswith(
        "The document cites [S7], [S9], but this task's sources are S1, S2, S3."
    )
    assert "[unverified]" in result.message


def test_citations_resolve_without_sources() -> None:
    assert only("No citations.", "citations_resolve", citations_resolve=True).ok
    result = only("A [S1].", "citations_resolve", citations_resolve=True)
    assert result.message is not None
    assert "this task has no sources" in result.message


def test_min_sources_counts_distinct_known_sources() -> None:
    text = "A [S1]. B [S1]. C [S3]. D [S9]."
    assert only(text, "min_sources", sources=4, min_sources=2).ok
    result = only(text, "min_sources", sources=4, min_sources=3)
    assert not result.ok
    assert result.repairable
    assert result.message == (
        "The document cites 2 distinct sources (S1, S3); it needs at least 3. "
        "Cite S2, S4 where they support a claim in the document."
    )


def test_min_sources_with_too_little_research_cannot_be_repaired() -> None:
    result = only("A [S1] [S2].", "min_sources", sources=2, min_sources=3)
    assert not result.ok
    assert not result.repairable
    assert result.message is not None
    assert result.message.startswith(
        "The research found 2 sources, and this task needs at least 3 cited."
    )
    assert "--refresh-research" in result.message


def test_min_sources_when_nothing_is_cited() -> None:
    result = only("No citations.", "min_sources", sources=2, min_sources=1)
    assert result.message == (
        "The document cites 0 distinct sources; it needs at least 1. "
        "Cite S1, S2 where they support a claim in the document."
    )


# Running them together.


def test_the_order_follows_the_spec() -> None:
    every = Validators(
        max_lines=100,
        min_lines=1,
        starts_with_h1=True,
        required_headings=("A",),
        forbidden_phrases=ENGINE_FORBIDDEN_PHRASES,
        max_open_questions=6,
        citations_resolve=True,
        min_sources=1,
    )
    results = run_validators(
        "# A\n[S1]\n", every, result=_record(), sources=_sources(1)
    )
    assert [r.name for r in results] == [
        "max_lines",
        "min_lines",
        "starts_with_h1",
        "required_headings",
        "forbidden_phrases",
        "max_open_questions",
        "citations_resolve",
        "min_sources",
    ]
    assert all(r.ok for r in results)


def test_with_a_playbook_s_resolved_settings(make_project: MakeProject) -> None:
    project = load_project(make_project())
    landscape = project.playbook.task("landscape")
    text = "# Landscape\n\nPeers [S1], [S2]. As an AI, [S4].\n"

    results = run_validators(
        text, landscape.validators, result=_record(), sources=_sources(3)
    )

    assert {r.name: r.ok for r in results} == {
        "max_lines": True,
        "starts_with_h1": True,
        "forbidden_phrases": False,
        "max_open_questions": True,
        "citations_resolve": False,
        "min_sources": True,
    }
    plan = project.playbook.task("plan")
    names = [r.name for r in run_validators("x", plan.validators, result=_record())]
    assert names == [
        "max_lines",
        "forbidden_phrases",
        "max_open_questions",
        "citations_resolve",
    ]


def test_failures_and_can_repair() -> None:
    ok = ValidatorResult(name="max_lines", ok=True)
    bad = ValidatorResult(name="max_lines", ok=False, message="Too long.")
    stuck = ValidatorResult(
        name="min_sources", ok=False, message="Too few.", repairable=False
    )
    assert failures([ok, bad, stuck]) == [bad, stuck]
    assert not can_repair([ok])
    assert can_repair([ok, bad])
    assert not can_repair([bad, stuck])


def test_results_serialise_compactly() -> None:
    ok = ValidatorResult(name="max_lines", ok=True)
    stuck = ValidatorResult(
        name="min_sources", ok=False, message="Too few.", repairable=False
    )
    assert json.loads(ok.model_dump_json()) == {"name": "max_lines", "ok": True}
    assert stuck.model_dump() == {
        "name": "min_sources",
        "ok": False,
        "message": "Too few.",
        "repairable": False,
    }
    assert ValidatorResult.model_validate_json(stuck.model_dump_json()) == stuck
    assert ValidatorResult.model_validate_json(ok.model_dump_json()) == ok
