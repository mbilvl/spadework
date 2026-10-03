"""Terminal output: results go to stdout; progress, warnings and errors to stderr."""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import traceback
from functools import cache, cached_property
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

from spadework.errors import SpadeworkError

if TYPE_CHECKING:
    from rich.console import Console, ConsoleOptions, RenderableType, RenderResult


def debug_enabled() -> bool:
    return os.environ.get("SPADEWORK_DEBUG", "") not in ("", "0")


def shown(path: Path) -> str:
    """A path as the user would type it here, in the current folder.

    Relative when it is the current folder, below it or above it; absolute
    otherwise, rather than a climb of ``../`` up and back down.
    """
    here, target = Path.cwd().resolve(), path.resolve()
    if here == Path(here.anchor):
        return target.as_posix()
    if target.is_relative_to(here) or here.is_relative_to(target):
        return Path(os.path.relpath(target, here)).as_posix()
    return target.as_posix()


def silence(stream: IO[str]) -> None:
    """Send whatever else is written to ``stream`` nowhere.

    Called when its reader has gone away, as with ``spadework run | head``:
    the command goes on and ends as it would have, and Python's own flush at
    exit no longer fails on the closed pipe.
    """
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return  # not a real file, as in tests
    devnull = os.open(os.devnull, os.O_WRONLY)
    try:
        os.dup2(devnull, fd)
    finally:
        os.close(devnull)


# A command a message suggests, such as `spadework check`. init never acts on
# an existing project, and playbooks needs no project.
_COMMAND = re.compile(
    r"\bspadework (?:check|run|status|logs|verify|doctor|clean)\b(?! -C\b)"
)


@cache
def _console_class() -> type[Console]:
    from rich.console import Console

    class QuietOnBrokenPipe(Console):
        def on_broken_pipe(self) -> None:
            # Rich's default raises SystemExit(1), which would end a run
            # half-way and give it an exit code that its manifest disagrees
            # with. Stop writing instead, and let the command finish.
            self.quiet = True
            silence(self.file)

    return QuietOnBrokenPipe


class Ui:
    """Where a command writes. Rich is imported only when something is printed.

    ``project`` is the project folder as the user gave it with ``-C``, when
    that is not the current folder. The commands that messages suggest then
    get ``-C`` too, so they can be pasted as they are.
    """

    def __init__(
        self,
        *,
        quiet: bool = False,
        verbose: bool = False,
        color: bool = True,
        project: str | None = None,
    ):
        self.quiet = quiet
        self.verbose = verbose
        self.color = color
        self.project = project

    def with_project(self, text: str) -> str:
        """``text`` with ``-C PROJECT`` added to each spadework command in it."""
        if self.project is None:
            return text
        flag = f" -C {shlex.quote(self.project)}"
        return _COMMAND.sub(lambda match: match.group(0) + flag, text)

    @property
    def debug(self) -> bool:
        return self.verbose or debug_enabled()

    def _console(self, *, stderr: bool) -> Console:
        return _console_class()(
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
        from rich.text import Text

        # Text is never re-wrapped, so it reads the same piped as on screen.
        # Tables still fit the terminal, without the padding at line ends.
        if isinstance(renderable, str | Text):
            self.out.print(renderable, style=style, soft_wrap=True)
        else:
            self.out.print(_Trimmed(renderable), style=style)

    def json(self, document: dict[str, Any]) -> None:
        sys.stdout.write(json.dumps(document, indent=2, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def info(self, message: str) -> None:
        if not self.quiet:
            self.err.print(self.with_project(message), soft_wrap=True)

    def warning(self, message: str, hint: str | None = None) -> None:
        from rich.text import Text

        self.err.print(
            Text.assemble(("warning: ", "bold yellow"), self.with_project(message)),
            soft_wrap=True,
        )
        if hint and not self.quiet:
            self.err.print(self.with_project(hint), soft_wrap=True)

    def error(self, exc: SpadeworkError) -> None:
        from rich.text import Text

        if self.debug:
            self.err.print("".join(traceback.format_exception(exc)), soft_wrap=True)
        first, *rest = exc.lines()
        self.err.print(Text.assemble(("error: ", "bold red"), first), soft_wrap=True)
        for line in rest:
            self.err.print(line, soft_wrap=True)
        self.err.print(self.with_project(exc.hint), soft_wrap=True)


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
