import os
from pathlib import Path

import pytest

from spadework.errors import InvalidError
from spadework.inputs import (
    InputFile,
    first_heading,
    glob_regex,
    ignored_inputs,
    ignored_inputs_warning,
    match_inputs,
    scan_inputs,
)


def test_scan_lists_md_and_txt_files_sorted_and_skips_hidden(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    (inputs / "calls").mkdir(parents=True)
    (inputs / "calls" / "2026-09-30-call.md").write_text("intro\n## Client call\n")
    (inputs / "2026-10-01-email.txt").write_text("plain")
    (inputs / ".DS_Store").write_bytes(b"\0\1")
    (inputs / ".git").mkdir()
    (inputs / ".git" / "x.pdf").write_bytes(b"%PDF")

    files = scan_inputs(inputs)

    assert [(f.name, f.heading, f.size) for f in files] == [
        ("2026-10-01-email.txt", None, 5),
        ("calls/2026-09-30-call.md", "Client call", 21),
    ]
    assert files[1].display == "inputs/calls/2026-09-30-call.md"


def test_a_missing_inputs_folder_means_no_inputs(tmp_path: Path) -> None:
    assert scan_inputs(tmp_path / "inputs") == []


def test_inputs_must_be_a_folder(tmp_path: Path) -> None:
    (tmp_path / "inputs").write_text("x")

    with pytest.raises(InvalidError, match="inputs is not a folder"):
        scan_inputs(tmp_path / "inputs")


def test_other_file_types_are_ignored(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    (inputs / "scans").mkdir(parents=True)
    (inputs / "brief.pdf").write_bytes(b"%PDF")
    (inputs / "scans" / "site.PNG").write_bytes(b"\x89PNG")
    (inputs / "notes.MD").write_text("# Upper case suffix is fine")
    (inputs / ".hidden.pdf").write_bytes(b"%PDF")

    files = scan_inputs(inputs)

    assert [file.name for file in files] == ["notes.MD"]
    assert ignored_inputs(inputs) == ["inputs/brief.pdf", "inputs/scans/site.PNG"]


def test_text_inputs_that_are_not_utf8_are_still_errors(tmp_path: Path) -> None:
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    (inputs / "bad.md").write_bytes(b"\xff\xfe")
    (inputs / "brief.pdf").write_bytes(b"%PDF")

    with pytest.raises(InvalidError) as caught:
        scan_inputs(inputs)

    assert caught.value.message == "inputs/ holds files spadework cannot use"
    assert [str(p) for p in caught.value.problems] == [
        "inputs/bad.md:1: the file is not valid UTF-8 text",
    ]
    assert "Convert them to Markdown or plain text" in caught.value.hint


def test_the_warning_for_ignored_inputs() -> None:
    assert ignored_inputs_warning([]) is None
    assert ignored_inputs_warning(["inputs/a.pdf"]) == (
        "inputs/a.pdf is ignored: only .md and .txt inputs are read",
        "Convert it to Markdown or plain text to use it, or move it out of inputs/.",
    )
    assert ignored_inputs_warning(["inputs/a.pdf", "inputs/b.docx"]) == (
        "2 files in inputs/ are ignored, because only .md and .txt inputs are read: "
        "inputs/a.pdf, inputs/b.docx",
        "Convert them to Markdown or plain text to use them, or move them out of "
        "inputs/.",
    )


def test_ignored_inputs_without_an_inputs_folder(tmp_path: Path) -> None:
    assert ignored_inputs(tmp_path / "inputs") == []


@pytest.mark.parametrize(
    ("text", "heading"),
    [
        ("# Title\n", "Title"),
        ("text\n  ## Indented #\n", "Indented"),
        ("#NoSpace\n####### seven\n", None),
        ("", None),
    ],
)
def test_first_heading(text: str, heading: str | None) -> None:
    assert first_heading(text) == heading


@pytest.mark.parametrize(
    ("pattern", "matches", "misses"),
    [
        ("**", ["a.md", "x/y/z.txt"], []),
        ("*.md", ["a.md"], ["x/a.md", "a.txt"]),
        ("calls/**", ["calls/a.md", "calls/x/b.md"], ["a.md", "callsx/a.md"]),
        ("**/*.md", ["a.md", "x/y/a.md"], ["a.txt"]),
        ("2026-0?-*.md", ["2026-09-30.md"], ["2026-10-01.md"]),
        ("[ab]*.md", ["a.md", "b1.md"], ["c.md"]),
        ("[!ab]*.md", ["c.md"], ["a.md"]),
        ("x**y.md", ["xy.md", "x-y.md"], ["x/y.md"]),
        ("a+b.md", ["a+b.md"], ["aab.md"]),
        ("[unclosed.md", ["[unclosed.md"], ["u"]),
    ],
)
def test_globs(pattern: str, matches: list[str], misses: list[str]) -> None:
    regex = glob_regex(pattern)

    for name in matches:
        assert regex.fullmatch(name), name
    for name in misses:
        assert not regex.fullmatch(name), name


def test_match_inputs_keeps_the_input_order(tmp_path: Path) -> None:
    files = [
        InputFile(name, tmp_path / name, 1, None) for name in ["a.md", "b.txt", "c.md"]
    ]

    assert [f.name for f in match_inputs(["*.md", "b.*"], files)] == [
        "a.md",
        "b.txt",
        "c.md",
    ]
    assert match_inputs([], files) == []


# Regressions from the M1 review.


def project_with_inputs(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "inputs").mkdir(parents=True)
    (root / "inputs" / "a.md").write_text("# A")
    return root


def scan_problems(root: Path) -> tuple[list[str], str]:
    with pytest.raises(InvalidError) as caught:
        scan_inputs(root / "inputs", root)
    return [str(p) for p in caught.value.problems], caught.value.hint


def test_linked_folders_are_reported(tmp_path: Path) -> None:
    root = project_with_inputs(tmp_path)
    (tmp_path / "shared").mkdir()
    (tmp_path / "shared" / "call.md").write_text("# Call")
    (root / "inputs" / "shared").symlink_to(tmp_path / "shared")

    assert scan_problems(root) == (
        ["inputs/shared: is a link to a folder; linked folders are not supported"],
        "Copy the files into inputs/ rather than linking a folder.",
    )


def test_a_linked_inputs_folder_is_reported(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    (tmp_path / "shared").mkdir()
    (root / "inputs").symlink_to(tmp_path / "shared")

    with pytest.raises(InvalidError, match="inputs is a link to a folder"):
        scan_inputs(root / "inputs", root)


def test_linked_files_inside_the_project_are_followed(tmp_path: Path) -> None:
    root = project_with_inputs(tmp_path)
    (root / "notes").mkdir()
    (root / "notes" / "call.md").write_text("# Call")
    (root / "inputs" / "call.md").symlink_to(root / "notes" / "call.md")

    files = scan_inputs(root / "inputs", root)

    assert [(f.name, f.heading) for f in files] == [("a.md", "A"), ("call.md", "Call")]


def test_linked_files_outside_the_project_are_reported(tmp_path: Path) -> None:
    root = project_with_inputs(tmp_path)
    (tmp_path / "secret.md").write_text("# Secret")
    (root / "inputs" / "secret.md").symlink_to(tmp_path / "secret.md")
    (root / "inputs" / "loop.md").symlink_to(root / "inputs" / "loop.md")

    messages, hint = scan_problems(root)

    assert messages == [
        "inputs/loop.md: is a link that cannot be followed",
        "inputs/secret.md: is a link to a file outside the project",
    ]
    assert hint == "Copy the file into inputs/ rather than linking to it."


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_unreadable_folders_are_reported(tmp_path: Path) -> None:
    root = project_with_inputs(tmp_path)
    (root / "inputs" / "private").mkdir()
    (root / "inputs" / "private" / "b.md").write_text("# B")
    (root / "inputs" / "scan.pdf").write_bytes(b"%PDF")
    (root / "inputs" / "private").chmod(0)
    try:
        messages, hint = scan_problems(root)
    finally:
        (root / "inputs" / "private").chmod(0o755)

    assert messages == [
        "inputs/private: cannot read this folder: Permission denied",
    ]
    assert hint == "Make the folder readable, or move it out of inputs/."


def test_bad_globs_raise_and_nested_brackets_do_not_warn() -> None:
    import re

    with pytest.raises(re.error, match="bad character range"):
        glob_regex("[z-a]*.md")
    with pytest.raises(re.error, match="Possible set difference"):
        glob_regex("[a--z]")
    assert glob_regex("[[]x.md").fullmatch("[x.md")
    assert glob_regex("[]a]").fullmatch("]")
    assert glob_regex("[!]a]").fullmatch("b")
    assert glob_regex("[^a]").fullmatch("^")
