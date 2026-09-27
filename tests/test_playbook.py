import os
import shutil
from pathlib import Path

import pytest

from spadework.errors import ExitCode, InvalidError
from spadework.playbook import (
    ENGINE_FORBIDDEN_PHRASES,
    Playbook,
    PlaybookRef,
    TaskFrontMatter,
    builtin_names,
    builtin_root,
    load_playbook,
    resolve_playbook,
)
from tests.conftest import PLAYBOOKS

LOCAL_HINT = "Fix the playbook, then run `spadework check` again."


def copy(tmp_path: Path, name: str = "valid") -> Path:
    root = tmp_path / "playbooks" / name
    shutil.copytree(PLAYBOOKS / name, root)
    return root


def load(root: Path) -> Playbook:
    return load_playbook(resolve_playbook(str(root), root.parent.parent))


def problems(root: Path) -> list[str]:
    with pytest.raises(InvalidError) as caught:
        load(root)
    assert caught.value.exit_code == ExitCode.INVALID
    return [str(problem) for problem in caught.value.problems]


def write_task(root: Path, name: str, front: str, body: str = "Body.\n") -> None:
    (root / "tasks" / f"{name}.md").write_text(
        f"---\n{front}---\n{body}", encoding="utf-8"
    )


def test_loads_the_valid_fixture(tmp_path: Path) -> None:
    playbook = load(copy(tmp_path))

    assert (playbook.name, playbook.version) == ("fixture", "1.0.0")
    assert playbook.description == "A small playbook for spadework's own tests."
    assert playbook.manifest.requires_brief == ["priorities"]
    assert playbook.manifest.review.max_findings == 4
    assert [task.id for task in playbook.tasks] == ["overview", "landscape", "plan"]
    assert [task.id for task in playbook.research_tasks] == ["landscape"]
    assert playbook.prompts == {"rules": "Prefer tables to long prose.\n"}
    assert playbook.brief_template is not None
    assert playbook.brief_template.startswith("---\n# REQUIRED")
    assert playbook.ref.kind == "path"
    assert playbook.ref.display_root == "playbooks/valid"
    assert playbook.ref.hint == LOCAL_HINT


def test_task_fields_and_defaults(tmp_path: Path) -> None:
    playbook = load(copy(tmp_path))
    overview, landscape, plan = (
        playbook.task(i) for i in ("overview", "landscape", "plan")
    )

    assert overview.title == "Overview"
    assert overview.output == "01-overview.md"
    assert overview.depends_on == ()
    assert overview.inputs == ("**",)
    assert (overview.tier, overview.budget_usd) == ("draft", 0.50)
    assert overview.body.startswith("Summarise {{ brief.title }}")
    assert overview.body_line == 7
    assert overview.display == "playbooks/valid/tasks/overview.md"
    assert overview.source.startswith("---\ntitle: Overview\n")
    assert overview.research is None

    assert landscape.inputs == ("calls/**", "*.txt")
    assert landscape.research is not None
    assert (landscape.research.max_searches, landscape.research.max_fetches) == (4, 5)
    assert landscape.key_lines[("research", "questions")] == 9

    assert (plan.tier, plan.budget_usd) == ("review", 1.25)
    assert plan.depends_on == ("landscape", "overview")
    assert plan.inputs == ()
    with pytest.raises(KeyError):
        playbook.task("nope")


def test_validators_merge_task_settings_over_the_defaults(tmp_path: Path) -> None:
    playbook = load(copy(tmp_path))
    overview = playbook.task("overview").validators
    landscape = playbook.task("landscape").validators
    plan = playbook.task("plan").validators

    assert overview.max_lines == 120
    assert overview.min_lines is None
    assert overview.required_headings == ("Summary", "Open questions")
    assert overview.starts_with_h1 is True
    assert overview.forbidden_phrases == ENGINE_FORBIDDEN_PHRASES
    assert overview.max_open_questions == 6
    # On for every task: one without research may cite no source at all.
    assert overview.citations_resolve is True
    assert overview.min_sources is None

    assert landscape.citations_resolve is True
    assert landscape.min_sources == 2
    assert landscape.max_lines == 120

    assert plan.max_lines == 80
    assert plan.starts_with_h1 is False


