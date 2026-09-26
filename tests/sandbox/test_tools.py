from typing import get_args

import pytest

from spadework.budget import SessionKind
from spadework.sandbox.tools import (
    KNOWN_TOOLS,
    STRUCTURED_OUTPUT,
    TOOLS_BY_KIND,
    WEB_TOOLS,
    WRITE_TOOLS,
    allowlist,
)

NEVER = {"Bash", "BashOutput", "KillShell", "Task", "Agent", "NotebookEdit"}


def test_every_session_kind_has_tools() -> None:
    assert set(TOOLS_BY_KIND) == set(get_args(SessionKind))


def test_tools_by_kind_match_the_spec() -> None:
    assert dict(TOOLS_BY_KIND) == {
        "research": ("WebSearch", "WebFetch"),
        "draft": ("Read", "Glob", "Grep", "Write", "Edit"),
        "repair": ("Read", "Glob", "Grep", "Write", "Edit"),
        "fix": ("Read", "Glob", "Grep", "Write", "Edit"),
        "critic": ("Read", "Glob", "Grep"),
        "summary": ("Read", "Glob", "Grep", "Write"),
    }


def test_tools_by_kind_is_read_only() -> None:
    with pytest.raises(TypeError):
        TOOLS_BY_KIND["draft"] = ("Bash",)  # type: ignore[index]


@pytest.mark.parametrize("kind", sorted(TOOLS_BY_KIND))
def test_web_access_and_files_never_mix(kind: str) -> None:
    """I5: only research sessions have web tools, and they have nothing else."""
    tools = set(TOOLS_BY_KIND[kind])
    if kind == "research":
        assert tools == set(WEB_TOOLS)
    else:
        assert not tools & set(WEB_TOOLS)


@pytest.mark.parametrize("kind", sorted(TOOLS_BY_KIND))
def test_allowlist_adds_structured_output(kind: str) -> None:
    assert allowlist(kind) == (*TOOLS_BY_KIND[kind], STRUCTURED_OUTPUT)


def test_no_kind_can_run_code_or_delegate() -> None:
    """I1."""
    for kind, tools in TOOLS_BY_KIND.items():
        assert not set(tools) & NEVER, kind
        assert not any(tool.startswith("mcp__") for tool in tools), kind
    assert not KNOWN_TOOLS & NEVER


def test_only_writing_kinds_can_write() -> None:
    """I3: research and critic sessions write nothing."""
    writers = {
        kind for kind, tools in TOOLS_BY_KIND.items() if set(tools) & set(WRITE_TOOLS)
    }

    assert writers == {"draft", "repair", "fix", "summary"}
