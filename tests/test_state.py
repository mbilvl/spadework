import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from spadework.errors import ExitCode
from spadework.keys import TaskMaterial, draft_material, hash_text, research_material
from spadework.plan import build_plan
from spadework.project import load_project
from spadework.state import (
    DerivedStatus,
    ReviewProgress,
    State,
    StateError,
    derive_statuses,
    file_hash,
    load_state,
    read_out_hashes,
    save_state,
    state_path,
    write_atomic,
)
from spadework.validators import ValidatorResult
from tests.conftest import MakeProject

ORDER = ["scope", "audience", "ia"]
DEPENDS = {"scope": [], "audience": ["scope"], "ia": ["scope", "audience"]}
PASS = [ValidatorResult(name="max_lines", ok=True)]
FAIL = [
    ValidatorResult(name="max_lines", ok=True),
    ValidatorResult(name="required_headings", ok=False, message="Missing 'Cost'."),
]


def materials(
    brief: str = "brief",
    *,
    task_sources: dict[str, str] | None = None,
    engine: str = "sha256:engine",
) -> dict[str, TaskMaterial]:
    """The current material of a three-task chain: scope → audience → ia."""
    sources = task_sources or {}
    built: dict[str, TaskMaterial] = {}
    for task in ORDER:
        research = None
        if task == "audience":
            research = research_material(
                engine=engine, public_context="pc", questions="q", model="sonnet"
            )
        draft = draft_material(
            engine=engine,
            playbook="discovery-website",
            playbook_version="0.1.0",
            task_source=sources.get(task, f"task {task}"),
            brief_source=brief,
            inputs={},
            model="sonnet",
            research_key=None if research is None else research.key,
            dependency_keys={dep: built[dep].key for dep in DEPENDS[task]},
        )
        built[task] = TaskMaterial(draft=draft, research=research)
    return built


def drafted(current: dict[str, TaskMaterial], *tasks: str) -> State:
    """A state in which ``tasks`` (default: all) were drafted and rendered."""
    state = State()
    for task in tasks or ORDER:
        state.record_draft(
            task,
            material=current[task],
            output_hash=hash_text(f"work {task}"),
            validators=PASS,
            run_id="20260927T210411Z-3f9a",
        )
        state.record_render(task, path=f"{task}.md", digest=hash_text(f"out {task}"))
    return state


def rendered(state: State) -> dict[str, str | None]:
    """out/ hashes with every rendered file as the engine left it."""
    return {
        entry.rendered.path: entry.rendered.hash
        for entry in state.tasks.values()
        if entry.rendered is not None
    }


def derive(
    state: State,
    current: dict[str, TaskMaterial],
    out: dict[str, str | None] | None = None,
    **kwargs: Any,
) -> dict[str, tuple[str, str | None]]:
    statuses = derive_statuses(
        ORDER, current, state, rendered(state) if out is None else out, **kwargs
    )
    return {task: (status.state, status.reason) for task, status in statuses.items()}


# Deriving states.


def test_every_task_is_pending_without_state() -> None:
    assert derive(State(), materials()) == {
        "scope": ("pending", None),
        "audience": ("pending", None),
        "ia": ("pending", None),
    }


def test_a_successful_draft_is_fresh() -> None:
    current = materials()
    assert derive(drafted(current), current) == {
        "scope": ("fresh", None),
        "audience": ("fresh", None),
        "ia": ("fresh", None),
    }


def test_a_change_propagates_to_dependents() -> None:
    state = drafted(materials())
    current = materials(task_sources={"scope": "task scope, edited"})

    assert derive(state, current) == {
        "scope": ("stale", "task file changed"),
        "audience": ("stale", "dependency scope is stale"),
        "ia": ("stale", "dependency audience is stale, dependency scope is stale"),
    }


def test_the_brief_is_named_with_the_dependencies() -> None:
    state = drafted(materials())
    statuses = derive_statuses(ORDER, materials("new brief"), state, rendered(state))
    assert statuses["ia"].reasons == (
        "brief changed",
        "dependency audience is stale",
        "dependency scope is stale",
    )
    assert statuses["ia"].reason == ", ".join(statuses["ia"].reasons)


