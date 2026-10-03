"""YAML front matter: the ``---`` block at the top of a Markdown file."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import yaml

from spadework.errors import InvalidError, Problem
from spadework.textfiles import KeyLines


@dataclass(frozen=True)
class FrontMatter:
    data: dict[str, Any]
    body: str
    # The 1-based line in the file where the body starts.
    body_line: int
    key_lines: KeyLines


_FENCE = "---"
_CLOSERS = ("---", "...")


def parse_front_matter(text: str, display: str, hint: str) -> FrontMatter:
    """Split ``text`` into its YAML front matter and its Markdown body.

    The YAML is parsed with ``yaml.safe_load``. Anchors and aliases are
    rejected, so a hostile file cannot expand into a huge document, and so
    are duplicate keys, which YAML would otherwise resolve silently.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].rstrip() != _FENCE:
        raise InvalidError(
            f"{display} has no front matter",
            hint,
            problems=[
                Problem(
                    "the file must start with a '---' line of YAML front matter",
                    display,
                    1,
                )
            ],
        )
    for index in range(1, len(lines)):
        if lines[index].rstrip() in _CLOSERS:
            end = index
            break
    else:
        raise InvalidError(
            f"{display} has unterminated front matter",
            hint,
            problems=[
                Problem(
                    "the front matter that starts here has no closing '---' line",
                    display,
                    1,
                )
            ],
        )

    yaml_text = "".join(lines[1:end])
    # YAML marks are 0-based and relative to yaml_text, which starts on line 2.
    first_line = 2

    def fail(message: str, line: int | None, why: str | None = None) -> InvalidError:
        return InvalidError(
            f"{display} has invalid front matter",
            why or hint,
            problems=[Problem(message, display, line)],
        )

    def invalid(exc: yaml.MarkedYAMLError) -> InvalidError:
        return fail(
            f"invalid YAML: {_yaml_problem(exc)}",
            _line(exc, first_line),
            yaml_hint(exc, yaml_text.splitlines(), first_line),
        )

    try:
        node = yaml.compose(yaml_text, Loader=yaml.SafeLoader)
    except yaml.MarkedYAMLError as exc:
        raise invalid(exc) from exc

    key_lines: KeyLines = {}
    if node is not None:
        if not isinstance(node, yaml.MappingNode):
            raise fail(
                "the front matter must be a mapping of keys to values",
                node.start_mark.line + first_line,
            )
        _index_nodes(node, (), key_lines, set(), first_line, fail)

    try:
        data = yaml.safe_load(yaml_text)
    except yaml.MarkedYAMLError as exc:
        raise invalid(exc) from exc
    if data is None:
        data = {}
    for key in data:
        if not isinstance(key, str):
            raise fail(f"keys must be text, not {key!r}", key_lines.get((str(key),)))

    return FrontMatter(
        data=data,
        body="".join(lines[end + 1 :]),
        body_line=end + 2,
        key_lines=key_lines,
    )


def _line(exc: yaml.MarkedYAMLError, first_line: int) -> int | None:
    mark = exc.problem_mark or exc.context_mark
    return None if mark is None else mark.line + first_line


def _yaml_problem(exc: yaml.MarkedYAMLError) -> str:
    parts = [exc.context, exc.problem]
    return ", ".join(part for part in parts if part) or "cannot parse"


# A line that holds a key and a value, or a list item.
_KEY_VALUE = re.compile(r"^\s*(?P<key>[^\s'\"#:-][^:#]*?):\s+(?P<value>.+?)\s*$")
_ITEM = re.compile(r"^\s*-\s+(?P<value>.+?)\s*$")
_CANNOT_START = re.compile(r"found character '.' that cannot start any token")


