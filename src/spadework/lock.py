"""``.spadework/lock``: one run at a time in a project.

The lock file is created with ``O_EXCL`` and holds the pid, the host and the
start time as JSON. A lock left by a process that is no longer running on
this host is reclaimed with a warning. Any other lock stops the run with exit
code 7. See docs/spec/04-engine.md, Durability.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import socket
from dataclasses import asdict, dataclass
from pathlib import Path
from types import TracebackType

from spadework.errors import ExitCode, SpadeworkError

LOCK_FILE = "lock"
# Reclaiming races with another run doing the same; give up after this many.
_ATTEMPTS = 3
# The locks this process holds, so that a lock naming our own pid can be told
# apart from one left by an earlier process that had the same pid.
_held: set[str] = set()


class LockedError(SpadeworkError):
    """Another run holds the project lock (exit code 7)."""

    exit_code = ExitCode.LOCKED


@dataclass(frozen=True)
class LockOwner:
    pid: int
    host: str
    started_at: str
    """UTC, such as 2026-09-27T21:04:11Z."""

    def to_bytes(self) -> bytes:
        return (json.dumps(asdict(self), sort_keys=True) + "\n").encode("utf-8")

    @classmethod
    def parse(cls, data: bytes) -> LockOwner | None:
        """The owner a lock file names, or None if it is not a lock we wrote."""
        try:
            document = json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(document, dict) or set(document) != {
            "pid",
            "host",
            "started_at",
        }:
            return None
        pid, host, started_at = (
            document["pid"],
            document["host"],
            document["started_at"],
        )
        if (
            not isinstance(pid, int)
            or isinstance(pid, bool)
            or pid <= 0
            or not isinstance(host, str)
            or not isinstance(started_at, str)
        ):
            return None
        return cls(pid, host, started_at)

    def describe(self) -> str:
        return f"pid {self.pid} on host {self.host}, started {self.started_at}"


def read_lock(path: Path) -> LockOwner | None:
    """Who holds the lock at ``path``: None if nobody, or if it is unreadable."""
    data = _read(path)
    return None if data is None else LockOwner.parse(data)


class ProjectLock:
    """Hold ``.spadework/lock`` for the length of a ``with`` block.

    ``warning`` is set when a dead run's lock was reclaimed, for the caller
    to print. Leaving the block releases the lock only if it is still ours.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.display = f"{path.parent.name}/{path.name}"
        self.owner: LockOwner | None = None
        self.warning: str | None = None

    def __enter__(self) -> ProjectLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()

    def acquire(self) -> str | None:
        """Take the lock. Returns a warning if a dead run's lock was reclaimed.

        Raises ``LockedError`` if another run holds it, or if the lock file
        cannot be read and so its owner cannot be checked.
        """
        if self.owner is not None:
            raise RuntimeError(f"{self.display} is already held by this process")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        me = LockOwner(
            os.getpid(),
            socket.gethostname(),
            dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        warning = None
        for _ in range(_ATTEMPTS):
            if self._create(me):
                self.owner, self.warning = me, warning
                _held.add(self._id)
                return warning
            data = _read(self.path)
            if data is None:
                continue  # released since we tried: try again
            holder = LockOwner.parse(data)
            if holder is None:
                raise LockedError(
                    f"the project lock {self.display} is unreadable, so spadework "
                    "cannot tell whether another run holds it",
                    f"If no other spadework run is active in this project, delete "
                    f"{self.display} and try again.",
                )
            if holder.host != me.host or self._running(holder.pid):
                raise LockedError(
                    f"another run holds the project lock ({holder.describe()})",
                    _held_hint(holder, me, self.display),
                )
            # Remove only the lock we found dead: another run reclaiming it at
            # the same moment may already have replaced it with its own.
            if _read(self.path) == data:
                with contextlib.suppress(FileNotFoundError):
                    self.path.unlink()
            warning = (
                f"reclaimed the project lock left by a run that is no longer "
                f"running ({holder.describe()})"
            )
        raise LockedError(
            f"another run took the project lock {self.display} first",
            "Wait for it to finish, then try again. `spadework status` shows it.",
        )

    def release(self) -> None:
        """Remove the lock file if it is still ours. Safe to call twice."""
        if self.owner is None:
            return
        data = _read(self.path)
        if data is not None and LockOwner.parse(data) == self.owner:
            with contextlib.suppress(FileNotFoundError):
                self.path.unlink()
        self.owner = None
        _held.discard(self._id)

    @property
    def _id(self) -> str:
        return os.path.abspath(self.path)

    def _running(self, pid: int) -> bool:
        if pid == os.getpid():
            # Ours only if this process holds it. Otherwise an earlier process
            # had our pid, as happens when each run starts in a new container.
            return self._id in _held
        return _running(pid)

    def _create(self, me: LockOwner) -> bool:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o644)
        except FileExistsError:
            return False
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(me.to_bytes())
                file.flush()
                os.fsync(file.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                self.path.unlink()
            raise
        return True


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError:
        # A lock we cannot read counts as unreadable, not as absent.
        return b""


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True  # EPERM: it exists, and belongs to another user
    return True


def _held_hint(holder: LockOwner, me: LockOwner, display: str) -> str:
    if holder.host != me.host:
        return (
            f"The lock was taken on another computer ({holder.host}). If no run "
            f"is active there, delete {display} and try again."
        )
    return (
        "Wait for that run to finish; `spadework status` shows it. If that process "
        f"is not a spadework run, delete {display} and try again."
    )
