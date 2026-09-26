from __future__ import annotations

from pathlib import Path

import pytest

import spadework.executors as executors
from spadework.executors.base import (
    READ_ONLY_KINDS,
    Diagnostic,
    SessionKind,
    SessionResult,
    Text,
    Usage,
    ignore_events,
)
from tests.executors.conftest import MakeSpec


def test_session_kinds() -> None:
    assert [kind.value for kind in SessionKind] == [
        "research",
        "draft",
        "repair",
        "critic",
        "fix",
        "summary",
    ]
    assert {SessionKind.RESEARCH, SessionKind.CRITIC} == READ_ONLY_KINDS


@pytest.mark.parametrize("kind", list(SessionKind))
def test_a_valid_spec_for_each_kind(make_spec: MakeSpec, kind: SessionKind) -> None:
    spec = make_spec(kind)

    assert (spec.output is None) == (kind in READ_ONLY_KINDS)
    assert spec.expected_tools == ()


@pytest.mark.parametrize("kind", sorted(READ_ONLY_KINDS))
def test_read_only_kinds_have_no_output(make_spec: MakeSpec, kind: SessionKind) -> None:
    workdir = make_spec(SessionKind.DRAFT).workdir

    with pytest.raises(ValueError, match=f"a {kind} session must not have an output"):
        make_spec(kind, output=workdir / "x.md")


def test_writing_kinds_need_an_output(make_spec: MakeSpec) -> None:
    with pytest.raises(ValueError, match="a fix session needs an output path"):
        make_spec(SessionKind.FIX, output=None)


def test_the_output_is_inside_the_workdir(make_spec: MakeSpec, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="is not inside"):
        make_spec(SessionKind.DRAFT, output=tmp_path / "elsewhere.md")
    with pytest.raises(ValueError, match="is not inside"):
        make_spec(SessionKind.DRAFT, output=Path("relative/../../escape.md"))


@pytest.mark.parametrize("name", ["policy_path", "log_path"])
def test_the_control_plane_is_outside_the_workdir(
    make_spec: MakeSpec, name: str
) -> None:
    workdir = make_spec(SessionKind.DRAFT).workdir

    with pytest.raises(ValueError, match=f"{name} must be outside the staging"):
        make_spec(SessionKind.DRAFT, **{name: workdir / "sub" / "file.json"})


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("max_budget_usd", 0, "must be a finite amount of at least 0.0001"),
        ("max_budget_usd", 0.00009, "must be a finite amount of at least 0.0001"),
        ("max_budget_usd", float("inf"), "must be a finite amount"),
        ("max_budget_usd", float("nan"), "must be a finite amount"),
        ("timeout_s", 0, "timeout_s must be positive"),
    ],
)
def test_limits_are_positive(
    make_spec: MakeSpec, field: str, value: float, error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        make_spec(SessionKind.DRAFT, **{field: value})


def test_the_smallest_budget_is_allowed(make_spec: MakeSpec) -> None:
    assert make_spec(max_budget_usd=0.0001).max_budget_usd == 0.0001


def test_specs_are_frozen(make_spec: MakeSpec) -> None:
    spec = make_spec()

    with pytest.raises(AttributeError):
        spec.model = "opus"  # type: ignore[misc]


def test_result_defaults() -> None:
    result = SessionResult(status="ok")

    assert result.usage == Usage()
    assert result.tool_calls == {}
    assert (result.cost_usd, result.cost_known, result.guard_denials) == (0.0, True, 0)
    assert result.error is None
    assert result.error_kind is None


def test_ignore_events() -> None:
    ignore_events(Text("anything"))


def test_diagnostics_are_required_by_default() -> None:
    assert Diagnostic("x", True, "fine").required is True


def test_the_package_exports_the_interface() -> None:
    assert set(executors.__all__) <= set(dir(executors))
    assert executors.EXECUTORS == ("claude-cli", "fake")


@pytest.mark.parametrize(
    "session_id", ["", "../escape", "a/b", "-x", ".hidden", "x" * 129]
)
def test_the_session_id_is_a_plain_name(make_spec: MakeSpec, session_id: str) -> None:
    with pytest.raises(ValueError, match="session_id must be 1 to 128 letters"):
        make_spec(session_id=session_id)
