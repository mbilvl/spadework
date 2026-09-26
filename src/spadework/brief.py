"""``brief.md``: the ground truth about a project."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spadework.errors import InvalidError, Problem
from spadework.frontmatter import parse_front_matter
from spadework.models import NonEmptyStr, matching, raise_for, schema_pattern
from spadework.playbook import Playbook
from spadework.textfiles import KeyLines, read_text

BRIEF_FILE = "brief.md"
BRIEF_HINT = "Fix the front matter of brief.md, then run `spadework check` again."

LANGUAGE = r"[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*"
CURRENCY = r"[A-Z]{3}"

Language = Annotated[
    str,
    matching(LANGUAGE, "use a language tag such as 'en' or 'pt-PT'"),
    schema_pattern(LANGUAGE),
]
Currency = Annotated[
    str,
    matching(CURRENCY, "use a three-letter ISO 4217 code such as 'EUR'"),
    schema_pattern(CURRENCY),
]


class BriefFrontMatter(BaseModel):
    """The YAML front matter of brief.md.

    Any other key is available to templates as brief.extra.<key>.
    """

    model_config = ConfigDict(extra="allow", strict=True, frozen=True)

    title: NonEmptyStr = Field(description="The pack title, also used in logs.")
    reader: NonEmptyStr = Field(
        description="Who every document is written for. Injected into the house rules."
    )
    public_context: str | None = Field(
        default=None,
        description=(
            "The only brief content that web-facing research sessions see. "
            "Required when the playbook has research tasks. It may be ''."
        ),
    )
    priorities: list[NonEmptyStr] | None = Field(
        default=None,
        description="The critic checks the pack against these.",
    )
    language: Language = Field(default="en", description="The language of the pack.")
    currencies: list[Currency] = Field(
        default_factory=list, description="Money figures are given in each of these."
    )


@dataclass(frozen=True)
class Brief:
    front: BriefFrontMatter
    # Everything below the front matter.
    body: str
    # The whole file, which is part of every task's fingerprint.
    source: str
    key_lines: KeyLines

    @property
    def extra(self) -> Mapping[str, Any]:
        return self.front.model_extra or {}

    @property
    def public_context(self) -> str | None:
        return self.front.public_context


def load_brief(
    path: Path, playbook: Playbook | None = None, display: str = BRIEF_FILE
) -> Brief:
    """Load brief.md, and check it has what ``playbook`` needs."""
    if not path.is_file():
        raise InvalidError(
            f"there is no {display}",
            "Create it from the playbook's brief template, or run `spadework init`.",
            problems=[Problem("the file does not exist", display)],
        )
    text = read_text(path, display, BRIEF_HINT)
    front_matter = parse_front_matter(text, display, BRIEF_HINT)
    try:
        front = BriefFrontMatter.model_validate(front_matter.data)
    except ValidationError as exc:
        raise_for(exc, BriefFrontMatter, display, front_matter.key_lines, BRIEF_HINT)
    brief = Brief(front, front_matter.body, text, front_matter.key_lines)
    if playbook is not None:
        _check_needs(brief, front_matter.data, playbook, display)
    return brief


def _check_needs(
    brief: Brief, data: Mapping[str, Any], playbook: Playbook, display: str
) -> None:
    problems = []
    hints = []
    research = playbook.research_tasks
    if research and brief.public_context is None:
        names = ", ".join(task.id for task in research)
        problems.append(
            Problem(
                "'public_context' is required because the playbook has research "
                f"tasks ({names})",
                display,
            )
        )
        hints.append(
            "Add public_context: with what web research may see about the project, "
            'or public_context: "" to share nothing.'
        )
    missing = [
        key for key in playbook.manifest.requires_brief if _is_missing(data.get(key))
    ]
    for key in missing:
        problems.append(
            Problem(
                f"'{key}' is required by the playbook '{playbook.name}'",
                display,
                brief.key_lines.get((key,)),
            )
        )
    if missing:
        hints.append(
            f"Add {', '.join(f'{key}:' for key in missing)} to the front matter."
        )
    if problems:
        raise InvalidError(
            f"{display} is missing what the playbook needs",
            " ".join(hints),
            problems=problems,
        )


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, list):
        return not value
    return False
