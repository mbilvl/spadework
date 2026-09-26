"""Playbooks: ``playbook.toml``, the task files, and the task graph.

A playbook is data: TOML, Markdown and sandboxed Jinja2 templates
(ADR 0004). Every file the loader reads must resolve to a path inside the
playbook directory (invariant I8 in docs/spec/07-security.md).
"""

from __future__ import annotations

import difflib
import heapq
import importlib.resources
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, Field, ValidationError, model_validator

from spadework.budget import DEFAULT_ESTIMATES_USD
from spadework.errors import InvalidError, Problem, SpadeworkError
from spadework.frontmatter import parse_front_matter
from spadework.inputs import glob_regex
from spadework.models import (
    SLUG,
    TASK_ID,
    NonEmptyStr,
    SchemaVersion,
    Slug,
    StrictModel,
    TaskId,
    Tier,
    Usd,
    matching,
    raise_for,
    schema_pattern,
)
from spadework.textfiles import KeyLines, load_toml, read_text

PLAYBOOK_FILE = "playbook.toml"
BRIEF_TEMPLATE = "brief.template.md"
TASKS_DIR = "tasks"
PROMPTS_DIR = "prompts"

# Engine base prompts a playbook may append to (docs/spec/03-playbooks.md).
PROMPT_NAMES = ("critic", "draft", "fix", "repair", "research", "rules", "summary")
# Files the engine itself writes into out/.
RESERVED_OUTPUTS = ("00-summary.md", "sources.md")
ENGINE_FORBIDDEN_PHRASES = (
    "lorem ipsum",
    "as an AI",
    "in this document we will",
    "this session",
    "session 2",
)
DEFAULT_MAX_OPEN_QUESTIONS = 6
DEFAULT_MAX_SEARCHES = 6
DEFAULT_MAX_FETCHES = 8
# The guard's cap on tool calls per session. A per-tool cap above it means nothing.
MAX_TOOL_CALLS = 80

OUTPUT_NAME = r"[A-Za-z0-9][A-Za-z0-9._-]*\.md"
BRIEF_KEY = r"[A-Za-z_][A-Za-z0-9_-]*"
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?"


def _not_reserved(value: str) -> str:
    if value.lower() in RESERVED_OUTPUTS:
        raise ValueError(f"'{value}' is reserved for the file spadework writes itself")
    return value


def _relative_glob(value: str) -> str:
    parts = value.split("/")
    if not value or value.startswith("/") or "\\" in value or ".." in parts:
        raise ValueError(
            f"{value!r} is not valid: globs are relative to inputs/, "
            "use '/' between folders, and cannot contain '..'"
        )
    try:
        glob_regex(value)
    except re.error as exc:
        raise ValueError(f"{value!r} is not a valid glob: {exc}") from None
    return value


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ValueError(f"'{value}' is listed twice")
        seen.add(value)
    return values


OutputName = Annotated[
    str,
    matching(OUTPUT_NAME, "use a file name ending in .md, such as '01-scope.md'"),
    AfterValidator(_not_reserved),
    schema_pattern(OUTPUT_NAME),
]
InputGlob = Annotated[str, AfterValidator(_relative_glob)]
BriefKey = Annotated[
    str,
    matching(BRIEF_KEY, "use a front matter key such as 'priorities'"),
    schema_pattern(BRIEF_KEY),
]
Version = Annotated[
    str,
    matching(VERSION, "use a version such as '0.1.0'"),
    schema_pattern(VERSION),
]


class ValidateSpec(StrictModel):
    """Parameters for the built-in validators, which run after every draft."""

    max_lines: int | None = Field(default=None, gt=0, description="Most lines allowed.")
    min_lines: int | None = Field(
        default=None, gt=0, description="Fewest lines allowed."
    )
    starts_with_h1: bool | None = Field(
        default=None, description="The first non-blank line is '# ...'. Default: true."
    )
    required_headings: list[NonEmptyStr] | None = Field(
        default=None,
        description="Headings that must appear, at any level, in any case.",
    )
    forbidden_phrases: list[NonEmptyStr] | None = Field(
        default=None, description="Phrases to reject, on top of the engine's own."
    )
    max_open_questions: int | None = Field(
        default=None, ge=0, description="Counted from the record. Default: 6."
    )
    citations_resolve: bool | None = Field(
        default=None,
        description=(
            "Every [S#] exists in the task's sources. Default: on for research tasks."
        ),
    )
    min_sources: int | None = Field(
        default=None, gt=0, description="Fewest sources. Research tasks only."
    )

    @model_validator(mode="after")
    def _lines_in_order(self) -> ValidateSpec:
        _check_line_limits(self.min_lines, self.max_lines)
        return self


