"""The pack as the engine renders it: each task's document, and the files made
from the records (docs/spec/06-records.md, docs/spec/02-project-layout.md).

``load_documents`` gathers what is known of each task: its state, its
current text (``out/`` when a person edited it, ``.spadework/work/``
otherwise), its record and its research. The functions after it turn that
into what goes into ``out/``: each document with its ``## Sources`` section,
``sources.md``, ``pack.json``, and the sections of ``00-summary.md`` that
code writes. They are plain functions of their arguments, so the pack reads
the same whether a run or ``spadework verify`` renders it.

Text that came from the web or a model (titles, publishers, quotes, gaps)
is escaped before it goes into Markdown, so it cannot add links, HTML or
table cells of its own.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from spadework.engine.store import (
    SUMMARY_FILE,
    DraftRecord,
    Produced,
    ResearchRecord,
    SummaryRecord,
    read_draft,
    read_research,
    work_path,
)
from spadework.engine.verification import (
    BADGES,
    STATUSES,
    VerificationStatus,
    counts,
)
from spadework.playbook import Playbook, Task
from spadework.records import Source
from spadework.state import OUT_DIR, ReviewFinding, State, TaskEntry
from spadework.validators import cited_sources

SOURCES_FILE = "sources.md"
PACK_FILE = "pack.json"
PACK_SCHEMA_VERSION = 1

NOTICE = (
    "This pack is automated research. A person must review it before anyone "
    "acts on it: check its sources, and treat claims tagged `[unverified]` and "
    "sources that failed their check with care."
)
_NOT_PROOF = (
    "Only a ✓ verified source had its quote found on its page. A failed check "
    "is not proof that a source was made up: pages change, and some sites turn "
    "away automated readers."
)
_STATE_LABELS = {
    "fresh": "fresh",
    "stale": "stale",
    "invalid": "invalid",
    "failed": "failed",
    "skipped": "skipped",
    "pending": "not produced",
    "pinned": "edited by a person",
}


@dataclass(frozen=True)
class Document:
    """One task's document in the pack, and what is known about it."""

    task: Task
    state: str
    """As ``status`` reports it: fresh, stale, invalid, failed, pending,
    pinned, or skipped (in this run)."""
    entry: TaskEntry | None
    record: DraftRecord | None
    research: ResearchRecord | None
    path: Path | None
    """The document as it stands: ``out/`` when a person edited it,
    ``.spadework/work/`` otherwise. None when the task has none."""
    pinned: bool

    @property
    def output(self) -> str:
        return self.task.output

    @property
    def sources(self) -> tuple[Source, ...]:
        return () if self.research is None else tuple(self.research.result.sources)

    def text(self) -> str | None:
        if self.path is None:
            return None
        try:
            return self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    def cited(self) -> list[str]:
        """The source ids the document cites, in order of first use."""
        text = self.text()
        return [] if text is None else cited_sources(text)

    @property
    def validators_ok(self) -> bool | None:
        if self.entry is None or self.entry.output is None:
            return None
        return all(result.ok for result in self.entry.validators)


def load_documents(
    root: Path,
    playbook: Playbook,
    state: State,
    states: Mapping[str, str],
    pinned: Collection[str],
) -> list[Document]:
    """Every task's document, in the order of their file names, as a reader
    meets them.

    ``states`` gives each task's state, and ``pinned`` the tasks whose
    ``out/`` file a person edited: their text is read from there.
    """
    documents = []
    for task in playbook.tasks:
        entry = state.tasks.get(task.id)
        is_pinned = task.id in pinned
        path: Path | None = None
        if is_pinned:
            path = root / OUT_DIR / task.output
        elif entry is not None and entry.output is not None:
            path = work_path(root, task.output)
        if path is not None and (path.is_symlink() or not path.is_file()):
            path = None  # sessions are staged with regular files only
        research = None
        if task.research is not None and task.id in state.research:
            research = read_research(root, state.research[task.id].record)
        record = read_draft(root, task.id) if entry is not None else None
        documents.append(
            Document(
                task=task,
                state=states.get(task.id, "pending"),
                entry=entry,
                record=record,
                research=research,
                path=path,
                pinned=is_pinned,
            )
        )
    return sorted(documents, key=lambda document: document.output)


# Escaping.

_MARKDOWN = re.compile(r"([\\`*_\[\]<>|])")


