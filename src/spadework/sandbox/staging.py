"""Staging directories and control files for one session.

See docs/spec/07-security.md (I2 to I5). A session runs in a fresh staging
directory that holds read-only copies of only the files it needs. Its guard
policy, settings, counters and denials live in `.spadework/control/`, which
is never inside any staging directory.

Research sessions are staged in an empty directory under the OS temporary
directory. The CLI tells the model its working directory, and a path inside
the project could reveal the client's name to a session with web access.
Every other session is staged under `.spadework/sessions/<session id>/`.

Typical use by the engine:

    staging = Staging.create(root, "001-scope-draft", "draft", output="01-scope.md")
    staging.add_file(root / "brief.md", "brief.md")
    staging.write_policy(deadline_epoch=deadline)
    ...  # run the session with cwd=staging.paths.workdir
    collected = staging.collect()
    denials = staging.denials()
    staging.cleanup()
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from spadework.sandbox.guard import read_counters
from spadework.sandbox.policy import (
    TOTAL,
    Policy,
    PolicyError,
    check_session_id,
    is_within,
    lock_path_for,
    marker_path,
)
from spadework.sandbox.tools import TOOLS_BY_KIND, WRITE_TOOLS, allowlist

DEFAULT_TOTAL_CALLS = 80
TEMP_PREFIX = "spadework-"
_READ_ONLY = 0o444
_WRITABLE = 0o644


class StagingError(Exception):
    """A session cannot be staged as asked."""


@dataclass(frozen=True)
class SessionPaths:
    """Where one session's files are. Every path is fully resolved."""

    session_id: str
    workdir: Path
    control_dir: Path
    policy: Path
    settings: Path
    counters: Path
    denials: Path
    lock: Path
    marker: Path  # written by the guard at session start (see marker_path)

    @classmethod
    def build(cls, control_dir: Path, session_id: str, workdir: Path) -> SessionPaths:
        policy = control_dir / f"{session_id}.policy.json"
        counters = control_dir / f"{session_id}.counters.json"
        return cls(
            session_id=session_id,
            workdir=workdir,
            control_dir=control_dir,
            policy=policy,
            settings=control_dir / f"{session_id}.settings.json",
            counters=counters,
            denials=control_dir / f"{session_id}.denials.jsonl",
            lock=Path(lock_path_for(str(counters))),
            marker=marker_path(policy, session_id),
        )

    def control_files(self) -> tuple[Path, ...]:
        return (
            self.policy,
            self.settings,
            self.counters,
            self.denials,
            self.lock,
            self.marker,
        )


@dataclass(frozen=True)
class CollectedOutput:
    """What a session left in its staging directory."""

    content: bytes | None  # the expected output, or None if it was not written
    extras: tuple[str, ...]  # other new files, relative; never copied back


def debug_enabled() -> bool:
    return os.environ.get("SPADEWORK_DEBUG", "") not in ("", "0")


def writes_output(kind: str) -> bool:
    """Whether sessions of this kind write exactly one output file."""
    return any(tool in WRITE_TOOLS for tool in TOOLS_BY_KIND[kind])


def build_policy(
    paths: SessionPaths,
    kind: str,
    *,
    writable: Path | None,
    deadline_epoch: float,
    max_searches: int | None = None,
    max_fetches: int | None = None,
    total_calls: int = DEFAULT_TOTAL_CALLS,
) -> Policy:
    """The guard policy for one session of a kind."""
    if kind not in TOOLS_BY_KIND:
        raise StagingError(f"unknown session kind {kind!r}")
    caps = {TOTAL: total_calls}
    if kind == "research":
        if max_searches is None or max_fetches is None:
            raise StagingError("a research session needs max_searches and max_fetches")
        caps["WebSearch"] = max_searches
        caps["WebFetch"] = max_fetches
    elif max_searches is not None or max_fetches is not None:
        raise StagingError(f"a {kind} session has no web tools to cap")
    if writes_output(kind) != (writable is not None):
        raise StagingError(f"a {kind} session needs exactly one writable output")
    try:
        return Policy(
            session_id=paths.session_id,
            workdir=str(paths.workdir),
            allowed_tools=allowlist(kind),
            writable=() if writable is None else (os.path.realpath(writable),),
            deadline_epoch=deadline_epoch,
            caps=caps,
            counters_path=str(paths.counters),
            denials_path=str(paths.denials),
        )
    except PolicyError as exc:
        raise StagingError(f"invalid policy for {paths.session_id}: {exc}") from None


