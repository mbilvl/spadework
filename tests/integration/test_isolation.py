"""A control for the isolation checks in the other integration tests.

Run without spadework's flags, Claude Code loads what the fixtures plant:
so when the sandboxed sessions show none of it, that is because of the
flags, not because the plants were in the wrong place.
"""

from __future__ import annotations

import os
import subprocess

from tests.integration.conftest import Isolation, claude_path
from tests.integration.mock_api import MockApi, steps, text

# Planted things Claude Code loads by default, in -p mode, with no approval.
LOADED_BY_DEFAULT = (
    "the project CLAUDE.md",
    "CLAUDE.local.md",
    "an ancestor CLAUDE.md",
    "the user CLAUDE.md",
    "a user skill",
    "auto-memory",
    "the git branch",
    "a git commit subject",
    "a changed file's name",
)


def test_without_spadework_the_planted_environment_reaches_the_model(
    api: MockApi, isolation: Isolation
) -> None:
    binary = claude_path()
    assert binary is not None
    api.respond = steps(text("Hello."))
    workdir = isolation.project / "work"
    workdir.mkdir()

    done = subprocess.run(
        [
            binary,
            "-p",
            "--model",
            "haiku",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--max-budget-usd",
            "0.05",
        ],
        input="Say hello.",
        cwd=workdir,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    seen = "\n".join(request.everything for request in api.message_requests)
    missed = [
        where for where in LOADED_BY_DEFAULT if isolation.tokens[where] not in seen
    ]
    assert missed == []
    hooks = isolation.leak_log.read_text() if isolation.leak_log.exists() else ""
    assert "USER_HOOK" in hooks
    assert "PROJECT_HOOK" in hooks
    for request in api.message_requests:
        assert request.api_key_is_placeholder
        assert not request.authorization_sent
