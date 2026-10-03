"""``spadework clean``: remove engine state.

See docs/spec/01-cli.md. Only ``.spadework/`` is touched: never
``spadework.toml``, ``brief.md``, ``inputs/`` or ``out/``.

- ``runs``: ``.spadework/runs/``, every run's manifest and session logs.
- ``cache``: the research records, ``.spadework/records/*.research.json``,
  so that the next run researches again.
- ``all``: the whole of ``.spadework/``.

The project lock is held while files are removed, so a run cannot start
half-way through, and a run that holds it stops ``clean`` (exit code 7).

Nothing outside the project is ever removed. A ``.spadework`` that is a link,
or that resolves outside the project, is refused before the lock is taken
(``check_state``), and no link inside it is followed: a link is removed as
a link.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from spadework.errors import InvalidError
from spadework.lock import LOCK_FILE, ProjectLock

STATE_DIR = ".spadework"
RUNS_DIR = "runs"
RECORDS_DIR = "records"
RESEARCH_SUFFIX = ".research.json"

What = Literal["runs", "cache", "all"]


@dataclass(frozen=True)
class Target:
    """One file or folder that ``clean`` removes."""

    path: Path
    relative: str
    """Relative to the project, with a '/' after a folder."""
    files: int
    size: int


@dataclass(frozen=True)
class CleanPlan:
    root: Path
    what: What
    targets: tuple[Target, ...]
    detail: str
    """What the targets hold, such as "3 runs" or "2 research records"."""

    @property
    def size(self) -> int:
        return sum(target.size for target in self.targets)

    @property
    def empty(self) -> bool:
        return not self.targets


def check_state(root: Path, shown: str = STATE_DIR) -> None:
    """Refuse a ``.spadework`` that ``clean`` cannot remove from safely.

    ``shown`` names it in messages. Raises InvalidError (exit code 3) when it
    is a link, resolves outside the project, or is not a folder. Called
    before the project lock is taken, since the lock is a file in it.
    """
    state = root / STATE_DIR
    mode = _mode(state)
    if mode is None:
        return
    hint = (
        f"spadework keeps its state in a {STATE_DIR} folder inside the project, "
        "and clean removes nothing outside it. Remove or move it yourself."
    )
    if stat.S_ISLNK(mode):
        raise InvalidError(f"{shown} is a link, so clean leaves it alone", hint)
    if not stat.S_ISDIR(mode):
        raise InvalidError(f"{shown} is not a folder, so clean leaves it alone", hint)
    real_root = Path(os.path.realpath(root))
    if not Path(os.path.realpath(state)).is_relative_to(real_root):
        raise InvalidError(
            f"{shown} resolves outside the project, so clean leaves it alone", hint
        )


def plan_clean(root: Path, what: What) -> CleanPlan:
    """What ``clean`` would remove from the project at ``root``, which
    ``check_state`` has passed."""
    state = root / STATE_DIR
    targets: list[Target] = []
    detail = ""
    if what == "all":
        if _mode(state) is not None:
            targets.append(_target(state, f"{STATE_DIR}/"))
            detail = _count(_runs(state), "run")
    elif what == "runs":
        runs = state / RUNS_DIR
        if _mode(runs) is not None:
            targets.append(_target(runs, f"{STATE_DIR}/{RUNS_DIR}/"))
            detail = _count(_runs(state), "run")
    else:
        records = state / RECORDS_DIR
        if _is_folder(records):
            for name in sorted(os.listdir(records)):
                if name.endswith(RESEARCH_SUFFIX):
                    path = records / name
                    targets.append(_target(path, f"{STATE_DIR}/{RECORDS_DIR}/{name}"))
        detail = _count(len(targets), "research record")
    return CleanPlan(root, what, tuple(targets), detail)


def clean(plan: CleanPlan, lock: ProjectLock) -> None:
    """Remove what ``plan`` names. The caller holds ``lock``.

    With ``all``, everything in ``.spadework/`` but the lock goes first; the
    lock is released, then the folder is removed if nothing new is in it.
    """
    check_state(plan.root)  # again: it may have changed since the plan
    state = plan.root / STATE_DIR
    for target in plan.targets:
        if target.path == state:
            for entry in sorted(os.listdir(state)):
                if entry != LOCK_FILE:
                    _remove(state / entry)
            lock.release()
            with contextlib.suppress(OSError):
                state.rmdir()  # a run that started since keeps its lock
        else:
            _remove(target.path)


def human_size(size: int) -> str:
    """A size in bytes, in the decimal units that inputs/ uses too."""
    if size < 1000:
        return f"{size} byte{'' if size == 1 else 's'}"
    amount = size / 1000
    for unit in ("KB", "MB"):
        if amount < 1000:
            return f"{amount:.1f} {unit}"
        amount /= 1000
    return f"{amount:.1f} GB"


def _target(path: Path, relative: str) -> Target:
    files, size = 0, 0
    for file in _files(path):
        files += 1
        with contextlib.suppress(OSError):
            size += file.lstat().st_size
    return Target(path, relative, files, size)


def _files(path: Path) -> Iterator[Path]:
    """Every file under ``path``, links included but not followed."""
    mode = _mode(path)
    if mode is None:
        return
    if not stat.S_ISDIR(mode):
        yield path
        return
    # os.walk does not follow links to folders: they are listed as files.
    for current, folders, names in os.walk(path):
        for name in [*names, *(f for f in folders if Path(current, f).is_symlink())]:
            yield Path(current, name)


def _mode(path: Path) -> int | None:
    """The mode of ``path`` itself, not of what a link points to; None if
    there is nothing there."""
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return None


def _is_folder(path: Path) -> bool:
    """Whether ``path`` is a folder, and not a link to one."""
    mode = _mode(path)
    return mode is not None and stat.S_ISDIR(mode)


def _runs(state: Path) -> int:
    runs = state / RUNS_DIR
    if not _is_folder(runs):
        return 0
    with os.scandir(runs) as entries:
        return sum(1 for entry in entries if entry.is_dir(follow_symlinks=False))


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _remove(path: Path) -> None:
    """Remove ``path``. A link is removed, never what it points to; rmtree
    does not follow the links inside a folder either."""
    if _is_folder(path):
        shutil.rmtree(path)
        return
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
