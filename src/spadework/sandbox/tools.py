"""The tools each kind of session may use.

See docs/spec/04-engine.md, Session kinds, and docs/spec/07-security.md, I1.
The executor passes `TOOLS_BY_KIND[kind]` to the CLI as `--tools`. The guard
allows those tools plus `StructuredOutput` and nothing else. Standard library
only: the guard imports this module.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

# Added by the CLI when a session has an output schema (`--json-schema`). Its
# input is the session's result, so the session cannot finish without it.
STRUCTURED_OUTPUT = "StructuredOutput"

WEB_TOOLS = ("WebSearch", "WebFetch")
FILE_TOOLS = ("Read", "Glob", "Grep", "Write", "Edit")
WRITE_TOOLS = ("Write", "Edit")

_READ = ("Read", "Glob", "Grep")

# Keyed by the session kind's string value.
TOOLS_BY_KIND: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "research": WEB_TOOLS,
        "draft": (*_READ, *WRITE_TOOLS),
        "repair": (*_READ, *WRITE_TOOLS),
        "fix": (*_READ, *WRITE_TOOLS),
        "critic": _READ,
        "summary": (*_READ, "Write"),
    }
)

# Every tool the guard has an argument check for. A tool outside this set is
# denied even if a policy lists it, so shell, notebook, subagent and MCP tools
# can never be allowed by configuration alone.
KNOWN_TOOLS = frozenset((*WEB_TOOLS, *FILE_TOOLS, STRUCTURED_OUTPUT))


def allowlist(kind: str) -> tuple[str, ...]:
    """The guard's allowlist for a session kind: its tools plus StructuredOutput."""
    return (*TOOLS_BY_KIND[kind], STRUCTURED_OUTPUT)
