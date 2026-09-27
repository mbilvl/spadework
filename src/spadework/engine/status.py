"""``spadework status``: task states, the last run, and whether one is active.

Reads only. Task states are planned exactly as ``check`` and ``run`` plan
them.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
from dataclasses import dataclass
from pathlib import Path

from spadework.engine.planning import Planned, plan_project
from spadework.engine.store import state_dir
from spadework.errors import SpadeworkError
from spadework.lock import LOCK_FILE, LockOwner, read_lock
from spadework.manifest import (
    MANIFEST_FILE,
    RUN_ID,
    RUNS_DIR,
    RunManifest,
    load_manifest,
)
from spadework.project import Project


@dataclass(frozen=True)
class ActiveRun:
    """Who holds the project lock."""

    owner: LockOwner | None
    """None when the lock file cannot be read."""
    alive: bool | None
    """None when it cannot be told: another host, or an unreadable lock."""


@dataclass(frozen=True)
class ProjectStatus:
    planned: Planned
    active: ActiveRun | None
    last_run: RunManifest | None
    warnings: tuple[str, ...] = ()

    @property
    def last_run_unfinished(self) -> bool:
        """The last run has no outcome, and no live run holds the lock: it was
        killed, or its machine stopped."""
        if self.last_run is None or self.last_run.outcome is not None:
            return False
        return self.active is None or self.active.alive is False


def project_status(project: Project) -> ProjectStatus:
    warnings: list[str] = []
    planned = plan_project(project)
    active = active_run(state_dir(project.root) / LOCK_FILE)
    last = None
    for path in reversed(run_manifests(project.root)):
        try:
            last = load_manifest(path)
        except SpadeworkError as exc:
            warnings.append(exc.message)
            continue
        break
    return ProjectStatus(planned, active, last, tuple(warnings))


def run_manifests(root: Path) -> list[Path]:
    """Every run's manifest, oldest first."""
    import re

    runs = state_dir(root) / RUNS_DIR
    try:
        entries = list(runs.iterdir())
    except OSError:
        return []
    return sorted(
        (
            entry / MANIFEST_FILE
            for entry in entries
            if re.fullmatch(RUN_ID, entry.name) and (entry / MANIFEST_FILE).is_file()
        ),
        key=_started,
    )


def _started(manifest: Path) -> tuple[str, int]:
    # A run id starts with its time to the second, then 4 random digits. Two
    # runs in the same second are ordered by their last write: runs never
    # overlap, so the later run wrote last.
    try:
        written = manifest.stat().st_mtime_ns
    except OSError:
        written = 0
    return manifest.parent.name.split("-")[0], written


def active_run(path: Path) -> ActiveRun | None:
    if not path.exists() and not path.is_symlink():
        return None
    owner = read_lock(path)
    if owner is None:
        return ActiveRun(None, None)
    if owner.host != socket.gethostname():
        return ActiveRun(owner, None)
    return ActiveRun(owner, _alive(owner.pid))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # it exists, and belongs to someone else
    return True


def run_seconds(manifest: RunManifest) -> float | None:
    if manifest.ended_at is None:
        return None
    started = dt.datetime.fromisoformat(manifest.started_at.replace("Z", "+00:00"))
    ended = dt.datetime.fromisoformat(manifest.ended_at.replace("Z", "+00:00"))
    return (ended - started).total_seconds()
