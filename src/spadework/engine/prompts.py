"""The engine's base prompts, and the prompts of each kind of session.

See docs/spec/03-playbooks.md, Engine base prompts. The base prompts ship in
``spadework/builtin/prompts/`` and are versioned together:
``ENGINE_PROMPT_VERSION`` is a hash over every one of them, and it is part of
every task key, so changing any base prompt makes every task stale.

A playbook's ``prompts/<name>.md`` is appended to the base prompt of the same
name, as written. It is never rendered as a template.

A research session is built from public data only (invariant I5): its system
prompt is the base research prompt and the playbook's research appendix, and
its prompt holds ``public_context`` and the rendered research questions.
``research_prompt`` and ``research_system_prompt`` take nothing else, so no
brief content can reach them. The house rules name the reader, so they are
not part of it.

Prompts describe the environment ("you can write only 06-tech-stack.md");
the guard enforces it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Mapping, Sequence
from dataclasses import replace
from functools import cache
from pathlib import Path
from typing import Final, Literal

from spadework.brief import Brief
from spadework.keys import hash_json
from spadework.playbook import Playbook, Task, Validators
from spadework.render import DependencyFacts, ResearchFindings, StagedFile
from spadework.validators import ValidatorResult

WriterKind = Literal["draft", "repair"]


def prompts_dir() -> Path:
    from spadework.playbook import builtin_root

    return builtin_root().parent / "prompts"


@cache
def base_prompts() -> Mapping[str, str]:
    """Every base prompt that ships with spadework, by name."""
    directory = prompts_dir()
    return {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(directory.glob("*.md"))
    }


ENGINE_PROMPT_VERSION: Final = hash_json(dict(base_prompts()))
"""A hash over every base prompt. Part of every research and draft key."""


def _prompt(name: str, playbook: Playbook) -> str:
    """A base prompt with the playbook's appendix, appended as written."""
    text = base_prompts()[name].rstrip("\n")
    appendix = playbook.prompts.get(name)
    if not appendix:
        return text
    # Only its trailing newlines go, so that sections join with one blank line.
    return text + "\n\n" + appendix.rstrip("\n")


# Research sessions: public data only (I5).


def research_system_prompt(playbook: Playbook) -> str:
    """The research base prompt and the playbook's research appendix. Nothing else."""
    return _prompt("research", playbook)


def research_prompt(
    *,
    public_context: str,
    questions: str,
    max_searches: int,
    max_fetches: int,
    today: dt.date,
) -> str:
    """The prompt of a research session: public context, questions and limits."""
    context = public_context.strip() or "(No public description was given.)"
    calls = []
    if max_searches:
        calls.append(_count(max_searches, "web search", "web searches"))
    if max_fetches:
        calls.append(_count(max_fetches, "page fetch", "page fetches"))
    return (
        "## The project, as publicly described\n\n"
        f"{context}\n\n"
        "## Research questions\n\n"
        f"{questions.strip()}\n\n"
        "## Limits\n\n"
        f"You can make up to {' and '.join(calls)} in this session. "
        f"Today is {today.isoformat()}.\n"
    )


def _count(number: int, one: str, many: str) -> str:
    return f"{number} {one if number == 1 else many}"


# Draft and repair sessions: the house rules, the reader, and the task.


def writer_system_prompt(kind: WriterKind, playbook: Playbook, brief: Brief) -> str:
    """The house rules, the reader, and the base prompt for ``kind``."""
    return "\n\n".join(
        [_prompt("rules", playbook), _reader(brief), _prompt(kind, playbook)]
    )


def _reader(brief: Brief) -> str:
    front = brief.front
    lines = [
        "## The reader",
        "",
        front.reader.strip(),
        "",
        f"Write the pack in the language with the tag `{front.language}`.",
    ]
    if front.currencies:
        lines.append(f"Give every amount of money in {' and '.join(front.currencies)}.")
    return "\n".join(lines)


def checks_for(task: Task) -> Validators:
    """The validators the engine runs on a task's document.

    Source ids are local to a task (docs/spec/06-records.md), so a document
    without research of its own may cite none: ``citations_resolve`` is on
    for every task, and with no sources any ``[S#]`` fails it.
    """
    if task.validators.citations_resolve:
        return task.validators
    return replace(task.validators, citations_resolve=True)


def requirements(validators: Validators, *, has_research: bool) -> list[str]:
    """What the validators check, said as instructions, one line each."""
    lines = []
    if validators.required_headings:
        # One line on its own, not a bullet: it is easy to find this way.
        lines.append(f"Required headings: {', '.join(validators.required_headings)}")
        lines.append("")
    if validators.starts_with_h1:
        lines.append("- The first line is a level-1 heading (`# Title`).")
    if validators.max_lines is not None:
        lines.append(f"- At most {validators.max_lines} lines.")
    if validators.min_lines is not None:
        lines.append(f"- At least {validators.min_lines} lines.")
    if validators.required_headings:
        lines.append(
            "- Each required heading appears as a Markdown heading, with that name."
        )
    if validators.citations_resolve and has_research:
        lines.append(
            "- Every source id you cite is one of this document's sources, "
            "listed below."
        )
    elif validators.citations_resolve:
        lines.append(
            "- No `[S#]` citations: this document has no sources of its own. "
            "Attribute a fact from another document by its file name."
        )
    if validators.min_sources is not None:
        lines.append(
            f"- At least {validators.min_sources} distinct sources cited in the text."
        )
    if validators.max_open_questions is not None:
        lines.append(
            f"- At most {validators.max_open_questions} open questions in your result."
        )
    phrases = ", ".join(f'"{phrase}"' for phrase in validators.forbidden_phrases)
    lines.append(f"- None of these phrases: {phrases}.")
    return lines


