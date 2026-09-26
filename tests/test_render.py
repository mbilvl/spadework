import shutil
from pathlib import Path

import pytest
from jinja2 import UndefinedError
from jinja2.sandbox import SandboxedEnvironment

from spadework.brief import Brief, load_brief
from spadework.errors import InvalidError
from spadework.inputs import InputFile
from spadework.playbook import Playbook, load_playbook, resolve_playbook
from spadework.render import (
    TEMPLATE_HINT,
    DependencyFacts,
    Namespace,
    PlaybookLoader,
    ResearchFindings,
    Source,
    StagedFile,
    check_templates,
    environment,
    placeholder_context,
    public_view,
    render_research_questions,
    render_task_body,
    research_context,
    task_context,
)
from tests.conftest import BRIEF, PLAYBOOKS


@pytest.fixture
def playbook_root(tmp_path: Path) -> Path:
    root = tmp_path / "playbooks" / "valid"
    shutil.copytree(PLAYBOOKS / "valid", root)
    return root


def load(root: Path) -> Playbook:
    return load_playbook(resolve_playbook(str(root), root.parent.parent))


def brief(tmp_path: Path, text: str = BRIEF) -> Brief:
    path = tmp_path / "brief.md"
    path.write_text(text, encoding="utf-8")
    return load_brief(path)


def write_task(root: Path, name: str, front: str, body: str) -> None:
    (root / "tasks" / f"{name}.md").write_text(
        f"---\n{front}---\n{body}", encoding="utf-8"
    )


def check_problems(
    tmp_path: Path, root: Path, brief_text: str = BRIEF
) -> tuple[list[str], str]:
    with pytest.raises(InvalidError) as caught:
        check_templates("acme-fabrication", brief(tmp_path, brief_text), load(root), [])
    return [str(problem) for problem in caught.value.problems], caught.value.hint


def render(template: str, **context: object) -> str:
    env = environment(Path("."))
    return env.from_string(template).render(context)


def test_the_environment_is_pinned() -> None:
    env = environment(Path("."))

    assert isinstance(env, SandboxedEnvironment)
    assert env.cache is None
    assert env.autoescape is False
    assert env.trim_blocks is False
    assert env.lstrip_blocks is False
    assert env.keep_trailing_newline is True
    assert render("<b>{{ x }}</b>\n", x="&") == "<b>&</b>\n"


def test_namespace_keys_win_over_method_names() -> None:
    extra = Namespace("brief.extra", {"items": "catalogue", "keys": 3, "get": "x"})

    assert render("{{ e.items }} {{ e.keys }} {{ e.get }}", e=extra) == "catalogue 3 x"
    assert render("{{ e['items'] }}", e=extra) == "catalogue"
    assert render("{% for k in e %}{{ k }},{% endfor %}", e=extra) == "items,keys,get,"
    assert render("{{ 'keys' in e }} {{ e | length }}", e=extra) == "True 3"


def test_namespace_methods_work_when_no_key_shadows_them() -> None:
    ns = Namespace("deps", {"scope": 1, "ia": 2})

    assert render(
        "{% for k, v in ns.items() %}{{ k }}={{ v }} {% endfor %}", ns=ns
    ) == ("scope=1 ia=2 ")


def test_namespace_is_read_only() -> None:
    ns = Namespace("brief", {"title": "T"})

    with pytest.raises(AttributeError, match="brief is read-only"):
        ns.title = "x"
    with pytest.raises(TypeError):
        ns["title"] = "x"  # type: ignore[index]
    assert repr(ns) == "Namespace('brief', {'title': 'T'})"


def test_the_sandbox_blocks_python_internals() -> None:
    from jinja2.exceptions import SecurityError

    with pytest.raises(SecurityError):
        render("{{ ns.__class__ }}", ns=Namespace("brief", {}))
    with pytest.raises(SecurityError):
        render("{{ ns._label }}", ns=Namespace("brief", {}))


