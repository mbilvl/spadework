"""The spadework command-line interface.

Commands stay thin: they read options, call the library, and present the
result. pydantic, jinja2, yaml and rich are imported inside the commands, so
that ``--help`` and ``--version`` stay fast.
"""

from __future__ import annotations

import posixpath
import shlex
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer
from typer.core import TyperCommand

from spadework import __version__
from spadework.console import Ui, shown
from spadework.errors import ExitCode, InvalidError, SpadeworkError, UsageError

if TYPE_CHECKING:
    from spadework.clean import CleanPlan

DEFAULT_PLAYBOOK = "discovery-website"
CONFIG_FILE = "spadework.toml"
"""spadework.config.CONFIG_FILE, which this module cannot import cheaply."""

# Lets a command see a stray argument, to suggest -C for it.
_EXTRA_ARGS: dict[str, Any] = {"allow_extra_args": True}


def _examples(*commands: str, note: str | None = None) -> str:
    # "\b" stops Click from re-wrapping the paragraph that follows it.
    text = "Examples:\n\n\b\n" + "\n".join(f"  {command}" for command in commands)
    return text if note is None else f"{note}\n\n{text}"


class _PlainUsage(TyperCommand):
    """A command whose usage line shows a required argument as NAME, not
    Typer's {NAME}."""

    def collect_usage_pieces(self, ctx: Any) -> list[str]:
        return [
            piece[1:-1] if piece.startswith("{") and piece.endswith("}") else piece
            for piece in super().collect_usage_pieces(ctx)
        ]


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

    def ui(self, *, for_project: bool = True) -> Ui:
        """Where the command writes. When ``-C`` names a project other than
        the current folder, the commands that messages suggest carry it too."""
        flag = None
        if for_project and self.project is not None:
            flag = shown(self.project)
        return Ui(
            quiet=self.quiet,
            verbose=self.verbose,
            color=not self.no_color,
            project=None if flag == "." else flag,
        )

    @property
    def base(self) -> Path:
        """The folder given with -C, or the current folder."""
        return self.project if self.project is not None else Path.cwd()

    def find_root(self) -> Path | None:
        """The project: the folder given with -C, or else the nearest folder
        with a spadework.toml, here or above. None if there is none."""
        from spadework.project import find_project

        if self.project is not None:
            return self.project
        return find_project(Path.cwd())

    def root(self) -> Path:
        """``find_root``, which must be a project."""
        root = self.find_root()
        if root is None:
            raise InvalidError(
                f"there is no {CONFIG_FILE} in this folder or any folder above it",
                "Run `spadework init` to create a project, or pass -C PROJECT_FOLDER.",
            )
        if not (root / CONFIG_FILE).is_file():
            raise InvalidError(
                f"there is no {CONFIG_FILE} in {root}",
                "Run `spadework init` to create a project, or pass -C PROJECT_FOLDER.",
            )
        return root


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
def _reporting(
    ui: Ui,
    *,
    json_output: bool = False,
    paths: Callable[[], Path | None] | None = None,
) -> Iterator[None]:
    """Report errors as a message and a hint, with the right exit code.

    ``paths`` gives the folder that the paths in errors are relative to, so
    that they can be shown relative to the current folder instead.
    """
    try:
        yield
    except typer.Exit:
        raise
    except SpadeworkError as exc:
        _fail(ui, exc, json_output=json_output, paths=paths)
    except KeyboardInterrupt:
        ui.err.print("interrupted")
        raise typer.Exit(ExitCode.INTERRUPTED) from None
    except Exception as exc:
        # A file system refusal says which path and why; anything else is a
        # bug. Either way, --json still gets its error document.
        if ui.debug:
            raise
        _fail(ui, _run_error(exc), json_output=json_output)


def _fail(
    ui: Ui,
    exc: SpadeworkError,
    *,
    json_output: bool,
    paths: Callable[[], Path | None] | None = None,
) -> NoReturn:
    """Report ``exc`` and exit. Paths in JSON stay relative to the project, as
    every path in spadework's JSON is; on stderr they are shown relative to
    the current folder."""
    if json_output:
        from spadework.views import error_document

        ui.json(error_document(exc))
    _relocate(exc, paths)
    ui.error(exc)
    raise typer.Exit(exc.exit_code) from None


