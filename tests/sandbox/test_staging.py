from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from spadework.sandbox.policy import is_within, load
from spadework.sandbox.staging import (
    DEFAULT_TOTAL_CALLS,
    CollectedOutput,
    Staging,
    StagingError,
    build_policy,
)

DEADLINE = 1_790_000_600.0


@pytest.fixture
def root(tmp_path: Path) -> Path:
    project = Path(os.path.realpath(tmp_path)) / "acme-fabrication"
    (project / "inputs").mkdir(parents=True)
    (project / "brief.md").write_text("# Brief\n", encoding="utf-8")
    (project / "inputs" / "2026-09-01-call.md").write_text("Notes\n", encoding="utf-8")
    return project


@pytest.fixture
def temp_root(tmp_path: Path) -> Path:
    path = Path(os.path.realpath(tmp_path)) / "tmp"
    path.mkdir()
    return path


def draft(root: Path, output: str = "01-scope.md") -> Staging:
    return Staging.create(root, "001-scope-draft", "draft", output=output)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


# Creating


def test_writing_session_is_staged_under_the_project(root: Path) -> None:
    staging = draft(root)
    paths = staging.paths

    assert paths.workdir == root / ".spadework" / "sessions" / "001-scope-draft"
    assert paths.control_dir == root / ".spadework" / "control"
    assert paths.policy == paths.control_dir / "001-scope-draft.policy.json"
    assert paths.settings == paths.control_dir / "001-scope-draft.settings.json"
    assert paths.counters == paths.control_dir / "001-scope-draft.counters.json"
    assert paths.denials == paths.control_dir / "001-scope-draft.denials.jsonl"
    assert paths.lock == paths.control_dir / "001-scope-draft.counters.json.lock"
    assert paths.marker == paths.control_dir / "001-scope-draft.alive"
    assert paths.marker in paths.control_files()
    assert list(paths.workdir.iterdir()) == []
    assert mode(paths.workdir) & 0o077 == 0
    assert mode(paths.control_dir) & 0o077 == 0


def test_control_files_are_never_inside_the_workdir(
    root: Path, temp_root: Path
) -> None:
    """I4."""
    for kind, output in [("draft", "x.md"), ("critic", None), ("research", None)]:
        staging = Staging.create(
            root, f"s-{kind}", kind, output=output, temp_root=temp_root
        )
        for path in staging.paths.control_files():
            assert not is_within(str(path), str(staging.paths.workdir)), path


def test_research_session_is_staged_in_a_neutral_empty_temp_dir(
    root: Path, temp_root: Path
) -> None:
    """The model sees its cwd, and the project path could name the client."""
    staging = Staging.create(
        root, "003-references-research", "research", temp_root=temp_root
    )
    workdir = staging.paths.workdir

    assert workdir.parent == temp_root
    assert workdir.name.startswith("spadework-")
    assert "acme" not in str(workdir)
    assert "references" not in str(workdir)
    assert list(workdir.iterdir()) == []
    assert mode(workdir) & 0o077 == 0
    assert staging.paths.control_dir == root / ".spadework" / "control"


def test_research_workdir_uses_the_os_temp_dir_by_default(root: Path) -> None:
    staging = Staging.create(root, "003-references-research", "research")
    try:
        assert not is_within(str(staging.paths.workdir), str(root))
        assert staging.paths.workdir == Path(os.path.realpath(staging.paths.workdir))
    finally:
        staging.cleanup()


def test_paths_are_fully_resolved(tmp_path: Path) -> None:
    real = Path(os.path.realpath(tmp_path)) / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)

    staging = Staging.create(alias, "s1", "draft", output="out.md")

    assert staging.project_root == real
    assert staging.paths.workdir == real / ".spadework" / "sessions" / "s1"


def test_create_replaces_stale_files_for_the_same_id(root: Path) -> None:
    """I4: policies and settings are never reused between sessions."""
    first = draft(root)
    first.add_text("old.md", "old")
    first.write_policy(deadline_epoch=DEADLINE)
    first.paths.settings.write_text("{}", encoding="utf-8")
    first.paths.counters.write_text('{"total": 79}', encoding="utf-8")
    first.paths.marker.write_text("001-scope-draft", encoding="utf-8")

    second = draft(root)

    assert list(second.paths.workdir.iterdir()) == []
    for path in second.paths.control_files():
        assert not path.exists(), path


