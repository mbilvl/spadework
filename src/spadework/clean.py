"""``spadework clean``: remove engine state.

See docs/spec/01-cli.md. Only ``.spadework/`` is touched: never
``spadework.toml``, ``brief.md``, ``inputs/`` or ``out/``.

- ``runs``: ``.spadework/runs/``, every run's manifest and session logs.
- ``cache``: the research records, ``.spadework/records/*.research.json``,
  so that the next run researches again.
- ``all``: the whole of ``.spadework/``.

The project lock is held while files are removed, so a run cannot start
half-way through, and a run that holds it stops ``clean`` (exit code 7).
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


def plan_clean(root: Path, what: What) -> CleanPlan:
    """What ``clean`` would remove from the project at ``root``."""
    state = root / STATE_DIR
    targets: list[Target] = []
    detail = ""
    if what == "all":
        if state.exists() or state.is_symlink():
            targets.append(_target(state, f"{STATE_DIR}/"))
            detail = _count(_runs(state), "run")
    elif what == "runs":
        runs = state / RUNS_DIR
        if runs.exists() or runs.is_symlink():
            targets.append(_target(runs, f"{STATE_DIR}/{RUNS_DIR}/"))
            detail = _count(_runs(state), "run")
    else:
        records = state / RECORDS_DIR
        if records.is_dir() and not records.is_symlink():
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
    state = plan.root / STATE_DIR
    for target in plan.targets:
        if target.path == state and not state.is_symlink():
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
    try:
        mode = path.lstat().st_mode
    except OSError:
        return
    if not stat.S_ISDIR(mode):
        yield path
        return
    for current, _, names in os.walk(path):
        for name in names:
            yield Path(current, name)


def _runs(state: Path) -> int:
    runs = state / RUNS_DIR
    if not runs.is_dir() or runs.is_symlink():
        return 0
    return sum(1 for entry in runs.iterdir() if entry.is_dir())


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _remove(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    shutil.rmtree(path)