def _check_line_limits(min_lines: int | None, max_lines: int | None) -> None:
    if min_lines is not None and max_lines is not None and min_lines > max_lines:
        raise ValueError(
            f"min_lines ({min_lines}) is greater than max_lines ({max_lines})"
        )


class ResearchSpec(StrictModel):
    """A research session with web access and public context only."""

    questions: NonEmptyStr = Field(
        description=(
            "A Jinja2 template. It can use only public_context, task and "
            "brief.language."
        )
    )
    max_searches: int = Field(
        default=DEFAULT_MAX_SEARCHES,
        ge=0,
        le=MAX_TOOL_CALLS,
        description="WebSearch cap.",
    )
    max_fetches: int = Field(
        default=DEFAULT_MAX_FETCHES,
        ge=0,
        le=MAX_TOOL_CALLS,
        description="WebFetch cap.",
    )

    @model_validator(mode="after")
    def _can_do_something(self) -> ResearchSpec:
        if self.max_searches == 0 and self.max_fetches == 0:
            raise ValueError("max_searches and max_fetches cannot both be 0")
        return self


class TaskFrontMatter(StrictModel):
    """The YAML front matter of a task file. The body is a Jinja2 template."""

    title: NonEmptyStr
    output: OutputName = Field(
        description="File name in out/. Unique within the playbook; not 00-summary.md."
    )
    depends_on: Annotated[list[TaskId], AfterValidator(_unique)] = Field(
        default_factory=list, description="Ids of the tasks this one builds on."
    )
    tier: Tier | None = Field(
        default=None, description="Model tier. Default: the playbook's defaults.tier."
    )
    inputs: list[InputGlob] = Field(
        default_factory=lambda: ["**"],
        description="Globs over inputs/ that are staged for this task.",
    )
    budget_usd: Usd | None = Field(
        default=None,
        description="Cost estimate and per-session cap. Default: defaults.budget_usd.",
    )
    research: ResearchSpec | None = None
    validate_: ValidateSpec | None = Field(
        default=None,
        alias="validate",
        description=(
            "Validator parameters. Keys override the playbook's [defaults] one by "
            "one, and null turns an inherited validator off."
        ),
    )


class PlaybookDefaults(ValidateSpec):
    """Task-level defaults, which tasks can override, and default validators."""

    tier: Tier = "draft"
    budget_usd: Usd | None = Field(
        default=None,
        description=f"Default: ${DEFAULT_ESTIMATES_USD['draft']:.2f}.",
    )


class ReviewSettings(StrictModel):
    max_findings: int = Field(
        default=12, gt=0, description="Critic findings per round."
    )


class PlaybookManifest(StrictModel):
    """The playbook.toml file of a playbook."""

    schema_version: SchemaVersion
    name: Slug
    version: Version = Field(description="Part of every task fingerprint.")
    description: NonEmptyStr
    requires_brief: Annotated[list[BriefKey], AfterValidator(_unique)] = Field(
        default_factory=list,
        description="Brief fields this playbook needs, on top of title and reader.",
    )
    defaults: PlaybookDefaults = PlaybookDefaults()
    review: ReviewSettings = ReviewSettings()


@dataclass(frozen=True)
class Validators:
    """The validators that apply to one task, with every default resolved."""

    max_lines: int | None
    min_lines: int | None
    starts_with_h1: bool
    required_headings: tuple[str, ...]
    forbidden_phrases: tuple[str, ...]
    max_open_questions: int | None
    citations_resolve: bool
    min_sources: int | None