def test_an_explicit_null_turns_off_an_inherited_validator(tmp_path: Path) -> None:
    root = copy(tmp_path)
    write_task(
        root,
        "extra",
        "title: Extra\noutput: 09-extra.md\nvalidate:\n  max_lines: null\n",
    )
    toml = (root / "playbook.toml").read_text()
    toml = toml.replace(
        "max_lines = 120", 'max_lines = 120\nforbidden_phrases = ["synergy"]'
    )
    (root / "playbook.toml").write_text(toml)

    validators = load(root).task("extra").validators

    assert validators.max_lines is None
    assert validators.forbidden_phrases == (*ENGINE_FORBIDDEN_PHRASES, "synergy")


def test_the_default_budget_is_the_engine_draft_estimate(tmp_path: Path) -> None:
    root = copy(tmp_path, "duplicate-output")
    (root / "tasks" / "summary.md").unlink()

    assert load(root).task("scope").budget_usd == 1.00


def test_order_is_topological_with_ties_broken_by_id(tmp_path: Path) -> None:
    root = copy(tmp_path, "duplicate-output")
    (root / "tasks" / "summary.md").unlink()
    for name, deps in [("delta", "[]"), ("bravo", "[scope]"), ("alpha", "[scope]")]:
        write_task(
            root, name, f"title: {name}\noutput: {name}.md\ndepends_on: {deps}\n"
        )
    write_task(root, "zulu", "title: Z\noutput: z.md\ndepends_on: [alpha, delta]\n")

    assert [task.id for task in load(root).tasks] == [
        "delta",
        "scope",
        "alpha",
        "bravo",
        "zulu",
    ]


def test_cycles_are_reported_with_their_path(tmp_path: Path) -> None:
    assert problems(copy(tmp_path, "cycle")) == [
        "playbooks/cycle/tasks/alpha.md:4: depends_on: tasks depend on each other "
        "in a cycle: alpha → gamma → beta → alpha"
    ]


def test_a_task_that_depends_on_itself_is_a_cycle(tmp_path: Path) -> None:
    root = copy(tmp_path, "unknown-dependency")
    write_task(root, "ia", "title: IA\noutput: 02-ia.md\ndepends_on: [ia]\n")

    assert problems(root) == [
        "playbooks/unknown-dependency/tasks/ia.md:4: depends_on: tasks depend on each "
        "other in a cycle: ia → ia"
    ]


def test_unknown_dependencies_are_reported_with_a_suggestion(tmp_path: Path) -> None:
    root = copy(tmp_path, "unknown-dependency")
    with pytest.raises(InvalidError) as caught:
        load(root)

    assert caught.value.message == "playbook 'unknown-dependency' is not valid"
    assert caught.value.hint == LOCAL_HINT
    assert [str(p) for p in caught.value.problems] == [
        "playbooks/unknown-dependency/tasks/ia.md:6: depends_on: there is no task "
        "'scop' (did you mean 'scope'?)"
    ]


def test_duplicate_outputs_are_reported_ignoring_case(tmp_path: Path) -> None:
    assert problems(copy(tmp_path, "duplicate-output")) == [
        "playbooks/duplicate-output/tasks/summary.md:3: output '01-Scope.md' is also "
        "used by task 'scope'"
    ]


def test_invalid_task_front_matter(tmp_path: Path) -> None:
    assert problems(copy(tmp_path, "bad-task")) == [
        "playbooks/bad-task/tasks/scope.md:3: output: '00-summary.md' is reserved for "
        "the file spadework writes itself",
        "playbooks/bad-task/tasks/scope.md:5: tier: should be 'draft' or 'review'",
        "playbooks/bad-task/tasks/scope.md:4: unknown key 'dependson' "
        "(did you mean 'depends_on'?)",
    ]


