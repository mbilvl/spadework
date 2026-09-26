import copy
import json
import re
from typing import Any

import pytest
from pydantic import ValidationError

from spadework.records import (
    RESULT_MODELS,
    ConsolidatedQuestion,
    CriticResult,
    DraftResult,
    FixResult,
    ResearchResult,
    SummaryResult,
    output_schema,
    result_model,
    validate_result,
)

# The examples in docs/spec/06-records.md.
RESEARCH: dict[str, Any] = {
    "sources": [
        {
            "id": "S1",
            "url": "https://cms.example/pricing",
            "title": "Pricing | ExampleCMS",
            "publisher": "ExampleCMS",
            "accessed": "2026-09-27",
            "quote": "Free plan … up to 20 editors … 10,000 documents",
            "claim": "ExampleCMS's free plan allows 20 editors and 10k documents",
        }
    ],
    "notes": "Markdown findings. Every factual sentence cites [S#].",
    "unanswered": ["Self-hosted CMS pricing with EU data residency"],
}
DRAFT: dict[str, Any] = {
    "status": "ok",
    "summary": (
        "Recommends a static site + hosted headless CMS; low two-figure yearly "
        "running cost."
    ),
    "key_facts": [
        "Stack: static site + hosted headless CMS",
        "Portuguese version: supported, adds ~30% effort",
    ],
    "decisions": [
        {
            "id": "D1",
            "decision": "Hosted headless CMS",
            "reason": "Best editor UX for non-technical staff",
            "reversibility": "moderate",
            "alternatives": ["WordPress", "Self-hosted CMS"],
        }
    ],
    "open_questions": [
        {
            "id": "Q1",
            "question": "Is a Portuguese version required at launch?",
            "why": "Changes build effort and CMS setup",
            "answer_format": "yes_no",
        }
    ],
    "assumptions": ["No e-commerce in the first 12 months"],
    "risks": [
        {
            "risk": "Single maintainer",
            "likelihood": "medium",
            "impact": "high",
            "mitigation": "Documented handover",
        }
    ],
    "gaps": ["Could not confirm Payload Cloud regional pricing"],
}
FIX: dict[str, Any] = {
    **DRAFT,
    "applied": ["F2", "F5"],
    "rejected": [{"finding": "F3", "reason": "Would require inventing a price"}],
}
CRITIC: dict[str, Any] = {
    "verdict": "continue",
    "findings": [
        {
            "id": "F1",
            "target": "06-tech-stack.md",
            "kind": "contradiction",
            "severity": "high",
            "instruction": (
                "Align the bilingual effort estimate with 02-audience.md "
                "(30% vs 'doubles')."
            ),
        }
    ],
    "biggest_gap": "No verified pricing for the recommended hosting in EUR.",
}
SUMMARY: dict[str, Any] = {
    "status": "ok",
    "open_questions": [
        {"question": "…", "why": "…", "from": ["stack:Q1", "audience:Q2"]}
    ],
}
EXAMPLES = {
    "research": RESEARCH,
    "draft": DRAFT,
    "repair": DRAFT,
    "fix": FIX,
    "critic": CRITIC,
    "summary": SUMMARY,
}


