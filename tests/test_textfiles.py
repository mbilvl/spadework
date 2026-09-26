from pathlib import Path

import pytest

from spadework.errors import InvalidError
from spadework.textfiles import load_toml, read_text, toml_key_lines


def test_read_text_drops_a_byte_order_mark(tmp_path: Path) -> None:
    path = tmp_path / "brief.md"
    path.write_bytes(b"\xef\xbb\xbfhello")

    assert read_text(path, "brief.md", "hint") == "hello"


def test_read_text_names_the_line_of_bad_utf8(tmp_path: Path) -> None:
    path = tmp_path / "brief.md"
    path.write_bytes(b"one\ntwo \xff\n")

    with pytest.raises(InvalidError) as caught:
        read_text(path, "brief.md", "hint")

    assert [str(p) for p in caught.value.problems] == [
        "brief.md:2: the file is not valid UTF-8 text"
    ]
    assert caught.value.hint == "Save the file as UTF-8 text."


def test_read_text_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(InvalidError, match=r"cannot read x\.md"):
        read_text(tmp_path / "x.md", "x.md", "hint")


def test_load_toml_reports_syntax_errors_with_the_line(tmp_path: Path) -> None:
    path = tmp_path / "spadework.toml"
    path.write_text("schema_version = 1\n\n[project\nname = 'x'\n")

    with pytest.raises(InvalidError) as caught:
        load_toml(path, "spadework.toml", "Fix it.")

    (problem,) = caught.value.problems
    assert problem.path == "spadework.toml"
    assert problem.line == 3
    assert problem.message.startswith("invalid TOML: ")
    assert "(at line" not in problem.message
    assert caught.value.hint == "Fix it."


def test_toml_key_lines() -> None:
    text = '''\
schema_version = 1
description = """
not = a key
"""

[project]
name = "x"   # comment
"quoted key" = 1
a.b = 2

[limits.inner]
rounds = 2
'''
    lines = toml_key_lines(text)

    assert lines[("schema_version",)] == 1
    assert lines[("description",)] == 2
    assert ("not",) not in lines
    assert lines[("project",)] == 6
    assert lines[("project", "name")] == 7
    assert lines[("project", "quoted key")] == 8
    assert lines[("project", "a", "b")] == 9
    assert lines[("limits", "inner", "rounds")] == 12
