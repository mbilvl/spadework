import pytest

from spadework.errors import InvalidError
from spadework.frontmatter import parse_front_matter


def parse(text: str):  # type: ignore[no-untyped-def]
    return parse_front_matter(text, "tasks/scope.md", "Fix it.")


def problem(text: str) -> str:
    with pytest.raises(InvalidError) as caught:
        parse(text)
    (only,) = caught.value.problems
    return str(only)


def hint(text: str) -> str:
    with pytest.raises(InvalidError) as caught:
        parse(text)
    return caught.value.hint


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


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        (
            "reader: Note: a freelance developer",
            'Put the value in double quotes: reader: "Note: a freelance developer"',
        ),
        (
            'reader: Note: a "freelance" developer',
            'Put the value in double quotes: reader: "Note: a \\"freelance\\" '
            'developer"',
        ),
        ("title: @acme", 'Put the value in double quotes: title: "@acme"'),
        ("title: `x`", 'Put the value in double quotes: title: "`x`"'),
        ("title: *bold*", 'Put the value in double quotes: title: "*bold*"'),
        (
            "reader: - a developer",
            'Put the value in double quotes: reader: "- a developer"',
        ),
        ("\treader: x", "YAML does not allow tabs: indent with spaces."),
        (
            'title: "Acme',
            "A quoted value is not closed: close the quote that opens on line 2.",
        ),
        (
            'title: "Acme" fabrication',
            "Text follows a closing quote: put the whole value inside the quotes, "
            'and write a quote inside it as \\".',
        ),
        (
            "public_context: > A fabricator",
            "After > or |, start the text on the next line, indented by two spaces.",
        ),
        (
            "title: x\nthis line goes on",
            "Each line needs a key and a colon. To go on with a value on more lines, "
            "indent them under its key.",
        ),
        (
            "priorities:\n  - a\n - b",
            "Check the indentation: the items of a list must line up.",
        ),
        (
            "title: x\n  reader: y",
            "Check the indentation of this line: keys of the front matter start at "
            "the beginning of a line.",
        ),
        (
            "title: [a]]",
            "A ] or } has no opening bracket: remove it, or put the value in double "
            "quotes if the bracket is part of the text.",
        ),
        (
            'title: "C:\\docs"',
            "Inside double quotes a backslash starts an escape: write \\\\ for a "
            "backslash, or use single quotes.",
        ),
        (
            'title: "C:\\Users"',
            "Inside double quotes a backslash starts an escape: write \\\\ for a "
            "backslash, or use single quotes.",
        ),
        (
            "currencies: [EUR",
            "A [ or { is not closed: close it, or put the value in double quotes if "
            "the bracket is part of the text.",
        ),
    ],
)
def test_common_yaml_mistakes_get_a_hint(line: str, expected: str) -> None:
    assert hint(f"---\n{line}\n---\n") == expected


def test_other_yaml_errors_keep_the_callers_hint() -> None:
    assert hint("---\ntitle: !!binary x\n---\n") == "Fix it."


def test_errors_that_are_not_yaml_syntax_keep_the_callers_hint() -> None:
    assert hint("---\ntitle: a\ntitle: b\n---\n") == "Fix it."