@dataclass(frozen=True)
class Task:
    id: str
    path: Path
    display: str
    # The whole task file, which is part of the task's fingerprint.
    source: str
    front: TaskFrontMatter
    body: str
    body_line: int
    key_lines: KeyLines
    tier: Tier
    budget_usd: float
    validators: Validators

    @property
    def title(self) -> str:
        return self.front.title

    @property
    def output(self) -> str:
        return self.front.output

    @property
    def depends_on(self) -> tuple[str, ...]:
        return tuple(self.front.depends_on)

    @property
    def inputs(self) -> tuple[str, ...]:
        return tuple(self.front.inputs)

    @property
    def research(self) -> ResearchSpec | None:
        return self.front.research


@dataclass(frozen=True)
class PlaybookRef:
    """Where a playbook lives, and how to name its files in messages."""

    spec: str
    root: Path
    kind: Literal["built-in", "path"]
    display_root: str

    def display(self, relative: str) -> str:
        return f"{self.display_root}/{relative}"

    @property
    def hint(self) -> str:
        if self.kind == "built-in":
            return "This is a bug in a built-in playbook. Please report it."
        return "Fix the playbook, then run `spadework check` again."


@dataclass(frozen=True)
class Playbook:
    ref: PlaybookRef
    manifest: PlaybookManifest
    # In execution order: dependencies first, ties broken by task id.
    tasks: tuple[Task, ...]
    # Prompt appendices by engine prompt name, appended verbatim.
    prompts: Mapping[str, str] = field(default_factory=dict)
    brief_template: str | None = None

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def version(self) -> str:
        return self.manifest.version

    @property
    def description(self) -> str:
        return self.manifest.description

    @property
    def research_tasks(self) -> tuple[Task, ...]:
        return tuple(task for task in self.tasks if task.research is not None)

    def task(self, task_id: str) -> Task:
        for task in self.tasks:
            if task.id == task_id:
                return task
        raise KeyError(task_id)


def builtin_root() -> Path:
    root = importlib.resources.files("spadework") / "builtin" / "playbooks"
    if not isinstance(root, Path):  # pragma: no cover - only for zip imports
        raise SpadeworkError(
            "the built-in playbooks are not on the file system",
            "Install spadework with pipx, uv or pip rather than from a zip file.",
        )
    return root


def builtin_names() -> list[str]:
    root = builtin_root()
    if not root.is_dir():
        return []
    return sorted(
        entry.name
        for entry in root.iterdir()
        if re.fullmatch(SLUG, entry.name) and (entry / PLAYBOOK_FILE).is_file()
    )


def resolve_playbook(spec: str, base: Path) -> PlaybookRef:
    """Find the playbook that ``spec`` names: a built-in name or a path.

    Relative paths are relative to ``base``, the project directory.
    """
    if spec.startswith("git+"):
        raise InvalidError(
            f"cannot load playbook {spec!r}",
            "Playbooks from git are not supported yet. Clone it and use its path.",
        )
    if re.fullmatch(SLUG, spec):
        root = builtin_root() / spec
        if (root / PLAYBOOK_FILE).is_file():
            return PlaybookRef(spec, root, "built-in", f"[built-in] {spec}")
        names = builtin_names()
        hint = f"Built-in playbooks: {', '.join(names) or 'none'}."
        close = difflib.get_close_matches(spec, names, n=1)
        if close:
            hint = f"Did you mean {close[0]!r}? {hint}"
        if (base / spec).is_dir():
            hint = f"For the playbook in the folder {spec!r}, write './{spec}'. {hint}"
        raise InvalidError(f"there is no built-in playbook named {spec!r}", hint)

    root = Path(spec).expanduser()
    if not root.is_absolute():
        root = base / root
    if not root.is_dir():
        raise InvalidError(
            f"playbook folder {spec!r} does not exist",
            "Check the path. Relative paths are relative to the project folder.",
        )
    display = os.path.relpath(root, base).replace(os.sep, "/")
    return PlaybookRef(spec, root, "path", display)


