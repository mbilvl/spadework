"""The guard policy: what one session may do.

See docs/spec/07-security.md, The guard. The engine writes a policy for each
session into `.spadework/control/`, outside every staging directory, and the
guard loads it on every tool call. Loading is strict: an unknown, missing or
mistyped field makes the policy invalid, and the guard then fails closed.

Standard library only: the guard imports this module. Error messages never
contain paths, because the guard may show them to the model.
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import sys
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Literal

from spadework.sandbox.tools import FILE_TOOLS, KNOWN_TOOLS, WEB_TOOLS, WRITE_TOOLS

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

SCHEMA_VERSION = 1
TOTAL = "total"
GuardEvent = Literal["session-start", "pre-tool-use"]

_FIELDS = (
    "schema_version",
    "session_id",
    "workdir",
    "allowed_tools",
    "writable",
    "deadline_epoch",
    "caps",
    "counters_path",
    "denials_path",
)
_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
)
_MAX_ID = 128


class PolicyError(ValueError):
    """A policy that cannot be read or is not valid."""


@dataclass(frozen=True)
class Policy:
    """What one session may do. Every path is absolute and fully resolved."""

    session_id: str
    workdir: str
    allowed_tools: tuple[str, ...]
    writable: tuple[str, ...]
    deadline_epoch: float
    caps: Mapping[str, int]
    counters_path: str
    denials_path: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        # Accept lists and dicts as JSON gives them, and freeze them.
        if isinstance(self.allowed_tools, list):
            object.__setattr__(self, "allowed_tools", tuple(self.allowed_tools))
        if isinstance(self.writable, list):
            object.__setattr__(self, "writable", tuple(self.writable))
        if isinstance(self.caps, dict):
            object.__setattr__(self, "caps", MappingProxyType(dict(self.caps)))
        _validate(self)

    @property
    def lock_path(self) -> str:
        """The lock file that serialises updates to the counters."""
        return lock_path_for(self.counters_path)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "workdir": self.workdir,
            "allowed_tools": list(self.allowed_tools),
            "writable": list(self.writable),
            "deadline_epoch": self.deadline_epoch,
            "caps": dict(self.caps),
            "counters_path": self.counters_path,
            "denials_path": self.denials_path,
        }

    def write(self, path: str | os.PathLike[str]) -> None:
        """Write the policy atomically, readable only by the user."""
        target = os.fspath(path)
        if is_within(os.path.abspath(target), self.workdir):
            raise PolicyError("the policy file must be outside the workdir")
        data = (json.dumps(self.to_dict(), indent=2) + "\n").encode("utf-8")
        write_atomic(target, data)


def lock_path_for(counters_path: str) -> str:
    return counters_path + ".lock"


def marker_path(policy_path: str | os.PathLike[str], session_id: str) -> Path:
    """The liveness marker the guard writes when the session starts.

    It sits next to the policy and holds the session id, so the executor can
    tell that our guard ran, and not a SessionStart hook from other settings.
    """
    from pathlib import Path

    check_session_id(session_id)
    return Path(policy_path).parent / f"{session_id}.alive"


def is_within(path: str, root: str) -> bool:
    """Whether an absolute, normalised path is `root` or below it.

    Compares whole path components, so `/a/bc` is not inside `/a/b`.
    """
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def load(path: str | os.PathLike[str]) -> Policy:
    """Read and validate a policy file."""
    target = os.fspath(path)
    try:
        with open(target, "rb") as file:
            raw = file.read()
    except OSError as exc:
        raise PolicyError(f"cannot read the policy: {exc.strerror}") from None
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise PolicyError("the policy is not valid JSON") from None
    if not isinstance(data, dict):
        raise PolicyError("the policy must be a JSON object")
    unknown = sorted(set(data) - set(_FIELDS))
    if unknown:
        raise PolicyError(f"unknown policy field {unknown[0]!r}")
    missing = [name for name in _FIELDS if name not in data]
    if missing:
        raise PolicyError(f"missing policy field {missing[0]!r}")
    for name in ("allowed_tools", "writable"):
        if not isinstance(data[name], list):
            raise PolicyError(f"policy field {name!r} must be a list")
    if not isinstance(data["caps"], dict):
        raise PolicyError("policy field 'caps' must be an object")
    policy = Policy(**data)
    if is_within(os.path.abspath(target), policy.workdir):
        raise PolicyError("the policy file must be outside the workdir")
    return policy


def guard_command(policy_path: Path, event: GuardEvent) -> list[str]:
    """The command line that runs the guard for one hook event.

    `-I` (isolated mode) ignores PYTHON* environment variables and user
    site-packages, and keeps the session's working directory off `sys.path`,
    so nothing in the staging directory can be imported by the guard.
    """
    if event not in ("session-start", "pre-tool-use"):
        raise ValueError(f"unknown guard event {event!r}")
    return [
        sys.executable,
        "-I",
        "-m",
        "spadework.sandbox.guard",
        "--policy",
        os.path.abspath(policy_path),
        "--event",
        event,
    ]


def write_atomic(path: str, data: bytes) -> None:
    """Replace a file's contents in one step, with mode 0600."""
    temporary = f"{path}.{os.getpid()}.tmp"
    # A leftover from a crashed process that had this pid is stale.
    with contextlib.suppress(FileNotFoundError):
        os.unlink(temporary)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as file:
            file.write(data)
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def _validate(policy: Policy) -> None:
    version = policy.schema_version
    if not _is_int(version) or version != SCHEMA_VERSION:
        raise PolicyError(
            f"unsupported policy schema_version, expected {SCHEMA_VERSION}"
        )
    check_session_id(policy.session_id)
    _check_path("workdir", policy.workdir)
    if policy.workdir == os.sep:
        raise PolicyError("policy field 'workdir' cannot be the root directory")
    tools = _check_names("allowed_tools", policy.allowed_tools)
    for tool in tools:
        if tool not in KNOWN_TOOLS:
            raise PolicyError(
                f"policy field 'allowed_tools' has an unsupported tool {tool!r}"
            )
    if any(tool in WEB_TOOLS for tool in tools) and any(
        tool in FILE_TOOLS for tool in tools
    ):
        # I5: a session with web access sees nothing on disk and writes nothing.
        raise PolicyError("a policy cannot allow both web tools and file tools")
    _check_writable(policy, tools)
    deadline = policy.deadline_epoch
    if isinstance(deadline, bool) or not isinstance(deadline, int | float):
        raise PolicyError("policy field 'deadline_epoch' must be a number")
    if not math.isfinite(deadline) or deadline <= 0:
        raise PolicyError("policy field 'deadline_epoch' must be a positive number")
    _check_caps(policy.caps)
    _check_path("counters_path", policy.counters_path)
    _check_path("denials_path", policy.denials_path)
    for name in ("counters_path", "denials_path"):
        if is_within(getattr(policy, name), policy.workdir):
            raise PolicyError(f"policy field {name!r} must be outside the workdir")
    if policy.counters_path == policy.denials_path:
        raise PolicyError(
            "policy fields 'counters_path' and 'denials_path' must differ"
        )


