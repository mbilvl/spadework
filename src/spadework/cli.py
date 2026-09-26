"""The spadework command-line interface."""

from typing import Annotated

import typer

from spadework import __version__

app = typer.Typer(
    name="spadework",
    help="Evidence-first research for projects in discovery.",
    no_args_is_help=True,
    add_completion=False,
)


def _print_version(value: bool) -> None:
    if value:
        typer.echo(f"spadework {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_print_version,
            is_eager=True,
            help="Print the version and exit.",
        ),
    ] = False,
) -> None:
    """Evidence-first research for projects in discovery."""
