"""Records: the structured result every session returns.

See docs/spec/06-records.md. The models here are the source of truth: the
JSON Schema that the executor passes to the session is generated from them,
and the engine validates each result against them again with
``validate_result``.
"""

from __future__ import annotations

import copy
import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, ConfigDict, Field, ValidationInfo, model_validator

from spadework.models import StrictModel

# Limits from docs/spec/06-records.md.
MAX_QUOTE_CHARS = 300
MAX_SOURCES = 30
MAX_KEY_FACTS = 5
MAX_DECISIONS = 8
MAX_OPEN_QUESTIONS = 6
MAX_LIST_ITEMS = 8
MAX_SUMMARY_QUESTIONS = 10
MAX_SUMMARY_CHARS = 300

# JSON Schema patterns are ECMA-262 and pydantic's are Rust regex; these are
# written in the subset both read the same way.
_LINE = r"^[^\r\n]*\S[^\r\n]*$"
_ONE_LINE_OR_EMPTY = r"^[^\r\n]*$"
_TEXT = r"\S"
_DATE = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$"
_URL = r"^https?://[^\s]+$"
_TASK_ID = r"[a-z][a-z0-9-]*"


_UNIQUE: dict[str, Any] = {"uniqueItems": True}


def _ids_once(what: str, prefix: str) -> str:
    return f"Each {what} has its own id ({prefix}1, {prefix}2, …); never repeat one."


def _real_date(value: str) -> str:
    try:
        dt.date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{value!r} is not a date in the form YYYY-MM-DD") from None
    return value


def _local_id(prefix: str, what: str) -> Any:
    return Field(
        pattern=rf"^{prefix}[1-9][0-9]*$",
        description=f"{what} id: {prefix}1, {prefix}2, …",
    )


Line = Annotated[str, Field(pattern=_LINE)]
"""One non-blank line."""

Text = Annotated[str, Field(pattern=_TEXT)]
"""Non-blank text, which may span lines."""

SourceId = Annotated[str, _local_id("S", "Source")]
DecisionId = Annotated[str, _local_id("D", "Decision")]
QuestionId = Annotated[str, _local_id("Q", "Question")]
FindingId = Annotated[str, _local_id("F", "Finding")]
QualifiedQuestionId = Annotated[
    str,
    Field(
        pattern=rf"^{_TASK_ID}:Q[1-9][0-9]*$",
        description="A task's question, qualified with its task id, such as stack:Q1.",
    ),
]

Level = Literal["low", "medium", "high"]
Reversibility = Literal["easy", "moderate", "hard"]
AnswerFormat = Literal["yes_no", "choice", "number", "date", "document", "text"]
FindingKind = Literal[
    "fabrication",
    "contradiction",
    "filler",
    "specificity",
    "priority",
    "missing",
    "length",
    "other",
]


class Record(StrictModel):
    """A part of a session's structured result."""

    model_config = ConfigDict(serialize_by_alias=True)


def _unique(ids: list[str], what: str) -> None:
    seen: set[str] = set()
    for value in ids:
        if value in seen:
            raise ValueError(f"{what} id {value} is used more than once")
        seen.add(value)


class Source(Record):
    """A web page the research relied on, with the quote that supports it."""

    id: SourceId
    url: Annotated[str, Field(pattern=_URL, description="The page's http(s) URL.")]
    title: Line
    publisher: Line
    accessed: Annotated[
        str,
        # The pattern must come first, or pydantic leaves it out of the schema.
        Field(
            pattern=_DATE,
            description="The day the page was read: a real date, YYYY-MM-DD.",
        ),
        AfterValidator(_real_date),
    ]
    quote: Annotated[
        str,
        Field(
            min_length=1,
            max_length=MAX_QUOTE_CHARS,
            pattern=_TEXT,
            description=(
                "A verbatim excerpt from the page, at most 300 characters. Join "
                "fragments with …. It is checked against the page."
            ),
        ),
    ]
    claim: Annotated[
        str, Field(pattern=_LINE, description="What the quote supports, in one line.")
    ]


