"""Rendering the pack into ``out/``.

At the end of a run, each task's current output is copied from
``.spadework/work/`` into ``out/``, and the hash of what was written is
recorded in ``state.json``: a later change to the file is a person's edit,
and pins the task (docs/spec/02-project-layout.md, Human edits).

- An edited file is never overwritten, unless ``--force --only`` redrafted
  its task in this run and recorded a new output for it.
- A deleted file is not an edit: it is written again.
- A file the engine has no record of is left alone with a warning, unless it
  already holds exactly what would be written.

The ``## Sources`` sections, ``sources.md``, ``pack.json`` and
``00-summary.md`` come with the review loop and the summary (M4).
"""

from __future__ import annotations

import contextlib
import os
import secrets
from collections.abc import Collection
from dataclasses import dataclass, field
from pathlib import Path

from spadework.engine.store import read_work
from spadework.keys import hash_bytes
from spadework.playbook import Playbook
from spadework.state import OUT_DIR, STATE_DIR, WORK_DIR, State, file_hash


@dataclass
class Rendered:
    written: list[str] = field(default_factory=list)
    """Files written, relative to the project, such as ``out/01-scope.md``."""
    warnings: list[tuple[str, str]] = field(default_factory=list)


def render_pack(
    root: Path,
    playbook: Playbook,
    state: State,
    *,
    replace: Collection[str] = (),
) -> Rendered:
    """Render every task that has an output. Updates ``state``; the caller saves it.

    ``replace`` names the tasks whose edited file may be overwritten: those
    that ``--force --only`` redrafted in this run, with a new output recorded.
    """
    rendered = Rendered()
    out = root / OUT_DIR
    for task in playbook.tasks:
        entry = state.tasks.get(task.id)
        if entry is None or entry.output is None:
            continue
        shown = f"{OUT_DIR}/{task.output}"
        content = read_work(root, task.output)
        if content is None:
            rendered.warnings.append(
                (
                    f"cannot render {shown}: {STATE_DIR}/{WORK_DIR}/{task.output} "
                    "is missing",
                    f"Run `spadework run --force --only {task.id}` to redraft it.",
                )
            )
            continue
        digest = hash_bytes(content)
        path = out / task.output
        current = file_hash(path)
        last = entry.rendered
        if current is not None and current != digest:
            if last is None:
                rendered.warnings.append(
                    (
                        f"{shown} was not written by spadework, so it was left as "
                        "it is",
                        f"Move it aside or delete it, and run again to render "
                        f"task {task.id!r}.",
                    )
                )
                continue
            if current != last.hash and task.id not in replace:
                continue  # edited by a person: pinned
        if current != digest:
            write_file(path, content)
            rendered.written.append(shown)
        if last is None or last.hash != digest or last.path != task.output:
            state.record_render(task.id, path=task.output, digest=digest)
    rendered.written.sort()
    return rendered


def adopt_rendered(root: Path, state: State) -> None:
    """Record the files in ``out/`` that hold their task's own output.

    A run that stopped while rendering may have written a file without
    recording its hash. Its bytes are the engine's, not a person's edit.
    Updates ``state``; the caller saves it.
    """
    for task_id, entry in list(state.tasks.items()):
        rendered, output = entry.rendered, entry.output
        if rendered is None or output is None or rendered.hash == output.hash:
            continue
        if file_hash(root / OUT_DIR / rendered.path) == output.hash:
            state.record_render(task_id, path=rendered.path, digest=output.hash)


def render_pending(root: Path, playbook: Playbook, state: State) -> bool:
    """Whether an output is missing from ``out/``, so a run should render it."""
    out = root / OUT_DIR
    for task in playbook.tasks:
        entry = state.tasks.get(task.id)
        missing = file_hash(out / task.output) is None
        if entry is not None and entry.output is not None and missing:
            return True
    return False


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