def test_create_replaces_a_stale_symlinked_workdir(root: Path, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "keep.md").write_text("keep", encoding="utf-8")
    sessions = root / ".spadework" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "001-scope-draft").symlink_to(elsewhere)

    staging = draft(root)

    assert not staging.paths.workdir.is_symlink()
    assert (elsewhere / "keep.md").exists()


@pytest.mark.parametrize(
    ("kind", "output", "message"),
    [
        pytest.param("draft", None, "needs an output file", id="draft-without-output"),
        pytest.param(
            "summary", None, "needs an output file", id="summary-without-output"
        ),
        pytest.param("critic", "x.md", "writes nothing", id="critic-with-output"),
        pytest.param("research", "x.md", "writes nothing", id="research-with-output"),
        pytest.param("shell", None, "unknown session kind", id="unknown-kind"),
        pytest.param(
            "draft", "../x.md", "not a plain relative path", id="output-dotdot"
        ),
        pytest.param(
            "draft", "/tmp/x.md", "not a plain relative path", id="output-absolute"
        ),
    ],
)
def test_create_rejects(
    root: Path, kind: str, output: str | None, message: str
) -> None:
    with pytest.raises(StagingError, match=message):
        Staging.create(root, "s1", kind, output=output)


@pytest.mark.parametrize("session_id", ["", "../x", "a/b", ".hidden", "x" * 129])
def test_create_rejects_bad_session_ids(root: Path, session_id: str) -> None:
    with pytest.raises(StagingError, match="invalid session id"):
        Staging.create(root, session_id, "critic")


def test_output_in_a_subdirectory(root: Path) -> None:
    staging = draft(root, output="out/01-scope.md")

    assert staging.output_path == staging.paths.workdir / "out" / "01-scope.md"
    assert staging.output_path.parent.is_dir()


# Staging files


def test_add_file_makes_a_read_only_copy(root: Path) -> None:
    staging = draft(root)

    copy = staging.add_file(root / "inputs" / "2026-09-01-call.md", "inputs/call.md")

    assert copy == staging.paths.workdir / "inputs" / "call.md"
    assert copy.read_text(encoding="utf-8") == "Notes\n"
    assert not copy.is_symlink()
    assert mode(copy) == 0o444
    (root / "inputs" / "2026-09-01-call.md").write_text("Changed\n", encoding="utf-8")
    assert copy.read_text(encoding="utf-8") == "Notes\n"


def test_add_file_accepts_engine_work_files(root: Path) -> None:
    work = root / ".spadework" / "work"
    work.mkdir(parents=True)
    (work / "02-ia.md").write_text("IA", encoding="utf-8")

    copy = draft(root).add_file(work / "02-ia.md", "deps/02-ia.md")

    assert copy.read_text(encoding="utf-8") == "IA"


def test_add_file_refuses_symlinks(root: Path, tmp_path: Path) -> None:
    (root / "link.md").symlink_to(root / "brief.md")

    with pytest.raises(StagingError, match="is a symlink"):
        draft(root).add_file(root / "link.md", "brief.md")


