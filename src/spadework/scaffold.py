"""``spadework init``: create a project folder from a playbook."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from spadework.brief import BRIEF_FILE
from spadework.config import CONFIG_FILE, render_config
from spadework.errors import ExitCode, InvalidError, UsageError
from spadework.inputs import INPUTS_DIR
from spadework.playbook import (
    BRIEF_TEMPLATE,
    Playbook,
    contains,
    load_playbook,
    resolve_playbook,
)

GITIGNORE = ".gitignore"
IGNORE_LINE = ".spadework/"


@dataclass
class InitResult:
    root: Path
    playbook: Playbook
    written: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)


def project_name(directory: Path) -> str:
    """A slug for the project, from its folder name."""
    slug = re.sub(r"[^a-z0-9]+", "-", directory.name.lower()).strip("-")
    return slug or "project"


def init_project(
    target: Path, playbook_spec: str, *, force: bool, cwd: Path, shown: str
) -> InitResult:
    """Create a project in ``target``, which messages call ``shown``.

    Everything is checked before anything is written.
    """
    try:
        ref = resolve_playbook(playbook_spec, cwd)
    except InvalidError as exc:
        # The playbook came from a command-line argument, not a project file.
        exc.exit_code = ExitCode.USAGE
        raise
    playbook = load_playbook(ref)
    if playbook.brief_template is None:
        raise InvalidError(
            f"playbook {playbook.name!r} has no {BRIEF_TEMPLATE}",
            f"Add a {BRIEF_TEMPLATE} to the playbook for init to copy to brief.md.",
        )
    if contains(ref, target):
        raise UsageError(
            f"{shown} is inside the playbook folder",
            "Create the project outside the playbook folder, so templates cannot "
            "read its brief.",
        )
    _check_target(target, shown, force=force)

    target.mkdir(parents=True, exist_ok=True)
    playbook_value = ref.spec
    if ref.kind == "path":
        playbook_value = _relative_to_project(ref.root, target)
    result = InitResult(target, playbook)
    config = render_config(project_name(target.resolve()), playbook_value)
    _write(target, CONFIG_FILE, config, result)
    _write(target, BRIEF_FILE, playbook.brief_template, result)
    inputs = target / INPUTS_DIR
    if not inputs.is_dir():
        inputs.mkdir()
        result.written.append(f"{INPUTS_DIR}/")
    _add_ignore_line(target / GITIGNORE, result)
    return result


def _check_target(target: Path, shown: str, *, force: bool) -> None:
    if target.exists() and not target.is_dir():
        raise UsageError(f"{shown} is a file", "Choose a folder for the project.")
    if not target.is_dir():
        return
    if not force:
        clutter = [entry for entry in target.iterdir() if entry.name != ".git"]
        if clutter:
            raise UsageError(
                f"{shown} is not empty",
                "Choose a new or empty folder, or pass --force to add a project here "
                f"(it replaces any {CONFIG_FILE} and {BRIEF_FILE}).",
            )
    for name, is_right_kind, kind in [
        (INPUTS_DIR, Path.is_dir, "a folder"),
        (CONFIG_FILE, Path.is_file, "a file"),
        (BRIEF_FILE, Path.is_file, "a file"),
        (GITIGNORE, Path.is_file, "a file"),
    ]:
        path = target / name
        if path.exists() and not is_right_kind(path):
            raise UsageError(
                f"{shown}/{name} is not {kind}",
                "Rename or remove it, then run `spadework init` again.",
            )


def _relative_to_project(playbook_root: Path, project_root: Path) -> str:
    relative = os.path.relpath(playbook_root.resolve(), project_root.resolve())
    relative = relative.replace(os.sep, "/")
    return relative if relative.startswith("..") else f"./{relative}"


def _write(root: Path, name: str, text: str, result: InitResult) -> None:
    path = root / name
    if path.exists():
        result.replaced.append(name)
    else:
        result.written.append(name)
    path.write_text(text, encoding="utf-8", newline="\n")


def _add_ignore_line(path: Path, result: InitResult) -> None:
    if not path.exists():
        path.write_text(
            f"# spadework's engine state\n{IGNORE_LINE}\n",
            encoding="utf-8",
            newline="\n",
        )
        result.written.append(GITIGNORE)
        return
    # Bytes, so a .gitignore in any encoding is kept exactly as it was.
    data = path.read_bytes()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if any(line.strip().strip("/") == ".spadework" for line in lines):
        return
    separator = b"" if not data or data.endswith(b"\n") else b"\n"
    with path.open("ab") as file:
        file.write(separator + IGNORE_LINE.encode() + b"\n")
    result.written.append(f"{GITIGNORE} (added {IGNORE_LINE})")
