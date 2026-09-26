"""The spadework command-line interface.

Commands stay thin: they read options, call the library, and present the
result. pydantic, jinja2, yaml and rich are imported inside the commands, so
that ``--help`` and ``--version`` stay fast.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import typer

from spadework import __version__
from spadework.console import Ui
from spadework.errors import ExitCode, SpadeworkError, UsageError

DEFAULT_PLAYBOOK = "discovery-website"


def _examples(*commands: str) -> str:
    # "\b" stops Click from re-wrapping the paragraph that follows it.
    return "Examples:\n\n\b\n" + "\n".join(f"  {command}" for command in commands)


app = typer.Typer(
    name="spadework",
    help="Evidence-first research for projects in discovery.",
    epilog=_examples(
        "spadework init acme-fabrication",
        "spadework check -C acme-fabrication",
        "spadework playbooks list",
    ),
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
    pretty_exceptions_enable=False,
)
playbooks_app = typer.Typer(
    name="playbooks",
    help="List or show the built-in and local playbooks.",
    epilog=_examples(
        "spadework playbooks list",
        "spadework playbooks show discovery-website",
    ),
    no_args_is_help=True,
    rich_markup_mode=None,
)
app.add_typer(playbooks_app)

ProjectOption = Annotated[
    Path | None,
    typer.Option(
        "-C",
        "--project",
        metavar="PATH",
        help="Run as if started in PATH, like git -C.",
        exists=True,
        file_okay=False,
        show_default=False,
    ),
]
QuietOption = Annotated[
    bool, typer.Option("-q", "--quiet", help="Print only results, warnings and errors.")
]
VerboseOption = Annotated[
    bool, typer.Option("-v", "--verbose", help="Print more, including tracebacks.")
]
NoColorOption = Annotated[
    bool,
    typer.Option("--no-color", help="Disable colour. NO_COLOR is honoured too."),
]

_OPTIONS = "spadework.options"


@dataclass(frozen=True)
class GlobalOptions:
    project: Path | None = None
    quiet: bool = False
    verbose: bool = False
    no_color: bool = False

    def ui(self) -> Ui:
        return Ui(quiet=self.quiet, verbose=self.verbose, color=not self.no_color)

    @property
    def base(self) -> Path:
        return self.project if self.project is not None else Path.cwd()


def _options(
    ctx: typer.Context,
    project: Path | None,
    quiet: bool,
    verbose: bool,
    no_color: bool,
) -> GlobalOptions:
    """Merge the global options given before and after the command name."""
    # Typer keeps its own settings in ctx.obj, so ours go in ctx.meta, which
    # every nested context shares.
    before: GlobalOptions = ctx.meta.get(_OPTIONS, GlobalOptions())
    options = GlobalOptions(
        project=project if project is not None else before.project,
        quiet=quiet or before.quiet,
        verbose=verbose or before.verbose,
        no_color=no_color or before.no_color,
    )
    if options.quiet and options.verbose:
        Ui(color=not options.no_color).error(
            UsageError("-q and -v cannot be used together", "Choose one of them.")
        )
        raise typer.Exit(ExitCode.USAGE)
    return options


@contextmanager
def _reporting(ui: Ui, *, json_output: bool = False) -> Iterator[None]:
    """Report errors as a message and a hint, with the right exit code."""
    try:
        yield
    except typer.Exit:
        raise
    except SpadeworkError as exc:
        if json_output:
            from spadework.views import error_document

            ui.json(error_document(exc))
        ui.error(exc)
        raise typer.Exit(exc.exit_code) from None
    except KeyboardInterrupt:
        ui.err.print("interrupted")
        raise typer.Exit(ExitCode.INTERRUPTED) from None
    except OSError as exc:
        # The file system said no: not a bug, so say which path and why.
        if ui.debug:
            raise
        where = _shown(Path(exc.filename)) if exc.filename else "a file"
        ui.error(
            SpadeworkError(
                f"cannot use {where}: {exc.strerror or exc}",
                "Check that the path exists and that you may read and write it.",
            )
        )
        raise typer.Exit(ExitCode.ERROR) from None
    except Exception as exc:
        if ui.debug:
            raise
        ui.error(
            SpadeworkError(
                f"unexpected error: {type(exc).__name__}: {exc}",
                "Run again with -v to see the traceback. Please report it as a bug.",
            )
        )
        raise typer.Exit(ExitCode.ERROR) from None


def _shown(path: Path) -> str:
    """A path as the user would type it: relative when it is below them."""
    here, target = Path.cwd().resolve(), path.resolve()
    if here != Path(here.anchor) and target.is_relative_to(here):
        return os.path.relpath(target, here)
    return target.as_posix()


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"spadework {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Evidence-first research for projects in discovery."""
    ctx.meta[_OPTIONS] = GlobalOptions(project, quiet, verbose, no_color)


