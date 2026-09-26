"""Helpers for the executor tests: session specs, stream-json events, and a
stand-in ``claude`` binary that never calls a model."""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from spadework.executors.base import (
    READ_ONLY_KINDS,
    Event,
    SessionKind,
    SessionSpec,
)
from spadework.records import output_schema
from spadework.sandbox.tools import TOOLS_BY_KIND

STREAM = Path(__file__).parent.parent / "fixtures" / "stream"

MakeSpec = Callable[..., SessionSpec]


@pytest.fixture
def make_spec(tmp_path: Path) -> MakeSpec:
    """A valid spec for a session of ``kind``, laid out like a project."""

    def make(kind: SessionKind = SessionKind.DRAFT, **overrides: Any) -> SessionSpec:
        dot = tmp_path / ".spadework"
        workdir = dot / "sessions" / "001"
        workdir.mkdir(parents=True, exist_ok=True)
        (dot / "control").mkdir(exist_ok=True)
        fields: dict[str, Any] = {
            "session_id": f"001-stack-{kind}",
            "kind": kind,
            "prompt": "Write the document.",
            "system_append": "Follow the house rules.",
            "model": "sonnet",
            "tools": TOOLS_BY_KIND[kind.value],
            "workdir": workdir,
            "policy_path": dot / "control" / "001.policy.json",
            "output_schema": output_schema(kind),
            "max_budget_usd": 1.0,
            "timeout_s": 30,
            "log_path": dot / "runs" / "r1" / "sessions" / f"001-stack-{kind}.jsonl",
            "output": None if kind in READ_ONLY_KINDS else workdir / "06-tech-stack.md",
            "task": "stack",
        }
        fields.update(overrides)
        return SessionSpec(**fields)

    return make


class Recorder:
    """An event sink that keeps what it gets."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def of(self, kind: type[Any]) -> list[Any]:
        return [event for event in self.events if isinstance(event, kind)]


@pytest.fixture
def recorder() -> Recorder:
    return Recorder()


# --- stream-json events -----------------------------------------------------


def session_start(exit_code: int = 0, stderr: str = "") -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "hook_response",
        "hook_name": "SessionStart:startup",
        "hook_event": "SessionStart",
        "stdout": "",
        "stderr": stderr,
        "exit_code": exit_code,
        "outcome": "success" if exit_code == 0 else "error",
    }


def init(tools: Iterable[str]) -> dict[str, Any]:
    return {"type": "system", "subtype": "init", "tools": list(tools)}


def text(value: str) -> dict[str, Any]:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": value}]},
    }


def tool_use(call_id: str, name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    block = {"type": "tool_use", "id": call_id, "name": name, "input": tool_input}
    return {"type": "assistant", "message": {"content": [block]}}


def pre_tool_use(
    name: str, exit_code: int = 0, stdout: str = "", stderr: str = ""
) -> dict[str, Any]:
    if exit_code == 0 and not stdout:
        stdout = json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow",
                }
            }
        )
    return {
        "type": "system",
        "subtype": "hook_response",
        "hook_name": f"PreToolUse:{name}",
        "hook_event": "PreToolUse",
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": exit_code,
    }


def json_deny(reason: str) -> str:
    return json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def tool_result(call_id: str, *, is_error: bool = False) -> dict[str, Any]:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": call_id,
        "content": "done",
    }
    if is_error:
        block["is_error"] = True
    return {"type": "user", "message": {"content": [block]}}


def result(**overrides: Any) -> dict[str, Any]:
    event: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "total_cost_usd": 0.0123,
        "duration_ms": 4500,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_input_tokens": 3000,
            "cache_creation_input_tokens": 400,
            "server_tool_use": {"web_search_requests": 2, "web_fetch_requests": 3},
        },
        "permission_denials": [],
        "structured_output": {"status": "ok"},
    }
    event.update(overrides)
    return {key: value for key, value in event.items() if value is not ...}


def lines(*events: dict[str, Any] | str) -> list[str]:
    return [
        (event if isinstance(event, str) else json.dumps(event)) + "\n"
        for event in events
    ]


def fixture_lines(name: str) -> list[str]:
    return (STREAM / name).read_text(encoding="utf-8").splitlines(keepends=True)


# --- a stand-in claude binary -----------------------------------------------

# The probes are answered in shell, so only sessions pay for starting Python.
FAKE_CLAUDE_SH = """\
#!/bin/sh
case "$1" in
  --version)
    echo "${{FAKE_CLAUDE_VERSION:-2.1.278 (Claude Code)}}"
    exit 0 ;;
  --help)
    if [ -n "$FAKE_CLAUDE_HELP_EXIT" ]; then
      exit "$FAKE_CLAUDE_HELP_EXIT"
    fi
    echo "Usage: claude [options] [prompt]"
    echo "  --tools <tools...>  the built-in tools"
    if [ -z "$FAKE_CLAUDE_NO_RESTRICTED" ]; then
      echo "  --restricted        confine the file tools to the working directory"
    fi
    exit 0 ;;
  auth)
    if [ -n "$FAKE_CLAUDE_AUTH" ]; then
      printf '%s\\n' "$FAKE_CLAUDE_AUTH"
    else
      echo '{{"loggedIn": true}}'
    fi
    exit 0 ;;
