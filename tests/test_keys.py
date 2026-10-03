import re
from pathlib import Path
from typing import Any

import pytest

from spadework.config import ModelsSection
from spadework.keys import (
    DraftMaterial,
    ResearchMaterial,
    TaskMaterial,
    canonical_json,
    draft_key,
    draft_material,
    hash_bytes,
    hash_file,
    hash_json,
    hash_text,
    research_key,
    research_material,
    stale_reasons,
    task_materials,
)
from spadework.project import load_project
from tests.conftest import BRIEF, MakeProject

ENGINE = "sha256:engine-v1"

RESEARCH: dict[str, Any] = {
    "engine": ENGINE,
    "public_context": "A stainless-steel fabricator.",
    "questions": "- five comparable companies",
    "model": "sonnet",
}

DRAFT: dict[str, Any] = {
    "engine": ENGINE,
    "playbook": "discovery-website",
    "playbook_version": "0.1.0",
    "task_source": "---\ntitle: Scope\n---\nBody",
    "brief_source": BRIEF,
    "inputs": {"calls/one.md": hash_text("one"), "two.txt": hash_text("two")},
    "model": "sonnet",
    "research_key": None,
    "dependency_keys": {"audience": "sha256:aaa", "scope": "sha256:bbb"},
}


def _task(**changes: Any) -> TaskMaterial:
    """A research task's material, with ``changes`` to the draft arguments."""
    research_changes = changes.pop("research", {})
    research = research_material(**{**RESEARCH, **research_changes})
    draft = draft_material(**{**DRAFT, "research_key": research.key, **changes})
    return TaskMaterial(draft=draft, research=research)


# Hashing.


def test_canonical_json_sorts_keys_and_drops_spaces() -> None:
    assert canonical_json({"b": [1, 2], "a": {"d": None, "c": "é"}}) == (
        '{"a":{"c":"é","d":null},"b":[1,2]}'.encode()
    )
    assert canonical_json({"x": 1, "y": 2}) == canonical_json({"y": 2, "x": 1})


def test_canonical_json_refuses_nan() -> None:
    with pytest.raises(ValueError, match="JSON"):
        canonical_json({"cost": float("nan")})


def test_hashes_are_sha256_with_a_prefix(tmp_path: Path) -> None:
    empty = "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    assert hash_bytes(b"") == empty
    assert hash_text("") == empty
    path = tmp_path / "file.md"
    path.write_bytes(b"x" * 200_000)
    assert hash_file(path) == hash_bytes(b"x" * 200_000)
    assert re.fullmatch(r"sha256:[0-9a-f]{64}", hash_json({"a": 1}))


# Keys.


def test_research_key_covers_each_component() -> None:
    base = research_key(**RESEARCH)
    assert base == research_key(**RESEARCH)
    for field, value in [
        ("engine", "sha256:engine-v2"),
        ("public_context", "A fabricator in Portugal."),
        ("questions", "- ten comparable companies"),
        ("model", "opus"),
    ]:
        assert research_key(**{**RESEARCH, field: value}) != base, field


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("engine", "sha256:engine-v2"),
        ("playbook", "quickstart"),
        ("playbook_version", "0.2.0"),
        ("task_source", "---\ntitle: Scope\n---\nNew body"),
        ("brief_source", BRIEF + "\nMore.\n"),
        ("inputs", {"calls/one.md": hash_text("one")}),
        ("inputs", {"calls/one.md": hash_text("ONE"), "two.txt": hash_text("two")}),
        ("model", "opus"),
        ("research_key", "sha256:research"),
        ("dependency_keys", {"audience": "sha256:aaa", "scope": "sha256:ccc"}),
    ],
)
def test_draft_key_covers_each_component(field: str, value: Any) -> None:
    assert draft_key(**{**DRAFT, field: value}) != draft_key(**DRAFT)


def test_draft_key_ignores_the_order_of_inputs_and_dependencies() -> None:
    reordered = {
        **DRAFT,
        "inputs": dict(reversed(DRAFT["inputs"].items())),
        "dependency_keys": dict(reversed(DRAFT["dependency_keys"].items())),
    }
    assert draft_key(**reordered) == draft_key(**DRAFT)