def test_brief_extra_keys_that_shadow_dict_methods(tmp_path: Path) -> None:
    text = BRIEF.replace("priorities:", "items: [a, b]\nkeys: {values: 7}\npriorities:")
    context = task_context(
        project_name="p",
        brief=brief(tmp_path, text),
        task=load_valid().task("overview"),
        files=[],
        deps=[],
        research=None,
    )

    assert render("{{ brief.extra.items | join(',') }}", **context) == "a,b"
    assert render("{{ brief.extra.keys.values }}", **context) == "7"
    assert render("{{ brief.extra.keys['values'] }}", **context) == "7"


def load_valid() -> Playbook:
    return load_playbook(resolve_playbook(str(PLAYBOOKS / "valid"), PLAYBOOKS))


def test_task_context_with_run_time_values(tmp_path: Path) -> None:
    playbook = load_valid()
    task = playbook.task("landscape")
    context = task_context(
        project_name="acme-fabrication",
        brief=brief(tmp_path),
        task=task,
        files=[StagedFile("inputs/call.md", "Call")],
        deps=[DependencyFacts(playbook.task("overview"), ("Fact one", "Fact two"))],
        research=ResearchFindings(
            notes="Notes [S1].",
            sources=(
                Source(
                    id="S1",
                    url="https://example.com/",
                    title="Example",
                    publisher="Example Ltd",
                    accessed="2026-09-27",
                    quote="a quote",
                    claim="a claim",
                    status="verified",
                ),
            ),
            unanswered=("pricing",),
        ),
    )

    text = render_task_body(load_valid(), task, context)

    assert text == (
        "Build on 01-overview.md:\n- Fact one\n- Fact two\n\n"
        "Notes [S1].\n[S1] Example\n\n"
    )
    assert set(context) == {"brief", "task", "project", "files", "deps", "research"}
    assert context["project"]["name"] == "acme-fabrication"
    assert context["task"]["has_research"] is True
    assert context["research"]["unanswered"] == ["pricing"]


def test_the_research_context_holds_three_things_only() -> None:
    task = load_valid().task("landscape")
    context = research_context(task, public_context="A fabricator.", language="pt")

    assert set(context) == {"public_context", "task", "brief"}
    assert dict(context["brief"]) == {"language": "pt"}


def test_research_questions_render_from_public_context_only(tmp_path: Path) -> None:
    playbook = load_valid()
    task = playbook.task("landscape")

    text = render_research_questions(
        playbook, task, public_context="A fabricator", language="en"
    )

    assert (
        text
        == 'For A fabricator:\n- five comparable companies for "Landscape", in en\n'
    )
    with pytest.raises(ValueError, match="has no research"):
        render_research_questions(
            playbook, playbook.task("overview"), public_context="", language="en"
        )


def test_research_questions_cannot_see_the_brief(tmp_path: Path) -> None:
    root = tmp_path / "playbooks" / "leaky-research"
    shutil.copytree(PLAYBOOKS / "leaky-research", root)

    messages, hint = check_problems(tmp_path, root)

    assert messages == [
        "playbooks/leaky-research/tasks/landscape.md:7: research.questions: brief.body "
        "is not available to research questions, which can use only public_context, "
        "task and brief.language"
    ]
    assert hint == TEMPLATE_HINT


@pytest.mark.parametrize("variable", ["project.name", "files", "deps", "research"])
def test_research_questions_cannot_see_task_variables(
    tmp_path: Path, playbook_root: Path, variable: str
) -> None:
    write_task(
        playbook_root,
        "landscape",
        "title: L\noutput: 02-landscape.md\nresearch:\n  questions: '{{ "
        + variable
        + " }}'\n",
        "Body.\n",
    )

    messages, _ = check_problems(tmp_path, playbook_root)

    name = variable.split(".")[0]
    assert messages == [
        f"playbooks/valid/tasks/landscape.md:5: research.questions: '{name}' is "
        "undefined; research questions can use only public_context, task and "
        "brief.language"
    ]