class ResearchResult(Record):
    """What a research session returns."""

    sources: list[Source] = Field(
        max_length=MAX_SOURCES,
        description="At most 30. " + _ids_once("source", "S"),
        json_schema_extra=_UNIQUE,
    )
    notes: Annotated[
        str,
        Field(
            pattern=_TEXT,
            description="Markdown findings. Every factual sentence cites [S#].",
        ),
    ]
    unanswered: list[Line] = Field(
        description="Questions the research could not answer."
    )

    @model_validator(mode="after")
    def _ids_are_unique(self) -> ResearchResult:
        _unique([source.id for source in self.sources], "Source")
        return self


class Decision(Record):
    id: DecisionId
    decision: Line
    reason: Text
    reversibility: Reversibility
    alternatives: list[Line]


class OpenQuestion(Record):
    id: QuestionId
    question: Line
    why: Text
    answer_format: AnswerFormat = Field(
        description="The kind of answer expected from the client."
    )


class Risk(Record):
    risk: Line
    likelihood: Level
    impact: Level
    mitigation: Text


class DraftResult(Record):
    """What a draft or repair session returns."""

    status: Literal["ok", "partial"]
    summary: Annotated[
        str,
        Field(
            max_length=MAX_SUMMARY_CHARS,
            pattern=_LINE,
            description="What the document concludes, in one line.",
        ),
    ]
    key_facts: list[Line] = Field(
        max_length=MAX_KEY_FACTS,
        description="The facts later documents must stay consistent with.",
    )
    decisions: list[Decision] = Field(
        max_length=MAX_DECISIONS,
        description=_ids_once("decision", "D"),
        json_schema_extra=_UNIQUE,
    )
    open_questions: list[OpenQuestion] = Field(
        max_length=MAX_OPEN_QUESTIONS,
        description=_ids_once("question", "Q"),
        json_schema_extra=_UNIQUE,
    )
    assumptions: list[Line] = Field(max_length=MAX_LIST_ITEMS)
    risks: list[Risk] = Field(max_length=MAX_LIST_ITEMS)
    gaps: list[Line] = Field(max_length=MAX_LIST_ITEMS)

    @model_validator(mode="after")
    def _ids_are_unique(self) -> DraftResult:
        _unique([decision.id for decision in self.decisions], "Decision")
        _unique([question.id for question in self.open_questions], "Question")
        return self


class Rejection(Record):
    finding: FindingId
    reason: Text


class FixResult(DraftResult):
    """What a fix session returns: a draft result plus what it did with the findings."""

    applied: list[FindingId] = Field(
        description=(
            "The findings you carried out, each once. A finding is either "
            "applied or rejected, never both."
        ),
        json_schema_extra=_UNIQUE,
    )
    rejected: list[Rejection] = Field(
        description="The findings you did not carry out, each once, with why.",
        json_schema_extra=_UNIQUE,
    )

    @model_validator(mode="after")
    def _findings_are_handled_once(self) -> FixResult:
        rejected = [rejection.finding for rejection in self.rejected]
        _unique(self.applied + rejected, "Finding")
        return self


class Finding(Record):
    id: FindingId
    target: Annotated[
        str,
        Field(pattern=_LINE, description="The file name of the document to change."),
    ]
    kind: FindingKind
    severity: Level
    instruction: Annotated[
        str,
        Field(
            pattern=_TEXT,
            description=(
                "What the fixer must change. It must be possible with the "
                "document, its sources and the brief, without inventing facts."
            ),
        ),
    ]


