"""A project folder: its config, brief, playbook and inputs, loaded together."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from spadework.brief import BRIEF_FILE, Brief, load_brief, unused_key_warnings
from spadework.config import CONFIG_FILE, ProjectConfig, load_config
from spadework.errors import InvalidError, Problem
from spadework.inputs import (
    INPUTS_DIR,
    SOFT_LIMIT_BYTES,
    InputFile,
    ignored_inputs,
    ignored_inputs_warning,
    scan_inputs,
    total_size,
)
from spadework.playbook import Playbook, contains, load_playbook, resolve_playbook
from spadework.render import check_templates
from spadework.textfiles import toml_key_lines


@dataclass(frozen=True)
class CheckWarning:
    message: str
    hint: str
    path: str | None = None
    """The file it is about, relative to the project, when it is about one."""
    line: int | None = None


@dataclass(frozen=True)
class Project:
    root: Path
    config: ProjectConfig
    playbook: Playbook
    brief: Brief
    inputs: tuple[InputFile, ...]
    warnings: tuple[CheckWarning, ...]
    ignored_inputs: tuple[str, ...] = ()
    """Files in inputs/ that are not .md or .txt, as inputs/<name>."""

    def ignored_inputs_warning(self) -> CheckWarning | None:
        warning = ignored_inputs_warning(self.ignored_inputs)
        return None if warning is None else CheckWarning(*warning)


def find_project(start: Path) -> Path | None:
    """The nearest folder with a spadework.toml: ``start``, or the first
    folder above it that has one, as git finds a repository."""
    folder = start.absolute()
    for candidate in (folder, *folder.parents):
        if (candidate / CONFIG_FILE).is_file():
            return candidate
    return None


def load_project(root: Path) -> Project:
    """Load and validate everything a run would use. This is ``check``'s work.

    Raises InvalidError on the first file that is not valid, with every
    problem found in it.
    """
    config_path = root / CONFIG_FILE
    if not config_path.is_file():
        raise InvalidError(
            f"there is no {CONFIG_FILE} in {root}",
            "Run `spadework init` to create a project, or pass -C PROJECT_FOLDER.",
        )
    config = load_config(config_path)
    try:
        ref = resolve_playbook(config.project.playbook, root)
    except InvalidError as exc:
        raise InvalidError(
            exc.message,
            exc.hint,
            problems=[Problem(exc.message, CONFIG_FILE, _playbook_line(config_path))],
        ) from exc
    if contains(ref, root):
        raise InvalidError(
            "the playbook folder contains the project",
            "Keep the playbook in a folder of its own, such as ./playbooks/mine.",
            problems=[
                Problem(
                    f"playbook {config.project.playbook!r} contains the project folder",
                    CONFIG_FILE,
                    _playbook_line(config_path),
                )
            ],
        )
    playbook = load_playbook(ref)
    _check_brief_link(root)
    brief = load_brief(root / BRIEF_FILE, playbook)
    inputs = tuple(scan_inputs(root / INPUTS_DIR, root))
    check_templates(config.project.name, brief, playbook, inputs)
    ignored = tuple(ignored_inputs(root / INPUTS_DIR))
    warnings = _warnings(brief, playbook, inputs, ignored)
    return Project(root, config, playbook, brief, inputs, tuple(warnings), ignored)


def _check_brief_link(root: Path) -> None:
    """A brief.md that is a link must stay inside the project.

    Sessions are given copies of project files only, so a brief kept
    elsewhere could not be staged.
    """
    path = root / BRIEF_FILE
    if not path.is_symlink():
        return
    try:
        target = path.resolve(strict=True)
    except (OSError, RuntimeError):  # RuntimeError: a link loop before 3.13
        return  # load_brief reports the missing file
    if not target.is_relative_to(root.resolve()):
        raise InvalidError(
            f"{BRIEF_FILE} is a link to a file outside the project",
            f"Copy the brief into the project folder as {BRIEF_FILE} rather than "
            "linking to it.",
            problems=[Problem("is a link to a file outside the project", BRIEF_FILE)],
        )


def _playbook_line(config_path: Path) -> int | None:
    return toml_key_lines(config_path.read_text(encoding="utf-8")).get(
        ("project", "playbook")
    )


def _warnings(
    brief: Brief,
    playbook: Playbook,
    inputs: tuple[InputFile, ...],
    ignored: tuple[str, ...],
) -> list[CheckWarning]:
    warnings = [
        CheckWarning(problem.message, hint, problem.path, problem.line)
        for problem, hint in unused_key_warnings(brief, playbook)
    ]
    ignored_warning = ignored_inputs_warning(ignored)
    if ignored_warning is not None:
        warnings.append(CheckWarning(*ignored_warning))
    size = total_size(inputs)
    if size > SOFT_LIMIT_BYTES:
        warnings.append(
            CheckWarning(
                f"{INPUTS_DIR}/ holds {math.ceil(size / 1000)} KB, over the "
                f"{SOFT_LIMIT_BYTES // 1000} KB soft limit",
                "Every draft session reads its inputs, so large inputs cost more. "
                "Trim or summarise them, or narrow the tasks' inputs globs.",
            )
        )
    if playbook.research_tasks and not (brief.public_context or "").strip():
        warnings.append(
            CheckWarning(
                "public_context in brief.md is empty, so research sessions will "
                "have nothing to go on",
                "Describe the project in public_context, in words you would post "
                "publicly. Preview it with `spadework check --show-public`.",
            )
        )
    return warnings