@app.command(
    epilog=_examples(
        "spadework init acme-fabrication",
        "spadework init acme-fabrication --playbook quickstart",
        "spadework init acme-fabrication --playbook ./playbooks/mine",
    )
)
def init(
    ctx: typer.Context,
    directory: Annotated[
        Path | None,
        typer.Argument(
            metavar="[DIR]",
            help="Folder to create the project in. Default: the current folder.",
            show_default=False,
        ),
    ] = None,
    playbook: Annotated[
        str,
        typer.Option(
            "--playbook",
            metavar="NAME",
            help="A built-in playbook, or the path to a playbook folder.",
        ),
    ] = DEFAULT_PLAYBOOK,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Allow a folder that is not empty. Replaces its spadework.toml and "
                "brief.md."
            ),
        ),
    ] = False,
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Create a project from a playbook.

    Writes spadework.toml, brief.md (copied from the playbook's brief
    template), an empty inputs/ folder, and a .gitignore for engine state.
    DIR must be empty or new, unless you pass --force.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui):
        from spadework.scaffold import init_project

        base = options.base
        target = base / directory if directory is not None else base
        shown = _shown(target)
        result = init_project(target, playbook, force=force, cwd=base, shown=shown)

        if result.replaced:
            ui.warning(f"replaced the existing {' and '.join(result.replaced)}")
        if not options.quiet:
            ui.result(f"Created a {result.playbook.name} project in {shown}:")
            for name in [*result.written, *result.replaced]:
                ui.result(f"  {name}")
        check = (
            "spadework check"
            if shown == "."
            else f"spadework check -C {shlex.quote(shown)}"
        )
        brief = "brief.md" if shown == "." else f"{shown}/brief.md"
        ui.info(f"Next: edit {brief}, then run `{check}`.")


@app.command(
    epilog=_examples(
        "spadework check",
        "spadework check -C acme-fabrication --show-public",
        "spadework check --json | jq '.tasks[] | {id, estimate_usd}'",
    )
)
def check(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json", help="Print one JSON document instead of the plan table."
        ),
    ] = False,
    show_public: Annotated[
        bool,
        typer.Option(
            "--show-public",
            help="Print exactly what web-facing research sessions will see.",
        ),
    ] = False,
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Validate the project and print the plan.

    This is the dry run: it loads everything a run would use, renders every
    template, and estimates the cost. It sends nothing to a model and spends
    nothing. Exits 3 when the project, brief or playbook is not valid.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui, json_output=json_output):
        from spadework import views
        from spadework.plan import SUMMARY_RESERVE_USD, build_plan
        from spadework.project import load_project
        from spadework.render import public_view

        loaded = load_project(options.base)
        plan = build_plan(loaded)
        if options.verbose:
            ui.info(f"playbook: {loaded.playbook.ref.display_root}")
            ui.info(f"inputs: {len(loaded.inputs)} files")

        warnings = [(warning.message, warning.hint) for warning in loaded.warnings]
        cap = views.usd(loaded.config.limits.budget_usd)
        reserve = views.usd(SUMMARY_RESERVE_USD)
        for task in plan.never_start():
            warnings.append(
                (
                    f"task {task.id!r} can never start: its estimate "
                    f"({views.usd(task.estimate_usd)}) plus the summary's reserve "
                    f"({reserve}) is over the run budget ({cap})",
                    "Raise limits.budget_usd in spadework.toml, or lower the task's "
                    "budget_usd in the playbook.",
                )
            )
        for message, hint in warnings:
            ui.warning(message, hint)

        public = public_view(loaded.brief, loaded.playbook) if show_public else None
        if json_output:
            documents = [
                {"message": message, "hint": hint} for message, hint in warnings
            ]
            ui.json(views.check_document(loaded, plan, documents, public))
        elif public is not None:
            views.print_public(ui, public)
        else:
            views.print_plan(ui, plan)
            if loaded.playbook.research_tasks:
                ui.info(
                    "See what research sessions will see: spadework check --show-public"
                )


@playbooks_app.command(
    "list",
    epilog=_examples(
        "spadework playbooks list", "spadework -C acme-fabrication playbooks list"
    ),
)
def list_playbooks(
    ctx: typer.Context,
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """List the built-in playbooks and the project's own.

    The project's playbook is listed too when it is a folder rather than a
    built-in name.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui):
        from spadework.config import CONFIG_FILE, load_config
        from spadework.playbook import (
            Playbook,
            builtin_names,
            load_playbook,
            resolve_playbook,
        )
        from spadework.views import print_playbook_list

        playbooks: list[Playbook] = []
        specs = builtin_names()
        config_path = options.base / CONFIG_FILE
        if config_path.is_file():
            try:
                spec = load_config(config_path).project.playbook
            except SpadeworkError as exc:
                ui.warning(f"cannot read {CONFIG_FILE}: {exc.lines()[0]}")
            else:
                if spec not in specs:
                    specs.append(spec)
        for spec in specs:
            try:
                playbooks.append(load_playbook(resolve_playbook(spec, options.base)))
            except SpadeworkError as exc:
                ui.warning(
                    f"playbook {spec!r} is not valid: {exc.lines()[0]}", exc.hint
                )
        if not playbooks:
            ui.info("No playbooks found.")
            return
        print_playbook_list(ui, playbooks)
        ui.info("Show one with: spadework playbooks show NAME")


@playbooks_app.command(
    "show",
    epilog=_examples(
        "spadework playbooks show discovery-website",
        "spadework playbooks show ./playbooks/mine",
    ),
)
def show_playbook(
    ctx: typer.Context,
    name: Annotated[
        str,
        typer.Argument(
            metavar="NAME",
            help="A built-in playbook, or the path to a playbook folder.",
            show_default=False,
        ),
    ],
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Show a playbook: what it needs from the brief, and its tasks in order."""
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui):
        from spadework.errors import InvalidError
        from spadework.playbook import load_playbook, resolve_playbook
        from spadework.views import print_playbook

        try:
            ref = resolve_playbook(name, options.base)
        except InvalidError as exc:
            exc.exit_code = ExitCode.USAGE
            raise
        print_playbook(ui, load_playbook(ref))
