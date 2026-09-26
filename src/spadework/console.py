"""Terminal output: results go to stdout; progress, warnings and errors to stderr."""

from __future__ import annotations

import json
import os
import sys
import traceback
from functools import cached_property
from typing import TYPE_CHECKING, Any

from spadework.errors import SpadeworkError

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderableType, RenderResult


def debug_enabled() -> bool:
    return os.environ.get("SPADEWORK_DEBUG", "") not in ("", "0")


class Ui:
    """Where a command writes. Rich is imported only when something is printed."""

    def __init__(
        self, *, quiet: bool = False, verbose: bool = False, color: bool = True
    ):
        self.quiet = quiet
        self.verbose = verbose
        self.color = color

    @property
    def debug(self) -> bool:
        return self.verbose or debug_enabled()

    def _console(self, *, stderr: bool) -> Console:
        from rich.console import Console

        return Console(
            stderr=stderr,
            # None lets Rich honour NO_COLOR from the environment.
            no_color=True if not self.color else None,
            highlight=False,
            markup=False,
            emoji=False,
        )

    @cached_property
    def out(self) -> Console:
        return self._console(stderr=False)

    @cached_property
    def err(self) -> Console:
        return self._console(stderr=True)

    def result(
        self, renderable: RenderableType = "", *, style: str | None = None
    ) -> None:
        # Text is never re-wrapped, so it reads the same piped as on screen.
        # Tables still fit the terminal, without the padding at line ends.
        if isinstance(renderable, str):
            self.out.print(renderable, style=style, soft_wrap=True)
        else:
            self.out.print(_Trimmed(renderable), style=style)

    def json(self, document: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def info(self, message: str) -> None:
        if not self.quiet:
            self.err.print(message, soft_wrap=True)

    def warning(self, message: str, hint: str | None = None) -> None:
        from rich.text import Text

        self.err.print(
            Text.assemble(("warning: ", "bold yellow"), message), soft_wrap=True
        )
        if hint and not self.quiet:
            self.err.print(hint, soft_wrap=True)

    def error(self, exc: SpadeworkError) -> None:
        from rich.text import Text

        if self.debug:
            self.err.print("".join(traceback.format_exception(exc)), soft_wrap=True)
        first, *rest = exc.lines()
        self.err.print(Text.assemble(("error: ", "bold red"), first), soft_wrap=True)
        for line in rest:
            self.err.print(line, soft_wrap=True)
        self.err.print(exc.hint, soft_wrap=True)


class _Trimmed:
    """Renders without the trailing spaces that Rich pads table cells with."""

    def __init__(self, renderable: RenderableType) -> None:
        self.renderable = renderable

    def __rich_console__(
        self, console: Console, options: ConsoleOptions
    ) -> RenderResult:
        from rich.segment import Segment

        for line in console.render_lines(self.renderable, options, pad=False):
            while line and not line[-1].control and not line[-1].text.strip():
                line.pop()
            if line:
                last = line.pop()
                line.append(Segment(last.text.rstrip(), last.style, last.control))
            yield from line
            yield Segment.line()
