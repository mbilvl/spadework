from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest

from spadework.sandbox.policy import (
    Policy,
    PolicyError,
    guard_command,
    is_within,
    load,
)


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return Path(os.path.realpath(tmp_path))


def policy_data(base: Path, **changes: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 1,
        "session_id": "003-references-draft",
        "workdir": str(base / "sessions" / "003"),
        "allowed_tools": ["Read", "Glob", "Grep", "Write", "Edit", "StructuredOutput"],
        "writable": [str(base / "sessions" / "003" / "03-references.md")],
        "deadline_epoch": 1790550000,
        "caps": {"total": 80},
        "counters_path": str(base / "control" / "003.counters.json"),
        "denials_path": str(base / "control" / "003.denials.jsonl"),
    }
    data.update(changes)
    return data


def write_json(path: Path, data: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_round_trip(base: Path) -> None:
    policy = Policy(**policy_data(base))
    path = base / "control" / "003.policy.json"
    path.parent.mkdir()

    policy.write(path)

    assert load(path) == policy
    assert json.loads(path.read_text()) == policy_data(base)
    assert path.stat().st_mode & 0o777 == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["003.policy.json"]


def test_research_policy_from_the_spec(base: Path) -> None:
    data = policy_data(
        base,
        allowed_tools=["WebSearch", "WebFetch", "StructuredOutput"],
        writable=[],
        caps={"WebSearch": 6, "WebFetch": 8, "total": 80},
    )

    policy = load(write_json(base / "control" / "p.json", data))

    assert policy.allowed_tools == ("WebSearch", "WebFetch", "StructuredOutput")
    assert policy.writable == ()
    assert dict(policy.caps) == {"WebSearch": 6, "WebFetch": 8, "total": 80}


def test_policy_is_immutable(base: Path) -> None:
    policy = Policy(**policy_data(base))

    with pytest.raises(FrozenInstanceError):
        policy.workdir = "/"  # type: ignore[misc]
    with pytest.raises(TypeError):
        policy.caps["total"] = 10**6  # type: ignore[index]


def test_lock_path_is_next_to_the_counters(base: Path) -> None:
    policy = Policy(**policy_data(base))

    assert policy.lock_path == str(base / "control" / "003.counters.json.lock")


Change = Callable[[Path], dict[str, Any]]

INVALID: list[Any] = [
    pytest.param(
        lambda b: {**policy_data(b), "extra": 1},
        "unknown policy field 'extra'",
        id="unknown",
    ),
    pytest.param(
        lambda b: {k: v for k, v in policy_data(b).items() if k != "workdir"},
        "missing policy field 'workdir'",
        id="missing",
    ),
    pytest.param(lambda b: policy_data(b, schema_version=2), "schema_version", id="v2"),
    pytest.param(
        lambda b: policy_data(b, schema_version="1"), "schema_version", id="v-str"
    ),
    pytest.param(
        lambda b: policy_data(b, schema_version=True), "schema_version", id="v-bool"
    ),
    pytest.param(lambda b: policy_data(b, session_id=""), "session_id", id="id-empty"),
    pytest.param(
        lambda b: policy_data(b, session_id="../x"), "session_id", id="id-slash"
    ),
    pytest.param(lambda b: policy_data(b, session_id=".x"), "session_id", id="id-dot"),
    pytest.param(
        lambda b: policy_data(b, session_id="x" * 129), "session_id", id="id-long"
    ),
    pytest.param(lambda b: policy_data(b, session_id=3), "session_id", id="id-int"),
    pytest.param(
        lambda b: policy_data(b, workdir="sessions/003"), "workdir", id="wd-rel"
    ),
    pytest.param(lambda b: policy_data(b, workdir="/"), "root directory", id="wd-root"),
    pytest.param(
        lambda b: policy_data(b, workdir=str(b / "a" / ".." / "b")),
        "workdir",
        id="wd-dots",
    ),
    pytest.param(
        lambda b: policy_data(b, workdir=str(b) + "/"), "workdir", id="wd-slash"
    ),
    pytest.param(
        lambda b: policy_data(b, workdir=f"{b}/x\x00"), "workdir", id="wd-nul"
    ),
    pytest.param(
        lambda b: policy_data(b, allowed_tools="Read"), "must be a list", id="tools"
    ),
    pytest.param(
        lambda b: policy_data(b, allowed_tools=["Read", "Bash"]),
        "'Bash'",
        id="tools-bash",
    ),
    pytest.param(
        lambda b: policy_data(b, allowed_tools=["Read", "mcp__x__y"]),
        "mcp__",
        id="tools-mcp",
    ),
    pytest.param(
        lambda b: policy_data(b, allowed_tools=["Read", "Read"]),
        "twice",
        id="tools-dup",
    ),
    pytest.param(
        lambda b: policy_data(b, allowed_tools=[""]), "non-empty", id="tools-empty"
    ),
    pytest.param(
        lambda b: policy_data(b, allowed_tools=["WebFetch", "Read"], writable=[]),
        "web tools and file tools",
        id="I5-web-and-files",
    ),
    pytest.param(
        lambda b: policy_data(
            b,
            writable=[
                str(b / "sessions" / "003" / "a.md"),
                str(b / "sessions" / "003" / "b.md"),
            ],
        ),
        "at most one path",
        id="I3-two-writable",
    ),
    pytest.param(
        lambda b: policy_data(b, writable=[str(b / "control" / "003.policy.json")]),
        "inside the workdir",
        id="I4-writable-control",
    ),
    pytest.param(
        lambda b: policy_data(b, writable=[str(b / "sessions" / "003")]),
        "inside the workdir",
        id="writable-is-workdir",
    ),
    pytest.param(
        lambda b: policy_data(b, writable=[str(b / "sessions" / "0031" / "x.md")]),
        "inside the workdir",
        id="writable-prefix",
    ),
    pytest.param(
        lambda b: policy_data(b, writable=["out.md"]),
        "absolute",
        id="writable-relative",
    ),
    pytest.param(
        lambda b: policy_data(b, allowed_tools=["Read"]),
        "no write tool",
        id="writable-no-tool",
    ),
    pytest.param(
        lambda b: policy_data(b, writable="x"), "must be a list", id="writable-str"
    ),
    pytest.param(
        lambda b: policy_data(b, deadline_epoch=True), "number", id="deadline-bool"
    ),
    pytest.param(
        lambda b: policy_data(b, deadline_epoch="1"), "number", id="deadline-str"
    ),
    pytest.param(
        lambda b: policy_data(b, deadline_epoch=0), "positive", id="deadline-zero"
    ),
    pytest.param(
        lambda b: policy_data(b, deadline_epoch=None), "number", id="deadline-null"
    ),
    pytest.param(
        lambda b: policy_data(b, caps=[]), "must be an object", id="caps-list"
    ),
    pytest.param(
        lambda b: policy_data(b, caps={}), "must set 'total'", id="I7-caps-no-total"
    ),
    pytest.param(
        lambda b: policy_data(b, caps={"total": -1}), "non-negative", id="caps-neg"
    ),
    pytest.param(
        lambda b: policy_data(b, caps={"total": 1.5}), "non-negative", id="caps-f"
    ),
    pytest.param(
        lambda b: policy_data(b, caps={"total": True}), "non-negative", id="caps-b"
    ),
    pytest.param(
        lambda b: policy_data(b, caps={"total": 80, "Bash": 1}),
        "unknown key",
        id="caps-key",
    ),
    pytest.param(
        lambda b: policy_data(b, counters_path=str(b / "sessions" / "003" / "c.json")),
        "outside the workdir",
        id="I4-counters-inside",
    ),
    pytest.param(
        lambda b: policy_data(b, denials_path=str(b / "sessions" / "003" / "d.jsonl")),
        "outside the workdir",
        id="I4-denials-inside",
    ),
    pytest.param(
        lambda b: policy_data(b, denials_path=str(b / "control" / "003.counters.json")),
        "must differ",
        id="same-files",
    ),
    pytest.param(
        lambda b: policy_data(b, counters_path="counters.json"),
        "absolute",
        id="counters-rel",
    ),
]


@pytest.mark.parametrize(("change", "message"), INVALID)
def test_invalid_policies_are_rejected(
    base: Path, change: Change, message: str
) -> None:
    path = write_json(base / "control" / "p.json", change(base))

    with pytest.raises(PolicyError) as error:
        load(path)

    assert message in str(error.value)
    assert str(base) not in str(error.value)


def test_nan_deadline_is_rejected(base: Path) -> None:
    path = base / "p.json"
    text = json.dumps(policy_data(base, deadline_epoch=1)).replace(
        '"deadline_epoch": 1', '"deadline_epoch": NaN'
    )
    path.write_text(text, encoding="utf-8")

    with pytest.raises(PolicyError, match="positive number"):
        load(path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        pytest.param(b"{", "not valid JSON", id="json"),
        pytest.param(b"\xff", "not valid JSON", id="bytes"),
        pytest.param(b"[]", "must be a JSON object", id="array"),
    ],
)
def test_unreadable_policies_are_rejected(
    base: Path, content: bytes, message: str
) -> None:
    path = base / "p.json"
    path.write_bytes(content)

    with pytest.raises(PolicyError, match=message):
        load(path)