class CriticResult(Record):
    """What a critic session returns."""

    verdict: Literal["continue", "ship"]
    findings: list[Finding] = Field(
        description=_ids_once("finding", "F"), json_schema_extra=_UNIQUE
    )
    biggest_gap: Annotated[str, Field(pattern=_ONE_LINE_OR_EMPTY)]

    @model_validator(mode="after")
    def _findings_fit(self, info: ValidationInfo) -> CriticResult:
        _unique([finding.id for finding in self.findings], "Finding")
        limit = (info.context or {}).get("max_findings")
        if limit is not None and len(self.findings) > limit:
            raise ValueError(
                f"{len(self.findings)} findings is more than this playbook's "
                f"max_findings of {limit}"
            )
        return self


class ConsolidatedQuestion(Record):
    question: Line
    why: Text
    from_: list[QualifiedQuestionId] = Field(
        alias="from",
        min_length=1,
        description="The task questions this one merges, each once.",
        json_schema_extra=_UNIQUE,
    )

    @model_validator(mode="after")
    def _merges_each_question_once(self) -> ConsolidatedQuestion:
        _unique(self.from_, "Question")
        return self


class SummaryResult(Record):
    """What the summary session returns."""

    status: Literal["ok", "partial"]
    open_questions: list[ConsolidatedQuestion] = Field(max_length=MAX_SUMMARY_QUESTIONS)


# Session kind → the model of the result it returns.
RESULT_MODELS: dict[str, type[Record]] = {
    "research": ResearchResult,
    "draft": DraftResult,
    "repair": DraftResult,
    "critic": CriticResult,
    "fix": FixResult,
    "summary": SummaryResult,
}


def result_model(kind: str) -> type[Record]:
    """The model for the result of a session of ``kind``."""
    try:
        return RESULT_MODELS[kind]
    except KeyError:
        raise ValueError(f"unknown session kind {kind!r}") from None


def validate_result(kind: str, data: Any, *, max_findings: int | None = None) -> Record:
    """Validate a session's structured result and return it as its model.

    Raises ``pydantic.ValidationError`` when it does not match. Narrow the
    result with ``isinstance``, for example ``isinstance(r, DraftResult)``.
    """
    return result_model(kind).model_validate(
        data, context={"max_findings": max_findings}
    )


def output_schema(kind: str, *, max_findings: int | None = None) -> dict[str, Any]:
    """The JSON Schema for a session's structured result, as the CLI gets it.

    It is self-contained: every ``$ref`` is inlined and there is no
    ``$defs``, because the validator on the other side may not resolve
    references. Every object is closed (``additionalProperties: false``) and
    lists all its properties as required. Titles are dropped to keep the
    command line short; descriptions stay, since they guide the model.
    """
    schema = result_model(kind).model_json_schema(mode="validation")
    definitions = schema.pop("$defs", {})
    inlined: dict[str, Any] = _clean(_inline(schema, definitions))
    if kind == "critic" and max_findings is not None:
        inlined["properties"]["findings"]["maxItems"] = max_findings
    return inlined


def _inline(node: Any, definitions: dict[str, Any]) -> Any:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            name = ref.removeprefix("#/$defs/")
            target = copy.deepcopy(definitions[name])
            siblings = {key: value for key, value in node.items() if key != "$ref"}
            return _inline({**target, **siblings}, definitions)
        return {key: _inline(value, definitions) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline(item, definitions) for item in node]
    return node


def _clean(node: Any) -> Any:
    """Drop titles, and close every object so it has no optional keys."""
    if isinstance(node, list):
        return [_clean(item) for item in node]
    if not isinstance(node, dict):
        return node
    cleaned: dict[str, Any] = {}
    for key, value in node.items():
        if key == "title":
            continue
        if key == "properties":
            cleaned[key] = {name: _clean(sub) for name, sub in value.items()}
        else:
            cleaned[key] = _clean(value)
    if cleaned.get("type") == "object" and "properties" in cleaned:
        cleaned["additionalProperties"] = False
        cleaned["required"] = list(cleaned["properties"])
    return cleaned
