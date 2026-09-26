"""``inputs/``: material that arrives after the brief."""

from __future__ import annotations

import os
import re
import warnings
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from pathlib import Path

from spadework.errors import InvalidError, Problem
from spadework.textfiles import read_text

INPUTS_DIR = "inputs"
INPUT_SUFFIXES = (".md", ".txt")
# The soft limit on the total size of inputs/ (docs/spec/02-project-layout.md).
SOFT_LIMIT_BYTES = 500_000

_HEADING = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$")


@dataclass(frozen=True)
class InputFile:
    # The path relative to inputs/, with '/' separators. Task globs match it.
    name: str
    path: Path
    size: int
    heading: str | None

    @property
    def display(self) -> str:
        return f"{INPUTS_DIR}/{self.name}"


def first_heading(text: str) -> str | None:
    """The text of the first Markdown ATX heading, if there is one."""
    for line in text.splitlines():
        if match := _HEADING.match(line):
            return match.group(1)
    return None


_UNSUPPORTED = "Convert them to Markdown or plain text, or move them out of inputs/."
_UNREADABLE = "Make the folder readable, or move it out of inputs/."
_LINKED_FOLDER = "Copy the files into inputs/ rather than linking a folder."
_LINK_OUTSIDE = "Copy the file into inputs/ rather than linking to it."


def scan_inputs(directory: Path, root: Path | None = None) -> list[InputFile]:
    """List the input files, sorted by name. Hidden files are ignored.

    ``root`` is the project folder (default: the parent of ``directory``).
    Links to files inside it are followed; links to folders, and to files
    outside it, are not. Raises InvalidError for anything spadework cannot
    use, including folders it cannot read.
    """
    real_root = (directory.parent if root is None else root).resolve()
    if not directory.exists() and not directory.is_symlink():
        return []
    if directory.is_symlink():
        raise InvalidError(
            f"{INPUTS_DIR} is a link to a folder",
            f"Make {INPUTS_DIR}/ a real folder. {_LINKED_FOLDER}",
        )
    if not directory.is_dir():
        raise InvalidError(
            f"{INPUTS_DIR} is not a folder",
            f"Rename that file and create an {INPUTS_DIR}/ folder for inputs.",
        )

    def shown(path: Path | str) -> str:
        relative = Path(path).relative_to(directory).as_posix()
        return INPUTS_DIR if relative == "." else f"{INPUTS_DIR}/{relative}"

    files = []
    problems: list[Problem] = []
    hints: list[str] = []

    def report(message: str, path: Path | str, hint: str) -> None:
        problems.append(Problem(message, shown(path)))
        if hint not in hints:
            hints.append(hint)

    def unreadable(exc: OSError) -> None:
        report(f"cannot read this folder: {exc.strerror}", exc.filename, _UNREADABLE)

    for current, folders, names in os.walk(directory, onerror=unreadable):
        visible = []
        for folder in sorted(folders):
            if folder.startswith("."):
                continue
            if Path(current, folder).is_symlink():
                report(
                    "is a link to a folder; linked folders are not supported",
                    Path(current, folder),
                    _LINKED_FOLDER,
                )
                continue
            visible.append(folder)
        folders[:] = visible
        for file_name in sorted(names):
            if file_name.startswith("."):
                continue
            path = Path(current, file_name)
            if path.suffix.lower() not in INPUT_SUFFIXES:
                report("only .md and .txt inputs are supported", path, _UNSUPPORTED)
                continue
            if path.is_symlink() and (problem := _link_problem(path, real_root)):
                report(problem, path, _LINK_OUTSIDE)
                continue
            try:
                text = read_text(path, shown(path), _UNSUPPORTED)
            except InvalidError as exc:
                problems.extend(exc.problems)
                if _UNSUPPORTED not in hints:
                    hints.append(_UNSUPPORTED)
                continue
            name = path.relative_to(directory).as_posix()
            files.append(
                InputFile(name, path, path.stat().st_size, first_heading(text))
            )
    if problems:
        raise InvalidError(
            f"{INPUTS_DIR}/ holds files spadework cannot use",
            " ".join(hints),
            problems=problems,
        )
    return sorted(files, key=lambda file: file.name)


def _link_problem(path: Path, root: Path) -> str | None:
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError):  # RuntimeError: a link loop before 3.13
        return "is a link that cannot be followed"
    if not target.is_relative_to(root):
        return "is a link to a file outside the project"
    return None


def total_size(files: Iterable[InputFile]) -> int:
    return sum(file.size for file in files)


def match_inputs(
    patterns: Sequence[str], files: Iterable[InputFile]
) -> list[InputFile]:
    """The files that any of ``patterns`` matches, in their original order."""
    compiled = [glob_regex(pattern) for pattern in patterns]
    return [
        file for file in files if any(regex.fullmatch(file.name) for regex in compiled)
    ]


@cache
def glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a glob over paths relative to inputs/.

    ``*`` and ``?`` stay within one folder, ``**`` as a whole segment matches
    any number of folders, and ``[...]`` is a character class.
    """
    segments = pattern.split("/")
    parts = []
    for index, segment in enumerate(segments):
        last = index == len(segments) - 1
        if segment == "**":
            parts.append(".*" if last else "(?:[^/]+/)*")
            continue
        parts.append(_segment_regex(segment) + ("" if last else "/"))
    # A FutureWarning marks a class that a later Python may read differently,
    # such as "[a--z]". Refuse it rather than print the warning.
    with warnings.catch_warnings():
        warnings.simplefilter("error", FutureWarning)
        try:
            return re.compile("".join(parts))
        except FutureWarning as exc:
            raise re.error(str(exc)) from None


def _segment_regex(segment: str) -> str:
    out = []
    index = 0
    while index < len(segment):
        char = segment[index]
        if char == "*":
            while index + 1 < len(segment) and segment[index + 1] == "*":
                index += 1
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        elif char == "[" and (end := _class_end(segment, index)) != -1:
            body = segment[index + 1 : end]
            negate = body.startswith("!")
            if negate:
                body = body[1:]
            # Everything but '-' is literal inside a class, as in fnmatch.
            escaped = "".join(c if c == "-" else re.escape(c) for c in body)
            out.append("[" + ("^" if negate else "") + escaped + "]")
            index = end
        else:
            out.append(re.escape(char))
        index += 1
    return "".join(out)


def _class_end(segment: str, start: int) -> int:
    """The index of the ']' closing the class that opens at ``start``, or -1.

    As in fnmatch, a ']' straight after '[' or '[!' is part of the class.
    """
    index = start + 1
    if index < len(segment) and segment[index] == "!":
        index += 1
    if index < len(segment) and segment[index] == "]":
        index += 1
    return segment.find("]", index)