def md(text: str) -> str:
    """Untrusted text as one line of plain Markdown."""
    return _MARKDOWN.sub(r"\\\1", " ".join(text.split()))


def md_url(url: str) -> str:
    """A URL for a Markdown link destination written as ``<…>``."""
    return "".join(
        f"%{ord(char):02X}" if char in "<>\\ " or ord(char) < 0x20 else char
        for char in url
    )


def link(title: str, url: str) -> str:
    return f"[{md(title)}](<{md_url(url)}>)"


def _row(*cells: str) -> str:
    return "| " + " | ".join(cells) + " |"


# Each document's Sources section.


def render_document(document: Document, content: bytes) -> bytes:
    """A task's output as it goes into ``out/``: with its Sources section."""
    text = content.decode("utf-8", errors="replace").rstrip("\n")
    section = sources_section(document, cited_sources(text))
    return f"{text}\n\n{section}".encode()


def sources_section(document: Document, cited: Sequence[str]) -> str:
    """``## Sources``: only the sources the document cites (docs/spec/06)."""
    lines = ["## Sources", ""]
    if document.task.research is None:
        lines.append(
            "This document has no web research of its own. Its facts come from "
            "the brief, the inputs and the other documents of the pack, which it "
            "names."
        )
        return "\n".join(lines) + "\n"
    if document.research is None:
        lines.append(
            "The record of this document's research is missing, so its sources "
            "cannot be listed. `spadework run --force --only "
            f"{document.task.id} --refresh-research` does the research again."
        )
        return "\n".join(lines) + "\n"
    known = {source.id: source for source in document.sources}
    listed = [source for source in document.sources if source.id in cited]
    unknown = [source_id for source_id in cited if source_id not in known]
    if not listed:
        found = len(document.sources)
        lines.append(
            f"This document cites none of the {_plural(found, 'source')} its "
            f"research found. They are listed in [{SOURCES_FILE}]({SOURCES_FILE})."
        )
    else:
        lines += [
            _row("Id", "Source", "Accessed", "Verification"),
            _row("---", "---", "---", "---"),
        ]
        research = document.research
        for source in listed:
            source_by = f"{link(source.title, source.url)}, {md(source.publisher)}"
            badge = BADGES[research.status(source.id)]
            lines.append(_row(source.id, source_by, source.accessed, badge))
        lines += [
            "",
            f"{_NOT_PROOF} Every source of the research, with its quote, is in "
            f"[{SOURCES_FILE}]({SOURCES_FILE}).",
        ]
    if unknown:
        lines += [
            "",
            f"Cited but not among the research's sources: {', '.join(unknown)}.",
        ]
    return "\n".join(lines) + "\n"


# sources.md


def sources_markdown(title: str, documents: Sequence[Document]) -> str:
    """``out/sources.md``: every source, grouped by document."""
    parts = [
        f"# Sources: {md(title)}",
        "Every source the research found, grouped by document, with the result "
        "of checking its quote against its page. Each document's own Sources "
        "section lists only the sources it cites. " + _NOT_PROOF,
    ]
    researched = [document for document in documents if document.research]
    if not researched:
        parts.append("No document in this pack has web research, so there are none.")
    for document in researched:
        research = document.research
        assert research is not None
        cited = set(document.cited())
        blocks = [f"## {document.output}: {md(document.task.title)}"]
        if not research.result.sources:
            blocks.append("The research found no sources.")
        for source in research.result.sources:
            result = research.result_of(source.id)
            checked = BADGES[result.status]
            if result.detail:
                checked += f" ({md(result.detail)})"
            if result.checked_at:
                checked += f", checked {result.checked_at}"
            lines = [
                f"### {source.id}: {md(source.title)}",
                "",
                f"- <{md_url(source.url)}>",
                f"- {md(source.publisher)}, accessed {source.accessed}",
                f"- Verification: {checked}",
            ]
            if result.final_url and result.final_url != source.url:
                lines.append(f"- Checked at: <{md_url(result.final_url)}>")
            lines += [
                f"- Cited in {document.output}: "
                + ("yes" if source.id in cited else "no"),
                f"- Claim: {md(source.claim)}",
                f"- Quote: “{md(source.quote)}”",
            ]
            blocks.append("\n".join(lines))
        parts.append("\n\n".join(blocks))
    return "\n\n".join(parts) + "\n"


# pack.json