def test_add_file_refuses_files_outside_the_project(root: Path, tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")

    with pytest.raises(StagingError, match="outside the project"):
        draft(root).add_file(secret, "secret.txt")
    with pytest.raises(StagingError, match="outside the project"):
        draft(root).add_file(root / ".." / "secret.txt", "secret.txt")


def test_add_file_refuses_a_path_through_a_symlinked_directory(
    root: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET", encoding="utf-8")
    (root / "inputs" / "linked").symlink_to(outside)

    with pytest.raises(StagingError, match="outside the project"):
        draft(root).add_file(root / "inputs" / "linked" / "secret.txt", "secret.txt")


def test_add_file_refuses_control_files(root: Path) -> None:
    """I4: nothing from the control plane is staged."""
    staging = draft(root)
    staging.write_policy(deadline_epoch=DEADLINE)

    with pytest.raises(StagingError, match="control file"):
        staging.add_file(staging.paths.policy, "policy.json")


def test_add_file_refuses_anything_but_regular_files(root: Path) -> None:
    staging = draft(root)
    os.mkfifo(root / "pipe")

    with pytest.raises(StagingError, match="not a regular file"):
        staging.add_file(root / "inputs", "inputs")
    with pytest.raises(StagingError, match="not a regular file"):
        staging.add_file(root / "pipe", "pipe")
    with pytest.raises(StagingError, match="cannot read"):
        staging.add_file(root / "missing.md", "missing.md")


def test_collect_ignores_an_output_that_is_a_directory(root: Path) -> None:
    staging = draft(root)
    staging.output_path.mkdir()

    assert staging.collect() == CollectedOutput(None, ())


@pytest.mark.parametrize(
    "dest",
    ["", "/etc/x", "../x", "a/../b", "./a", "a//b", "a/", "a\\b", "a\x00b"],
)
def test_add_rejects_unsafe_destinations(root: Path, dest: str) -> None:
    with pytest.raises(StagingError, match="not a plain relative path"):
        draft(root).add_text(dest, "x")


def test_add_rejects_duplicates_and_the_output(root: Path) -> None:
    staging = draft(root)
    staging.add_text("notes.md", "x")

    with pytest.raises(StagingError, match="already staged"):
        staging.add_text("notes.md", "y")
    with pytest.raises(StagingError, match="use seed_output"):
        staging.add_text("01-scope.md", "y")


def test_research_sessions_are_staged_empty(root: Path, temp_root: Path) -> None:
    """I5."""
    staging = Staging.create(root, "r1", "research", temp_root=temp_root)

    with pytest.raises(StagingError, match="staged empty"):
        staging.add_file(root / "brief.md", "brief.md")
    with pytest.raises(StagingError, match="staged empty"):
        staging.add_text("notes.md", "x")
    assert list(staging.paths.workdir.iterdir()) == []


def test_seed_output_is_writable(root: Path) -> None:
    staging = Staging.create(root, "001-scope-fix", "fix", output="01-scope.md")

    path = staging.seed_output("# Scope\n")

    assert path == staging.output_path
    assert path.read_text(encoding="utf-8") == "# Scope\n"
    assert mode(path) == 0o644
    with pytest.raises(StagingError, match=r"already staged|cannot stage"):
        staging.seed_output(b"again")


def test_seed_output_needs_an_output(root: Path) -> None:
    staging = Staging.create(root, "c1", "critic")

    with pytest.raises(StagingError, match="no output"):
        staging.seed_output("x")
    with pytest.raises(StagingError, match="no output"):
        _ = staging.output_path


# Policies


def test_draft_policy(root: Path) -> None:
    staging = draft(root)

    policy = staging.write_policy(deadline_epoch=DEADLINE)

    assert load(staging.paths.policy) == policy
    assert policy.session_id == "001-scope-draft"
    assert policy.workdir == str(staging.paths.workdir)
    assert policy.allowed_tools == (
        "Read",
        "Glob",
        "Grep",
        "Write",
        "Edit",
        "StructuredOutput",
    )
    assert policy.writable == (str(staging.output_path),)
    assert policy.deadline_epoch == DEADLINE
    assert dict(policy.caps) == {"total": DEFAULT_TOTAL_CALLS}
    assert policy.counters_path == str(staging.paths.counters)
    assert policy.denials_path == str(staging.paths.denials)


def test_research_policy(root: Path, temp_root: Path) -> None:
    staging = Staging.create(root, "r1", "research", temp_root=temp_root)

    policy = staging.write_policy(
        deadline_epoch=DEADLINE, max_searches=6, max_fetches=8, total_calls=40
    )

    assert policy.allowed_tools == ("WebSearch", "WebFetch", "StructuredOutput")
    assert policy.writable == ()
    assert dict(policy.caps) == {"total": 40, "WebSearch": 6, "WebFetch": 8}


def test_critic_policy_writes_nothing(root: Path) -> None:
    """I3."""
    policy = Staging.create(root, "c1", "critic").write_policy(deadline_epoch=DEADLINE)

    assert policy.allowed_tools == ("Read", "Glob", "Grep", "StructuredOutput")
    assert policy.writable == ()


def test_research_policy_needs_caps(root: Path, temp_root: Path) -> None:
    staging = Staging.create(root, "r1", "research", temp_root=temp_root)

    with pytest.raises(StagingError, match="max_searches and max_fetches"):
        staging.write_policy(deadline_epoch=DEADLINE, max_searches=6)


def test_only_research_policies_have_web_caps(root: Path) -> None:
    with pytest.raises(StagingError, match="no web tools"):
        draft(root).write_policy(deadline_epoch=DEADLINE, max_fetches=3)


def test_invalid_policy_values_are_staging_errors(root: Path) -> None:
    with pytest.raises(StagingError, match="deadline_epoch"):
        draft(root).write_policy(deadline_epoch=-1)


# Collecting


def test_collect_returns_the_output_and_ignores_staged_files(root: Path) -> None:
    staging = draft(root)
    staging.add_file(root / "brief.md", "brief.md")
    staging.output_path.write_text("# Scope\n", encoding="utf-8")

    assert staging.collect() == CollectedOutput(b"# Scope\n", ())


def test_collect_without_output(root: Path) -> None:
    assert draft(root).collect() == CollectedOutput(None, ())


def test_collect_lists_unexpected_files(root: Path, tmp_path: Path) -> None:
    """I3: extra files are reported and never copied back."""
    staging = draft(root)
    workdir = staging.paths.workdir
    staging.output_path.write_text("ok", encoding="utf-8")
    (workdir / "sneaky.md").write_text("x", encoding="utf-8")
    (workdir / "deep" / "er").mkdir(parents=True)
    (workdir / "deep" / "er" / "x.txt").write_text("x", encoding="utf-8")
    (workdir / "link").symlink_to(tmp_path)
    (workdir / "file-link").symlink_to(root / "brief.md")

    collected = staging.collect()

    assert collected.content == b"ok"
    assert collected.extras == ("deep/er/x.txt", "file-link", "link", "sneaky.md")


def test_collect_does_not_follow_a_symlinked_output(root: Path) -> None:
    staging = draft(root)
    staging.output_path.symlink_to(root / "brief.md")

    assert staging.collect() == CollectedOutput(None, ("01-scope.md",))


def test_collect_in_a_research_session(root: Path, temp_root: Path) -> None:
    staging = Staging.create(root, "r1", "research", temp_root=temp_root)
    (staging.paths.workdir / "notes.md").write_text("x", encoding="utf-8")

    assert staging.collect() == CollectedOutput(None, ("notes.md",))


# Guard records and cleanup


def test_counters_and_denials(root: Path) -> None:
    staging = draft(root)
    assert staging.counters() == {}
    assert staging.denials() == []

    staging.paths.counters.write_text('{"Read": 2, "total": 2}', encoding="utf-8")
    record = {
        "ts": "2026-09-27T21:04:11Z",
        "tool_name": "Bash",
        "reason": "r",
        "target": "",
    }
    staging.paths.denials.write_text(
        json.dumps(record) + "\n\nnot json\n" + json.dumps([1]) + "\n", encoding="utf-8"
    )

    assert staging.counters() == {"Read": 2, "total": 2}
    assert staging.denials() == [
        record,
        {"reason": "unreadable denial record"},
        {"reason": "unreadable denial record"},
    ]


def test_cleanup_removes_everything(root: Path, temp_root: Path) -> None:
    for kind, output in [("draft", "01-scope.md"), ("research", None)]:
        staging = Staging.create(
            root, f"s-{kind}", kind, output=output, temp_root=temp_root
        )
        if kind == "draft":
            staging.add_file(root / "brief.md", "inputs/brief.md")
        policy = staging.write_policy(
            deadline_epoch=DEADLINE,
            max_searches=1 if kind == "research" else None,
            max_fetches=1 if kind == "research" else None,
        )
        staging.paths.settings.write_text("{}", encoding="utf-8")
        Path(policy.lock_path).touch()

        assert staging.cleanup() is True

        assert not staging.paths.workdir.exists()
        for path in staging.paths.control_files():
            assert not path.exists(), path
    assert (root / "brief.md").exists()
    assert list(temp_root.iterdir()) == []


def test_debug_keeps_the_staging_directory(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SPADEWORK_DEBUG", "1")
    staging = draft(root)
    staging.write_policy(deadline_epoch=DEADLINE)

    assert staging.cleanup() is False

    assert staging.paths.workdir.exists()
    assert staging.paths.policy.exists()


def test_debug_zero_does_not_keep(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SPADEWORK_DEBUG", "0")

    assert draft(root).cleanup() is True


def test_build_policy_checks_the_kind_and_output(root: Path) -> None:
    paths = draft(root).paths

    with pytest.raises(StagingError, match="unknown session kind"):
        build_policy(paths, "shell", writable=None, deadline_epoch=DEADLINE)
    with pytest.raises(StagingError, match="exactly one writable output"):
        build_policy(paths, "draft", writable=None, deadline_epoch=DEADLINE)
    with pytest.raises(StagingError, match="exactly one writable output"):
        build_policy(
            paths, "critic", writable=paths.workdir / "x.md", deadline_epoch=DEADLINE
        )


def test_create_refuses_a_workdir_inside_the_control_dir(root: Path) -> None:
    control = root / ".spadework" / "control"
    control.mkdir(parents=True)

    with pytest.raises(StagingError, match="overlap"):
        Staging.create(root, "r1", "research", temp_root=control)