def yaml_hint(
    exc: yaml.MarkedYAMLError, lines: list[str], first_line: int
) -> str | None:
    """A hint for the YAML mistakes people make most, or None.

    ``lines`` are the front matter's lines; ``first_line`` is the file line
    of the first of them.
    """
    problem = exc.problem or ""
    context = exc.context or ""
    mark = exc.problem_mark
    text = lines[mark.line] if mark is not None and mark.line < len(lines) else ""
    if "'\\t'" in problem:
        return "YAML does not allow tabs: indent with spaces."
    if context.startswith("while scanning a quoted scalar"):
        start = exc.context_mark
        where = "" if start is None else f" on line {start.line + first_line}"
        return f"A quoted value is not closed: close the quote that opens{where}."
    if context.startswith("while scanning a block scalar"):
        return "After > or |, start the text on the next line, indented by two spaces."
    if problem == "could not find expected ':'":
        return (
            "Each line needs a key and a colon. To go on with a value on more lines, "
            "indent them under its key."
        )
    if "expected <block end>, but found '<scalar>'" in problem:
        return (
            "Text follows a closing quote: put the whole value inside the quotes, "
            'and write a quote inside it as \\".'
        )
    if "expected <block end>, but found '<block" in problem:
        return "Check the indentation: the items of a list must line up."
    if "expected <block end>, but found ']'" in problem or problem.endswith("'}'"):
        return (
            "A ] or } has no opening bracket: remove it, or put the value in double "
            "quotes if the bracket is part of the text."
        )
    if problem.startswith(("found unknown escape character", "expected escape")):
        return (
            "Inside double quotes a backslash starts an escape: write \\\\ for a "
            "backslash, or use single quotes."
        )
    if context.startswith("while parsing a flow"):
        return (
            "A [ or { is not closed: close it, or put the value in double quotes if "
            "the bracket is part of the text."
        )
    parts = _value(text)
    if parts is not None:
        lead, value = parts
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        quoted = f'Put the value in double quotes: {lead}"{escaped}"'
        if problem == "mapping values are not allowed here" and ": " in value:
            return quoted
        if (
            problem.startswith("sequence entries are not allowed")
            or context.startswith("while scanning an alias")
            or _CANNOT_START.search(problem)
        ):
            return quoted
    if problem == "mapping values are not allowed here":
        return (
            "Check the indentation of this line: keys of the front matter start at "
            "the beginning of a line."
        )
    return None


def _value(line: str) -> tuple[str, str] | None:
    """The start of ``line`` up to its value, and its value, if it has an
    unquoted one: ``("title: ", "x")``, or ``("- ", "x")`` for a list item."""
    if (match := _KEY_VALUE.match(line)) is not None:
        lead, value = f"{match['key']}: ", match["value"]
    elif (item := _ITEM.match(line)) is not None:
        lead, value = "- ", item["value"]
    else:
        return None
    if value[0] in "'\"" or value in (">", "|"):
        return None
    return lead, value


def _index_nodes(
    node: yaml.Node,
    path: tuple[str | int, ...],
    lines: KeyLines,
    seen: set[int],
    first_line: int,
    fail: Callable[[str, int | None], InvalidError],
) -> None:
    # PyYAML resolves an alias to the very node object it refers to, so a
    # node seen twice is an alias. Report where it is used, not the anchor.
    if id(node) in seen:
        where = path if path and isinstance(path[-1], str) else path[:-1]
        raise fail(
            "YAML anchors and aliases (& and *) are not supported", lines.get(where)
        )
    seen.add(id(node))
    if isinstance(node, yaml.MappingNode):
        keys: set[str] = set()
        for key_node, value_node in node.value:
            key = str(key_node.value)
            if key in keys:
                raise fail(
                    f"duplicate key '{key}'", key_node.start_mark.line + first_line
                )
            keys.add(key)
            lines[(*path, key)] = key_node.start_mark.line + first_line
            _index_nodes(value_node, (*path, key), lines, seen, first_line, fail)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            lines[(*path, index)] = item.start_mark.line + first_line
            _index_nodes(item, (*path, index), lines, seen, first_line, fail)