def test_the_key_is_the_hash_of_the_material() -> None:
    material = draft_material(**DRAFT)
    assert material.key == draft_key(**DRAFT)
    # Without appendices, ``prompts`` is left out: keys stay as they were.
    assert material.key == hash_json(
        material.model_dump(mode="json", exclude={"prompts"})
    )
    with_prompts = draft_material(**DRAFT, appendices={"rules": "Be brief."})
    assert with_prompts.key == hash_json(with_prompts.model_dump(mode="json"))
    task = _task()
    assert task.key == task.draft.key
    assert task.research_key == research_key(**RESEARCH)
    assert task.draft.research == task.research_key


def test_material_holds_hashes_not_content() -> None:
    material = _task()
    dumped = material.model_dump_json()
    assert BRIEF not in dumped
    assert RESEARCH["public_context"] not in dumped
    assert material.draft.brief == hash_text(BRIEF)


def test_material_round_trips_through_json() -> None:
    material = _task()
    loaded = TaskMaterial.model_validate_json(material.model_dump_json())
    assert loaded == material
    assert loaded.key == material.key


# Stale reasons.


@pytest.mark.parametrize(
    ("changes", "reasons"),
    [
        ({"brief_source": BRIEF + "\nMore.\n"}, ["brief changed"]),
        (
            {"inputs": {**DRAFT["inputs"], "2026-09-30-call.md": hash_text("c")}},
            ["inputs/2026-09-30-call.md added"],
        ),
        (
            {"inputs": {**DRAFT["inputs"], "two.txt": hash_text("TWO")}},
            ["inputs/two.txt changed"],
        ),
        (
            {"inputs": {"two.txt": DRAFT["inputs"]["two.txt"]}},
            ["inputs/calls/one.md removed"],
        ),
        ({"task_source": "---\ntitle: Scope\n---\nNew"}, ["task file changed"]),
        ({"playbook_version": "0.2.0"}, ["playbook 0.1.0 → 0.2.0"]),
        ({"playbook": "quickstart"}, ["playbook discovery-website → quickstart"]),
        ({"model": "opus"}, ["draft model sonnet → opus"]),
        (
            {"research": {"questions": "- ten companies"}},
            ["research questions changed"],
        ),
        ({"research": {"public_context": "Elsewhere."}}, ["public context changed"]),
        ({"research": {"model": "haiku"}}, ["research model sonnet → haiku"]),
        (
            {"engine": "sha256:engine-v2", "research": {"engine": "sha256:engine-v2"}},
            ["engine prompts changed"],
        ),
        (
            {"dependency_keys": {**DRAFT["dependency_keys"], "scope": "sha256:new"}},
            ["dependency scope is stale"],
        ),
    ],
)
def test_every_stale_reason(changes: dict[str, Any], reasons: list[str]) -> None:
    old = _task()
    new = _task(**changes)
    assert new.key != old.key
    assert stale_reasons(old, new) == reasons


def test_no_reasons_when_the_keys_match() -> None:
    assert stale_reasons(_task(), _task()) == []


def test_several_reasons_are_all_given() -> None:
    old = _task()
    new = _task(
        brief_source=BRIEF + "\nMore.\n",
        inputs={"two.txt": hash_text("TWO"), "new.md": hash_text("n")},
        dependency_keys={"audience": "sha256:new", "scope": "sha256:bbb"},
    )
    assert stale_reasons(old, new) == [
        "brief changed",
        "inputs/calls/one.md removed",
        "inputs/new.md added",
        "inputs/two.txt changed",
        "dependency audience is stale",
    ]


def test_a_dependency_gives_its_name_not_its_reasons() -> None:
    old = _task()
    new = _task(dependency_keys={**DRAFT["dependency_keys"], "audience": "sha256:x"})
    assert stale_reasons(
        old, new, changed_dependencies={"audience": "dependency audience changed"}
    ) == ["dependency audience changed"]


def test_an_engine_change_is_named_once() -> None:
    old = _task()
    new = _task(research={"engine": "sha256:engine-v2"})
    # Only the research engine differs here; the draft's own engine is the same.
    assert stale_reasons(old, new) == ["engine prompts changed"]


def test_a_backstop_reason_when_nothing_else_explains_it() -> None:
    old = _task()
    changed = old.draft.model_copy(update={"research": "sha256:elsewhere"})
    new = TaskMaterial(draft=changed, research=old.research)
    assert stale_reasons(old, new) == ["fingerprint changed"]