def load_playbook(ref: PlaybookRef) -> Playbook:
    """Load and validate a playbook, reporting every problem found at once."""
    manifest = _load_manifest(ref)
    problems: list[Problem] = []
    hints: list[str] = []
    tasks, failed = _load_tasks(ref, manifest, problems, hints)
    prompts = _load_prompts(ref, problems)
    template_path = ref.root / BRIEF_TEMPLATE
    brief_template = None
    if template_path.exists():
        brief_template = _read_inside(ref, BRIEF_TEMPLATE, problems)
    if tasks:
        _check_outputs(ref, tasks, problems)
        tasks = _in_order(ref, tasks, problems, failed)
    if problems:
        # A single problem keeps its own hint, unless the playbook is built in.
        hint = ref.hint
        if len(problems) == 1 and hints and ref.kind == "path":
            hint = hints[0]
        raise InvalidError(
            f"playbook {manifest.name!r} is not valid", hint, problems=problems
        )
    return Playbook(ref, manifest, tuple(tasks), prompts, brief_template)


def _load_manifest(ref: PlaybookRef) -> PlaybookManifest:
    display = ref.display(PLAYBOOK_FILE)
    path = ref.root / PLAYBOOK_FILE
    try:
        is_file = stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        is_file = False
    except OSError as exc:
        raise InvalidError(
            f"cannot read {display}",
            "Make the playbook folder and its files readable.",
            problems=[Problem(f"cannot read the file: {exc.strerror}", display)],
        ) from exc
    if not is_file:
        raise InvalidError(
            f"{ref.display_root} is not a playbook",
            f"A playbook folder holds a {PLAYBOOK_FILE}. Check the playbook path.",
            problems=[Problem(f"there is no {PLAYBOOK_FILE}", display)],
        )
    if _outside(ref, path):
        raise InvalidError(
            f"{display} is not valid", ref.hint, problems=[Problem(_OUTSIDE, display)]
        )
    document = load_toml(path, display, ref.hint)
    try:
        return PlaybookManifest.model_validate(document.data)
    except ValidationError as exc:
        raise_for(exc, PlaybookManifest, display, document.key_lines, ref.hint)


_OUTSIDE = "is a link to a file outside the playbook folder"


def contains(ref: PlaybookRef, folder: Path) -> bool:
    """Whether ``folder`` is the playbook folder or inside it.

    A project must not be: templates can include any file in the playbook
    folder, and they must not be able to include brief.md (I5, I8).
    """
    return folder.resolve().is_relative_to(ref.root.resolve())


def _link_problem(ref: PlaybookRef, path: Path) -> str | None:
    """Why ``path`` may not be read, if it is a link that leaves the playbook."""
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError):  # RuntimeError: a link loop before 3.13
        return "is a link that cannot be followed" if path.is_symlink() else None
    if not target.is_relative_to(ref.root.resolve()):
        return _OUTSIDE
    return None


def _outside(ref: PlaybookRef, path: Path) -> bool:
    return _link_problem(ref, path) is not None


def _read_inside(
    ref: PlaybookRef, relative: str, problems: list[Problem]
) -> str | None:
    display = ref.display(relative)
    path = ref.root / relative
    if (problem := _link_problem(ref, path)) is not None:
        problems.append(Problem(problem, display))
        return None
    try:
        return read_text(path, display, ref.hint)
    except InvalidError as exc:
        problems.extend(exc.problems)
        return None


def _visible_entries(
    ref: PlaybookRef, relative: str, problems: list[Problem]
) -> list[Path] | None:
    try:
        entries = list((ref.root / relative).iterdir())
    except OSError as exc:
        problems.append(
            Problem(f"cannot read this folder: {exc.strerror}", ref.display(relative))
        )
        return None
    return sorted(entry for entry in entries if not entry.name.startswith("."))


