"""``brief.md``: the ground truth about a project."""

from __future__ import annotations

import difflib
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from spadework.errors import InvalidError, Problem
from spadework.frontmatter import FrontMatter, parse_front_matter
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


FIELDS = tuple(BriefFrontMatter.model_fields)
"""The keys spadework itself reads. Any other key is for templates only."""


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
    missing = _MissingKeys(front_matter, text)
    try:
        front = BriefFrontMatter.model_validate(front_matter.data)
    except ValidationError as exc:
        try:
            raise_for(
                exc, BriefFrontMatter, display, front_matter.key_lines, BRIEF_HINT
            )
        except InvalidError as error:
            error.problems = tuple(missing.locate(p) for p in error.problems)
            raise
    brief = Brief(front, front_matter.body, text, front_matter.key_lines)
    if playbook is not None:
        _check_needs(brief, front_matter.data, playbook, display, missing)
    return brief


_REQUIRED = re.compile(r"^'(?P<key>[^']+)' is required\b")


class _MissingKeys:
    """Where a missing key probably is: misspelt, or commented out."""

    def __init__(self, front_matter: FrontMatter, text: str) -> None:
        # The front matter's lines, numbered as in the file.
        end = front_matter.body_line - 2
        self.lines = list(enumerate(text.splitlines()[1:end], start=2))
        self.key_lines = front_matter.key_lines
        self.extra = [key for key in front_matter.data if key not in FIELDS]

    def misspelt(self, key: str) -> str | None:
        """The extra key that looks like a misspelling of ``key``, if any."""
        close = difflib.get_close_matches(key, self.extra, n=1)
        return close[0] if close else None

    def locate(self, problem: Problem) -> Problem:
        """``problem`` with a line, if it says a key is required and the key
        is found misspelt or commented out."""
        match = _REQUIRED.match(problem.message)
        if match is None or problem.line is not None:
            return problem
        key = match["key"]
        misspelt = self.misspelt(key)
        if misspelt is not None:
            line = self.key_lines.get((misspelt,))
            note = f" (is '{misspelt}' on this line a misspelling of it?)"
        else:
            # A key commented out where it stood: "# key:" at the start of a line.
            regex = re.compile(rf"^# ?{re.escape(key)}\s*:")
            line = next((n for n, text in self.lines if regex.match(text)), None)
            note = " (it is commented out on this line)"
        if line is None:
            return problem
        return replace(problem, message=problem.message + note, line=line)


def _check_needs(
    brief: Brief,
    data: Mapping[str, Any],
    playbook: Playbook,
    display: str,
    missing_keys: _MissingKeys,
) -> None:
    problems = []
    hints = []
    research = playbook.research_tasks
    if research and brief.public_context is None:
        names = ", ".join(task.id for task in research)
        problems.append(
            missing_keys.locate(
                Problem(
                    "'public_context' is required because the playbook has research "
                    f"tasks ({names})",
                    display,
                )
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
            missing_keys.locate(
                Problem(
                    f"'{key}' is required by the playbook '{playbook.name}'",
                    display,
                    brief.key_lines.get((key,)),
                )
            )
        )
    absent = [key for key in missing if missing_keys.misspelt(key) is None]
    if absent:
        hints.append(
            f"Add {', '.join(f'{key}:' for key in absent)} to the front matter."
        )
    for key in missing:
        if (misspelt := missing_keys.misspelt(key)) is not None:
            hints.append(f"Rename {misspelt}: to {key}:.")
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


def unused_key_warnings(
    brief: Brief, playbook: Playbook, display: str = BRIEF_FILE
) -> list[tuple[Problem, str]]:
    """A warning for each extra key that nothing will read: probably a typo.

    Keys other than the brief fields are only available to templates, as
    ``brief.extra.<key>``. A key the playbook requires, or that one of its
    templates reads, is in use.
    """
    required = playbook.manifest.requires_brief
    extra = [key for key in brief.extra if key not in required]
    used = playbook.brief_extra_keys() if extra else set()
    if used is None:
        return []  # a template reads brief.extra as a whole
    known = [*FIELDS, *required]
    warnings = []
    for key in extra:
        if key in used:
            continue
        line = brief.key_lines.get((key,))
        close = difflib.get_close_matches(key, known, n=1)
        if close:
            problem = Problem(
                f"unknown key '{key}' (did you mean '{close[0]}'?)", display, line
            )
            hint = f"Rename it to {close[0]}: as it is, spadework does not read it."
        else:
            problem = Problem(
                f"'{key}' is not a brief field, and no template of playbook "
                f"'{playbook.name}' uses it",
                display,
                line,
            )
            hint = (
                "Other keys are only for templates, as brief.extra.<key>. For notes, "
                "use the body of brief.md, below the front matter."
            )
        warnings.append((problem, hint))
    return warnings