def _relocate(exc: SpadeworkError, paths: Callable[[], Path | None] | None) -> None:
    """Make the paths of ``exc``'s problems relative to the current folder."""
    if paths is None or not exc.problems:
        return
    try:
        base = paths()
    except OSError:
        return
    if base is None:
        return
    for problem in exc.problems:
        # The message often starts with the file too: "brief.md is not valid".
        path = problem.path
        if path is not None and exc.message.startswith(f"{path} "):
            exc.message = f"{_from_here(base, path)}{exc.message[len(path) :]}"
            break
    exc.problems = tuple(
        replace(problem, path=_from_here(base, problem.path))
        for problem in exc.problems
    )


def _from_here(base: Path, path: str | None) -> str | None:
    """``path``, relative to ``base``, as seen from the current folder."""
    if path is None or path.startswith(("/", "[")):
        return path
    prefix = shown(base)
    return path if prefix == "." else posixpath.normpath(f"{prefix}/{path}")


def _here(root: Path, path: str) -> str:
    """A path in the project, as seen from the current folder."""
    return _from_here(root, path) or path


def _located(root: Path | None, warning: Any) -> str:
    """A project warning's message, after the file and line it is about: as
    seen from the current folder, or with no ``root``, from the project."""
    from spadework.errors import Problem

    path = warning.path if root is None else _from_here(root, warning.path)
    return str(Problem(warning.message, path, warning.line))


def _ignored_inputs(ui: Ui, root: Path) -> None:
    """Warn about the files in inputs/ that are not read, for the commands
    that do not print every project warning as check does."""
    from spadework.inputs import INPUTS_DIR, ignored_inputs, ignored_inputs_warning

    warning = ignored_inputs_warning(ignored_inputs(root / INPUTS_DIR))
    if warning is not None:
        ui.warning(*warning)


def _no_arguments(ctx: typer.Context, command: str) -> None:
    """A command that takes no arguments was given one: maybe a project."""
    if not ctx.args:
        return
    argument = ctx.args[0]
    hint = f"See `spadework {command} --help`."
    if Path(argument).is_dir():
        hint = f"Did you mean `spadework {command} -C {shlex.quote(argument)}`?"
    raise UsageError(f"{command} takes no arguments, but was given {argument!r}", hint)


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
                "brief.md, and keeps the old ones as .bak files."
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
    # -C is where DIR is, not a project, and init never looks for one.
    ui = options.ui(for_project=False)
    with _reporting(ui, paths=lambda: options.base):
        from spadework.scaffold import init_project

        base = options.base
        target = base / directory if directory is not None else base
        where = shown(target)
        result = init_project(target, playbook, force=force, cwd=base, shown=where)

        if result.replaced:
            message = f"replaced the existing {' and '.join(result.replaced)}"
            backups = list(result.backups.values())
            if backups:
                kept = "one is" if len(backups) == 1 else "ones are"
                message += f"; the old {kept} kept as {' and '.join(backups)}"
            ui.warning(message)
        if not options.quiet:
            ui.result(f"Created a {result.playbook.name} project in {where}:")
            for name in [*result.written, *result.replaced]:
                ui.result(f"  {name}")
        check = (
            "spadework check"
            if where == "."
            else f"spadework check -C {shlex.quote(where)}"
        )
        brief = "brief.md" if where == "." else f"{where}/brief.md"
        ui.info(f"Next: edit {brief}, then run `{check}`.")


