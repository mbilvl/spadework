import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from spadework.errors import ExitCode, SpadeworkError
from spadework.manifest import (
    OUTCOME_EXIT_CODES,
    Outcome,
    RunManifest,
    SessionEntry,
    exit_code,
    load_manifest,
    manifest_path,
    new_run_id,
    session_log,
    utc_timestamp,
    write_manifest,
)
from spadework.validators import ValidatorResult

# The example in docs/spec/04-engine.md, Run manifest.
SPEC_EXAMPLE: dict[str, Any] = {
    "schema_version": 1,
    "run_id": "20260927T210411Z-3f9a",
    "spadework_version": "0.1.0",
    "executor": {"name": "claude-cli", "version": "2.1.278"},
    "playbook": {"name": "discovery-website", "version": "0.1.0"},
    "limits": {"budget_usd": 6.0, "max_time_s": 7200, "rounds": 2},
    "started_at": "2026-09-27T21:04:11Z",
    "ended_at": "2026-09-27T21:47:02Z",
    "outcome": "complete",
    "sessions": [
        {
            "n": 1,
            "task": "scope",
            "kind": "draft",
            "round": 0,
            "model": "sonnet",
            "key": "sha256:…",
            "status": "ok",
            "cost_usd": 0.21,
            "duration_s": 72.4,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0},
            "tool_calls": {"Read": 3, "Write": 1},
            "guard_denials": 0,
            "validators": [{"name": "max_lines", "ok": True}],
            "rejected_findings": [],
            "log": "sessions/001-scope-draft.jsonl",
        }
    ],
    "totals": {"cost_usd": 3.12, "sessions": 17, "guard_denials": 0},
}


def manifest() -> RunManifest:
    return RunManifest.model_validate(
        {
            key: value
            for key, value in SPEC_EXAMPLE.items()
            if key not in {"sessions", "totals", "ended_at", "outcome"}
        }
    )


def session(n: int, **changes: Any) -> SessionEntry:
    data = {**SPEC_EXAMPLE["sessions"][0], "n": n, **changes}
    return SessionEntry.model_validate(data)


def test_the_spec_example_is_a_valid_manifest() -> None:
    loaded = RunManifest.model_validate_json(json.dumps(SPEC_EXAMPLE))
    dumped = json.loads(loaded.model_dump_json())
    # Every field in the spec, plus the error and reset time, which are null
    # unless a run or a session ends that way, and the cache creation tokens
    # that the spec's text adds to the example's usage.
    example = SPEC_EXAMPLE["sessions"][0]
    assert dumped == {
        **SPEC_EXAMPLE,
        "error": None,
        "rate_limit_reset": None,
        "sessions": [
            {
                **example,
                "error": None,
                "usage": {**example["usage"], "cache_creation_tokens": 0},
            }
        ],
    }


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        ("complete", 0),
        ("partial", 4),
        ("budget", 5),
        ("time", 5),
        ("rate_limited", 6),
        ("interrupted", 130),
        ("error", 1),
    ],
)
def test_outcomes_match_exit_codes(outcome: Outcome, code: int) -> None:
    assert exit_code(outcome) == code
    assert isinstance(exit_code(outcome), ExitCode)


def test_every_outcome_has_an_exit_code() -> None:
    assert set(OUTCOME_EXIT_CODES) == set(get_args(Outcome))


def test_run_ids() -> None:
    moment = dt.datetime(
        2026, 9, 27, 23, 4, 11, tzinfo=dt.timezone(dt.timedelta(hours=2))
    )
    run_id = new_run_id(moment)
    assert re.fullmatch(r"20260927T210411Z-[0-9a-f]{4}", run_id)
    assert re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{4}", new_run_id())
    assert len({new_run_id(moment) for _ in range(50)}) > 1


def test_timestamps_are_utc() -> None:
    moment = dt.datetime(
        2026, 9, 27, 23, 4, 11, 999, tzinfo=dt.timezone(dt.timedelta(hours=2))
    )
    assert utc_timestamp(moment) == "2026-09-27T21:04:11Z"
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", utc_timestamp())


def test_session_logs() -> None:
    assert session_log(1, "draft", "scope") == "sessions/001-scope-draft.jsonl"
    assert session_log(12, "critic") == "sessions/012-critic.jsonl"


def test_sessions_add_up_to_the_totals() -> None:
    run = manifest()
    assert run.next_session() == 1
    run.add_session(session(1, cost_usd=0.1, guard_denials=2))
    run.add_session(
        session(2, cost_usd=0.2, kind="critic", task=None, key=None, round=1)
    )
    assert run.totals.model_dump() == {
        "cost_usd": 0.3,
        "sessions": 2,
        "guard_denials": 2,
    }
    with pytest.raises(ValueError, match="session 5 added as session 3"):
        run.add_session(session(5))


