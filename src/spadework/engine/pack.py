"""Rendering the pack into ``out/``.

At the end of a run, each task's current output is copied from
``.spadework/work/`` into ``out/`` with its ``## Sources`` section, and
``sources.md``, ``pack.json`` and ``00-summary.md`` are written from the
records (``documents``). The hash of each file written is recorded in
``state.json``: a later change to it is a person's edit
(docs/spec/02-project-layout.md, Human edits).

- An edited document is never overwritten, unless ``--force --only``
  redrafted its task in this run and recorded a new output for it. Its task
  is pinned. An edited ``00-summary.md``, ``sources.md`` or ``pack.json`` is
  left as it is too, with a warning.
- A deleted file is not an edit: it is written again.
- A file the engine has no record of is left alone with a warning, unless it
  already holds exactly what would be written.

Before any file is written, the hash of what is about to be written is saved
(``state.rendering``). A render cut short can then leave a file holding the
engine's bytes without its hash recorded, and that file is still the
engine's own, never mistaken for a person's edit.
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from spadework.engine.documents import (
    PACK_FILE,
    SOURCES_FILE,
    Document,
    load_documents,
    pack_json,
    render_document,
    sources_markdown,
    summary_markdown,
    summary_prose,
)
from spadework.engine.store import SUMMARY_FILE, read_summary, read_work
from spadework.keys import hash_bytes
from spadework.project import Project
from spadework.state import OUT_DIR, STATE_DIR, WORK_DIR, State, file_hash

PACK_FILES = (SUMMARY_FILE, SOURCES_FILE, PACK_FILE)
"""The files in ``out/`` that belong to no task."""


@dataclass
class Rendered:
    written: list[str] = field(default_factory=list)
    """Files written, relative to the project, such as ``out/01-scope.md``."""
    kept: list[str] = field(default_factory=list)
    """Documents a person edited (their task is pinned), left as they are."""
    warnings: list[tuple[str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class _File:
    name: str
    """Relative to ``out/``."""
    content: bytes
    task: str | None
    """None for a pack file."""

    @property
    def digest(self) -> str:
        return hash_bytes(self.content)


def render_pack(
    project: Project,
    state: State,
    *,
    states: Mapping[str, str],
    pinned: Collection[str],
    versions: Mapping[str, str],
    generated_at: str,
    save: Callable[[], None],
    replace: Collection[str] = (),
) -> Rendered:
    """Render every document that has an output, and the pack's own files.

    ``states`` gives each task's state for the summary and ``pack.json``,
    ``pinned`` the tasks whose document a person edited, and ``versions``
    the versions for ``pack.json`` when no summary recorded them. ``replace``
    names the pinned tasks whose file may be overwritten: those that
    ``--force --only`` redrafted in this run, with a new output recorded.
    Updates ``state`` and saves it once before writing; the caller saves it
    after.
    """
    root = project.root
    playbook = project.playbook
    rendered = Rendered()
    documents = load_documents(
        root, playbook, state, states, set(pinned) - set(replace)
    )
    files: list[_File] = []
    for document in documents:
        entry = state.tasks.get(document.task.id)
        if entry is None or entry.output is None:
            continue
        task_id = document.task.id
        content = read_work(root, document.output)
        if content is None:
            rendered.warnings.append(
                (
                    f"cannot render {OUT_DIR}/{document.output}: "
                    f"{STATE_DIR}/{WORK_DIR}/{document.output} is missing",
                    f"Run `spadework run --force --only {task_id}` to redraft it.",
                )
            )
            continue
        files.append(
            _File(document.output, render_document(document, content), task_id)
        )
    if files or state.files:
        files += _pack_files(project, documents, versions, generated_at)

    to_write: list[_File] = []
    to_record: list[_File] = []
    for item in files:
        verdict = _verdict(root, state, item, replace)
        if verdict == "write":
            to_write.append(item)
        elif verdict == "record":
            to_record.append(item)
        elif verdict == "kept":
            if item.task is not None:
                rendered.kept.append(f"{OUT_DIR}/{item.name}")
            else:
                rendered.warnings.append(
                    (
                        f"{OUT_DIR}/{item.name} was edited, so it was not updated",
                        "Move it aside or delete it, and run `spadework run` or "
                        "`spadework verify` to get the new one.",
                    )
                )
        else:
            rendered.warnings.append(
                (
                    f"{OUT_DIR}/{item.name} was not written by spadework, so it was "
                    "left as it is",
                    "Move it aside or delete it, and run again to render it.",
                )
            )
    if to_write:
        state.rendering = {item.name: item.digest for item in to_write}
        save()
        for item in to_write:
            write_file(root / OUT_DIR / item.name, item.content)
            rendered.written.append(f"{OUT_DIR}/{item.name}")
    for item in [*to_write, *to_record]:
        _record(state, item)
    state.rendering = {}
    rendered.written.sort()
    return rendered


def _pack_files(
    project: Project,
    documents: list[Document],
    versions: Mapping[str, str],
    generated_at: str,
) -> list[_File]:
    root = project.root
    title = project.brief.front.title
    summary = read_summary(root)
    if summary is not None:
        produced = summary.produced
        versions = {
            "spadework": produced.spadework,
            "playbook": produced.playbook,
            "executor": produced.executor,
        }
    files = [
        _File(SOURCES_FILE, sources_markdown(title, documents).encode(), None),
        _File(
            PACK_FILE,
            pack_json(
                project=project.config.project.name,
                documents=documents,
                summary=summary,
                versions=versions,
                generated_at=generated_at,
            ).encode(),
            None,
        ),
    ]
    if summary is not None:
        text = summary_markdown(
            title=title,
            record=summary,
            prose=summary_prose(root),
            documents=documents,
        )
        files.insert(0, _File(SUMMARY_FILE, text.encode(), None))
    return files


def _verdict(root: Path, state: State, item: _File, replace: Collection[str]) -> str:
    """write, record (it already holds these bytes), kept (edited by a
    person), or unknown (the engine never wrote it)."""
    current = file_hash(root / OUT_DIR / item.name)
    digest = item.digest
    if current == digest:
        return "record"
    if current is None:
        return "write"
    last = _last(state, item)
    if last is None:
        return "unknown"
    if current != last and item.task not in replace:
        return "kept"
    return "write"


def _last(state: State, item: _File) -> str | None:
    if item.task is None:
        return state.files.get(item.name)
    entry = state.tasks.get(item.task)
    if entry is None or entry.rendered is None:
        return None
    return entry.rendered.hash


def _record(state: State, item: _File) -> None:
    if item.task is None:
        state.files[item.name] = item.digest
        return
    entry = state.tasks[item.task]
    if entry.rendered is None or entry.rendered.hash != item.digest:
        state.record_render(item.task, path=item.name, digest=item.digest)


def adopt_rendered(project: Project, state: State) -> None:
    """Record the files in ``out/`` that a render cut short left behind.

    Their bytes are the engine's, as ``state.rendering`` says, not a
    person's edit. Updates ``state``; the caller saves it.
    """
    if not state.rendering:
        return
    root = project.root
    for task in project.playbook.tasks:
        entry = state.tasks.get(task.id)
        if entry is None:
            continue
        name = task.output if entry.rendered is None else entry.rendered.path
        expected = state.rendering.get(name)
        if expected is not None and file_hash(root / OUT_DIR / name) == expected:
            state.record_render(task.id, path=name, digest=expected)
    for name in PACK_FILES:
        expected = state.rendering.get(name)
        if expected is not None and file_hash(root / OUT_DIR / name) == expected:
            state.files[name] = expected
    state.rendering = {}


def render_pending(project: Project, state: State) -> bool:
    """Whether a file of the pack is missing from ``out/``, so a run should
    render it."""
    out = project.root / OUT_DIR
    produced = False
    for task in project.playbook.tasks:
        entry = state.tasks.get(task.id)
        if entry is None or entry.output is None:
            continue
        produced = True
        if file_hash(out / task.output) is None:
            return True
    if not produced:
        return False
    names = [SOURCES_FILE, PACK_FILE]
    if SUMMARY_FILE in state.files:
        names.append(SUMMARY_FILE)
    return any(file_hash(out / name) is None for name in names)


def write_file(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` in one step, with the user's usual permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(temporary, flags, 0o666)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            temporary.unlink()
        raise