def test_invalid_manifest(tmp_path: Path) -> None:
    assert problems(copy(tmp_path, "bad-manifest")) == [
        "playbooks/bad-manifest/playbook.toml:3: version: 'one' is not valid: use a "
        "version such as '0.1.0'",
        "playbooks/bad-manifest/playbook.toml:7: unknown key 'defaults.budget' "
        "(did you mean 'budget_usd'?)",
    ]


@pytest.mark.parametrize(
    ("front", "expected"),
    [
        ("title: T\n", "'output' is required"),
        ("title: T\noutput: sources.md\n", "'sources.md' is reserved"),
        (
            "title: T\noutput: Brief.md\n",
            "'Brief.md' is reserved for the project's brief",
        ),
        ("title: T\noutput: notes.txt\n", "use a file name ending in .md"),
        ("title: T\noutput: out/x.md\n", "use a file name ending in .md"),
        (
            "title: T\noutput: x.md\ndepends_on: [Scope]\n",
            "'Scope' is not valid: start with",
        ),
        ("title: T\noutput: x.md\ndepends_on: [a, a]\n", "'a' is listed twice"),
        ("title: T\noutput: x.md\ninputs: ['../secrets/*']\n", "cannot contain '..'"),
        (
            "title: T\noutput: x.md\ninputs: ['/etc/*']\n",
            "globs are relative to inputs/",
        ),
        ("title: T\noutput: x.md\nbudget_usd: 0\n", "should be greater than 0"),
        ("title: T\noutput: x.md\nresearch: {}\n", "'research.questions' is required"),
        (
            "title: T\noutput: x.md\nresearch:\n  questions: q\n  max_searches: 0\n"
            "  max_fetches: 0\n",
            "max_searches and max_fetches cannot both be 0",
        ),
        (
            "title: T\noutput: x.md\nresearch:\n  questions: q\n  max_fetches: 81\n",
            "should be less than or equal to 80",
        ),
        (
            "title: T\noutput: x.md\nvalidate:\n  min_lines: 10\n  max_lines: 5\n",
            "min_lines (10) is greater than max_lines (5)",
        ),
        (
            "title: T\noutput: x.md\nvalidate: {max_line: 5}\n",
            "did you mean 'max_lines'?",
        ),
    ],
)
def test_invalid_task_fields(tmp_path: Path, front: str, expected: str) -> None:
    root = copy(tmp_path, "unknown-dependency")
    (root / "tasks" / "ia.md").unlink()
    write_task(root, "extra", front)

    (message,) = problems(root)

    assert message.startswith("playbooks/unknown-dependency/tasks/extra.md")
    assert expected in message


