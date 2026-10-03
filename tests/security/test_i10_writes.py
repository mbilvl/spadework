"""I10: spadework itself writes only inside the project and the OS temporary
directory, and removes what it stages there (docs/spec/07-security.md).

A full run of the example project (research, verification, review and
summary) runs in a separate process with the fake executor, its home, its
temporary directory and its working directory each in a folder of their own.
Then `verify`, `status`, `check` and `logs` run, and `clean --all` once the
command exists. Nothing may appear outside the project but in the temporary
directory, and nothing may be left there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

EXAMPLE = Path(__file__).parents[2] / "examples" / "acme-fabrication"


def tree(folder: Path) -> set[str]:
    """Every file and folder under ``folder``, relative to it."""
    return {path.relative_to(folder).as_posix() for path in folder.rglob("*")}


class Sandbox:
    def __init__(self, base: Path) -> None:
        self.home = base / "home"
        self.temp = base / "tmp"
        self.cwd = base / "elsewhere"
        self.project = base / "project"
        for folder in (self.home, self.temp, self.cwd):
            folder.mkdir()
        shutil.copytree(EXAMPLE, self.project)
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.home),
            "TMPDIR": str(self.temp),
            "TEMP": str(self.temp),
            "TMP": str(self.temp),
            "SPADEWORK_EXECUTOR": "fake",
            "PYTHONDONTWRITEBYTECODE": "1",
            "NO_COLOR": "1",
        }

    def spadework(self, *args: str, **env: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "spadework", *args, "-C", str(self.project)],
            cwd=self.cwd,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            check=False,
            timeout=300,
        )


def test_a_full_run_writes_only_in_the_project_and_its_temp_area(
    tmp_path: Path,
) -> None:
    box = Sandbox(tmp_path)
    project_before = tree(box.project)

    commands = [
        ("run",),
        ("verify", "--json"),
        ("status",),
        ("check",),
        ("logs",),
    ]
    results = [box.spadework(*command) for command in commands]

    for command, result in zip(commands, results, strict=True):
        assert result.returncode == 0, (command, result.stderr)
    assert "spent $" in results[0].stderr  # it did run: research, review, summary
    assert (box.project / "out" / "00-summary.md").is_file()
    # Nothing outside the project, and the temporary directory is left empty.
    assert tree(box.home) == set()
    assert tree(box.cwd) == set()
    assert tree(box.temp) == set()
    added = tree(box.project) - project_before
    assert added
    assert {path.split("/")[0] for path in added} == {".spadework", "out"}
    # Staging directories and control files are removed after each session.
    state = box.project / ".spadework"
    assert not any((state / "sessions").iterdir())
    assert not any((state / "control").iterdir())

    help_text = box.spadework("--help").stdout
    if "\n  clean " in help_text:
        cleaned = box.spadework("clean", "--all", "--yes")
        assert cleaned.returncode == 0, cleaned.stderr
        assert tree(box.home) == tree(box.cwd) == tree(box.temp) == set()
        assert not (box.project / ".spadework").exists()


def test_research_is_staged_in_the_temp_area_only(tmp_path: Path) -> None:
    box = Sandbox(tmp_path)

    # With SPADEWORK_DEBUG, staging is kept, so where it went can be seen.
    result = box.spadework("run", "--no-review", SPADEWORK_DEBUG="1")

    assert result.returncode == 0, result.stderr
    staged = {path.split("/")[0] for path in tree(box.temp)}
    # The research sessions of references, stack and seo, and nothing else.
    assert len(staged) == 3
    assert all(name.startswith("spadework-") for name in staged)
    for folder in staged:
        assert list((box.temp / folder).iterdir()) == []  # staged empty (I5)
    assert tree(box.home) == tree(box.cwd) == set()
