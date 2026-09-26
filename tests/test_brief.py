import datetime
import shutil
from pathlib import Path

import pytest

from spadework.brief import Brief, BriefFrontMatter, load_brief
from spadework.errors import ExitCode, InvalidError
from spadework.playbook import Playbook, load_playbook, resolve_playbook
from tests.conftest import BRIEF, PLAYBOOKS

HINT = "Fix the front matter of brief.md, then run `spadework check` again."


def fixture_playbook(name: str = "valid") -> Playbook:
    return load_playbook(resolve_playbook(str(PLAYBOOKS / name), PLAYBOOKS))


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "brief.md"
    path.write_text(text, encoding="utf-8")
    return path


def load(tmp_path: Path, text: str, playbook: Playbook | None = None) -> Brief:
    return load_brief(write(tmp_path, text), playbook)


def failure(
    tmp_path: Path, text: str, playbook: Playbook | None = None
) -> InvalidError:
    with pytest.raises(InvalidError) as caught:
        load(tmp_path, text, playbook)
    assert caught.value.exit_code == ExitCode.INVALID
    return caught.value


def test_loads_fields_extra_keys_and_body(tmp_path: Path) -> None:
    text = BRIEF.replace(
        "priorities:",
        "language: pt-PT\ncurrencies: [EUR, GBP]\nsite_languages: [pt, en]\n"
        "launch: 2027-05-01\npriorities:",
    )
    brief = load(tmp_path, text, fixture_playbook())

    assert brief.front.title == "Acme Fabrication — test pack"
    assert brief.front.language == "pt-PT"
    assert brief.front.currencies == ["EUR", "GBP"]
    assert brief.front.priorities == ["Staff can edit the catalogue unaided"]
    assert (
        brief.public_context
        == "A stainless-steel fabricator planning a catalogue website."
    )
    assert dict(brief.extra) == {
        "site_languages": ["pt", "en"],
        "launch": datetime.date(2027, 5, 1),
    }
    assert brief.body.startswith("\n# The client")
    assert brief.source == text


def test_defaults(tmp_path: Path) -> None:
    brief = load(tmp_path, "---\ntitle: T\nreader: R\n---\n")

    assert brief.front.language == "en"
    assert brief.front.currencies == []
    assert brief.front.priorities is None
    assert brief.public_context is None
    assert dict(brief.extra) == {}


def test_missing_required_fields(tmp_path: Path) -> None:
    error = failure(tmp_path, "---\ntitle: T\n---\n")

    assert [str(p) for p in error.problems] == ["brief.md: 'reader' is required"]
    assert error.hint == HINT


def test_blank_title(tmp_path: Path) -> None:
    error = failure(tmp_path, "---\ntitle: '  '\nreader: R\n---\n")

    assert [str(p) for p in error.problems] == ["brief.md:2: title: must not be empty"]


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("language: english", "language: 'english' is not valid: use a language tag"),
        (
            "currencies: [eur]",
            "currencies[0]: 'eur' is not valid: use a three-letter ISO 4217",
        ),
        ("currencies: EUR", "currencies: should be a valid list"),
        ("priorities: first", "priorities: should be a valid list"),
        ("priorities: ['']", "priorities[0]: must not be empty"),
        ("public_context: 3", "public_context: should be a valid string"),
    ],
)
def test_invalid_fields(tmp_path: Path, line: str, expected: str) -> None:
    error = failure(tmp_path, f"---\ntitle: T\nreader: R\n{line}\n---\n")

    (problem,) = error.problems
    assert problem.line == 4
    assert expected in problem.message


def test_yaml_booleans_get_a_quoting_hint(tmp_path: Path) -> None:
    error = failure(tmp_path, "---\ntitle: T\nreader: R\nlanguage: no\n---\n")

    assert [str(p) for p in error.problems] == [
        "brief.md:4: language: should be text, but YAML read it as false"
    ]
    assert error.hint == (
        "YAML reads unquoted yes, no, on, off, true and false as true or false. "
        'Put the value in quotes, for example language: "no".'
    )


def test_yaml_booleans_in_lists_get_the_hint_too(tmp_path: Path) -> None:
    error = failure(tmp_path, "---\ntitle: T\nreader: R\npriorities: [yes]\n---\n")

    assert "should be text, but YAML read it as true" in str(error.problems[0])
    assert 'for example priorities: "yes"' in error.hint


def test_public_context_is_required_for_research_playbooks(tmp_path: Path) -> None:
    text = BRIEF.replace(
        'public_context: "A stainless-steel fabricator planning a catalogue website."'
        "\n",
        "",
    )
    error = failure(tmp_path, text, fixture_playbook())

    assert [str(p) for p in error.problems] == [
        "brief.md: 'public_context' is required because the playbook has research "
        "tasks (landscape)"
    ]
    assert 'public_context: "" to share nothing' in error.hint


def test_public_context_may_be_empty(tmp_path: Path) -> None:
    text = BRIEF.replace(
        '"A stainless-steel fabricator planning a catalogue website."', '""'
    )
    brief = load(tmp_path, text, fixture_playbook())

    assert brief.public_context == ""


def test_public_context_is_optional_without_research(tmp_path: Path) -> None:
    brief = load(
        tmp_path,
        "---\ntitle: T\nreader: R\n---\n",
        fixture_playbook("undefined-variable"),
    )

    assert brief.public_context is None


@pytest.mark.parametrize("value", ["", "priorities: []", "priorities:"])
def test_playbook_required_fields(tmp_path: Path, value: str) -> None:
    text = f"---\ntitle: T\nreader: R\npublic_context: ''\n{value}\n---\n"
    error = failure(tmp_path, text, fixture_playbook())

    assert [p.message for p in error.problems] == [
        "'priorities' is required by the playbook 'fixture'"
    ]
    assert error.hint == "Add priorities: to the front matter."


def test_missing_brief(tmp_path: Path) -> None:
    with pytest.raises(InvalidError) as caught:
        load_brief(tmp_path / "brief.md")

    assert caught.value.message == "there is no brief.md"
    assert "spadework init" in caught.value.hint


def test_extra_keys_are_allowed_by_the_model() -> None:
    front = BriefFrontMatter.model_validate(
        {"title": "T", "reader": "R", "industry": "steel"}
    )

    assert front.model_extra == {"industry": "steel"}


@pytest.mark.parametrize(
    ("extra", "missing"),
    [
        ("site: []\ncount: 0\n", ["site"]),
        ("site: '  '\ncount: 0\n", ["site"]),
        ("site: {pages: 3}\ncount: 0\n", []),
        ("count: 2\n", ["site"]),
    ],
)
def test_playbooks_can_require_extra_keys(
    tmp_path: Path, extra: str, missing: list[str]
) -> None:
    root = tmp_path / "playbook"
    shutil.copytree(PLAYBOOKS / "valid", root)
    toml = (root / "playbook.toml").read_text()
    (root / "playbook.toml").write_text(
        toml.replace(
            'requires_brief = ["priorities"]', 'requires_brief = ["site", "count"]'
        )
    )
    playbook = load_playbook(resolve_playbook(str(root), tmp_path))
    text = f"---\ntitle: T\nreader: R\npublic_context: ''\n{extra}---\n"

    if not missing:
        assert load(tmp_path, text, playbook).extra["site"] == {"pages": 3}
        return
    error = failure(tmp_path, text, playbook)
    assert [p.message for p in error.problems] == [
        f"'{key}' is required by the playbook 'fixture'" for key in missing
    ]