class Staging:
    """One session's staging directory and control files.

    Create it with `Staging.create`. Files are added as read-only copies.
    After the session, `collect` returns the expected output and lists any
    other new files, and `cleanup` removes everything unless SPADEWORK_DEBUG
    is set.
    """

    def __init__(
        self, project_root: Path, paths: SessionPaths, kind: str, output: str | None
    ) -> None:
        self.project_root = project_root
        self.paths = paths
        self.kind = kind
        self.output = output
        self._staged: set[str] = set()

    @classmethod
    def create(
        cls,
        project_root: Path,
        session_id: str,
        kind: str,
        *,
        output: str | None = None,
        temp_root: Path | None = None,
    ) -> Staging:
        """Make a fresh staging directory, removing any stale one for this id.

        `output` is the file, relative to the staging directory, that the
        session writes. Research and critic sessions write nothing.
        `temp_root` overrides the OS temporary directory for research sessions.
        """
        if kind not in TOOLS_BY_KIND:
            raise StagingError(f"unknown session kind {kind!r}")
        try:
            check_session_id(session_id)
        except PolicyError as exc:
            raise StagingError(f"invalid session id {session_id!r}: {exc}") from None
        if writes_output(kind) and output is None:
            raise StagingError(f"a {kind} session needs an output file")
        if not writes_output(kind) and output is not None:
            raise StagingError(f"a {kind} session writes nothing")
        if output is not None:
            output = _relative(output).as_posix()
        root = Path(os.path.realpath(project_root))
        state = root / ".spadework"
        control = state / "control"
        control.mkdir(mode=0o700, parents=True, exist_ok=True)
        control = Path(os.path.realpath(control))
        if kind == "research":
            base = None if temp_root is None else os.fspath(temp_root)
            workdir = Path(
                os.path.realpath(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=base))
            )
        else:
            sessions = state / "sessions"
            sessions.mkdir(mode=0o700, parents=True, exist_ok=True)
            _remove(sessions / session_id)
            (sessions / session_id).mkdir(mode=0o700)
            workdir = Path(os.path.realpath(sessions / session_id))
        if is_within(str(workdir), str(control)) or is_within(
            str(control), str(workdir)
        ):
            raise StagingError(
                "the staging directory and the control directory overlap"
            )
        paths = SessionPaths.build(control, session_id, workdir)
        for path in paths.control_files():
            _remove(path)
        staging = cls(root, paths, kind, output)
        if output is not None:
            staging.output_path.parent.mkdir(parents=True, exist_ok=True)
        return staging

    @property
    def output_path(self) -> Path:
        """The absolute path of the session's output file."""
        if self.output is None:
            raise StagingError(f"a {self.kind} session has no output")
        return self.paths.workdir.joinpath(*PurePosixPath(self.output).parts)

    def add_file(self, source: Path, dest: str) -> Path:
        """Copy a project file into the staging directory, read-only.

        Refuses symlinks, anything outside the project, and the control plane.
        """
        if os.path.islink(source):
            raise StagingError(f"{source} is a symlink; only regular files are staged")
        real = os.path.realpath(source)
        if not is_within(real, str(self.project_root)):
            raise StagingError(f"{source} is outside the project")
        if is_within(real, str(self.paths.control_dir)):
            raise StagingError(f"{source} is a control file")
        try:
            data = _read_regular(Path(real))
        except OSError as exc:
            raise StagingError(f"cannot read {source}: {exc.strerror}") from None
        if data is None:
            raise StagingError(f"{source} is not a regular file")
        return self._place(dest, data, _READ_ONLY)

    def add_text(self, dest: str, text: str) -> Path:
        """Write engine-generated text, such as research notes, read-only."""
        return self._place(dest, text.encode("utf-8"), _READ_ONLY)

    def seed_output(self, content: str | bytes) -> Path:
        """Start the output from existing content, as fix sessions do."""
        if self.output is None:
            raise StagingError(f"a {self.kind} session has no output")
        data = content.encode("utf-8") if isinstance(content, str) else content
        return self._place(self.output, data, _WRITABLE, output=True)

    def write_policy(
        self,
        *,
        deadline_epoch: float,
        max_searches: int | None = None,
        max_fetches: int | None = None,
        total_calls: int = DEFAULT_TOTAL_CALLS,
    ) -> Policy:
        """Build the session's guard policy and write it to `paths.policy`."""
        policy = build_policy(
            self.paths,
            self.kind,
            writable=None if self.output is None else self.output_path,
            deadline_epoch=deadline_epoch,
            max_searches=max_searches,
            max_fetches=max_fetches,
            total_calls=total_calls,
        )
        policy.write(self.paths.policy)
        return policy

    def collect(self) -> CollectedOutput:
        """The expected output and any other new files. Never follows symlinks."""
        workdir = self.paths.workdir
        found: list[str] = []
        for directory, dirnames, filenames in os.walk(workdir):
            base = Path(directory)
            for name in dirnames:
                if (base / name).is_symlink():
                    found.append((base / name).relative_to(workdir).as_posix())
            for name in filenames:
                found.append((base / name).relative_to(workdir).as_posix())
        content = None
        if self.output is not None:
            with contextlib.suppress(OSError):  # missing, or a symlink
                content = _read_regular(self.output_path)
        expected = set(self._staged)
        if content is not None and self.output is not None:
            expected.add(self.output)
        extras = tuple(sorted(path for path in found if path not in expected))
        return CollectedOutput(content, extras)

    def counters(self) -> dict[str, int]:
        """The tool calls the guard allowed, by tool and in total."""
        return read_counters(str(self.paths.counters))

    def denials(self) -> list[dict[str, Any]]:
        """The guard's denials, oldest first. Read them before `cleanup`."""
        try:
            lines = self.paths.denials.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        records: list[dict[str, Any]] = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if isinstance(record, dict):
                records.append(record)
            elif line.strip():
                records.append({"reason": "unreadable denial record"})
        return records

    def cleanup(self) -> bool:
        """Remove the staging directory and control files. False if kept."""
        if debug_enabled():
            return False
        _remove(self.paths.workdir)
        for path in self.paths.control_files():
            _remove(path)
        return True

    def _place(
        self, dest: str, data: bytes, mode: int, *, output: bool = False
    ) -> Path:
        if self.kind == "research":
            # I5: research sessions see nothing on disk.
            raise StagingError("research sessions are staged empty")
        relative = _relative(dest).as_posix()
        if not output and relative == self.output:
            raise StagingError(f"{dest} is the session's output; use seed_output")
        if relative in self._staged:
            raise StagingError(f"{dest} is already staged")
        target = self.paths.workdir.joinpath(*PurePosixPath(relative).parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            with os.fdopen(os.open(target, flags, mode), "wb") as file:
                file.write(data)
        except OSError as exc:
            raise StagingError(f"cannot stage {dest}: {exc.strerror}") from None
        if not output:
            self._staged.add(relative)
        return target


def _relative(dest: str) -> PurePosixPath:
    """A plain relative path: no `..`, `.`, empty parts, backslashes or NULs."""
    parts = dest.split("/")
    if (
        not dest
        or "\x00" in dest
        or "\\" in dest
        or dest.startswith("/")
        or any(part in ("", ".", "..") for part in parts)
    ):
        raise StagingError(f"{dest!r} is not a plain relative path")
    return PurePosixPath(dest)


def _read_regular(path: Path) -> bytes | None:
    """A file's bytes, or None if it is not a regular file. Never follows a
    final symlink, and never blocks on a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        with os.fdopen(fd, "rb", closefd=False) as file:
            return file.read()
    finally:
        os.close(fd)


def _remove(path: Path) -> None:
    """Remove a file, symlink or directory tree if it exists."""
    if path.is_symlink() or path.is_file():
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)
