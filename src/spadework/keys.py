"""Task fingerprints: the research key and the draft key.

See docs/spec/04-engine.md, Fingerprints and staleness. A key is SHA-256 over
canonical JSON. What goes into it is kept as **key material**: the hash of
each component, so that the reasons a task is stale can be found by comparing
the material stored after its last draft with the material now.

Keys depend only on what a session is given, never on what it wrote.
Dependencies contribute their draft keys, not their output, so a review fix
changes no key and can never cause a redraft.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from spadework.models import StrictModel

if TYPE_CHECKING:
    from spadework.config import ModelsSection
    from spadework.inputs import InputFile
    from spadework.project import Project

HASH_PREFIX = "sha256:"
_CHUNK = 1 << 16


def canonical_json(value: Any) -> bytes:
    """JSON with sorted keys and no spaces, as UTF-8: the same value, the same bytes."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def hash_bytes(data: bytes) -> str:
    return HASH_PREFIX + hashlib.sha256(data).hexdigest()


def hash_text(text: str) -> str:
    return hash_bytes(text.encode("utf-8"))


def hash_json(value: Any) -> str:
    return hash_bytes(canonical_json(value))


def hash_file(path: Path) -> str:
    """The hash of a file's bytes. Raises OSError if it cannot be read."""
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(_CHUNK):
            digest.update(chunk)
    return HASH_PREFIX + digest.hexdigest()


class Material(StrictModel):
    """Key material: the hashes a key is made of. Stored in state.json."""

    @property
    def key(self) -> str:
        data = self.model_dump(mode="json")
        # A playbook without prompt appendices keys as before they counted.
        if data.get("prompts", "") is None:
            del data["prompts"]
        return hash_json(data)


class ResearchMaterial(Material):
    """What a research key is made of."""

    engine: str
    public_context: str
    """Hash of ``public_context``."""
    questions: str
    """Hash of the rendered research questions."""
    model: str
    prompts: str | None = None
    """Hash of the playbook's ``prompts/research.md``, or None without one."""


class DraftMaterial(Material):
    """What a draft key is made of."""

    engine: str
    playbook: str
    playbook_version: str
    task: str
    """Hash of the whole task file."""
    brief: str
    """Hash of the whole brief.md."""
    inputs: dict[str, str]
    """Each matching input, by its path relative to inputs/, to its hash."""
    model: str
    research: str | None
    """The task's research key, or None for a task without research."""
    dependencies: dict[str, str]
    """Each dependency's id to its draft key."""
    prompts: str | None = None
    """Hash of the playbook's ``prompts/rules.md`` and ``prompts/draft.md``,
    or None without either."""


class TaskMaterial(Material):
    """Everything a task's keys are made of. Its key is the draft key."""

    draft: DraftMaterial
    research: ResearchMaterial | None

    @property
    def key(self) -> str:
        return self.draft.key

    @property
    def research_key(self) -> str | None:
        return None if self.research is None else self.research.key


def research_material(
    *,
    engine: str,
    public_context: str,
    questions: str,
    model: str,
    appendices: Mapping[str, str] | None = None,
) -> ResearchMaterial:
    """``questions`` is the rendered text, not the template. ``appendices``
    are the playbook's prompt appendices by name; only ``research`` counts."""
    return ResearchMaterial(
        engine=engine,
        public_context=hash_text(public_context),
        questions=hash_text(questions),
        model=model,
        prompts=prompts_hash(appendices or {}, RESEARCH_APPENDICES),
    )


def draft_material(
    *,
    engine: str,
    playbook: str,
    playbook_version: str,
    task_source: str,
    brief_source: str,
    inputs: Mapping[str, str],
    model: str,
    research_key: str | None,
    dependency_keys: Mapping[str, str],
    appendices: Mapping[str, str] | None = None,
) -> DraftMaterial:
    """``inputs`` maps each matching input's name to its hash (``input_hashes``).

    ``appendices`` are the playbook's prompt appendices by name: ``rules`` and
    ``draft`` change what a draft is told, so they count. The critic's, the
    fixer's and the summary's do not change a draft.

    ``dependency_keys`` is the draft key of each dependency. To plan, pass
    their current keys. To record a draft, pass the key of each dependency
    output the draft actually read: with ``--only``, a stale dependency's old
    output is used, and the task must stay stale until that dependency is
    redrafted.
    """
    return DraftMaterial(
        engine=engine,
        playbook=playbook,
        playbook_version=playbook_version,
        task=hash_text(task_source),
        brief=hash_text(brief_source),
        inputs=dict(sorted(inputs.items())),
        model=model,
        research=research_key,
        dependencies=dict(sorted(dependency_keys.items())),
        prompts=prompts_hash(appendices or {}, DRAFT_APPENDICES),
    )


RESEARCH_APPENDICES = ("research",)
DRAFT_APPENDICES = ("rules", "draft")


def prompts_hash(appendices: Mapping[str, str], names: Iterable[str]) -> str | None:
    """The hash of the named appendices a playbook has, or None if none."""
    present = {name: appendices[name] for name in names if appendices.get(name)}
    return hash_json(present) if present else None


