"""Rendering playbook templates in a sandbox.

Task bodies and research questions are Jinja2 templates, rendered with
``SandboxedEnvironment`` and ``StrictUndefined``. The only loader reads files
inside the playbook directory (invariant I8).

Research questions get their own context, built by ``research_context``
from ``public_context``, the task, and ``brief.language``, and from nothing
else. They are never given the brief (invariant I5). Every render uses a
fresh environment with no template cache, so nothing a task body stashes in
an imported template can reach a later render.
"""

from __future__ import annotations

import datetime
import json
import traceback
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jinja2 import (
    BaseLoader,
    StrictUndefined,
    TemplateError,
    TemplateNotFound,
    TemplateSyntaxError,
    UndefinedError,
)
from jinja2.environment import Environment
from jinja2.sandbox import SandboxedEnvironment

from spadework.brief import Brief
from spadework.errors import InvalidError, Problem
from spadework.inputs import InputFile, first_heading, match_inputs
from spadework.playbook import Playbook, PlaybookRef, Task

RESEARCH_VARIABLES = "public_context, task and brief.language"
TEMPLATE_HINT = (
    "Fix the template. Task bodies can use brief, task, project, files, deps and "
    f"research. Research questions can use only {RESEARCH_VARIABLES}."
)
_MISSING_BRIEF_KEY = "brief.md has no front matter key"


class Namespace(Mapping[str, Any]):
    """A read-only template variable.

    In templates, its keys win over its method names: ``brief.extra.items``
    is the brief's ``items`` field (see ``_Environment.getattr``). The label
    names the variable in error messages; templates cannot read it, because
    the sandbox hides attributes that start with an underscore.
    """

    __slots__ = ("_label", "_values")
    _label: str
    _values: dict[str, Any]

    def __init__(self, label: str, values: Mapping[str, Any]) -> None:
        object.__setattr__(self, "_label", label)
        object.__setattr__(self, "_values", dict(values))

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(f"{self._label} is read-only")

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:
        return f"Namespace({self._label!r}, {self._values!r})"


class _Undefined(StrictUndefined):
    @property
    def _undefined_message(self) -> str:
        owner = self._undefined_obj
        name = self._undefined_name
        # A hint is the sandbox saying why it refused; keep it.
        if self._undefined_hint or not isinstance(owner, Namespace) or name is None:
            return str(super()._undefined_message)
        label = owner._label
        if label == "brief.extra":
            return f"{_MISSING_BRIEF_KEY} '{name}' (used as brief.extra.{name})"
        if label == "deps":
            return f"'{name}' is not in this task's depends_on (used as deps.{name})"
        if label == "research brief":
            return (
                f"brief.{name} is not available to research questions, which can use "
                f"only {RESEARCH_VARIABLES}"
            )
        return f"{label} has no '{name}'"