@app.command(
    context_settings=_EXTRA_ARGS,
    epilog=_examples(
        "spadework check",
        "spadework check -C acme-fabrication --show-public",
        "spadework check --json | jq '.tasks[] | {id, estimate_usd}'",
    ),
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
    template, works out which tasks are stale and why, and estimates the
    cost. It sends nothing to a model and spends nothing. Exits 3 when the
    project, brief or playbook is not valid.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui, json_output=json_output, paths=options.find_root):
        _no_arguments(ctx, "check")
        from spadework import views
        from spadework.engine.planning import plan_project
        from spadework.project import load_project
        from spadework.render import public_view

        root = options.root()
        loaded = load_project(root)
        plan = plan_project(loaded).plan
        if options.verbose:
            ui.info(f"playbook: {loaded.playbook.ref.display_root}")
            ui.info(f"inputs: {len(loaded.inputs)} files")

        warnings: list[tuple[str, str | None, str]] = [
            (
                _located(None, warning),
                warning.hint,
                _located(root, warning),
            )
            for warning in loaded.warnings
        ]
        warnings += [
            (message, hint, message) for message, hint in views.plan_warnings(plan)
        ]
        for _, hint, shown_message in warnings:
            ui.warning(shown_message, hint)

        public = public_view(loaded.brief, loaded.playbook) if show_public else None
        if json_output:
            documents = [
                {"message": message, "hint": hint} for message, hint, _ in warnings
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


@app.command(
    "run",
    context_settings=_EXTRA_ARGS,
    epilog=_examples(
        "spadework run",
        "spadework run --only stack --force",
        "spadework run --budget-usd 3 --max-time 90m --no-review",
        "spadework run --json | jq -c 'select(.event == \"task_finished\")'",
        note=(
            "To try a run without spending anything, set SPADEWORK_EXECUTOR=fake: "
            "no model is called, and placeholder documents are written."
        ),
    ),
)
def run_command(
    ctx: typer.Context,
    only: Annotated[
        list[str] | None,
        typer.Option(
            "--only",
            metavar="TASK",
            help=(
                "Run only this task; repeat for more. Dependencies are not rerun: "
                "their existing outputs are used."
            ),
            show_default=False,
        ),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                "Redraft the selected tasks even if they are fresh. With --only, "
                "also replaces your edits to their files in out/."
            ),
        ),
    ] = False,
    refresh_research: Annotated[
        bool,
        typer.Option(
            "--refresh-research",
            help="Redo the research of the tasks that run, even if it is current.",
        ),
    ] = False,
    no_review: Annotated[
        bool, typer.Option("--no-review", help="Skip the review rounds.")
    ] = False,
    rounds: Annotated[
        int | None,
        typer.Option(
            "--rounds",
            metavar="N",
            min=0,
            help="Most review rounds. Default: limits.rounds.",
            show_default=False,
        ),
    ] = None,
    budget_usd: Annotated[
        float | None,
        typer.Option(
            "--budget-usd",
            metavar="X",
            help="Spending cap for this run, in dollars. Default: limits.budget_usd.",
            show_default=False,
        ),
    ] = None,
    max_time: Annotated[
        str | None,
        typer.Option(
            "--max-time",
            metavar="D",
            help="Wall-clock cap, such as 90m or 2h. Default: limits.max_time.",
            show_default=False,
        ),
    ] = None,
    wait_on_rate_limit: Annotated[
        bool,
        typer.Option(
            "--wait-on-rate-limit",
            help="Wait for a usage limit to reset instead of stopping, within "
            "--max-time.",
        ),
    ] = False,
    no_verify: Annotated[
        bool, typer.Option("--no-verify", help="Skip source verification.")
    ] = False,
    draft_model: Annotated[
        str | None,
        typer.Option(
            "--draft-model",
            metavar="M",
            help="Model for the draft tier. Default: models.draft.",
            show_default=False,
        ),
    ] = None,
    review_model: Annotated[
        str | None,
        typer.Option(
            "--review-model",
            metavar="M",
            help="Model for the review tier. Default: models.review.",
            show_default=False,
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print NDJSON events instead of the live view."),
    ] = False,
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Run the pipeline. This spends money.

    Prints the plan, then runs every task that is pending, stale or failed:
    research, source verification, draft, validators and repair. Then the
    critic reviews the pack and fixes follow, for up to --rounds rounds, the
    summary is written, and the pack is rendered into out/.
    It does not ask first: the budget is the safety net, and `spadework
    check` is the preview. Prints `up to date` when there is nothing to do.
    Exits 4 when tasks failed or are invalid, 5 at the budget or time limit,
    6 at a rate limit, 7 when another run holds the project, and 130 when
    interrupted. A run that stops can be resumed by running again.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui, paths=options.find_root):
        _no_arguments(ctx, "run")
        from spadework.engine.display import LinesDisplay, LiveDisplay, NdjsonDisplay
        from spadework.engine.run import RunOptions, run_project
        from spadework.views import error_document

        settings = RunOptions(
            only=tuple(dict.fromkeys(only or ())),
            force=force,
            refresh_research=refresh_research,
            review=not no_review,
            rounds=rounds,
            budget_usd=budget_usd,
            max_time=max_time,
            wait_on_rate_limit=wait_on_rate_limit,
            verify=not no_verify,
            draft_model=draft_model,
            review_model=review_model,
        )
        display: NdjsonDisplay | LinesDisplay | LiveDisplay
        if json_output:
            display = NdjsonDisplay()
        elif ui.err.is_terminal:
            display = LiveDisplay(ui)
        else:
            display = LinesDisplay(ui)
        try:
            root = options.root()
            _ignored_inputs(ui, root)
            result = run_project(root, settings, reporter=display)
        except BaseException as exc:
            # The run did not start. With --json this is the last event; the
            # human-readable error follows on stderr, from _reporting.
            if isinstance(display, NdjsonDisplay):
                display.error(error_document(_run_error(exc)))
            raise
        finally:
            display.close()
        if result.reporter_error is not None:
            ui.warning(
                f"progress output stopped: {type(result.reporter_error).__name__}: "
                f"{result.reporter_error}",
                "The run went on; `spadework status` shows how it ended.",
            )
        if result.exception is not None and ui.debug:
            import traceback

            ui.err.print(
                "".join(traceback.format_exception(result.exception)), soft_wrap=True
            )
    raise typer.Exit(result.exit_code)