def test_a_dependency_redrafted_since_is_named_as_changed() -> None:
    # `run --only scope` redrafted scope after the brief changed; its
    # dependents are stale because of it, and scope itself is fresh.
    new = materials("new brief")
    state = drafted(materials())
    state.record_draft(
        "scope",
        material=new["scope"],
        output_hash=hash_text("work scope 2"),
        validators=PASS,
        run_id=None,
    )

    assert derive(state, new) == {
        "scope": ("fresh", None),
        "audience": ("stale", "brief changed, dependency scope changed"),
        "ia": (
            "stale",
            "brief changed, dependency audience is stale, dependency scope changed",
        ),
    }


def test_invalid_keeps_its_state_until_the_key_changes() -> None:
    current = materials()
    state = drafted(current)
    state.record_draft(
        "scope",
        material=current["scope"],
        output_hash=hash_text("x"),
        validators=FAIL,
        run_id=None,
    )
    assert derive(state, current)["scope"] == (
        "invalid",
        "validators failed: required_headings",
    )
    assert derive(state, materials("new brief"))["scope"] == ("stale", "brief changed")


def test_failed_keeps_its_state_until_the_key_changes() -> None:
    current = materials()
    state = State()
    state.record_failure(
        "scope", material=current["scope"], error="the executor crashed", run_id="r"
    )
    assert derive(state, current)["scope"] == ("failed", "the executor crashed")
    assert derive(state, materials("new brief"))["scope"] == ("stale", "brief changed")


def test_force_makes_fresh_failed_and_invalid_tasks_stale() -> None:
    current = materials()
    state = drafted(current, "scope", "audience")
    state.record_failure(
        "audience", material=current["audience"], error="x", run_id=None
    )
    assert derive(state, current, force={"scope", "audience", "ia"}) == {
        "scope": ("stale", "--force"),
        "audience": ("stale", "--force"),
        "ia": ("pending", None),
    }


# Pinned outputs.


def test_an_edited_output_is_pinned() -> None:
    current = materials()
    state = drafted(current)
    out = {**rendered(state), "audience.md": hash_text("edited by hand")}

    assert derive(state, current, out) == {
        "scope": ("fresh", None),
        "audience": ("pinned", "out/audience.md edited"),
        "ia": ("fresh", None),
    }


def test_pinned_wins_over_stale_and_failed() -> None:
    state = drafted(materials())
    state.record_failure("ia", material=materials()["ia"], error="x", run_id=None)
    out = {
        **rendered(state),
        "scope.md": hash_text("edited"),
        "ia.md": hash_text("edited"),
    }

    statuses = derive(state, materials("new brief"), out)

    assert statuses["scope"] == ("pinned", "out/scope.md edited")
    assert statuses["ia"] == ("pinned", "out/ia.md edited")
    # A pinned task still has a new key, so its dependents are stale.
    assert statuses["audience"] == (
        "stale",
        "brief changed, dependency scope is stale",
    )


def test_force_regenerates_a_pinned_output() -> None:
    current = materials()
    state = drafted(current)
    out = {**rendered(state), "scope.md": hash_text("edited")}
    assert derive(state, current, out, force={"scope"})["scope"] == (
        "stale",
        "--force replaces your edits to out/scope.md",
    )


def test_a_deleted_output_is_not_pinned() -> None:
    current = materials()
    state = drafted(current)
    out: dict[str, str | None] = {**rendered(state), "scope.md": None}
    assert derive(state, current, out)["scope"] == ("fresh", None)
    del out["scope.md"]
    assert derive(state, current, out)["scope"] == ("fresh", None)


def test_a_task_never_rendered_cannot_be_pinned() -> None:
    current = materials()
    state = State()
    state.record_draft(
        "scope",
        material=current["scope"],
        output_hash="h",
        validators=PASS,
        run_id=None,
    )
    assert derive(state, current, {"scope.md": "anything"})["scope"] == ("fresh", None)


def test_read_out_hashes_reads_every_rendered_file(tmp_path: Path) -> None:
    state = drafted(materials(), "scope", "audience")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "scope.md").write_text("# Scope\n")

    assert read_out_hashes(tmp_path, state) == {
        "scope.md": hash_text("# Scope\n"),
        "audience.md": None,
    }
    assert file_hash(tmp_path / "out") is None
    assert file_hash(tmp_path / "out" / "scope.md" / "x") is None


# Review fixes.


def test_review_fixes_never_change_keys() -> None:
    current = materials()
    state = drafted(current)
    before = state.tasks["scope"]

    after = state.record_fix("scope", output_hash=hash_text("fixed"), validators=PASS)

    assert after.material == before.material
    assert after.key == before.key
    assert after.output is not None
    assert before.output is not None
    assert after.output.key == before.output.key
    assert after.output.hash == hash_text("fixed")
    assert after.rendered == before.rendered
    assert derive(state, current) == derive(drafted(current), current)