class PlaybookLoader(BaseLoader):
    """Loads ``{% include %}`` and friends from inside the playbook directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.real_root = root.resolve()

    def get_source(
        self, environment: Environment, template: str
    ) -> tuple[str, str, Callable[[], bool]]:
        parts = template.split("/")
        if (
            template.startswith("/")
            or "\\" in template
            or any(not part or part.startswith(".") for part in parts)
        ):
            raise TemplateNotFound(template)
        path = self.root.joinpath(*parts)
        try:
            # RuntimeError: a symlink loop on Python 3.11 and 3.12.
            # ValueError: a NUL byte in the name.
            resolved = path.resolve(strict=True)
            if not resolved.is_relative_to(self.real_root) or not resolved.is_file():
                raise TemplateNotFound(template)
            source = resolved.read_text(encoding="utf-8")
        except (OSError, RuntimeError, ValueError):
            raise TemplateNotFound(template) from None
        return source, str(path), lambda: True


class _Environment(SandboxedEnvironment):
    def getattr(self, obj: Any, attribute: str) -> Any:
        # Keys win over method names, so a brief key called `items` is not
        # shadowed by Mapping.items. Filters still get the Mapping methods.
        if isinstance(obj, Namespace) and attribute in obj:
            return obj[attribute]
        return super().getattr(obj, attribute)


def _json_default(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def _json_dumps(value: Any, **kwargs: Any) -> str:
    return json.dumps(value, default=_json_default, **kwargs)


def environment(root: Path) -> SandboxedEnvironment:
    """A new environment for one render. Never share one between renders."""
    env = _Environment(
        loader=PlaybookLoader(root),
        undefined=_Undefined,
        autoescape=False,
        trim_blocks=False,
        lstrip_blocks=False,
        keep_trailing_newline=True,
        auto_reload=False,
        # Imported templates keep their module state in the cache.
        cache_size=0,
    )
    env.policies["json.dumps_function"] = _json_dumps
    return env


# What sessions produce, as templates see it. The engine fills these in at
# run time; `check` uses placeholders of the same shape.


@dataclass(frozen=True)
class StagedFile:
    path: str
    heading: str | None


@dataclass(frozen=True)
class DependencyFacts:
    task: Task
    key_facts: tuple[str, ...]


@dataclass(frozen=True)
class Source:
    id: str
    url: str
    title: str
    publisher: str
    accessed: str
    quote: str
    claim: str
    status: str


@dataclass(frozen=True)
class ResearchFindings:
    notes: str
    sources: tuple[Source, ...]
    unanswered: tuple[str, ...]


def brief_variables(brief: Brief) -> Namespace:
    front = brief.front
    return Namespace(
        "brief",
        {
            "title": front.title,
            "reader": front.reader,
            "language": front.language,
            "currencies": list(front.currencies),
            "priorities": None if front.priorities is None else list(front.priorities),
            "public_context": front.public_context,
            "extra": _namespaced("brief.extra", brief.extra),
            "body": brief.body,
        },
    )


def _namespaced(label: str, value: Any) -> Any:
    """Turn nested mappings from YAML into namespaces, so keys win everywhere."""
    if isinstance(value, Mapping):
        return Namespace(
            label, {str(k): _namespaced(f"{label}.{k}", v) for k, v in value.items()}
        )
    if isinstance(value, list):
        return [_namespaced(label, item) for item in value]
    return value


def task_variables(task: Task) -> Namespace:
    return Namespace(
        "task",
        {
            "id": task.id,
            "title": task.title,
            "output": task.output,
            "tier": task.tier,
            "depends_on": list(task.depends_on),
            "inputs": list(task.inputs),
            "has_research": task.research is not None,
        },
    )


def task_context(
    *,
    project_name: str,
    brief: Brief,
    task: Task,
    files: Sequence[StagedFile],
    deps: Sequence[DependencyFacts],
    research: ResearchFindings | None,
) -> dict[str, Any]:
    """The variables of a task body (draft, repair and fix sessions)."""
    return {
        "brief": brief_variables(brief),
        "task": task_variables(task),
        "project": Namespace("project", {"name": project_name}),
        "files": [
            Namespace("file", {"path": f.path, "heading": f.heading}) for f in files
        ],
        "deps": Namespace(
            "deps",
            {
                dep.task.id: Namespace(
                    f"deps.{dep.task.id}",
                    {
                        "id": dep.task.id,
                        "title": dep.task.title,
                        "output": dep.task.output,
                        "key_facts": list(dep.key_facts),
                    },
                )
                for dep in deps
            },
        ),
        "research": None if research is None else _research_variables(research),
    }


def _research_variables(research: ResearchFindings) -> Namespace:
    return Namespace(
        "research",
        {
            "notes": research.notes,
            "sources": [
                Namespace(f"source {source.id}", vars(source))
                for source in research.sources
            ],
            "unanswered": list(research.unanswered),
        },
    )


def research_context(
    task: Task, *, public_context: str, language: str
) -> dict[str, Any]:
    """The variables of research questions: these three and nothing else."""
    return {
        "public_context": public_context,
        "task": task_variables(task),
        "brief": Namespace("research brief", {"language": language}),
    }


def render_task_body(playbook: Playbook, task: Task, context: Mapping[str, Any]) -> str:
    return environment(playbook.ref.root).from_string(task.body).render(context)


def render_research_questions(
    playbook: Playbook, task: Task, *, public_context: str, language: str
) -> str:
    """Render a task's research questions from public data only.

    This builds its own environment on every call and never takes one, so a
    caller cannot hand it one that a task body has already used.
    """
    if task.research is None:
        raise ValueError(f"task {task.id!r} has no research")
    context = research_context(task, public_context=public_context, language=language)
    env = environment(playbook.ref.root)
    return env.from_string(task.research.questions).render(context)


# Checking templates before a run.


def placeholder_context(
    project_name: str,
    brief: Brief,
    task: Task,
    playbook: Playbook,
    inputs: Iterable[InputFile],
) -> dict[str, Any]:
    """A task context with stand-ins for what exists only at run time."""
    deps = [playbook.task(dep) for dep in task.depends_on]
    files = [StagedFile("brief.md", first_heading(brief.body))]
    files += [
        StagedFile(file.display, file.heading)
        for file in match_inputs(task.inputs, inputs)
    ]
    files += [StagedFile(dep.output, dep.title) for dep in deps]
    research = None
    if task.research is not None:
        research = ResearchFindings(
            notes="(research notes, written at run time)",
            sources=(
                Source(
                    id="S1",
                    url="https://example.com/",
                    title="(source title)",
                    publisher="(publisher)",
                    accessed="(access date)",
                    quote="(quote)",
                    claim="(claim)",
                    status="verified",
                ),
            ),
            unanswered=(),
        )
    return task_context(
        project_name=project_name,
        brief=brief,
        task=task,
        files=files,
        deps=[
            DependencyFacts(dep, (f"(key facts from {dep.id}, written at run time)",))
            for dep in deps
        ],
        research=research,
    )


def check_templates(
    project_name: str, brief: Brief, playbook: Playbook, inputs: Sequence[InputFile]
) -> None:
    """Render every template, so that missing variables surface before a run.

    Any exception a template raises, not only a TemplateError, is a problem
    with the playbook or the brief, located as precisely as the traceback
    allows.
    """
    problems: list[Problem] = []
    hints: set[str] = set()
    public_context = brief.public_context or ""
    for task in playbook.tasks:
        context = placeholder_context(project_name, brief, task, playbook, inputs)
        try:
            render_task_body(playbook, task, context)
        except Exception as exc:
            problems.append(
                _template_problem(exc, playbook.ref, task.display, task.body_line)
            )
            hints.add(_hint_for(exc, playbook.ref))
        if task.research is not None:
            try:
                render_research_questions(
                    playbook,
                    task,
                    public_context=public_context,
                    language=brief.front.language,
                )
            except Exception as exc:
                problem = _template_problem(
                    exc,
                    playbook.ref,
                    task.display,
                    _questions_line(task),
                    research=True,
                )
                problems.append(problem)
                hints.add(_hint_for(exc, playbook.ref))
    if problems:
        raise InvalidError(
            "templates do not render",
            " ".join(sorted(hints)),
            problems=problems,
        )


@dataclass(frozen=True)
class ResearchPrompt:
    task: Task
    questions: str


@dataclass(frozen=True)
class PublicView:
    """Everything from the project that web-facing research sessions see."""

    public_context: str
    research: tuple[ResearchPrompt, ...]


def public_view(brief: Brief, playbook: Playbook) -> PublicView:
    public_context = brief.public_context or ""
    prompts = tuple(
        ResearchPrompt(
            task,
            render_research_questions(
                playbook,
                task,
                public_context=public_context,
                language=brief.front.language,
            ),
        )
        for task in playbook.research_tasks
    )
    return PublicView(public_context, prompts)


def _questions_line(task: Task) -> int | None:
    line = task.key_lines.get(("research", "questions"))
    # A block scalar (`questions: |`) starts on the line after its key.
    if line is not None and task.research and task.research.questions.endswith("\n"):
        return line + 1
    return line


def _hint_for(exc: Exception, ref: PlaybookRef) -> str:
    if isinstance(exc, UndefinedError) and str(exc).startswith(_MISSING_BRIEF_KEY):
        return "Add the key to the front matter of brief.md."
    return ref.hint if ref.kind == "built-in" else TEMPLATE_HINT


def _template_problem(
    exc: Exception,
    ref: PlaybookRef,
    display: str,
    first_line: int | None,
    *,
    research: bool = False,
) -> Problem:
    where = "research.questions" if research else "template"
    file_name: str | None = None
    line: int | None = None
    if isinstance(exc, TemplateSyntaxError):
        file_name, line = exc.filename, exc.lineno
        message = f"{where}: syntax error: {exc.message}"
    else:
        for frame in traceback.extract_tb(exc.__traceback__):
            if frame.filename == "<template>" or _inside(frame.filename, ref.root):
                file_name, line = frame.filename, frame.lineno
        if isinstance(exc, TemplateNotFound):
            message = (
                f"cannot include '{exc.name}': no such file in the playbook folder"
            )
        elif isinstance(exc, TemplateError):
            message = str(exc.message or exc)
            if (
                isinstance(exc, UndefinedError)
                and research
                and "is undefined" in message
            ):
                message += f"; research questions can use only {RESEARCH_VARIABLES}"
        else:
            message = f"{exc} ({type(exc).__name__})"
        message = f"{where}: {message}"
    if file_name and file_name != "<template>":
        # The error is in an included file.
        relative = Path(file_name).relative_to(ref.root).as_posix()
        return Problem(message, ref.display(relative), line)
    if line is None or first_line is None:
        return Problem(message, display, first_line)
    return Problem(message, display, first_line + line - 1)


def _inside(file_name: str, root: Path) -> bool:
    return Path(file_name).is_relative_to(root)