def _run_error(exc: BaseException) -> SpadeworkError:
    """What _reporting will say about ``exc``, as an error to report in JSON."""
    if isinstance(exc, SpadeworkError):
        return exc
    if isinstance(exc, KeyboardInterrupt):
        error = SpadeworkError("interrupted", "Nothing was run.")
        error.exit_code = ExitCode.INTERRUPTED
        return error
    if isinstance(exc, OSError):
        where = shown(Path(exc.filename)) if exc.filename else "a file"
        return SpadeworkError(
            f"cannot use {where}: {exc.strerror or exc}",
            "Check that the path exists and that you may read and write it.",
        )
    return SpadeworkError(
        f"unexpected error: {type(exc).__name__}: {exc}",
        "Run again with -v to see the traceback. Please report it as a bug.",
    )


@app.command(
    context_settings=_EXTRA_ARGS,
    epilog=_examples(
        "spadework status",
        "spadework status -C acme-fabrication --json | jq .last_run",
    ),
)
def status(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON document instead of the table."),
    ] = False,
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Show task states and the last run.

    Shows each task's state and why, worked out as `spadework check` works it
    out; the last run's outcome, spend, duration and sessions; and whether a
    run is active. It sends nothing to a model and spends nothing.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui, json_output=json_output, paths=options.find_root):
        _no_arguments(ctx, "status")
        from spadework import views
        from spadework.engine.status import project_status
        from spadework.project import load_project

        loaded = load_project(options.root())
        current = project_status(loaded)
        ignored = loaded.ignored_inputs_warning()
        if ignored is not None:
            ui.warning(ignored.message, ignored.hint)
        for warning in current.warnings:
            ui.warning(warning)
        if json_output:
            ui.json(views.status_document(loaded, current))
        else:
            views.print_status(ui, current)


@app.command(
    context_settings=_EXTRA_ARGS,
    epilog=_examples(
        "spadework verify",
        "spadework verify -C acme-fabrication --json | jq .counts",
    ),
)
def verify(
    ctx: typer.Context,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON document instead of the table."),
    ] = False,
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Check the quote of every source in the pack again.

    Fetches each source's page and checks that its quote is on it. It sends
    nothing to a model and spends nothing. Then it updates the research
    records and renders out/sources.md, out/pack.json, out/00-summary.md and
    each document's Sources section again; documents you edited are never
    touched. Exits 0 whatever the checks find, 3 when the project is not
    valid, and 7 when a run holds the project.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui, json_output=json_output, paths=options.find_root):
        _no_arguments(ctx, "verify")
        from spadework import views
        from spadework.engine.reverify import verify_pack

        root = options.root()
        _ignored_inputs(ui, root)
        report = verify_pack(root, progress=None if json_output else ui.info)
        if json_output:
            for message, hint in report.warnings:
                ui.warning(message, hint)
            ui.json(views.verify_document(report))
        else:
            views.print_verify(ui, report)