def test_finish() -> None:
    run = manifest()
    assert (run.outcome, run.ended_at, run.exit_code) == (None, None, None)

    run.finish(
        "rate_limited",
        ended_at="2026-09-27T21:47:02Z",
        rate_limit_reset="2026-09-27T23:00:00Z",
    )

    assert run.outcome == "rate_limited"
    assert run.exit_code == ExitCode.RATE_LIMITED
    assert run.rate_limit_reset == "2026-09-27T23:00:00Z"
    run.finish("error", error="the executor crashed")
    assert (run.error, run.rate_limit_reset) == ("the executor crashed", None)
    assert run.ended_at is not None
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", run.ended_at)


def test_a_manifest_round_trips(tmp_path: Path) -> None:
    run = manifest()
    run.add_session(
        session(
            1,
            status="ok",
            validators=[
                ValidatorResult(name="max_lines", ok=True),
                ValidatorResult(
                    name="min_sources", ok=False, message="Too few.", repairable=False
                ),
            ],
        )
    )
    run.add_session(
        session(
            2,
            kind="fix",
            round=1,
            rejected_findings=[
                {"finding": "F3", "reason": "Would require inventing a price"}
            ],
            status="error",
            error="schema failure after one retry",
        )
    )
    run.finish("partial")
    state_dir = tmp_path / ".spadework"

    path = write_manifest(state_dir, run)

    assert path == state_dir / "runs" / run.run_id / "manifest.json"
    assert path == manifest_path(state_dir, run.run_id)
    loaded = load_manifest(path)
    assert loaded == run
    document = json.loads(path.read_text())
    assert document["sessions"][0]["validators"] == [
        {"name": "max_lines", "ok": True},
        {
            "name": "min_sources",
            "ok": False,
            "message": "Too few.",
            "repairable": False,
        },
    ]
    assert document["sessions"][1]["rejected_findings"] == [
        {
            "finding": "F3",
            "reason": "Would require inventing a price",
            "instruction": None,
        }
    ]
    assert list(document) == [
        "schema_version",
        "run_id",
        "spadework_version",
        "executor",
        "playbook",
        "limits",
        "started_at",
        "ended_at",
        "outcome",
        "error",
        "rate_limit_reset",
        "sessions",
        "totals",
    ]


def test_writing_again_replaces_the_manifest(tmp_path: Path) -> None:
    run = manifest()
    path = write_manifest(tmp_path, run)
    run.add_session(session(1))
    run.finish("complete")
    write_manifest(tmp_path, run)
    assert load_manifest(path).outcome == "complete"
    assert [p.name for p in path.parent.iterdir()] == ["manifest.json"]


def test_an_executor_without_a_version() -> None:
    run = RunManifest.model_validate(
        {**SPEC_EXAMPLE, "executor": {"name": "fake", "version": None}}
    )
    assert run.executor.version is None


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"run_id": "2026-09-27-3f9a"}, "run_id"),
        ({"started_at": "2026-09-27 21:04:11"}, "started_at"),
        ({"outcome": "done"}, "outcome"),
        ({"surprise": 1}, "surprise"),
        ({"schema_version": 3}, "schema_version"),
    ],
)
def test_invalid_manifests(changes: dict[str, Any], field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        RunManifest.model_validate({**SPEC_EXAMPLE, **changes})


def test_invalid_sessions() -> None:
    with pytest.raises(ValidationError, match="kind"):
        session(1, kind="lunch")
    with pytest.raises(ValidationError, match="cost_usd"):
        session(1, cost_usd=-1)
    with pytest.raises(ValidationError, match="n"):
        session(0)


def test_a_corrupt_manifest(tmp_path: Path) -> None:
    path = tmp_path / ".spadework" / "runs" / "20260927T210411Z-3f9a" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text("{")
    with pytest.raises(SpadeworkError) as caught:
        load_manifest(path)
    assert caught.value.exit_code == ExitCode.ERROR
    assert caught.value.message == (
        ".spadework/runs/20260927T210411Z-3f9a/manifest.json is corrupt: "
        "it is not valid JSON (line 1)"
    )


def test_a_newer_manifest(tmp_path: Path) -> None:
    path = tmp_path / "runs" / "20260927T210411Z-3f9a" / "manifest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({**SPEC_EXAMPLE, "schema_version": 2}))
    with pytest.raises(SpadeworkError, match="written by a newer version"):
        load_manifest(path)


def test_a_missing_manifest(tmp_path: Path) -> None:
    with pytest.raises(SpadeworkError, match="cannot read"):
        load_manifest(tmp_path / "runs" / "x" / "manifest.json")