def _load_tasks(
    ref: PlaybookRef,
    manifest: PlaybookManifest,
    problems: list[Problem],
    hints: list[str],
) -> tuple[list[Task], set[str]]:
    """Load every task file. Also returns the ids of the tasks that failed."""
    directory = ref.root / TASKS_DIR
    failed: set[str] = set()
    if not directory.is_dir() or _outside(ref, directory):
        problems.append(Problem("there is no tasks/ folder", ref.display(TASKS_DIR)))
        return [], failed
    tasks: list[Task] = []
    entries = _visible_entries(ref, TASKS_DIR, problems)
    if entries is None:
        return tasks, failed
    for entry in entries:
        relative = f"{TASKS_DIR}/{entry.name}"
        if (problem := _link_problem(ref, entry)) is not None:
            problems.append(Problem(problem, ref.display(relative)))
            failed.add(entry.stem)
            continue
        if entry.suffix != ".md" or not entry.is_file():
            problems.append(
                Problem(
                    "tasks/ holds only task files ending in .md", ref.display(relative)
                )
            )
            continue
        if not re.fullmatch(TASK_ID, entry.stem):
            problems.append(
                Problem(
                    f"'{entry.stem}' is not a valid task id: start with a lowercase "
                    "letter and use only a-z, 0-9 and '-'",
                    ref.display(relative),
                )
            )
            continue
        try:
            tasks.append(_load_task(ref, manifest, entry, relative))
        except InvalidError as exc:
            problems.extend(exc.problems)
            hints.append(exc.hint)
            failed.add(entry.stem)
    if not entries:
        problems.append(Problem("there are no task files", ref.display(TASKS_DIR)))
    return tasks, failed


def _load_task(
    ref: PlaybookRef, manifest: PlaybookManifest, path: Path, relative: str
) -> Task:
    display = ref.display(relative)
    problems: list[Problem] = []
    text = _read_inside(ref, relative, problems)
    if text is None:
        raise InvalidError(f"{display} is not valid", ref.hint, problems=problems)
    front_matter = parse_front_matter(text, display, ref.hint)
    lines = front_matter.key_lines
    try:
        front = TaskFrontMatter.model_validate(front_matter.data)
    except ValidationError as exc:
        raise_for(exc, TaskFrontMatter, display, lines, ref.hint)

    validate = front.validate_
    if (
        front.research is None
        and validate is not None
        and validate.min_sources is not None
    ):
        raise InvalidError(
            f"{display} is not valid",
            "Add a research: section to the task, or remove validate.min_sources.",
            problems=[
                Problem(
                    "validate.min_sources applies only to tasks with research",
                    display,
                    lines.get(("validate", "min_sources")),
                )
            ],
        )
    validators = _resolve_validators(manifest.defaults, front)
    try:
        _check_line_limits(validators.min_lines, validators.max_lines)
    except ValueError as exc:
        raise InvalidError(
            f"{display} is not valid",
            "Make min_lines no greater than max_lines, in the task or in [defaults].",
            problems=[Problem(f"validate: {exc}", display, lines.get(("validate",)))],
        ) from exc

    budget = (
        front.budget_usd
        or manifest.defaults.budget_usd
        or DEFAULT_ESTIMATES_USD["draft"]
    )
    return Task(
        id=path.stem,
        path=path,
        display=display,
        source=text,
        front=front,
        body=front_matter.body,
        body_line=front_matter.body_line,
        key_lines=lines,
        tier=front.tier or manifest.defaults.tier,
        budget_usd=budget,
        validators=validators,
    )


def _resolve_validators(
    defaults: PlaybookDefaults, front: TaskFrontMatter
) -> Validators:
    # Keys set in the task replace the playbook's defaults one by one. A null
    # in the task turns a validator off: a flag becomes false, a limit goes
    # away, and forbidden_phrases falls back to the engine's own phrases.
    values: dict[str, Any] = {
        name: getattr(defaults, name)
        for name in ValidateSpec.model_fields
        if name in defaults.model_fields_set
    }
    if front.validate_ is not None:
        values.update(
            {
                name: getattr(front.validate_, name)
                for name in front.validate_.model_fields_set
            }
        )
    has_research = front.research is not None

    def setting(name: str, default: Any) -> Any:
        # An explicit None is kept: it is what turns a validator off.
        return values.get(name, default)

    return Validators(
        max_lines=setting("max_lines", None),
        min_lines=setting("min_lines", None),
        starts_with_h1=bool(setting("starts_with_h1", True)),
        required_headings=tuple(setting("required_headings", None) or ()),
        forbidden_phrases=(
            ENGINE_FORBIDDEN_PHRASES + tuple(setting("forbidden_phrases", None) or ())
        ),
        max_open_questions=setting("max_open_questions", DEFAULT_MAX_OPEN_QUESTIONS),
        citations_resolve=bool(setting("citations_resolve", has_research)),
        min_sources=setting("min_sources", None) if has_research else None,
    )