@playbooks_app.callback()
def playbooks(
    ctx: typer.Context,
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """List or show the built-in and local playbooks."""
    ctx.meta[_OPTIONS] = _options(ctx, project, quiet, verbose, no_color)


@playbooks_app.command(
    "list",
    context_settings=_EXTRA_ARGS,
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
        _no_arguments(ctx, "playbooks list")
        from spadework.config import load_config
        from spadework.playbook import (
            Playbook,
            builtin_names,
            load_playbook,
            resolve_playbook,
        )
        from spadework.views import print_playbook_list

        playbooks: list[Playbook] = []
        specs = builtin_names()
        root = options.find_root() or options.base
        config_path = root / CONFIG_FILE
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
                playbooks.append(load_playbook(resolve_playbook(spec, root)))
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
    cls=_PlainUsage,
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
    """Show a playbook: what it needs from the brief, and its tasks in order.

    It also shows the playbook's folder, so that you can copy a built-in
    one to start your own.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui, paths=lambda: options.base):
        from spadework.playbook import load_playbook, resolve_playbook
        from spadework.views import print_playbook

        try:
            ref = resolve_playbook(name, options.base)
        except InvalidError as exc:
            exc.exit_code = ExitCode.USAGE
            raise
        print_playbook(ui, load_playbook(ref))


@app.command(
    epilog=_examples(
        "spadework logs",
        "spadework logs references",
        "spadework logs --follow",
        "spadework logs --list",
        "spadework logs --run 20260927T210411Z-3f9a --raw | jq -c .type",
    )
)
def logs(
    ctx: typer.Context,
    task: Annotated[
        str | None,
        typer.Argument(
            metavar="[TASK]",
            help="Show only this task's sessions, or 'critic' or 'summary'.",
            show_default=False,
        ),
    ] = None,
    run_id: Annotated[
        str | None,
        typer.Option(
            "--run",
            metavar="ID",
            help="Show this run instead of the last one.",
            show_default=False,
        ),
    ] = None,
    follow: Annotated[
        bool,
        typer.Option("--follow", help="Keep printing as the run goes, until it ends."),
    ] = False,
    raw: Annotated[
        bool, typer.Option("--raw", help="Print the raw JSONL event logs.")
    ] = False,
    list_runs: Annotated[
        bool,
        typer.Option("--list", help="List the runs, newest first, and their outcomes."),
    ] = False,
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Show session traces.

    By default it shows every session of the last run that has any: one
    line per tool call with its target, the calls the sandbox denied,
    snippets of the model's text, and how each session ended. With TASK, it
    shows that task's sessions from the last run that has some. --follow
    keeps printing while a run goes on, and stops when it ends.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui):
        from spadework import logs as session_logs

        root = options.root()
        if list_runs:
            if task is not None or run_id is not None or follow or raw:
                raise UsageError(
                    "--list cannot be used with TASK, --run, --follow or --raw",
                    "List the runs with `spadework logs --list`, then show one "
                    "with --run ID.",
                )
            session_logs.list_runs(ui, root)
            return
        session_logs.show_logs(
            ui, root, task=task, run_id=run_id, follow=follow, raw=raw
        )


def _can_ask() -> bool:
    """Whether a question can be put to the user and answered."""
    return sys.stdin.isatty() and sys.stderr.isatty()


@app.command(
    context_settings=_EXTRA_ARGS,
    epilog=_examples(
        "spadework clean --runs",
        "spadework clean --cache -C acme-fabrication",
        "spadework clean --all --yes",
    ),
)
def clean(
    ctx: typer.Context,
    runs: Annotated[
        bool,
        typer.Option("--runs", help="Remove every run's manifest and session logs."),
    ] = False,
    cache: Annotated[
        bool,
        typer.Option(
            "--cache",
            help="Remove the research records, so that the next run researches again.",
        ),
    ] = False,
    everything: Annotated[
        bool, typer.Option("--all", help="Remove all of .spadework/.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Do not ask first. Needed without a terminal.")
    ] = False,
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Remove engine state from .spadework/.

    Give one of --runs, --cache and --all. It never touches spadework.toml,
    brief.md, inputs/ or out/. It says what it will remove and asks first;
    without a terminal to ask in, it needs --yes. Exits 7 when a run holds
    the project.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui):
        _no_arguments(ctx, "clean")
        from spadework import clean as cleaning
        from spadework.lock import LOCK_FILE, ProjectLock

        chosen = [
            name
            for name, given in (("runs", runs), ("cache", cache), ("all", everything))
            if given
        ]
        if len(chosen) != 1:
            flags = " and ".join(f"--{name}" for name in chosen)
            raise UsageError(
                "choose what to remove: --runs, --cache or --all"
                if not chosen
                else f"choose one of {flags}, not both"
                if len(chosen) == 2
                else "choose one of --runs, --cache and --all, not all three",
                "--runs removes the run logs, --cache the research records, and "
                "--all every file in .spadework/.",
            )
        what: cleaning.What = "all" if everything else "runs" if runs else "cache"
        root = options.root()
        state = root / cleaning.STATE_DIR
        if not (state.exists() or state.is_symlink()):
            ui.info(
                f"Nothing to remove: there is no {_here(root, cleaning.STATE_DIR)}/."
            )
            return
        with ProjectLock(state / LOCK_FILE) as lock:
            if lock.warning:
                ui.warning(lock.warning)
            plan = cleaning.plan_clean(root, what)
            if plan.empty:
                nothing = {
                    "runs": "there are no runs",
                    "cache": "there are no research records",
                    "all": f"there is no {_here(root, cleaning.STATE_DIR)}/",
                }[what]
                ui.info(f"Nothing to remove: {nothing}.")
                return
            description = _clean_description(plan)
            for target in plan.targets if what == "cache" else ():
                ui.info(f"  {_here(root, target.relative)}")
            if what == "all" and _has_files(root / "out"):
                ui.warning(
                    f"the next run will have no record of the files in "
                    f"{_here(root, 'out')}/, so it will leave them as they are",
                    "Delete out/ as well if you want the next run to write the pack "
                    "afresh.",
                )
            if not yes:
                if not _can_ask():
                    raise UsageError(
                        f"not removing {description} without --yes",
                        "Pass --yes to remove it, or run clean in a terminal to be "
                        "asked first.",
                    )
                if not typer.confirm(f"Remove {description}?", err=True):
                    ui.info("Nothing was removed.")
                    return
            cleaning.clean(plan, lock)
        ui.result(f"removed {description}")
        if what == "cache":
            ui.info("The next run researches these tasks again.")


def _clean_description(plan: CleanPlan) -> str:
    """What ``clean`` removes, in a few words with its size."""
    from spadework.clean import STATE_DIR, human_size

    size = human_size(plan.size)
    if plan.what == "cache":
        records = _here(plan.root, f"{STATE_DIR}/records")
        return f"{plan.detail} from {records}/ ({size})"
    if plan.what == "runs":
        runs = _here(plan.root, f"{STATE_DIR}/runs")
        return f"{runs}/ ({plan.detail}, {size})"
    state = _here(plan.root, STATE_DIR)
    return f"{state}/ (all engine state, with {plan.detail}; {size})"


def _has_files(folder: Path) -> bool:
    try:
        return folder.is_dir() and any(folder.iterdir())
    except OSError:
        return False


@app.command(
    context_settings=_EXTRA_ARGS,
    epilog=_examples(
        "spadework doctor",
        "spadework doctor -C acme-fabrication",
        "spadework doctor --canary",
    ),
)
def doctor(
    ctx: typer.Context,
    canary: Annotated[
        bool,
        typer.Option(
            "--canary",
            help="Also run a tiny live sandbox test. It costs a few cents.",
        ),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print one JSON document instead of the list."),
    ] = False,
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Check the environment spadework runs in.

    Checks Python, the executor and Claude Code (its version, restricted
    mode and login), and whether the project passes `spadework check`.
    --canary also runs one small session that tries to read a file outside
    its sandbox and to write a second file, and passes only if the guard
    denies both. Exits 1 when a required check fails.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui, json_output=json_output):
        _no_arguments(ctx, "doctor")
        from spadework.doctor import run_doctor

        ok = run_doctor(
            ui,
            options.find_root(),
            explicit_project=options.project is not None,
            canary=canary,
            json_output=json_output,
        )
        if not ok:
            raise typer.Exit(ExitCode.ERROR)
