"""Errors that the CLI reports to the user, and the exit codes they map to."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import IntEnum


class ExitCode(IntEnum):
    """Process exit codes. See docs/spec/01-cli.md."""

    OK = 0
    ERROR = 1
    USAGE = 2
    INVALID = 3
    PARTIAL = 4
    LIMIT = 5
    RATE_LIMITED = 6
    LOCKED = 7
    INTERRUPTED = 130


@dataclass(frozen=True)
class Problem:
    """One thing wrong with a file, located as precisely as we can."""

    message: str
    path: str | None = None
    line: int | None = None

    @property
    def location(self) -> str | None:
        if self.path is None:
            return None
        return self.path if self.line is None else f"{self.path}:{self.line}"

    def __str__(self) -> str:
        location = self.location
        return self.message if location is None else f"{location}: {self.message}"


class SpadeworkError(Exception):
    """An error with a message for the user and a hint on what to do next."""

    exit_code: ExitCode = ExitCode.ERROR

    def __init__(
        self,
        message: str,
        hint: str,
        *,
        problems: Iterable[Problem] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.problems = tuple(problems)

    def lines(self) -> list[str]:
        """What happened, one problem per line, without the hint."""
        if len(self.problems) == 1:
            return [str(self.problems[0])]
        return [self.message, *(f"  {problem}" for problem in self.problems)]


class InvalidError(SpadeworkError):
    """The project, brief, or playbook is not valid (exit code 3)."""

    exit_code = ExitCode.INVALID


class UsageError(SpadeworkError):
    """Bad flags or arguments (exit code 2)."""

    exit_code = ExitCode.USAGE