def _load_prompts(ref: PlaybookRef, problems: list[Problem]) -> dict[str, str]:
    directory = ref.root / PROMPTS_DIR
    if not directory.exists():
        return {}
    if not directory.is_dir() or _outside(ref, directory):
        problems.append(Problem("prompts must be a folder", ref.display(PROMPTS_DIR)))
        return {}
    prompts = {}
    known = ", ".join(f"{name}.md" for name in PROMPT_NAMES)
    for entry in _visible_entries(ref, PROMPTS_DIR, problems) or []:
        relative = f"{PROMPTS_DIR}/{entry.name}"
        if (
            entry.suffix != ".md"
            or entry.stem not in PROMPT_NAMES
            or not entry.is_file()
        ):
            problems.append(
                Problem(
                    f"not a prompt appendix; prompts/ can hold only {known}",
                    ref.display(relative),
                )
            )
            continue
        text = _read_inside(ref, relative, problems)
        if text is not None:
            prompts[entry.stem] = text
    return prompts


def _check_outputs(
    ref: PlaybookRef, tasks: list[Task], problems: list[Problem]
) -> None:
    # Compared without case, because macOS and Windows file systems ignore it.
    owners: dict[str, Task] = {}
    for task in tasks:
        key = task.output.lower()
        if key in owners:
            problems.append(
                Problem(
                    f"output '{task.output}' is also used by task '{owners[key].id}'",
                    task.display,
                    task.key_lines.get(("output",)),
                )
            )
        else:
            owners[key] = task


def _in_order(
    ref: PlaybookRef, tasks: list[Task], problems: list[Problem], failed: set[str]
) -> list[Task]:
    """Order tasks by dependency, ties broken by id. Report unknown ids and cycles.

    Tasks in ``failed`` exist but could not be loaded: they are already
    reported, and the order cannot be known until they are fixed.
    """
    by_id = {task.id: task for task in tasks}
    missing = bool(failed)
    for task in tasks:
        for index, dependency in enumerate(task.depends_on):
            if dependency not in by_id and dependency not in failed:
                missing = True
                message = f"depends_on: there is no task '{dependency}'"
                close = difflib.get_close_matches(dependency, list(by_id), n=1)
                if close:
                    message += f" (did you mean '{close[0]}'?)"
                problems.append(
                    Problem(
                        message, task.display, task.key_lines.get(("depends_on", index))
                    )
                )
    if missing:
        return tasks

    waiting = {task.id: len(task.depends_on) for task in tasks}
    dependents: dict[str, list[str]] = {task.id: [] for task in tasks}
    for task in tasks:
        for dependency in task.depends_on:
            dependents[dependency].append(task.id)
    ready = [task_id for task_id, count in waiting.items() if count == 0]
    heapq.heapify(ready)
    ordered = []
    while ready:
        task_id = heapq.heappop(ready)
        ordered.append(by_id[task_id])
        for dependent in dependents[task_id]:
            waiting[dependent] -= 1
            if waiting[dependent] == 0:
                heapq.heappush(ready, dependent)
    if len(ordered) < len(tasks):
        cycle = _find_cycle(
            by_id, {task_id for task_id, count in waiting.items() if count}
        )
        first = by_id[cycle[0]]
        problems.append(
            Problem(
                "depends_on: tasks depend on each other in a cycle: "
                + " → ".join(cycle),
                first.display,
                first.key_lines.get(("depends_on", first.depends_on.index(cycle[1]))),
            )
        )
        return tasks
    return ordered


def _find_cycle(by_id: Mapping[str, Task], candidates: set[str]) -> list[str]:
    # Every task left over by the topological sort is on a cycle or behind
    # one, so walking dependencies from any of them must revisit a task.
    start = min(candidates)
    path = [start]
    while True:
        following = min(dep for dep in by_id[path[-1]].depends_on if dep in candidates)
        if following in path:
            return [*path[path.index(following) :], following]
        path.append(following)