def test_undefined_variables_are_reported_with_the_line(tmp_path: Path) -> None:
    root = tmp_path / "playbooks" / "undefined-variable"
    shutil.copytree(PLAYBOOKS / "undefined-variable", root)

    messages, hint = check_problems(tmp_path, root)

    assert messages == [
        "playbooks/undefined-variable/tasks/scope.md:7: template: brief has no 'budget'"
    ]
    assert hint == TEMPLATE_HINT


def test_a_missing_brief_key_points_at_the_brief(
    tmp_path: Path, playbook_root: Path
) -> None:
    write_task(
        playbook_root,
        "plan",
        "title: P\noutput: 03-plan.md\n",
        "{{ brief.extra.industry }}\n",
    )

    messages, hint = check_problems(tmp_path, playbook_root)

    assert messages == [
        "playbooks/valid/tasks/plan.md:5: template: brief.md has no front matter key "
        "'industry' (used as brief.extra.industry)"
    ]
    assert hint == "Add the key to the front matter of brief.md."


def test_is_defined_tests_on_brief_extra(tmp_path: Path, playbook_root: Path) -> None:
    write_task(
        playbook_root,
        "plan",
        "title: P\noutput: 03-plan.md\n",
        "{% if brief.extra.launch is defined %}{{ brief.extra.launch }}{% endif %}\n",
    )

    check_templates("p", brief(tmp_path), load(playbook_root), [])


def test_unknown_dependencies_in_templates(tmp_path: Path, playbook_root: Path) -> None:
    write_task(
        playbook_root, "plan", "title: P\noutput: 03-plan.md\n", "{{ deps.scope }}\n"
    )

    messages, _ = check_problems(tmp_path, playbook_root)

    assert messages == [
        "playbooks/valid/tasks/plan.md:5: template: 'scope' is not in this task's "
        "depends_on (used as deps.scope)"
    ]


def test_syntax_errors_are_reported_with_the_line(
    tmp_path: Path, playbook_root: Path
) -> None:
    write_task(
        playbook_root, "plan", "title: P\noutput: 03-plan.md\n", "a\nb\n{% if x %}\n"
    )

    messages, _ = check_problems(tmp_path, playbook_root)

    assert len(messages) == 1
    assert messages[0].startswith(
        "playbooks/valid/tasks/plan.md:7: template: syntax error: "
    )


def test_every_template_problem_is_reported(
    tmp_path: Path, playbook_root: Path
) -> None:
    write_task(playbook_root, "plan", "title: P\noutput: 03-plan.md\n", "{{ nope }}\n")
    write_task(
        playbook_root,
        "landscape",
        "title: L\noutput: 02-landscape.md\n"
        "research:\n  questions: |\n    {{ nope }}\n",
        "{{ also_nope }}\n",
    )

    messages, _ = check_problems(tmp_path, playbook_root)

    assert messages == [
        "playbooks/valid/tasks/landscape.md:8: template: 'also_nope' is undefined",
        "playbooks/valid/tasks/landscape.md:6: research.questions: 'nope' is "
        "undefined; research questions can use only public_context, task and "
        "brief.language",
        "playbooks/valid/tasks/plan.md:5: template: 'nope' is undefined",
    ]


@pytest.mark.parametrize(
    "name",
    [
        "../secret.md",
        "/etc/hosts",
        "partials/../../secret.md",
        ".hidden/x.md",
        "partials",
        "partials/missing.md",
    ],
)
def test_includes_stay_inside_the_playbook(
    tmp_path: Path, playbook_root: Path, name: str
) -> None:
    (tmp_path / "playbooks" / "secret.md").write_text("secret")
    (playbook_root / ".hidden").mkdir()
    (playbook_root / ".hidden" / "x.md").write_text("hidden")
    write_task(
        playbook_root,
        "plan",
        "title: P\noutput: 03-plan.md\n",
        f'{{% include "{name}" %}}\n',
    )

    messages, _ = check_problems(tmp_path, playbook_root)

    assert messages == [
        f"playbooks/valid/tasks/plan.md:5: template: cannot include '{name}': no such "
        "file in the playbook folder"
    ]