def pack_document(
    *,
    project: str,
    documents: Sequence[Document],
    summary: SummaryRecord | None,
    versions: Mapping[str, str],
    generated_at: str,
) -> dict[str, Any]:
    """``out/pack.json``: the whole pack as data (docs/spec/06-records.md).

    Ids that are local to a task are qualified with its id, as ``stack:D1``.
    """
    listed: dict[str, list[dict[str, Any]]] = {
        name: []
        for name in (
            "documents",
            "decisions",
            "open_questions",
            "risks",
            "assumptions",
            "gaps",
            "sources",
        )
    }
    for document in documents:
        task_id = document.task.id
        result = None if document.record is None else document.record.result
        listed["documents"].append(
            {
                "task": task_id,
                "title": document.task.title,
                "file": document.output,
                "state": document.state,
                "status": None if result is None else result.status,
                "validators_ok": document.validators_ok,
                "summary": None if result is None else result.summary,
                "key_facts": [] if result is None else list(result.key_facts),
            }
        )
        if result is not None:
            for decision in result.decisions:
                listed["decisions"].append(
                    {
                        "id": f"{task_id}:{decision.id}",
                        "task": task_id,
                        **decision.model_dump(exclude={"id"}),
                    }
                )
            for question in result.open_questions:
                listed["open_questions"].append(
                    {
                        "id": f"{task_id}:{question.id}",
                        "task": task_id,
                        **question.model_dump(exclude={"id"}),
                    }
                )
            for risk in result.risks:
                listed["risks"].append({"task": task_id, **risk.model_dump()})
            for assumption in result.assumptions:
                listed["assumptions"].append(
                    {"task": task_id, "assumption": assumption}
                )
            for gap in result.gaps:
                listed["gaps"].append({"task": task_id, "gap": gap})
        if document.research is not None:
            cited = set(document.cited())
            for source in document.research.result.sources:
                check = document.research.result_of(source.id)
                listed["sources"].append(
                    {
                        "id": f"{task_id}:{source.id}",
                        "task": task_id,
                        **source.model_dump(exclude={"id"}),
                        "cited": source.id in cited,
                        "verification": check.status,
                        "verification_detail": check.detail,
                        "checked_at": check.checked_at,
                        "final_url": check.final_url,
                    }
                )
    known = {question["id"] for question in listed["open_questions"]}
    consolidated = []
    if summary is not None and summary.result is not None:
        for merged in summary.result.open_questions:
            consolidated.append(
                {
                    "question": merged.question,
                    "why": merged.why,
                    "from": [qid for qid in merged.from_ if qid in known],
                }
            )
    return {
        "schema_version": PACK_SCHEMA_VERSION,
        "project": project,
        "generated_at": generated_at,
        "versions": dict(versions),
        "documents": listed["documents"],
        "decisions": listed["decisions"],
        "open_questions": listed["open_questions"],
        "consolidated_questions": consolidated,
        "risks": listed["risks"],
        "assumptions": listed["assumptions"],
        "gaps": listed["gaps"],
        "sources": listed["sources"],
    }


# 00-summary.md


def summary_markdown(
    *,
    title: str,
    record: SummaryRecord | None,
    prose: str | None,
    documents: Sequence[Document],
) -> str:
    """``out/00-summary.md``: the session's prose, then what code writes."""
    if record is not None and record.result is not None and prose is not None:
        head = prose.rstrip("\n")
    elif record is not None and record.pending:
        head = (
            f"# {md(title)}\n\nThe summary of this research pack.\n\n"
            "The last run stopped before it finished, so this page has only the "
            "parts that code writes. The prose summary is written when a run "
            "finishes."
        )
    else:
        why = "" if record is None or not record.error else f" ({md(record.error)})"
        head = (
            f"# {md(title)}\n\nThe summary of this research pack.\n\n"
            f"The summary session did not finish{why}, so this page has only the "
            "parts that code writes. The next `spadework run` writes it again."
        )
    parts = [
        head,
        documents_section(documents),
        verification_section(documents),
        pinned_section(() if record is None else record.pinned_findings),
        produced_section(None if record is None else record.produced),
    ]
    return "\n\n".join(part for part in parts if part) + "\n"