esac
exec "{python}" "{script}" "$@"
"""

FAKE_CLAUDE = r"""
import json, os, signal, subprocess, sys, time

args = sys.argv[1:]
env = os.environ
if env.get("FAKE_CLAUDE_IGNORE_TERM"):
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
prompt = sys.stdin.read()
record = env.get("FAKE_CLAUDE_RECORD")
if record:
    watched = (
        "CLAUDE_CODE_SIMPLE",
        "CLAUDE_CODE_SAFE_MODE",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS",
        "FAKE_CLAUDE_INHERITED",
    )
    with open(record, "w") as f:
        json.dump({
            "argv": args,
            "cwd": os.getcwd(),
            "stdin": prompt,
            "env": {key: env[key] for key in watched if key in env},
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
        }, f)
# Play the guard's part: its SessionStart hook writes the marker.
guard = env.get("FAKE_CLAUDE_GUARD", "ok")
if "--settings" in args and guard != "missing":
    settings = args[args.index("--settings") + 1]
    session_id = os.path.basename(settings)[: -len(".settings.json")]
    marker = os.path.join(os.path.dirname(settings), session_id + ".alive")
    with open(marker, "w") as f:
        f.write((session_id if guard == "ok" else guard) + "\n")
child = env.get("FAKE_CLAUDE_CHILD")
if child:
    # Detached from our pipes, so only killing the group can reach it.
    quiet = subprocess.DEVNULL if env.get("FAKE_CLAUDE_CHILD_DETACHED") else None
    grandchild = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=quiet, stdout=quiet, stderr=quiet,
    )
    with open(child, "w") as f:
        f.write(str(grandchild.pid))
stream = env.get("FAKE_CLAUDE_STREAM")
if stream:
    with open(stream) as f:
        for line in f:
            sys.stdout.write(line)
            sys.stdout.flush()
sys.stderr.write(env.get("FAKE_CLAUDE_STDERR", ""))
sys.stderr.flush()
time.sleep(float(env.get("FAKE_CLAUDE_SLEEP", "0")))
sys.exit(int(env.get("FAKE_CLAUDE_EXIT", "0")))
"""


@pytest.fixture(scope="session")
def fake_claude(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An executable ``claude`` that follows FAKE_CLAUDE_* environment variables.

    Made once: macOS checks a new executable the first time it runs.
    """
    directory = tmp_path_factory.mktemp("bin")
    script = directory / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    binary = directory / "claude"
    binary.write_text(
        FAKE_CLAUDE_SH.format(python=sys.executable, script=script), encoding="utf-8"
    )
    binary.chmod(0o755)
    return binary


def write_stream(path: Path, events: Iterable[dict[str, Any] | str]) -> Path:
    path.write_text("".join(lines(*events)), encoding="utf-8")
    return path