def test_a_fix_updates_the_validators() -> None:
    current = materials()
    state = drafted(current)
    assert state.record_fix("scope", output_hash="h1", validators=FAIL).status == (
        "invalid"
    )
    assert derive(state, current)["scope"][0] == "invalid"
    assert state.record_fix("scope", output_hash="h2", validators=PASS).status == (
        "fresh"
    )


def test_a_fix_does_not_clear_a_failure() -> None:
    current = materials()
    state = drafted(current)
    state.record_failure(
        "scope", material=materials("new")["scope"], error="x", run_id=None
    )
    assert (
        state.record_fix("scope", output_hash="h", validators=PASS).status == "failed"
    )


def test_a_fix_needs_an_output() -> None:
    state = State()
    state.record_failure("scope", material=materials()["scope"], error="x", run_id=None)
    with pytest.raises(ValueError, match="no output"):
        state.record_fix("scope", output_hash="h", validators=PASS)


# Recording.


def test_a_failure_keeps_the_earlier_output() -> None:
    old = materials()
    state = drafted(old)
    before = state.tasks["scope"]

    entry = state.record_failure(
        "scope", material=materials("new")["scope"], error="timed out", run_id="r2"
    )

    assert entry.status == "failed"
    assert entry.output == before.output
    assert entry.output is not None
    assert entry.output.key == old["scope"].key != entry.key
    assert entry.rendered == before.rendered
    assert entry.validators == before.validators
    assert (entry.error, entry.run_id) == ("timed out", "r2")


def test_a_redraft_keeps_the_rendered_file_until_the_next_render() -> None:
    current = materials()
    state = drafted(current)
    rendered_before = state.tasks["scope"].rendered
    entry = state.record_draft(
        "scope",
        material=materials("new")["scope"],
        output_hash="h",
        validators=PASS,
        run_id=None,
    )
    assert entry.rendered == rendered_before
    assert entry.error is None


def test_research_is_reused_while_its_key_matches() -> None:
    state = State()
    state.record_research(
        "audience", key="sha256:r1", record="records/audience.research.json", run_id="r"
    )
    assert state.research_record("audience", "sha256:r1") == (
        "records/audience.research.json"
    )
    assert state.research_record("audience", "sha256:r2") is None
    assert state.research_record("scope", "sha256:r1") is None


def test_review_progress() -> None:
    progress = ReviewProgress(run_id="20260927T210411Z-3f9a")
    assert (progress.round, progress.critic, progress.fixed) == (0, None, [])

    progress = progress.next_round().with_critic("records/critic-1.json")
    progress = progress.with_fixed("scope").with_fixed("ia").with_fixed("scope")
    assert (progress.round, progress.critic, progress.fixed) == (
        1,
        "records/critic-1.json",
        ["scope", "ia"],
    )
    progress = progress.next_round()
    assert (progress.round, progress.critic, progress.fixed) == (2, None, [])