def documents_section(documents: Sequence[Document]) -> str:
    lines = [
        "## Documents",
        "",
        _row("Document", "State", "Checks", "Gaps"),
        _row("---", "---", "---", "---"),
    ]
    gaps: list[str] = []
    for document in documents:
        name = f"{document.output}: {md(document.task.title)}"
        if document.path is not None:
            name = f"[{document.output}]({document.output}): {md(document.task.title)}"
        state = _STATE_LABELS.get(document.state, document.state)
        if document.path is None and document.state != "pending":
            state += ", no document"
        elif document.record is not None and document.record.result.status == "partial":
            # The session said part of its task could not be done (its gaps).
            state += ", partial"
        checks = "—"
        if document.pinned:
            checks = "— (edited by a person)"
        elif document.entry is not None and document.entry.output is not None:
            failed = [r.name for r in document.entry.validators if not r.ok]
            checks = "failed: " + ", ".join(failed) if failed else "pass"
        found = [] if document.record is None else document.record.result.gaps
        gaps += [f"- {document.output}: {md(gap)}" for gap in found]
        lines.append(_row(name, state, checks, str(len(found)) if found else "—"))
    if gaps:
        lines += ["", "### Gaps", "", *gaps]
    return "\n".join(lines)


def verification_section(documents: Sequence[Document]) -> str:
    researched = [document for document in documents if document.research]
    lines = ["## Source verification", ""]
    if not researched:
        lines.append(
            "No document in this pack has web research, so there are no sources "
            "to verify."
        )
        return "\n".join(lines)
    lines += [
        _row("Document", "Sources", *(BADGES[status] for status in STATUSES)),
        _row(*["---"] * (2 + len(STATUSES))),
    ]
    everything: list[VerificationStatus] = []
    for document in researched:
        found = statuses_of(document)
        everything += found
        lines.append(_counts_row(document.output, found))
    if len(researched) > 1:
        lines.append(_counts_row("All documents", everything))
    lines += [
        "",
        f"{_NOT_PROOF} Every source is listed in [{SOURCES_FILE}]({SOURCES_FILE}).",
    ]
    flagged = [
        document.output
        for document in researched
        if document.research is not None and document.research.guard_denials
    ]
    if flagged:
        lines += [
            "",
            "**Possible prompt injection encountered** in the research for "
            f"{', '.join(flagged)}: the sandbox blocked calls that a web page may "
            "have asked for. Review those documents and their sources with extra "
            "care.",
        ]
    return "\n".join(lines)


def statuses_of(document: Document) -> list[VerificationStatus]:
    research = document.research
    if research is None:
        return []
    return [research.status(source.id) for source in research.result.sources]


def _counts_row(name: str, statuses: Sequence[VerificationStatus]) -> str:
    found = counts(statuses)
    return _row(name, str(len(statuses)), *(str(found[s]) for s in STATUSES))


def pinned_section(findings: Iterable[ReviewFinding]) -> str:
    listed = list(findings)
    if not listed:
        return ""
    lines = [
        "## Review findings for a person",
        "",
        "A person edited these documents, so the review did not change them. "
        "These findings are for that person to act on.",
        "",
        _row("Document", "Kind", "Severity", "Finding"),
        _row("---", "---", "---", "---"),
    ]
    seen: set[tuple[str, str]] = set()
    for finding in listed:
        if (finding.target, finding.instruction) in seen:
            continue
        seen.add((finding.target, finding.instruction))
        lines.append(
            _row(
                finding.target, finding.kind, finding.severity, md(finding.instruction)
            )
        )
    return "\n".join(lines)


def produced_section(produced: Produced | None) -> str:
    lines = ["## How this pack was produced", ""]
    if produced is not None:
        runs = _plural(produced.runs, "run")
        lines += [
            f"- Date: {produced.date} (UTC)",
            f"- spadework {produced.spadework}, playbook "
            f"{produced.playbook.replace('@', ' ')}, executor "
            f"{produced.executor.replace('@', ' ')}",
            f"- Cost: ${produced.cost_usd:.2f} in total over {runs}, "
            f"${produced.run_cost_usd:.2f} of it in the run that wrote this summary"
            + (
                ", all simulated by the fake executor: no model was called and "
                "nothing was spent"
                if produced.simulated
                else ""
            ),
        ]
    lines.append(f"- {NOTICE}")
    return "\n".join(lines)


def summary_prose(root: Path) -> str | None:
    try:
        return work_path(root, SUMMARY_FILE).read_text(encoding="utf-8")
    except OSError:
        return None


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"