def check_session_id(value: object) -> None:
    """Session ids name control files, so they are short and plain."""
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= _MAX_ID
        or not set(value) <= _ID_CHARS
        or value[0] in "._-"
    ):
        raise PolicyError(
            "policy field 'session_id' must be 1 to 128 letters, digits, "
            "'.', '_' or '-'"
        )


def _check_path(name: str, value: object) -> None:
    if (
        not isinstance(value, str)
        or "\x00" in value
        or not os.path.isabs(value)
        or os.path.normpath(value) != value
    ):
        raise PolicyError(f"policy field {name!r} must be an absolute, normalised path")


def _check_names(name: str, values: Iterable[object]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise PolicyError(f"policy field {name!r} must be a list")
    names: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise PolicyError(f"policy field {name!r} must hold non-empty strings")
        if value in names:
            raise PolicyError(f"policy field {name!r} lists {value!r} twice")
        names.append(value)
    return tuple(names)


def _check_writable(policy: Policy, tools: tuple[str, ...]) -> None:
    writable = _check_names("writable", policy.writable)
    if len(writable) > 1:
        # I3: at most one writable path.
        raise PolicyError("policy field 'writable' can hold at most one path")
    for path in writable:
        _check_path("writable", path)
        if path == policy.workdir or not is_within(path, policy.workdir):
            raise PolicyError(
                "policy field 'writable' must name a file inside the workdir"
            )
    if writable and not any(tool in WRITE_TOOLS for tool in tools):
        raise PolicyError("policy field 'writable' is set but no write tool is allowed")


def _check_caps(caps: Mapping[str, int]) -> None:
    if not isinstance(caps, MappingProxyType):
        raise PolicyError("policy field 'caps' must be an object")
    for key, value in caps.items():
        if key != TOTAL and key not in KNOWN_TOOLS:
            raise PolicyError(f"policy field 'caps' has an unknown key {key!r}")
        if not _is_int(value) or value < 0:
            raise PolicyError("policy field 'caps' must map to non-negative integers")
    if TOTAL not in caps:
        # I7: every session has a cap on its total tool calls.
        raise PolicyError("policy field 'caps' must set 'total'")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