def test_an_interrupted_review_resumes_its_round(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    state = drafted(materials())
    state.review = (
        ReviewProgress(run_id="20260927T210411Z-3f9a")
        .next_round()
        .with_critic("records/critic-1.json")
        .with_fixed("scope")
    )
    save_state(path, state)

    resumed = load_state(path).review

    assert resumed is not None
    assert (resumed.run_id, resumed.round, resumed.critic, resumed.fixed) == (
        "20260927T210411Z-3f9a",
        1,
        "records/critic-1.json",
        ["scope"],
    )


def test_statuses_feed_the_plan(make_project: MakeProject) -> None:
    project = load_project(make_project())
    derived = {
        "overview": DerivedStatus.of("fresh"),
        "landscape": DerivedStatus.of("stale", ["brief changed", "inputs/x.md added"]),
        "plan": DerivedStatus.of("pending"),
    }

    plan = build_plan(project, derived)

    assert [(t.id, t.state, t.reason, t.will_run) for t in plan.tasks] == [
        ("overview", "fresh", None, False),
        ("landscape", "stale", "brief changed, inputs/x.md added", True),
        ("plan", "pending", None, True),
    ]


# Reading and writing.


def test_a_missing_file_is_an_empty_state(tmp_path: Path) -> None:
    state = load_state(state_path(tmp_path))
    assert state == State()
    assert state.schema_version == 1


def test_state_round_trips(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    state = drafted(materials())
    state.record_failure("ia", material=materials("new")["ia"], error="x", run_id="r")
    state.record_research(
        "audience", key="sha256:r", record="records/a.json", run_id=None
    )
    state.tasks["audience"] = state.tasks["audience"].model_copy(
        update={"validators": FAIL}
    )

    save_state(path, state)
    loaded = load_state(path)

    assert loaded == state
    assert path.read_text().endswith("}\n")
    document = json.loads(path.read_text())
    assert document["schema_version"] == 1
    assert document["tasks"]["audience"]["validators"][0] == {
        "name": "max_lines",
        "ok": True,
    }
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in path.parent.iterdir()) == ["state.json"]


def test_corrupt_json_is_an_error(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir()
    path.write_text('{"schema_version": 1,\n "tasks": {')

    with pytest.raises(StateError) as caught:
        load_state(path)

    error = caught.value
    assert error.exit_code == ExitCode.ERROR
    assert (
        error.message
        == ".spadework/state.json is corrupt: it is not valid JSON (line 2)"
    )
    assert error.hint.startswith("Move .spadework/state.json aside and run again")
    assert "Back up out/" in error.hint


@pytest.mark.parametrize(
    ("document", "message"),
    [
        ([], "the top level: Input should be an object"),
        (
            {"schema_version": 1, "tasks": {"scope": {}}},
            "tasks.scope.status: Field required",
        ),
        ({"schema_version": 1, "extra": 1}, "extra: Extra inputs are not permitted"),
        ({"schema_version": 0}, "schema_version: Value error"),
        ({"schema_version": "2"}, "schema_version: Input should be a valid integer"),
    ],
)
def test_a_file_of_the_wrong_shape_is_corrupt(
    tmp_path: Path, document: Any, message: str
) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps(document))
    with pytest.raises(StateError, match="is corrupt: ") as caught:
        load_state(path)
    assert message in caught.value.message


def test_a_newer_schema_is_an_error(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.parent.mkdir()
    path.write_text(json.dumps({"schema_version": 2, "whatever": True}))

    with pytest.raises(StateError) as caught:
        load_state(path)

    assert caught.value.message == (
        ".spadework/state.json was written by a newer version of spadework "
        "(schema_version 2; this version reads 1)"
    )
    assert caught.value.hint.startswith("Upgrade spadework. Move ")


def test_an_unreadable_file_is_an_error(tmp_path: Path) -> None:
    path = state_path(tmp_path)
    path.mkdir(parents=True)  # a folder where the file should be
    with pytest.raises(StateError, match=r"cannot read \.spadework/state\.json"):
        load_state(path)


# Crash safety.


def _crash(*args: Any, **kwargs: Any) -> None:
    raise OSError(28, "No space left on device")


@pytest.mark.parametrize("step", ["fsync", "replace"])
def test_a_crash_mid_write_leaves_the_old_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, step: str
) -> None:
    path = state_path(tmp_path)
    old = drafted(materials())
    save_state(path, old)
    before = path.read_bytes()
    monkeypatch.setattr(os, step, _crash)

    with pytest.raises(OSError, match="No space"):
        save_state(path, State())

    assert path.read_bytes() == before
    assert load_state(path) == old
    assert sorted(p.name for p in path.parent.iterdir()) == ["state.json"]


def test_a_crash_while_writing_the_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "file.json"
    path.write_bytes(b"old")
    real_fdopen = os.fdopen

    class Torn:
        def __init__(self, fd: int, mode: str) -> None:
            self.file = real_fdopen(fd, mode)

        def __enter__(self) -> "Torn":
            return self

        def __exit__(self, *exc: object) -> None:
            self.file.close()

        def write(self, data: bytes) -> None:
            self.file.write(data[:3])
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "fdopen", Torn)

    with pytest.raises(KeyboardInterrupt):
        write_atomic(path, b"new contents")

    assert path.read_bytes() == b"old"
    assert [p.name for p in tmp_path.iterdir()] == ["file.json"]


def test_write_atomic_creates_the_folder(tmp_path: Path) -> None:
    path = tmp_path / "a" / "b" / "file.json"
    write_atomic(path, b"x")
    write_atomic(path, b"y")
    assert path.read_bytes() == b"y"
    assert [p.name for p in path.parent.iterdir()] == ["file.json"]