def test_adding_research_is_a_task_file_change() -> None:
    plain = TaskMaterial(draft=draft_material(**DRAFT), research=None)
    with_research = _task(task_source="---\ntitle: Scope\nresearch: …\n---\nBody")
    assert stale_reasons(plain, with_research) == ["task file changed"]


# Keys for a whole project.


def test_task_materials_for_a_project(make_project: MakeProject) -> None:
    project = load_project(
        make_project(inputs={"calls/one.md": "# One", "two.txt": "x", "notes.md": "n"})
    )

    materials = task_materials(project, engine=ENGINE)

    assert list(materials) == ["overview", "landscape", "plan"]
    overview, landscape, plan = materials.values()
    assert overview.research is None
    assert landscape.research is not None
    assert landscape.research.model == "sonnet"
    assert landscape.draft.research == landscape.research_key
    assert list(overview.draft.inputs) == ["calls/one.md", "notes.md", "two.txt"]
    assert list(landscape.draft.inputs) == ["calls/one.md", "two.txt"]
    assert plan.draft.inputs == {}
    assert plan.draft.model == "opus"
    assert landscape.draft.dependencies == {"overview": overview.key}
    assert plan.draft.dependencies == {
        "landscape": landscape.key,
        "overview": overview.key,
    }
    assert overview.draft.task == hash_text(project.playbook.task("overview").source)
    assert overview.draft.brief == hash_text(project.brief.source)
    assert (overview.draft.playbook, overview.draft.playbook_version) == (
        "fixture",
        "1.0.0",
    )
    assert task_materials(project, engine=ENGINE) == materials


def test_research_uses_the_draft_tier_model(make_project: MakeProject) -> None:
    project = load_project(make_project())
    models = ModelsSection(draft="haiku", review="opus")

    materials = task_materials(project, engine=ENGINE, models=models)

    landscape = materials["landscape"].research
    assert landscape is not None
    assert landscape.model == "haiku"
    assert materials["plan"].draft.model == "opus"


def _reasons(before: dict[str, TaskMaterial], after: dict[str, TaskMaterial]) -> Any:
    return {task: stale_reasons(before[task], after[task]) for task in before}


def test_editing_the_brief_body_keeps_the_research(make_project: MakeProject) -> None:
    root = make_project()
    before = task_materials(load_project(root), engine=ENGINE)
    brief = root / "brief.md"
    brief.write_text(brief.read_text() + "\nThey also make sinks.\n")

    after = task_materials(load_project(root), engine=ENGINE)

    assert after["landscape"].research_key == before["landscape"].research_key
    assert _reasons(before, after) == {
        "overview": ["brief changed"],
        "landscape": ["brief changed", "dependency overview is stale"],
        "plan": [
            "brief changed",
            "dependency landscape is stale",
            "dependency overview is stale",
        ],
    }


def test_public_context_changes_the_research(make_project: MakeProject) -> None:
    root = make_project()
    before = task_materials(load_project(root), engine=ENGINE)
    brief = root / "brief.md"
    brief.write_text(brief.read_text().replace("stainless-steel", "copper"))

    after = task_materials(load_project(root), engine=ENGINE)

    # The questions render public_context, so they change with it.
    assert _reasons(before, after)["landscape"] == [
        "brief changed",
        "public context changed",
        "research questions changed",
        "dependency overview is stale",
    ]


def test_an_input_makes_only_the_tasks_that_match_it_stale(
    make_project: MakeProject,
) -> None:
    root = make_project(inputs={"calls/one.md": "# One"})
    before = task_materials(load_project(root), engine=ENGINE)
    (root / "inputs" / "notes.md").write_text("# Notes\n")

    after = task_materials(load_project(root), engine=ENGINE)

    # landscape's globs (calls/**, *.txt) do not match notes.md, and plan
    # has no inputs: they are stale only through their dependencies.
    assert _reasons(before, after) == {
        "overview": ["inputs/notes.md added"],
        "landscape": ["dependency overview is stale"],
        "plan": ["dependency landscape is stale", "dependency overview is stale"],
    }