def test_missing_policy_is_rejected_without_its_path(base: Path) -> None:
    with pytest.raises(PolicyError) as error:
        load(base / "control" / "missing.json")

    assert str(error.value) == "cannot read the policy: No such file or directory"


def test_policy_file_must_be_outside_the_workdir(base: Path) -> None:
    """I4: a policy inside the staging directory is refused on load and write."""
    policy = Policy(**policy_data(base))
    inside = base / "sessions" / "003" / "policy.json"

    with pytest.raises(PolicyError, match="outside the workdir"):
        policy.write(inside)
    with pytest.raises(PolicyError, match="outside the workdir"):
        load(write_json(inside, policy_data(base)))


def test_write_replaces_atomically(base: Path) -> None:
    path = base / "p.json"
    path.write_text("old", encoding="utf-8")
    (base / f"p.json.{os.getpid()}.tmp").write_text("stale", encoding="utf-8")

    Policy(**policy_data(base)).write(path)

    assert json.loads(path.read_text())["session_id"] == "003-references-draft"
    assert sorted(p.name for p in base.iterdir()) == ["p.json"]


def test_failed_write_leaves_nothing_behind(base: Path) -> None:
    target = base / "dir"
    target.mkdir()
    (target / "x").touch()  # os.replace cannot put a file over a non-empty dir

    with pytest.raises(IsADirectoryError):
        Policy(**policy_data(base)).write(target)

    assert sorted(p.name for p in base.iterdir()) == ["dir"]


