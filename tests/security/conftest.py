"""Staged sessions and guard helpers for the security tests.

Each box is a real staging directory made by `Staging`, with the control
files beside it, a directory outside the project holding a secret, and
symlinks inside the workdir that point out of it. The sessions cannot make
symlinks (they have no shell), so those model a guard that must hold even if
something else puts one there.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from spadework.sandbox import guard
from spadework.sandbox.policy import GuardEvent, Policy, guard_command
from spadework.sandbox.staging import Staging

NOW = 1_790_000_000.0
OUTPUT = "out-café.md"  # NFC, so that the NFD spelling is a different path

# Stands in for DNS, so the tests never touch the network.
DNS: dict[str, list[str]] = {
    "example.com": ["93.184.215.14"],
    "v6.example.com": ["2606:4700::1111"],
    "internal.example.com": ["10.0.0.5"],
    "mixed.example.com": ["93.184.215.14", "127.0.0.1"],
    "metadata.example.com": ["169.254.169.254"],
    "mapped.example.com": ["::ffff:10.0.0.1"],
    "scoped.example.com": ["fe80::1%en0"],
    "127.0.0.1.nip.io": ["127.0.0.1"],
    "garbage.example.com": ["not-an-address"],
}


def fake_resolve(host: str) -> list[str]:
    try:
        return DNS[host]
    except KeyError:
        raise OSError("unknown host") from None


@dataclass
class Box:
    """One staged session and the paths the tests aim at."""

    staging: Staging
    policy: Policy
    outside: Path  # outside the project; holds secret.txt

    @property
    def workdir(self) -> Path:
        return self.staging.paths.workdir

    @property
    def control(self) -> Path:
        return self.staging.paths.control_dir

    @property
    def root(self) -> Path:
        return self.staging.project_root

    def fill(self, value: Any) -> Any:
        """Replace <w>, <W>, <c>, <root> and <outside> in strings, recursively."""
        if isinstance(value, str):
            return (
                value.replace("<w>", str(self.workdir))
                .replace("<W>", str(self.workdir).upper())
                .replace("<c>", str(self.control))
                .replace("<root>", str(self.root))
                .replace("<outside>", str(self.outside))
            )
        if isinstance(value, dict):
            return {key: self.fill(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.fill(item) for item in value]
        return value

    def payload(self, tool: str, tool_input: Mapping[str, Any]) -> dict[str, Any]:
        return pre_tool_use(tool, self.fill(dict(tool_input)), str(self.workdir))

    def decide(self, tool: str, tool_input: Mapping[str, Any]) -> guard.Decision:
        return guard.decide(
            self.payload(tool, tool_input),
            self.policy,
            now=NOW,
            resolve=fake_resolve,
        )


def pre_tool_use(tool: str, tool_input: Mapping[str, Any], cwd: str) -> dict[str, Any]:
    """A PreToolUse payload shaped like the ones Claude Code sends."""
    return {
        "session_id": "00000000-0000-0000-0000-000000000000",
        "transcript_path": "/HOME/.claude/projects/-WORKDIR/transcript.jsonl",
        "cwd": cwd,
        "prompt_id": "prompt_id-0001",
        "permission_mode": "dontAsk",
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": dict(tool_input),
        "tool_use_id": "toolu_test_0001",
    }


MakeBox = Callable[..., Box]


@pytest.fixture
def make_box(tmp_path: Path) -> MakeBox:
    root = Path(os.path.realpath(tmp_path)) / "acme-fabrication"
    (root / "inputs").mkdir(parents=True)
    (root / "brief.md").write_text("# The client\n", encoding="utf-8")
    (root / "inputs" / "notes.md").write_text("Call notes.\n", encoding="utf-8")
    outside = Path(os.path.realpath(tmp_path)) / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET\n", encoding="utf-8")
    temp_root = Path(os.path.realpath(tmp_path)) / "tmp"
    temp_root.mkdir()

    def make(kind: str = "draft", *, deadline: float = NOW + 600, **caps: int) -> Box:
        session_id = f"001-scope-{kind}"
        output = OUTPUT if kind in ("draft", "repair", "fix", "summary") else None
        staging = Staging.create(
            root, session_id, kind, output=output, temp_root=temp_root
        )
        if kind != "research":
            staging.add_file(root / "brief.md", "brief.md")
            staging.add_file(root / "inputs" / "notes.md", "inputs/notes.md")
            staging.add_text("deps/01-scope.md", "Another task's document.\n")
            workdir = staging.paths.workdir
            (workdir / "link-file").symlink_to(outside / "secret.txt")
            (workdir / "link-dir").symlink_to(outside)
            (workdir / "link-control").symlink_to(staging.paths.control_dir)
        staging.paths.settings.write_text("{}\n", encoding="utf-8")
        policy = staging.write_policy(
            deadline_epoch=deadline,
            max_searches=caps.get("max_searches", 2 if kind == "research" else None),
            max_fetches=caps.get("max_fetches", 3 if kind == "research" else None),
            total_calls=caps.get("total_calls", 80),
        )
        return Box(staging, policy, outside)

    return make


@pytest.fixture
def writer(make_box: MakeBox) -> Box:
    return make_box("draft")


@pytest.fixture
def researcher(make_box: MakeBox) -> Box:
    return make_box("research")


def run_guard(
    policy_path: Path | str,
    stdin: bytes | Mapping[str, Any],
    *,
    event: GuardEvent = "pre-tool-use",
    argv: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the real entry point in a subprocess, as the CLI does."""
    command = argv if argv is not None else guard_command(Path(policy_path), event)
    data = stdin if isinstance(stdin, bytes) else json.dumps(stdin).encode("utf-8")
    completed = subprocess.run(
        command, input=data, capture_output=True, timeout=30, check=False
    )
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        completed.stdout.decode("utf-8"),
        completed.stderr.decode("utf-8"),
    )


def hook_decision(completed: subprocess.CompletedProcess[str]) -> tuple[str, str]:
    """The (decision, reason) from a guard that exited 0."""
    assert completed.returncode == 0, completed.stderr
    output = json.loads(completed.stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == "PreToolUse"
    return output["permissionDecision"], output["permissionDecisionReason"]


def future_deadline() -> float:
    """A deadline for subprocess tests, which use the real clock."""
    return time.time() + 600