def draft_prompt(
    task: Task,
    body: str,
    *,
    files: Sequence[StagedFile],
    deps: Sequence[DependencyFacts],
    research: ResearchFindings | None,
    pinned: Collection[str] = (),
) -> str:
    """The prompt of a draft session: the task, then what the engine staged.

    ``body`` is the rendered task body. ``pinned`` names the dependencies
    whose document a person edited: their files are used as they stand, and
    their key facts, which describe the engine's version, are left out.
    """
    parts = [f"# {task.title}", body.strip()]
    checks = requirements(checks_for(task), has_research=research is not None)
    parts.append("## Checked in code when you finish\n\n" + "\n".join(checks))
    parts.append(_files_section(files, deps, pinned))
    facts = _facts_section([dep for dep in deps if dep.task.id not in pinned])
    if facts:
        parts.append(facts)
    if research is not None:
        parts.append(_research_section(research))
    parts.append(
        "## Your output\n\n"
        f"Write the document to `{task.output}` in your working directory. It is "
        "the only file you can write. Then return your result."
    )
    return "\n\n".join(part for part in parts if part) + "\n"


def repair_prompt(task: Task, draft: str, failures: Sequence[ValidatorResult]) -> str:
    """The draft prompt, plus what failed. The draft is in the output file."""
    listed = "\n".join(
        f"- `{result.name}`: {result.message or 'failed'}" for result in failures
    )
    return (
        draft.rstrip("\n") + "\n\n## What to repair\n\n"
        f"`{task.output}` holds the draft of this document. These checks failed:\n\n"
        f"{listed}\n\n"
        f"Edit `{task.output}` so that they pass, and change nothing else. Then "
        "return your result for the document as it now stands.\n"
    )


def retry_prompt(prompt: str, problem: str) -> str:
    """The prompt again, after a session whose result was rejected."""
    return (
        prompt.rstrip("\n") + "\n\n## Your last attempt was rejected\n\n"
        f"{problem}. Do the task again from the start, and return a result that "
        "matches the schema exactly.\n"
    )


def _files_section(
    files: Sequence[StagedFile],
    deps: Sequence[DependencyFacts],
    pinned: Collection[str],
) -> str:
    outputs = {dep.task.output: dep for dep in deps}
    lines = ["## Your working directory", ""]
    for file in files:
        what = ""
        if file.path == "brief.md":
            what = "the brief"
        elif file.path.startswith("inputs/"):
            what = "an input"
        elif file.path in outputs:
            dep = outputs[file.path]
            what = f"the {dep.task.title} document"
            if dep.task.id in pinned:
                what += ", edited by a person: use it as it stands"
        elif file.path == RESEARCH_NOTES:
            what = "the research notes"
        elif file.path == RESEARCH_SOURCES:
            what = "the sources, with their quotes and verification status"
        heading = f' ("{file.heading}")' if file.heading else ""
        lines.append(f"- `{file.path}`: {what}{heading}")
    return "\n".join(lines)


def _facts_section(deps: Sequence[DependencyFacts]) -> str:
    blocks = []
    for dep in deps:
        facts = "\n".join(f"- {fact}" for fact in dep.key_facts) or "- (none)"
        blocks.append(f"### {dep.task.output}\n\n{facts}")
    if not blocks:
        return ""
    return "## Key facts from the documents this one builds on\n\n" + "\n\n".join(
        blocks
    )


RESEARCH_NOTES = "research/notes.md"
RESEARCH_SOURCES = "research/sources.md"


def _research_section(research: ResearchFindings) -> str:
    lines = ["## Research", "", research.notes.strip() or "(No notes.)", ""]
    lines += ["### Sources", ""]
    if research.sources:
        lines += [
            "| Id | Title | Publisher | Accessed | Verification |",
            "|---|---|---|---|---|",
        ]
        for source in research.sources:
            cells = [
                source.id,
                _cell(source.title),
                _cell(source.publisher),
                source.accessed,
                source.status,
            ]
            lines.append("| " + " | ".join(cells) + " |")
        lines += [
            "",
            f"Each source's URL, quote and claim are in `{RESEARCH_SOURCES}`. Only "
            "`verified` sources prove a claim by themselves.",
        ]
    else:
        lines.append("The research found no sources.")
    if research.unanswered:
        lines += ["", "### Not answered by the research", ""]
        lines += [f"- {question}" for question in research.unanswered]
    return "\n".join(lines)


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def notes_file(research: ResearchFindings) -> str:
    """``research/notes.md``, as staged for a draft or repair session."""
    text = f"# Research notes\n\n{research.notes.strip()}\n"
    if research.unanswered:
        text += "\n## Not answered by the research\n\n"
        text += "".join(f"- {question}\n" for question in research.unanswered)
    return text


def sources_file(research: ResearchFindings) -> str:
    """``research/sources.md``: every source with its verification status."""
    blocks = ["# Sources"]
    for source in research.sources:
        blocks.append(
            f"## {source.id}: {source.title}\n\n"
            f"- URL: {source.url}\n"
            f"- Publisher: {source.publisher}\n"
            f"- Accessed: {source.accessed}\n"
            f"- Verification: {source.status}\n"
            f"- Claim: {source.claim}\n"
            f"- Quote: {source.quote}"
        )
    if not research.sources:
        blocks.append("The research found no sources.")
    return "\n\n".join(blocks) + "\n"