def changed(data: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """A deep copy of ``data`` with the value at a dotted ``path`` replaced."""
    result = copy.deepcopy(data)
    *parents, last = path.split(".")
    node: Any = result
    for part in parents:
        node = node[int(part)] if part.isdigit() else node[part]
    node[int(last) if last.isdigit() else last] = value
    return result


def conforms(schema: dict[str, Any], value: Any) -> bool:
    """A small JSON Schema check, for the keywords output_schema uses."""
    if "enum" in schema and value not in schema["enum"]:
        return False
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if any(key not in value for key in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(conforms(properties[key], item) for key, item in value.items())
    if kind == "array":
        if not isinstance(value, list):
            return False
        if len(value) > schema.get("maxItems", len(value)):
            return False
        if len(value) < schema.get("minItems", 0):
            return False
        if schema.get("uniqueItems"):
            seen = [json.dumps(item, sort_keys=True) for item in value]
            if len(set(seen)) != len(seen):
                return False
        return all(conforms(schema["items"], item) for item in value)
    if kind == "string":
        if not isinstance(value, str):
            return False
        if (
            not schema.get("minLength", 0)
            <= len(value)
            <= schema.get("maxLength", 10**9)
        ):
            return False
        return (
            "pattern" not in schema or re.search(schema["pattern"], value) is not None
        )
    return True


@pytest.mark.parametrize("kind", sorted(EXAMPLES))
def test_the_spec_examples_are_valid(kind: str) -> None:
    record = validate_result(kind, EXAMPLES[kind])

    assert isinstance(record, result_model(kind))
    assert record.model_dump() == EXAMPLES[kind]
    assert conforms(output_schema(kind), EXAMPLES[kind])


def test_each_kind_has_its_model() -> None:
    assert {
        "research": ResearchResult,
        "draft": DraftResult,
        "repair": DraftResult,
        "critic": CriticResult,
        "fix": FixResult,
        "summary": SummaryResult,
    } == RESULT_MODELS
    with pytest.raises(ValueError, match="unknown session kind 'plan'"):
        result_model("plan")


# (kind, path, value, the error it gives)
INVALID = [
    ("research", "sources.0.quote", "x" * 301, "at most 300 characters"),
    ("research", "sources", [RESEARCH["sources"][0]] * 31, "at most 30 items"),
    ("research", "sources.0.url", "ftp://cms.example/", "should match pattern"),
    ("research", "sources.0.accessed", "2026-02-30", "not a date"),
    ("research", "sources.0.accessed", "20260927", "should match pattern"),
    ("research", "sources.0.id", "1", "should match pattern"),
    ("research", "sources.0.quote", "  ", "should match pattern"),
    ("draft", "status", "done", "'ok' or 'partial'"),
    ("draft", "summary", "Two\nlines", "should match pattern"),
    ("draft", "summary", "x" * 301, "at most 300 characters"),
    ("draft", "key_facts", ["fact"] * 6, "at most 5 items"),
    ("draft", "decisions", [DRAFT["decisions"][0]] * 9, "at most 8 items"),
    ("draft", "open_questions", [DRAFT["open_questions"][0]] * 7, "at most 6 items"),
    ("draft", "assumptions", ["a"] * 9, "at most 8 items"),
    ("draft", "risks", [DRAFT["risks"][0]] * 9, "at most 8 items"),
    ("draft", "gaps", ["g"] * 9, "at most 8 items"),
    ("draft", "decisions.0.reversibility", "never", "'easy', 'moderate' or 'hard'"),
    ("draft", "open_questions.0.answer_format", "essay", "'yes_no'"),
    ("draft", "risks.0.likelihood", "certain", "'low', 'medium' or 'high'"),
    ("draft", "decisions.0.id", "Q1", "should match pattern"),
    ("critic", "verdict", "stop", "'continue' or 'ship'"),
    ("critic", "findings.0.kind", "typo", "'fabrication', 'contradiction'"),
    ("critic", "findings.0.severity", "critical", "'low', 'medium' or 'high'"),
    ("critic", "biggest_gap", "one\ntwo", "should match pattern"),
    ("fix", "applied", ["2"], "should match pattern"),
    ("summary", "open_questions", [SUMMARY["open_questions"][0]] * 11, "at most 10"),
    ("summary", "open_questions.0.from", [], "at least 1 item"),
    ("summary", "open_questions.0.from", ["Q1"], "should match pattern"),
]


@pytest.mark.parametrize(("kind", "path", "value", "error"), INVALID)
def test_limits_and_enums_are_enforced(
    kind: str, path: str, value: Any, error: str
) -> None:
    data = changed(EXAMPLES[kind], path, value)

    with pytest.raises(ValidationError, match=re.escape(error)):
        validate_result(kind, data)
    if error != "not a date":  # a pattern cannot check the calendar
        assert not conforms(output_schema(kind), data)


@pytest.mark.parametrize("kind", sorted(EXAMPLES))
def test_unknown_and_missing_keys_are_errors(kind: str) -> None:
    extra = {**EXAMPLES[kind], "confidence": 0.9}
    missing = dict(list(EXAMPLES[kind].items())[1:])

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        validate_result(kind, extra)
    with pytest.raises(ValidationError, match="Field required"):
        validate_result(kind, missing)
    assert not conforms(output_schema(kind), extra)
    assert not conforms(output_schema(kind), missing)


def test_values_are_not_coerced() -> None:
    with pytest.raises(ValidationError, match="Input should be a valid string"):
        validate_result("draft", changed(DRAFT, "key_facts", [1]))


@pytest.mark.parametrize(
    ("kind", "path", "value", "error"),
    [
        ("research", "sources", [RESEARCH["sources"][0]] * 2, "Source id S1"),
        ("draft", "decisions", [DRAFT["decisions"][0]] * 2, "Decision id D1"),
        ("draft", "open_questions", [DRAFT["open_questions"][0]] * 2, "Question id Q1"),
        ("critic", "findings", [CRITIC["findings"][0]] * 2, "Finding id F1"),
        ("fix", "applied", ["F3"], "Finding id F3"),
        ("fix", "applied", ["F2", "F2"], "Finding id F2"),
        ("summary", "open_questions.0.from", ["a:Q1", "a:Q1"], "Question id a:Q1"),
    ],
)
def test_ids_are_unique(kind: str, path: str, value: Any, error: str) -> None:
    with pytest.raises(ValidationError, match=f"{error} is used more than once"):
        validate_result(kind, changed(EXAMPLES[kind], path, value))


def test_findings_are_capped_by_the_playbook() -> None:
    two = changed(CRITIC, "findings", [CRITIC["findings"][0]] * 2)
    two["findings"][1] = {**two["findings"][1], "id": "F2"}

    assert isinstance(validate_result("critic", two, max_findings=2), CriticResult)
    with pytest.raises(ValidationError, match="more than this playbook's max_findings"):
        validate_result("critic", two, max_findings=1)
    assert (
        output_schema("critic", max_findings=1)["properties"]["findings"]["maxItems"]
        == 1
    )
    assert "maxItems" not in output_schema("critic")["properties"]["findings"]
    assert not conforms(output_schema("critic", max_findings=1), two)


def test_an_empty_research_result_is_valid() -> None:
    empty = {"sources": [], "notes": "Nothing found.", "unanswered": ["Everything"]}

    assert validate_result("research", empty) == ResearchResult.model_validate(empty)


def test_consolidated_questions_use_from_as_the_key() -> None:
    question = validate_result("summary", SUMMARY)

    assert isinstance(question, SummaryResult)
    assert question.open_questions[0].from_ == ["stack:Q1", "audience:Q2"]
    assert json.loads(question.model_dump_json())["open_questions"][0]["from"] == [
        "stack:Q1",
        "audience:Q2",
    ]
    with pytest.raises(ValidationError):
        ConsolidatedQuestion.model_validate({"question": "?", "why": "?", "from_": []})


def walk(node: Any, parent_key: str | None = None) -> list[tuple[str | None, Any]]:
    nodes = [(parent_key, node)]
    if isinstance(node, dict):
        for key, value in node.items():
            nodes += walk(value, key)
    elif isinstance(node, list):
        for value in node:
            nodes += walk(value, parent_key)
    return nodes


@pytest.mark.parametrize("kind", sorted(RESULT_MODELS))
def test_the_output_schema_is_self_contained_and_closed(kind: str) -> None:
    schema = output_schema(kind, max_findings=12)
    text = json.dumps(schema)

    assert "$ref" not in text
    assert "$defs" not in text
    assert len(text) < 4000  # it goes on the command line
    for key, node in walk(schema):
        if not isinstance(node, dict) or key == "properties":
            continue
        assert "title" not in node
        if node.get("type") == "object":
            assert node["additionalProperties"] is False
            assert node["required"] == list(node["properties"])


def test_the_output_schema_carries_the_limits() -> None:
    research = output_schema("research")["properties"]
    draft = output_schema("draft")["properties"]
    critic = output_schema("critic")["properties"]
    source = research["sources"]["items"]["properties"]

    assert research["sources"]["maxItems"] == 30
    assert source["quote"]["maxLength"] == 300
    assert source["title"]["type"] == "string"  # a property, not a schema title
    assert [draft[key]["maxItems"] for key in ("key_facts", "decisions")] == [5, 8]
    assert draft["open_questions"]["maxItems"] == 6
    assert {draft[key]["maxItems"] for key in ("assumptions", "risks", "gaps")} == {8}
    assert critic["findings"]["items"]["properties"]["kind"]["enum"] == [
        "fabrication",
        "contradiction",
        "filler",
        "specificity",
        "priority",
        "missing",
        "length",
        "other",
    ]
    assert output_schema("repair") == output_schema("draft")
    assert set(output_schema("fix")["properties"]) == {*draft, "applied", "rejected"}
    summary = output_schema("summary")["properties"]["open_questions"]
    assert summary["maxItems"] == 10
    assert "from" in summary["items"]["properties"]


def test_the_output_schema_is_a_fresh_copy() -> None:
    schema = output_schema("draft")
    schema["properties"]["status"]["enum"].append("broken")

    assert output_schema("draft")["properties"]["status"]["enum"] == ["ok", "partial"]


UNIQUE_LISTS = [
    ("research", ("sources",)),
    ("draft", ("decisions",)),
    ("draft", ("open_questions",)),
    ("fix", ("applied",)),
    ("fix", ("rejected",)),
    ("critic", ("findings",)),
    ("summary", ("open_questions", "items", "properties", "from")),
]


@pytest.mark.parametrize(("kind", "path"), UNIQUE_LISTS)
def test_lists_of_ids_are_unique_in_the_schema(
    kind: str, path: tuple[str, ...]
) -> None:
    node: Any = output_schema(kind)["properties"]
    for key in path:
        node = node[key]

    assert node["uniqueItems"] is True
    assert "once" in node["description"] or "never repeat" in node["description"]


@pytest.mark.parametrize(
    ("kind", "path", "value"),
    [
        ("research", "sources", [RESEARCH["sources"][0]] * 2),
        ("draft", "decisions", [DRAFT["decisions"][0]] * 2),
        ("fix", "applied", ["F2", "F2"]),
        ("summary", "open_questions.0.from", ["a:Q1", "a:Q1"]),
    ],
)
def test_the_schema_rejects_repeated_items(kind: str, path: str, value: Any) -> None:
    assert not conforms(output_schema(kind), changed(EXAMPLES[kind], path, value))


def test_the_schema_states_the_rules_it_cannot_check() -> None:
    source = output_schema("research")["properties"]["sources"]["items"]
    fix = output_schema("fix")["properties"]

    assert "a real date" in source["properties"]["accessed"]["description"]
    assert "never both" in fix["applied"]["description"]