def test_guard_command(base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(base)

    command = guard_command(Path("control/003.policy.json"), "session-start")

    assert command == [
        sys.executable,
        "-I",
        "-m",
        "spadework.sandbox.guard",
        "--policy",
        str(base / "control" / "003.policy.json"),
        "--event",
        "session-start",
    ]


def test_guard_command_rejects_unknown_events(base: Path) -> None:
    with pytest.raises(ValueError, match="unknown guard event"):
        guard_command(base / "p.json", "post-tool-use")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("path", "root", "inside"),
    [
        ("/a/b", "/a/b", True),
        ("/a/b/c", "/a/b", True),
        ("/a/bc", "/a/b", False),
        ("/a", "/a/b", False),
        ("/", "/a", False),
        ("relative", "/a", False),
    ],
)
def test_is_within_compares_components(path: str, root: str, inside: bool) -> None:
    assert is_within(path, root) is inside


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        pytest.param("allowed_tools", "Read", "must be a list", id="tools-string"),
        pytest.param("writable", None, "must be a list", id="writable-none"),
        pytest.param("caps", [("total", 1)], "must be an object", id="caps-pairs"),
    ],
)
def test_constructor_validates_too(
    base: Path, field: str, value: Any, message: str
) -> None:
    with pytest.raises(PolicyError, match=message):
        Policy(**policy_data(base, **{field: value}))