def test_includes_cannot_follow_links_out(tmp_path: Path, playbook_root: Path) -> None:
    (tmp_path / "secret.md").write_text("secret")
    (playbook_root / "partials" / "link.md").symlink_to(tmp_path / "secret.md")
    write_task(
        playbook_root,
        "plan",
        "title: P\noutput: 03-plan.md\n",
        '{% include "partials/link.md" %}\n',
    )

    messages, _ = check_problems(tmp_path, playbook_root)

    assert "cannot include 'partials/link.md'" in messages[0]


def test_includes_that_are_not_utf8_are_not_found(
    tmp_path: Path, playbook_root: Path
) -> None:
    (playbook_root / "partials" / "bad.md").write_bytes(b"\xff")
    write_task(
        playbook_root,
        "plan",
        "title: P\noutput: 03-plan.md\n",
        '{% include "partials/bad.md" %}\n',
    )

    messages, _ = check_problems(tmp_path, playbook_root)

    assert "cannot include 'partials/bad.md'" in messages[0]


def test_errors_inside_an_include_point_at_the_included_file(
    tmp_path: Path, playbook_root: Path
) -> None:
    (playbook_root / "partials" / "house.md").write_text(
        "line one\n{{ brief.missing }}\n"
    )

    messages, _ = check_problems(tmp_path, playbook_root)

    assert messages == [
        "playbooks/valid/partials/house.md:2: template: brief has no 'missing'"
    ]


def test_the_sandbox_is_reported_as_a_template_problem(
    tmp_path: Path, playbook_root: Path
) -> None:
    write_task(
        playbook_root,
        "plan",
        "title: P\noutput: 03-plan.md\n",
        "{{ brief.__class__ }}\n",
    )

    messages, _ = check_problems(tmp_path, playbook_root)

    assert messages == [
        "playbooks/valid/tasks/plan.md:5: template: access to attribute '__class__' of "
        "'Namespace' object is unsafe."
    ]


def test_check_templates_uses_real_inputs_and_placeholders(
    tmp_path: Path, playbook_root: Path
) -> None:
    write_task(
        playbook_root,
        "plan",
        "title: P\noutput: 03-plan.md\ndepends_on: [landscape]\n",
        "{% for f in files %}{{ f.path }}={{ f.heading }};{% endfor %}"
        "{{ deps.landscape.key_facts | length }}",
    )
    inputs = [InputFile("calls/one.md", tmp_path / "one.md", 10, "Call one")]
    loaded = brief(tmp_path)
    playbook = load(playbook_root)

    check_templates("p", loaded, playbook, inputs)
    context = placeholder_context("p", loaded, playbook.task("plan"), playbook, inputs)
    rendered = render_task_body(playbook, playbook.task("plan"), context)
    assert rendered == (
        "brief.md=The client;inputs/calls/one.md=Call one;02-landscape.md=Landscape;1"
    )


def test_public_view(tmp_path: Path) -> None:
    view = public_view(brief(tmp_path), load_valid())

    assert (
        view.public_context
        == "A stainless-steel fabricator planning a catalogue website."
    )
    (prompt,) = view.research
    assert prompt.task.id == "landscape"
    assert prompt.questions.startswith("For A stainless-steel fabricator")


def test_undefined_messages_fall_back_to_jinja() -> None:
    with pytest.raises(UndefinedError, match="'x' is undefined"):
        render("{{ x.y }}")


# Regressions from the M1 review.

STASH_BRIEF = BRIEF.replace(
    'title: "Acme Fabrication — test pack"', "title: SECRET-TITLE"
)


def stash_playbook() -> Playbook:
    return load_playbook(resolve_playbook(str(PLAYBOOKS / "stash"), PLAYBOOKS))


