"""YAML front matter: the ``---`` block at the top of a Markdown file."""

from __future__ import annotations

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

    def fail(message: str, line: int | None) -> InvalidError:
        return InvalidError(
            f"{display} has invalid front matter",
            hint,
            problems=[Problem(message, display, line)],
        )

    try:
        node = yaml.compose(yaml_text, Loader=yaml.SafeLoader)
    except yaml.MarkedYAMLError as exc:
        raise fail(
            f"invalid YAML: {_yaml_problem(exc)}", _line(exc, first_line)
        ) from exc

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
        raise fail(
            f"invalid YAML: {_yaml_problem(exc)}", _line(exc, first_line)
        ) from exc
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