def research_key(
    *, engine: str, public_context: str, questions: str, model: str
) -> str:
    return research_material(
        engine=engine, public_context=public_context, questions=questions, model=model
    ).key


def draft_key(
    *,
    engine: str,
    playbook: str,
    playbook_version: str,
    task_source: str,
    brief_source: str,
    inputs: Mapping[str, str],
    model: str,
    research_key: str | None,
    dependency_keys: Mapping[str, str],
) -> str:
    return draft_material(
        engine=engine,
        playbook=playbook,
        playbook_version=playbook_version,
        task_source=task_source,
        brief_source=brief_source,
        inputs=inputs,
        model=model,
        research_key=research_key,
        dependency_keys=dependency_keys,
    ).key


def input_hashes(files: Iterable[InputFile]) -> dict[str, str]:
    """Each input file's name, relative to inputs/, to the hash of its bytes."""
    return {file.name: hash_file(file.path) for file in files}


def task_materials(
    project: Project, *, engine: str, models: ModelsSection | None = None
) -> dict[str, TaskMaterial]:
    """The current key material of every task, in execution order.

    ``engine`` is the engine prompt version. ``models`` overrides the
    project's ``[models]``, as ``--draft-model`` and ``--review-model`` do.
    Research uses the draft tier's model, whatever the task's tier.
    """
    from spadework.inputs import match_inputs
    from spadework.render import render_research_questions

    models = models or project.config.models
    playbook = project.playbook
    brief = project.brief
    public_context = brief.public_context or ""
    materials: dict[str, TaskMaterial] = {}
    for task in playbook.tasks:
        research = None
        if task.research is not None:
            questions = render_research_questions(
                playbook,
                task,
                public_context=public_context,
                language=brief.front.language,
            )
            research = research_material(
                engine=engine,
                public_context=public_context,
                questions=questions,
                model=models.draft,
                appendices=playbook.prompts,
            )
        draft = draft_material(
            engine=engine,
            playbook=playbook.name,
            playbook_version=playbook.version,
            task_source=task.source,
            brief_source=brief.source,
            inputs=input_hashes(match_inputs(task.inputs, project.inputs)),
            model=models.draft if task.tier == "draft" else models.review,
            research_key=None if research is None else research.key,
            dependency_keys={dep: materials[dep].key for dep in task.depends_on},
            appendices=playbook.prompts,
        )
        materials[task.id] = TaskMaterial(draft=draft, research=research)
    return materials


def stale_reasons(
    old: TaskMaterial,
    new: TaskMaterial,
    *,
    changed_dependencies: Mapping[str, str] | None = None,
) -> list[str]:
    """Why a task whose material was ``old`` is stale now that it is ``new``.

    A dependency whose key changed is named, not the reasons it changed:
    *dependency ia is stale*. ``changed_dependencies`` can give the wording
    for a dependency, such as ``{"ia": "dependency ia changed"}`` for one
    already redrafted since. Returns [] when the keys match.
    """
    if old.key == new.key:
        return []
    before, after = old.draft, new.draft
    reasons: list[str] = []
    if before.brief != after.brief:
        reasons.append("brief changed")
    reasons.extend(_input_reasons(before.inputs, after.inputs))
    if before.task != after.task:
        reasons.append("task file changed")
    if old.research is not None and new.research is not None:
        if old.research.public_context != new.research.public_context:
            reasons.append("public context changed")
        if old.research.questions != new.research.questions:
            reasons.append("research questions changed")
    if (before.playbook, before.playbook_version) != (
        after.playbook,
        after.playbook_version,
    ):
        if before.playbook != after.playbook:
            reasons.append(f"playbook {before.playbook} → {after.playbook}")
        else:
            reasons.append(
                f"playbook {before.playbook_version} → {after.playbook_version}"
            )
    if before.model != after.model:
        reasons.append(f"draft model {before.model} → {after.model}")
    if (
        old.research is not None
        and new.research is not None
        and old.research.model != new.research.model
    ):
        reasons.append(f"research model {old.research.model} → {new.research.model}")
    if before.engine != after.engine or (
        old.research is not None
        and new.research is not None
        and old.research.engine != new.research.engine
    ):
        reasons.append("engine prompts changed")
    if before.prompts != after.prompts or (
        old.research is not None
        and new.research is not None
        and old.research.prompts != new.research.prompts
    ):
        reasons.append("playbook prompts changed")
    wording = changed_dependencies or {}
    for dep, key in after.dependencies.items():
        if dep in before.dependencies and before.dependencies[dep] != key:
            reasons.append(wording.get(dep, f"dependency {dep} is stale"))
    # Adding or removing research or a dependency needs a task file edit,
    # which is already named. This is a backstop, never expected.
    return reasons or ["fingerprint changed"]


def _input_reasons(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    reasons = []
    for name in sorted(before.keys() | after.keys()):
        if name not in before:
            reasons.append(f"inputs/{name} added")
        elif name not in after:
            reasons.append(f"inputs/{name} removed")
        elif before[name] != after[name]:
            reasons.append(f"inputs/{name} changed")
    return reasons