def test_the_stash_fixture_would_leak_through_a_shared_cached_environment(
    tmp_path: Path,
) -> None:
    # The attack is real: one cached environment for both renders leaks.
    playbook = stash_playbook()
    alpha, beta = playbook.task("alpha"), playbook.task("beta")
    assert beta.research is not None
    env = SandboxedEnvironment(loader=PlaybookLoader(PLAYBOOKS / "stash"))
    loaded = brief(tmp_path, STASH_BRIEF)
    env.from_string(alpha.body).render(
        placeholder_context("p", loaded, alpha, playbook, [])
    )
    public = research_context(beta, public_context="public", language="en")

    leaked = env.from_string(beta.research.questions).render(public)

    assert "SECRET-TITLE" in leaked
    assert "Acme Fabrication makes" in leaked


def test_task_bodies_cannot_stash_brief_data_for_research_questions(
    tmp_path: Path,
) -> None:
    playbook = stash_playbook()
    alpha, beta = playbook.task("alpha"), playbook.task("beta")
    loaded = brief(tmp_path, STASH_BRIEF)

    render_task_body(
        playbook, alpha, placeholder_context("p", loaded, alpha, playbook, [])
    )
    questions = render_research_questions(
        playbook, beta, public_context="public", language="en"
    )

    assert questions == "box=[] ns=\n"


def test_check_templates_and_show_public_cannot_leak_stashed_data(
    tmp_path: Path,
) -> None:
    playbook = stash_playbook()
    loaded = brief(tmp_path, STASH_BRIEF)

    check_templates("p", loaded, playbook, [])
    view = public_view(loaded, playbook)

    assert [prompt.questions for prompt in view.research] == ["box=[] ns=\n"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("{{ 1 / 0 }}", "template: division by zero (ZeroDivisionError)"),
        (
            "{% for p in brief.extra.none %}{% endfor %}",
            "template: 'NoneType' object is not iterable (TypeError)",
        ),
        ("{{ 1 + 'a' }}", "(TypeError)"),
        ('{% include "tasks/plan.md" %}', "(RecursionError)"),
        (
            "{{ brief.extra.when | tojson }}{{ cycler(1) | tojson }}",
            "Object of type Cycler is not JSON serializable (TypeError)",
        ),
    ],
)
def test_any_exception_in_a_template_is_a_located_problem(
    tmp_path: Path, playbook_root: Path, body: str, expected: str
) -> None:
    write_task(playbook_root, "plan", "title: P\noutput: 03-plan.md\n", f"x\n{body}\n")
    text = BRIEF.replace("priorities:", "none: null\nwhen: 2027-05-01\npriorities:")

    messages, hint = check_problems(tmp_path, playbook_root, text)

    assert len(messages) == 1
    assert messages[0].startswith("playbooks/valid/tasks/plan.md:")
    assert expected in messages[0]
    assert hint == TEMPLATE_HINT


def test_mapping_filters_work_on_brief_extra(tmp_path: Path) -> None:
    text = BRIEF.replace(
        "priorities:", "items: [a, b]\nwhen: 2027-05-01\nsite: {pages: 3}\npriorities:"
    )
    context = task_context(
        project_name="p",
        brief=brief(tmp_path, text),
        task=load_valid().task("overview"),
        files=[],
        deps=[],
        research=None,
    )

    assert render("{{ brief.extra | tojson }}", **context) == (
        '{"items": ["a", "b"], "site": {"pages": 3}, "when": "2027-05-01"}'
    )
    assert render(
        "{{ brief.extra | dictsort | map(attribute=0) | join(',') }}", **context
    ) == ("items,site,when")
    assert render("{{ brief.extra.site | list }}", **context) == "['pages']"
    assert render("{{ brief.extra.items | join(',') }}", **context) == "a,b"


def test_loader_refuses_link_loops_and_nul_bytes(tmp_path: Path) -> None:
    from jinja2 import TemplateNotFound

    (tmp_path / "loop.md").symlink_to(tmp_path / "loop.md")
    loader = PlaybookLoader(tmp_path)

    for name in ("loop.md", "a\x00b.md"):
        with pytest.raises(TemplateNotFound):
            loader.get_source(environment(tmp_path), name)


def test_research_questions_never_take_an_environment() -> None:
    import inspect

    parameters = inspect.signature(render_research_questions).parameters

    assert list(parameters) == ["playbook", "task", "public_context", "language"]
