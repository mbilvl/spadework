"""Executors run one session each. See docs/spec/05-executors.md.

The backends are imported only when asked for, so importing this package
stays cheap.
"""

from __future__ import annotations

from spadework.errors import UsageError
from spadework.executors.base import (
    Denied,
    Diagnostic,
    ErrorKind,
    Event,
    EventSink,
    Executor,
    Finished,
    Notice,
    RateLimit,
    SessionKind,
    SessionResult,
    SessionSpec,
    Status,
    Text,
    ToolCall,
    Usage,
    ignore_events,
    is_simulated,
)

EXECUTORS = ("claude-cli", "fake")

__all__ = [
    "EXECUTORS",
    "Denied",
    "Diagnostic",
    "ErrorKind",
    "Event",
    "EventSink",
    "Executor",
    "Finished",
    "Notice",
    "RateLimit",
    "SessionKind",
    "SessionResult",
    "SessionSpec",
    "Status",
    "Text",
    "ToolCall",
    "Usage",
    "get_executor",
    "ignore_events",
    "is_simulated",
]


def get_executor(name: str, *, claude_bin: str | None = None) -> Executor:
    """The executor called ``name``.

    ``claude_bin`` is the configured ``[executor].bin``. ``claude-cli`` uses
    ``SPADEWORK_CLAUDE_BIN`` before it, and ``claude`` on PATH after it.
    ``fake`` reads its script from ``SPADEWORK_FAKE_SCRIPT``, if set.
    """
    if name == "claude-cli":
        from spadework.executors.claude_cli import ClaudeCliExecutor

        return ClaudeCliExecutor(claude_bin)
    if name == "fake":
        from spadework.executors.fake import FakeExecutor

        return FakeExecutor.from_env()
    raise UsageError(
        f"unknown executor '{name}'",
        f"Use one of: {', '.join(EXECUTORS)}.",
    )
