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
    template, works out which tasks are stale and why, and estimates the
    cost. It sends nothing to a model and spends nothing. Exits 3 when the
    project, brief or playbook is not valid.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui, json_output=json_output):
        from spadework import views
        from spadework.engine.planning import plan_project
        from spadework.project import load_project
        from spadework.render import public_view

        loaded = load_project(options.base)
        plan = plan_project(loaded).plan
        if options.verbose:
            ui.info(f"playbook: {loaded.playbook.ref.display_root}")
            ui.info(f"inputs: {len(loaded.inputs)} files")

        warnings: list[tuple[str, str | None]] = [
            (warning.message, warning.hint) for warning in loaded.warnings
        ]
        warnings += views.plan_warnings(plan)
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


@app.command(
    "run",
    epilog=_examples(
        "spadework run",
        "spadework run --only stack --force",
        "spadework run --budget-usd 3 --max-time 90m --no-review",
        "spadework run --json | jq -c 'select(.event == \"task_finished\")'",
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
    with _reporting(ui):
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
            result = run_project(options.base, settings, reporter=display)
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
        where = _shown(Path(exc.filename)) if exc.filename else "a file"
        return SpadeworkError(
            f"cannot use {where}: {exc.strerror or exc}",
            "Check that the path exists and that you may read and write it.",
        )
    return SpadeworkError(
        f"unexpected error: {type(exc).__name__}: {exc}",
        "Run again with -v to see the traceback. Please report it as a bug.",
    )


@app.command(
    epilog=_examples(
        "spadework status",
        "spadework status -C acme-fabrication --json | jq .last_run",
    )
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
    with _reporting(ui, json_output=json_output):
        from spadework import views
        from spadework.engine.status import project_status
        from spadework.project import load_project

        loaded = load_project(options.base)
        current = project_status(loaded)
        for warning in current.warnings:
            ui.warning(warning)
        if json_output:
            ui.json(views.status_document(loaded, current))
        else:
            views.print_status(ui, current)


@app.command(
    epilog=_examples(
        "spadework verify",
        "spadework verify -C acme-fabrication --json | jq .counts",
    )
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
    with _reporting(ui, json_output=json_output):
        from spadework import views
        from spadework.engine.reverify import verify_pack

        report = verify_pack(options.base, progress=None if json_output else ui.info)
        if json_output:
            for message, hint in report.warnings:
                ui.warning(message, hint)
            ui.json(views.verify_document(report))
        else:
            views.print_verify(ui, report)


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


@app.command(
    epilog=_examples(
        "spadework logs",
        "spadework logs references",
        "spadework logs --follow",
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
    project: ProjectOption = None,
    quiet: QuietOption = False,
    verbose: VerboseOption = False,
    no_color: NoColorOption = False,
) -> None:
    """Show session traces.

    By default it shows every session of the last run: one line per tool
    call with its target, the calls the sandbox denied, snippets of the
    model's text, and how each session ended. --follow keeps printing while
    a run goes on, and stops when it ends.
    """
    options = _options(ctx, project, quiet, verbose, no_color)
    ui = options.ui()
    with _reporting(ui):
        from spadework.logs import show_logs

        show_logs(ui, options.base, task=task, run_id=run_id, follow=follow, raw=raw)


@app.command(
    epilog=_examples(
        "spadework doctor",
        "spadework doctor -C acme-fabrication",
        "spadework doctor --canary",
    )
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
        from spadework.doctor import run_doctor

        ok = run_doctor(
            ui,
            options.base,
            explicit_project=options.project is not None,
            canary=canary,
            json_output=json_output,
        )
        if not ok:
            raise typer.Exit(ExitCode.ERROR)
