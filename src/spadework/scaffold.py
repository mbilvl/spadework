"""``spadework init``: create a project folder from a playbook."""

from __future__ import annotations

import os
import re
import stat
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
    backups: dict[str, str] = field(default_factory=dict)
    """The name each replaced file was kept under, if it differed."""


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
    found = _check_target(target, shown, force=force)

    target.mkdir(parents=True, exist_ok=True)
    playbook_value = ref.spec
    if ref.kind == "path":
        playbook_value = _relative_to_project(ref.root, target)
    result = InitResult(target, playbook)
    config = render_config(project_name(target.resolve()), playbook_value)
    _write(target, CONFIG_FILE, config, found.files[CONFIG_FILE], result)
    _write(target, BRIEF_FILE, playbook.brief_template, found.files[BRIEF_FILE], result)
    if not found.has_inputs:
        (target / INPUTS_DIR).mkdir()
        result.written.append(f"{INPUTS_DIR}/")
    _add_ignore_line(target / GITIGNORE, found.gitignore, result)
    return result


class _Link:
    """An existing file that is a link: it is moved aside, never followed."""


LINK = _Link()


@dataclass(frozen=True)
class _Found:
    """What the target folder holds, read before anything is written."""

    files: dict[str, bytes | _Link | None]
    """The content of spadework.toml and brief.md; None if absent."""
    gitignore: bytes | None
    """The content of .gitignore; None if absent."""
    has_inputs: bool


def _check_target(target: Path, shown: str, *, force: bool) -> _Found:
    """Check the target folder and read what init would replace, so that
    nothing is written unless every file can be handled."""
    nothing = _Found({CONFIG_FILE: None, BRIEF_FILE: None}, None, False)
    if target.is_symlink() and not target.exists():
        raise UsageError(
            f"{shown} is a link to nothing", "Choose a folder for the project."
        )
    if target.exists() and not target.is_dir():
        raise UsageError(f"{shown} is a file", "Choose a folder for the project.")
    if not target.is_dir():
        return nothing
    if not force:
        clutter = [entry for entry in target.iterdir() if entry.name != ".git"]
        if clutter:
            raise UsageError(
                f"{shown} is not empty",
                "Create the project in a new folder with `spadework init NAME`, or "
                "pass --force to add it here; any "
                f"{CONFIG_FILE} and {BRIEF_FILE} it replaces are kept as .bak files.",
            )

    def refuse(name: str, problem: str, hint: str) -> UsageError:
        return UsageError(f"{shown}/{name} {problem}", hint)

    again = "then run `spadework init` again."
    files: dict[str, bytes | _Link | None] = {}
    for name in (CONFIG_FILE, BRIEF_FILE):
        mode = _mode(target / name)
        if mode is None:
            files[name] = None
        elif stat.S_ISLNK(mode):
            files[name] = LINK
        elif not stat.S_ISREG(mode):
            raise refuse(name, "is not a file", f"Rename or remove it, {again}")
        else:
            files[name] = _read(target / name, shown, name)

    inputs = _mode(target / INPUTS_DIR)
    if inputs is not None and stat.S_ISLNK(inputs):
        raise refuse(
            INPUTS_DIR,
            "is a link",
            f"Make {INPUTS_DIR}/ a real folder: copy the files into it rather than "
            f"linking a folder, {again}",
        )
    if inputs is not None and not stat.S_ISDIR(inputs):
        raise refuse(INPUTS_DIR, "is not a folder", f"Rename or remove it, {again}")

    gitignore = None
    mode = _mode(target / GITIGNORE)
    if mode is not None and stat.S_ISLNK(mode):
        raise refuse(
            GITIGNORE,
            "is a link, and init does not write through links",
            f"Replace it with a copy of the file it points to, {again}",
        )
    if mode is not None and not stat.S_ISREG(mode):
        raise refuse(GITIGNORE, "is not a file", f"Rename or remove it, {again}")
    if mode is not None:
        gitignore = _read(target / GITIGNORE, shown, GITIGNORE)
        if not _ignores_state(gitignore) and not os.access(target / GITIGNORE, os.W_OK):
            raise refuse(
                GITIGNORE,
                "cannot be written to",
                f"Make it writable, or add {IGNORE_LINE} to it yourself, {again}",
            )
    return _Found(files, gitignore, inputs is not None)


def _mode(path: Path) -> int | None:
    """The mode of ``path`` itself, not of what a link points to; None if
    there is nothing there."""
    try:
        return path.lstat().st_mode
    except FileNotFoundError:
        return None


def _read(path: Path, shown: str, name: str) -> bytes:
    """A file's bytes, without following a link to it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as file:
            return file.read()
    except OSError as exc:
        raise UsageError(
            f"cannot read {shown}/{name}: {exc.strerror or exc}",
            "Make it readable, or move it out of the folder, then run "
            "`spadework init` again.",
        ) from exc


def _relative_to_project(playbook_root: Path, project_root: Path) -> str:
    relative = os.path.relpath(playbook_root.resolve(), project_root.resolve())
    relative = relative.replace(os.sep, "/")
    return relative if relative.startswith("..") else f"./{relative}"


def _write(
    root: Path,
    name: str,
    text: str,
    existing: bytes | _Link | None,
    result: InitResult,
) -> None:
    """Write ``name``, which held ``existing``. A file it replaces is kept as
    ``name.bak`` (or ``name.1.bak``, and so on), unless it already held
    ``text``. A link is moved aside in the same way, and never followed."""
    path = root / name
    data = text.encode("utf-8")
    if existing is None:
        result.written.append(name)
    else:
        result.replaced.append(name)
        if existing == data:
            return
        backup = _backup_name(root, name)
        os.replace(path, root / backup)  # renames a link, not what it points to
        result.backups[name] = backup
    _create(path, data)


def _create(path: Path, data: bytes) -> None:
    """Create ``path``, which must not exist, not even as a link."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    with os.fdopen(os.open(path, flags, 0o644), "wb") as file:
        file.write(data)


def _backup_name(root: Path, name: str) -> str:
    backup = f"{name}.bak"
    number = 0
    while _mode(root / backup) is not None:
        number += 1
        backup = f"{name}.{number}.bak"
    return backup


def _ignores_state(data: bytes) -> bool:
    lines = data.decode("utf-8", errors="replace").splitlines()
    return any(line.strip().strip("/") == ".spadework" for line in lines)


def _add_ignore_line(path: Path, existing: bytes | None, result: InitResult) -> None:
    """Create .gitignore, or add the engine state to it. ``_check_target`` has
    refused a link, so this never writes through one."""
    if existing is None:
        _create(path, f"# spadework's engine state\n{IGNORE_LINE}\n".encode())
        result.written.append(GITIGNORE)
        return
    # Bytes, so a .gitignore in any encoding is kept exactly as it was.
    if _ignores_state(existing):
        return
    separator = b"" if not existing or existing.endswith(b"\n") else b"\n"
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW)
    with os.fdopen(fd, "ab") as file:
        file.write(separator + IGNORE_LINE.encode() + b"\n")
    result.written.append(f"{GITIGNORE} (added {IGNORE_LINE})")
