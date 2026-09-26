import pytest

from spadework.errors import InvalidError
from spadework.frontmatter import parse_front_matter


def parse(text: str):  # type: ignore[no-untyped-def]
    return parse_front_matter(text, "tasks/scope.md", "Fix it.")


def problem(text: str) -> str:
    with pytest.raises(InvalidError) as caught:
        parse(text)
    assert caught.value.hint == "Fix it."
    (only,) = caught.value.problems
    return str(only)


def test_splits_front_matter_from_the_body() -> None:
    doc = parse("---\ntitle: Scope\nlist:\n  - a\n  - b\n---\n# Body\n\ntext\n")

    assert doc.data == {"title": "Scope", "list": ["a", "b"]}
    assert doc.body == "# Body\n\ntext\n"
    assert doc.body_line == 7
    assert doc.key_lines[("title",)] == 2
    assert doc.key_lines[("list", 1)] == 5


def test_accepts_windows_line_endings_and_the_dots_closer() -> None:
    doc = parse("---\r\ntitle: Scope\r\n...\r\nBody\r\n")

    assert doc.data == {"title": "Scope"}
    assert doc.body == "Body\r\n"


def test_empty_front_matter_is_an_empty_mapping() -> None:
    doc = parse("---\n---\nBody\n")

    assert doc.data == {}
    assert doc.body_line == 3


def test_requires_front_matter() -> None:
    assert problem("# Just markdown\n") == (
        "tasks/scope.md:1: the file must start with a '---' line of YAML front matter"
    )
    assert problem("").startswith("tasks/scope.md:1:")


def test_requires_a_closing_fence() -> None:
    assert "has no closing '---' line" in problem("---\ntitle: x\n")


def test_reports_yaml_syntax_errors_with_the_line() -> None:
    message = problem("---\ntitle: Scope\ndepends_on: [a, b\n---\n")

    assert message.startswith("tasks/scope.md:4: invalid YAML: ")


def test_rejects_a_front_matter_that_is_not_a_mapping() -> None:
    assert problem("---\n- a\n- b\n---\n") == (
        "tasks/scope.md:2: the front matter must be a mapping of keys to values"
    )


def test_rejects_duplicate_keys() -> None:
    assert (
        problem("---\ntitle: a\ntitle: b\n---\n")
        == "tasks/scope.md:3: duplicate key 'title'"
    )


def test_rejects_anchors_and_aliases() -> None:
    text = "---\na: &x [1, 2]\nb: *x\n---\n"

    assert problem(text) == (
        "tasks/scope.md:3: YAML anchors and aliases (& and *) are not supported"
    )


def test_rejects_python_tags() -> None:
    message = problem("---\nx: !!python/object/apply:os.system ['true']\n---\n")

    assert message.startswith("tasks/scope.md:2: invalid YAML:")


def test_rejects_keys_that_are_not_text() -> None:
    assert problem("---\n1: one\n---\n") == "tasks/scope.md:2: keys must be text, not 1"