def test_engine_prompts_change_every_key(make_project: MakeProject) -> None:
    project = load_project(make_project())
    before = task_materials(project, engine=ENGINE)
    after = task_materials(project, engine="sha256:engine-v2")
    assert all(after[task].key != before[task].key for task in before)
    assert after["landscape"].research_key != before["landscape"].research_key
    assert _reasons(before, after)["overview"] == ["engine prompts changed"]


def test_review_fixes_never_change_keys(make_project: MakeProject) -> None:
    # Keys are made from what sessions are given, never from what they wrote:
    # rewriting every output, as review fixes do, leaves every key alone.
    root = make_project()
    project = load_project(root)
    before = task_materials(project, engine=ENGINE)
    for folder in (root / ".spadework" / "work", root / "out"):
        folder.mkdir(parents=True)
        for task in project.playbook.tasks:
            (folder / task.output).write_text(f"# {task.title}\n\nFixed text.\n")

    after = task_materials(load_project(root), engine=ENGINE)

    assert after == before


def test_draft_material_is_frozen() -> None:
    material = draft_material(**DRAFT)
    with pytest.raises(ValueError, match="frozen"):
        material.model = "opus"  # type: ignore[misc]
    assert isinstance(material, DraftMaterial)
    assert isinstance(research_material(**RESEARCH), ResearchMaterial)


# The playbook's prompt appendices.


def test_without_appendices_keys_are_as_before() -> None:
    material = draft_material(**DRAFT)
    research = research_material(**RESEARCH)

    assert material.prompts is None
    assert research.prompts is None
    dumped = material.model_dump(mode="json")
    del dumped["prompts"]
    assert material.key == hash_json(dumped)


@pytest.mark.parametrize("name", ["rules", "draft"])
def test_rules_and_draft_appendices_change_the_draft_key(name: str) -> None:
    plain = draft_material(**DRAFT)
    one = draft_material(**DRAFT, appendices={name: "Prefer tables."})
    two = draft_material(**DRAFT, appendices={name: "Prefer prose."})

    assert len({plain.key, one.key, two.key}) == 3
    old = TaskMaterial(draft=plain, research=None)
    new = TaskMaterial(draft=one, research=None)
    assert stale_reasons(old, new) == ["playbook prompts changed"]


@pytest.mark.parametrize("name", ["critic", "fix", "summary", "repair", "research"])
def test_other_appendices_leave_the_draft_key(name: str) -> None:
    assert (
        draft_material(**DRAFT, appendices={name: "Anything."}).key
        == draft_material(**DRAFT).key
    )


def test_the_research_appendix_changes_the_research_key() -> None:
    old = _task()
    research = research_material(**RESEARCH, appendices={"research": "Aim for 5."})
    draft = draft_material(**{**DRAFT, "research_key": research.key})
    new = TaskMaterial(draft=draft, research=research)

    assert old.research_key != new.research_key
    assert stale_reasons(old, new) == ["playbook prompts changed"]
    assert (
        research_material(**RESEARCH, appendices={"rules": "x", "draft": "y"}).key
        == research_material(**RESEARCH).key
    )


def test_editing_a_playbook_appendix_makes_tasks_stale(
    make_project: MakeProject,
) -> None:
    root = make_project()
    before = task_materials(load_project(root), engine=ENGINE)
    rules = root / "playbooks" / "valid" / "prompts" / "rules.md"
    rules.write_text(rules.read_text() + "Use British English.\n")
    (root / "playbooks" / "valid" / "prompts" / "critic.md").write_text("Be harsh.\n")

    after = task_materials(load_project(root), engine=ENGINE)

    for task_id in ("overview", "landscape", "plan"):
        assert before[task_id].key != after[task_id].key
        assert "playbook prompts changed" in stale_reasons(
            before[task_id], after[task_id]
        )
    # The research is told nothing of the house rules: it is reused.
    assert before["landscape"].research_key == after["landscape"].research_key


def test_a_critic_appendix_alone_changes_no_key(make_project: MakeProject) -> None:
    root = make_project()
    before = task_materials(load_project(root), engine=ENGINE)
    for name in ("critic", "fix", "summary"):
        path = root / "playbooks" / "valid" / "prompts" / f"{name}.md"
        path.write_text(f"Extra {name} guidance.\n")

    after = task_materials(load_project(root), engine=ENGINE)

    assert {k: m.key for k, m in before.items()} == {k: m.key for k, m in after.items()}
