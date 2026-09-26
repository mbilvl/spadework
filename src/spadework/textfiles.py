"""Reading the text files a project and a playbook are made of."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from spadework.errors import InvalidError, Problem

# A key path inside a document, such as ("limits", "budget_usd") or
# ("depends_on", 2), mapped to the 1-based line it is written on.
KeyLines = dict[tuple[str | int, ...], int]


def read_text(path: Path, display: str, hint: str) -> str:
    """Read a UTF-8 file, dropping a byte-order mark if there is one."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise InvalidError(
            f"cannot read {display}",
            hint,
            problems=[Problem(f"cannot read the file: {exc.strerror}", display)],
        ) from exc
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        line = data[: exc.start].count(b"\n") + 1
        raise InvalidError(
            f"{display} is not UTF-8",
            "Save the file as UTF-8 text.",
            problems=[Problem("the file is not valid UTF-8 text", display, line)],
        ) from exc


@dataclass(frozen=True)
class TomlDocument:
    data: dict[str, Any]
    key_lines: KeyLines = field(default_factory=dict)


_TOML_POSITION = re.compile(r"\s*\(at line (\d+), column \d+\)$")


def load_toml(path: Path, display: str, hint: str) -> TomlDocument:
    """Parse a TOML file. Syntax errors name the file and the line."""
    text = read_text(path, display, hint)
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        message = str(exc.args[0]) if exc.args else str(exc)
        # Python 3.14 has exc.lineno, older versions only put it in the message.
        position = _TOML_POSITION.search(message)
        line = int(position.group(1)) if position else None
        message = _TOML_POSITION.sub("", message)
        raise InvalidError(
            f"{display} is not valid TOML",
            hint,
            problems=[Problem(f"invalid TOML: {message}", display, line)],
        ) from exc
    return TomlDocument(data, toml_key_lines(text))


_KEY_PART = r"(?:[A-Za-z0-9_-]+|\"[^\"]*\"|'[^']*')"
_TABLE = re.compile(r"^\s*\[\s*([^\[\]]+?)\s*\]\s*(?:#.*)?$")
_KEY = re.compile(rf"^\s*({_KEY_PART}(?:\s*\.\s*{_KEY_PART})*)\s*=")


def _split_dotted(key: str) -> tuple[str, ...]:
    parts = re.findall(r"\"[^\"]*\"|'[^']*'|[A-Za-z0-9_-]+", key)
    return tuple(part.strip("\"'") for part in parts)


def toml_key_lines(text: str) -> KeyLines:
    """Find the line of each table and key, for error messages.

    tomllib does not report positions, so this is a line scanner. It handles
    tables, dotted keys and multi-line strings, which is all spadework's files
    need. A key it cannot place simply gets no line number.
    """
    lines: KeyLines = {}
    table: tuple[str, ...] = ()
    in_multiline: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if in_multiline is not None:
            if line.count(in_multiline) % 2 == 1:
                in_multiline = None
            continue
        if match := _TABLE.match(line):
            table = _split_dotted(match.group(1))
            lines.setdefault(table, number)
        elif match := _KEY.match(line):
            lines.setdefault((*table, *_split_dotted(match.group(1))), number)
        for quote in ('"""', "'''"):
            if line.count(quote) % 2 == 1:
                in_multiline = quote
                break
    return lines