def test_min_sources_needs_research(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    write_task(root, "extra", "title: E\noutput: e.md\nvalidate:\n  min_sources: 2\n")

    with pytest.raises(InvalidError) as caught:
        load(root)

    assert [str(p) for p in caught.value.problems] == [
        "playbooks/valid/tasks/extra.md:5: validate.min_sources applies only to tasks "
        "with research"
    ]
    assert "Add a research: section" in caught.value.hint


def test_min_sources_in_the_defaults_applies_to_research_tasks_only(
    tmp_path: Path,
) -> None:
    root = copy(tmp_path, "valid")
    toml = (
        (root / "playbook.toml")
        .read_text()
        .replace("max_lines = 120", "min_sources = 3")
    )
    (root / "playbook.toml").write_text(toml)
    write_task(
        root,
        "landscape",
        "title: L\noutput: 02-landscape.md\nresearch:\n  questions: q\n",
    )

    playbook = load(root)

    assert playbook.task("landscape").validators.min_sources == 3
    assert playbook.task("overview").validators.min_sources is None


def test_merged_line_limits_must_be_in_order(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    write_task(root, "extra", "title: E\noutput: e.md\nvalidate:\n  min_lines: 500\n")

    assert problems(root) == [
        "playbooks/valid/tasks/extra.md:4: validate: min_lines (500) is greater than "
        "max_lines (120)"
    ]


def test_task_files_must_be_markdown_with_valid_ids(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    (root / "tasks" / "notes.txt").write_text("x")
    (root / "tasks" / "Big.md").write_text("---\ntitle: B\noutput: b.md\n---\n")
    (root / "tasks" / "folder").mkdir()
    (root / "tasks" / ".DS_Store").write_bytes(b"\0")

    assert problems(root) == [
        "playbooks/valid/tasks/Big.md: 'Big' is not a valid task id: start with a "
        "lowercase letter and use only a-z, 0-9 and '-'",
        "playbooks/valid/tasks/folder: tasks/ holds only task files ending in .md",
        "playbooks/valid/tasks/notes.txt: tasks/ holds only task files ending in .md",
    ]


def test_all_task_problems_are_reported_together(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    write_task(root, "one", "title: One\n")
    (root / "tasks" / "two.md").write_text("no front matter\n")

    assert problems(root) == [
        "playbooks/valid/tasks/one.md: 'output' is required",
        "playbooks/valid/tasks/two.md:1: the file must start with a '---' line of "
        "YAML front matter",
    ]


def test_a_playbook_needs_tasks(tmp_path: Path) -> None:
    root = copy(tmp_path, "cycle")
    shutil.rmtree(root / "tasks")
    assert problems(root) == ["playbooks/cycle/tasks: there is no tasks/ folder"]

    (root / "tasks").mkdir()
    assert problems(root) == ["playbooks/cycle/tasks: there are no task files"]


def test_a_playbook_needs_a_manifest(tmp_path: Path) -> None:
    root = copy(tmp_path, "cycle")
    (root / "playbook.toml").unlink()

    with pytest.raises(InvalidError) as caught:
        load(root)

    assert caught.value.message == "playbooks/cycle is not a playbook"
    assert [str(p) for p in caught.value.problems] == [
        "playbooks/cycle/playbook.toml: there is no playbook.toml"
    ]


def test_prompt_appendices_must_be_known_prompts(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    (root / "prompts" / "critique.md").write_text("x")
    (root / "prompts" / "summary.txt").write_text("x")

    known = "critic.md, draft.md, fix.md, repair.md, research.md, rules.md, summary.md"
    assert problems(root) == [
        f"playbooks/valid/prompts/{name}: not a prompt appendix; prompts/ can hold "
        f"only {known}"
        for name in ("critique.md", "summary.txt")
    ]


def test_every_engine_prompt_can_have_an_appendix(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    names = ["rules", "research", "draft", "repair", "fix", "critic", "summary"]
    for name in names:
        (root / "prompts" / f"{name}.md").write_text(f"{name} extra\n")

    assert sorted(load(root).prompts) == sorted(names)


def test_prompts_must_be_a_folder(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    shutil.rmtree(root / "prompts")
    (root / "prompts").write_text("x")

    assert problems(root) == ["playbooks/valid/prompts: prompts must be a folder"]


def test_prompts_and_brief_template_are_optional(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    shutil.rmtree(root / "prompts")
    (root / "brief.template.md").unlink()

    playbook = load(root)

    assert playbook.prompts == {}
    assert playbook.brief_template is None


@pytest.mark.parametrize(
    "relative",
    ["tasks/overview.md", "playbook.toml", "brief.template.md", "prompts/rules.md"],
)
def test_files_that_link_outside_the_playbook_are_rejected(
    tmp_path: Path, relative: str
) -> None:
    root = copy(tmp_path, "valid")
    outside = tmp_path / "secret.md"
    outside.write_text((root / relative).read_text())
    (root / relative).unlink()
    (root / relative).symlink_to(outside)

    with pytest.raises(InvalidError) as caught:
        load(root)

    assert [str(p) for p in caught.value.problems] == [
        f"playbooks/valid/{relative}: is a link to a file outside the playbook folder"
    ]


def test_a_tasks_folder_that_links_outside_is_rejected(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    shutil.move(root / "tasks", tmp_path / "elsewhere")
    (root / "tasks").symlink_to(tmp_path / "elsewhere")

    assert problems(root) == ["playbooks/valid/tasks: there is no tasks/ folder"]


def test_links_inside_the_playbook_are_fine(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    (root / "partials" / "plan.md").write_text((root / "tasks" / "plan.md").read_text())
    (root / "tasks" / "plan.md").unlink()
    (root / "tasks" / "plan.md").symlink_to(root / "partials" / "plan.md")

    assert load(root).task("plan").title == "Plan"


def test_resolve_a_relative_path(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")

    ref = resolve_playbook("./playbooks/valid", tmp_path)

    assert ref == PlaybookRef("./playbooks/valid", root, "path", "playbooks/valid")


def test_resolve_a_path_outside_the_project(tmp_path: Path) -> None:
    copy(tmp_path, "valid")
    project = tmp_path / "project"
    project.mkdir()

    ref = resolve_playbook("../playbooks/valid", project)

    assert ref.display_root == "../playbooks/valid"
    assert load_playbook(ref).name == "fixture"


def test_resolve_a_missing_folder(tmp_path: Path) -> None:
    with pytest.raises(InvalidError) as caught:
        resolve_playbook("./playbooks/nope", tmp_path)

    assert caught.value.message == "playbook folder './playbooks/nope' does not exist"
    assert "relative to the project folder" in caught.value.hint


def test_resolve_git_is_not_supported_yet(tmp_path: Path) -> None:
    with pytest.raises(InvalidError, match="cannot load playbook") as caught:
        resolve_playbook("git+https://example.com/playbooks.git", tmp_path)

    assert "not supported yet" in caught.value.hint


def test_resolve_an_unknown_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_builtins = tmp_path / "builtins"
    shutil.copytree(PLAYBOOKS / "valid", fake_builtins / "discovery-website")
    monkeypatch.setattr("spadework.playbook.builtin_root", lambda: fake_builtins)

    with pytest.raises(InvalidError) as caught:
        resolve_playbook("discovery-web", tmp_path)

    assert caught.value.message == "there is no built-in playbook named 'discovery-web'"
    assert caught.value.hint == (
        "Did you mean 'discovery-website'? Built-in playbooks: discovery-website."
    )


def test_resolve_a_bare_name_that_is_a_local_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("spadework.playbook.builtin_root", lambda: tmp_path / "none")
    copy(tmp_path, "valid")

    with pytest.raises(InvalidError) as caught:
        resolve_playbook("playbooks", tmp_path)

    assert caught.value.hint == (
        "For the playbook in the folder 'playbooks', write './playbooks'. "
        "Built-in playbooks: none."
    )


def test_builtin_names_and_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_builtins = tmp_path / "builtins"
    shutil.copytree(PLAYBOOKS / "valid", fake_builtins / "fixture")
    (fake_builtins / "not-a-playbook").mkdir()
    (fake_builtins / "Upper").mkdir()
    monkeypatch.setattr("spadework.playbook.builtin_root", lambda: fake_builtins)

    assert builtin_names() == ["fixture"]
    ref = resolve_playbook("fixture", tmp_path)
    assert ref == PlaybookRef(
        "fixture", fake_builtins / "fixture", "built-in", "[built-in] fixture"
    )
    assert ref.display("tasks/x.md") == "[built-in] fixture/tasks/x.md"
    assert "bug in a built-in playbook" in ref.hint


def test_builtin_root_is_in_the_package() -> None:
    assert builtin_root().parts[-3:] == ("spadework", "builtin", "playbooks")


def test_task_front_matter_accepts_validate_by_its_key() -> None:
    front = TaskFrontMatter.model_validate(
        {"title": "T", "output": "x.md", "validate": {"max_lines": 3}}
    )

    assert front.validate_ is not None
    assert front.validate_.max_lines == 3
    with pytest.raises(ValueError, match="validate_"):
        TaskFrontMatter.model_validate(
            {"title": "T", "output": "x.md", "validate_": {}}
        )


def test_task_files_must_be_utf8(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    (root / "tasks" / "overview.md").write_bytes(b"---\ntitle: \xff\n---\n")

    with pytest.raises(InvalidError) as caught:
        load(root)

    assert [str(p) for p in caught.value.problems] == [
        "playbooks/valid/tasks/overview.md:2: the file is not valid UTF-8 text"
    ]


# Regressions from the M1 review.


@pytest.mark.parametrize("value", ["false", "null"])
def test_citations_resolve_can_be_turned_off(tmp_path: Path, value: str) -> None:
    root = copy(tmp_path)
    write_task(
        root,
        "extra",
        f"title: Extra\noutput: 09-extra.md\nvalidate:\n  citations_resolve: {value}\n",
    )

    assert load(root).task("extra").validators.citations_resolve is False


def test_null_turns_every_kind_of_inherited_validator_off(tmp_path: Path) -> None:
    root = copy(tmp_path)
    toml = (root / "playbook.toml").read_text()
    (root / "playbook.toml").write_text(
        toml.replace(
            "max_lines = 120",
            "max_lines = 120\nmin_lines = 5\nstarts_with_h1 = true\n"
            'required_headings = ["Summary"]\nforbidden_phrases = ["synergy"]\n'
            "max_open_questions = 4\ncitations_resolve = true\nmin_sources = 2",
        )
    )
    nulls = "".join(
        f"  {key}: null\n"
        for key in (
            "max_lines",
            "min_lines",
            "starts_with_h1",
            "required_headings",
            "forbidden_phrases",
            "max_open_questions",
            "citations_resolve",
            "min_sources",
        )
    )
    write_task(
        root,
        "extra",
        "title: E\noutput: e.md\nresearch:\n  questions: q\nvalidate:\n" + nulls,
    )

    playbook = load(root)
    off = playbook.task("extra").validators
    inherited = playbook.task("landscape").validators

    assert off == type(off)(
        max_lines=None,
        min_lines=None,
        starts_with_h1=False,
        required_headings=(),
        forbidden_phrases=ENGINE_FORBIDDEN_PHRASES,
        max_open_questions=None,
        citations_resolve=False,
        min_sources=None,
    )
    assert inherited.forbidden_phrases == (*ENGINE_FORBIDDEN_PHRASES, "synergy")
    assert inherited.max_open_questions == 4
    assert inherited.min_sources == 2


@pytest.mark.parametrize(
    ("glob", "expected"),
    [
        ("[z-a]*.md", "'[z-a]*.md' is not a valid glob: bad character range z-a"),
        ("[a--z].md", "'[a--z].md' is not a valid glob: Possible set difference"),
    ],
)
def test_input_globs_are_compiled_when_the_task_loads(
    tmp_path: Path, glob: str, expected: str
) -> None:
    root = copy(tmp_path, "valid")
    write_task(
        root, "extra", f"title: E\noutput: e.md\ninputs:\n  - '*.md'\n  - '{glob}'\n"
    )

    (message,) = problems(root)

    assert message.startswith("playbooks/valid/tasks/extra.md:6: inputs[1]: ")
    assert expected in message


def test_a_glob_class_holding_a_bracket_is_fine(tmp_path: Path) -> None:
    # filterwarnings = error makes the FutureWarning Python used to print fail.
    root = copy(tmp_path, "valid")
    write_task(root, "extra", "title: E\noutput: e.md\ninputs: ['[[]x.md']\n")

    assert load(root).task("extra").inputs == ("[[]x.md",)


def test_a_task_file_in_a_link_loop_is_reported(tmp_path: Path) -> None:
    root = copy(tmp_path, "valid")
    (root / "tasks" / "loop.md").symlink_to(root / "tasks" / "loop.md")

    assert problems(root) == [
        "playbooks/valid/tasks/loop.md: is a link that cannot be followed"
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
@pytest.mark.parametrize("folder", ["tasks", "prompts"])
def test_unreadable_playbook_folders_are_problems(tmp_path: Path, folder: str) -> None:
    root = copy(tmp_path, "valid")
    (root / folder).chmod(0)
    try:
        messages = problems(root)
    finally:
        (root / folder).chmod(0o755)

    assert (
        f"playbooks/valid/{folder}: cannot read this folder: Permission denied"
        in messages
    )
