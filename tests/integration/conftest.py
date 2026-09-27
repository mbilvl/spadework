"""Integration tests: the real ``claude`` binary, with the real staging and
guard, driven by a local mock of the Messages API.

They are opt-in. They run only when ``SPADEWORK_TEST_CLAUDE=1`` and
``claude`` is on PATH, and they are skipped otherwise, so the default test
run never starts Claude Code.

No model is called and nothing is spent. Claude Code runs in an isolated
environment: every inherited ``CLAUDE*`` and ``ANTHROPIC*`` variable is
removed (including any OAuth token), ``ANTHROPIC_BASE_URL`` points at the
mock on 127.0.0.1 with a placeholder API key, and ``HOME`` and
``CLAUDE_CONFIG_DIR`` are fresh temporary folders, so the user's own
``~/.claude`` is never read or written. The executor passes
``--no-session-persistence``.

To prove that the sandbox keeps the user's environment out of the model's
context, each test's project is planted with things Claude Code would
normally load: ``CLAUDE.md`` files, project and user settings with hooks,
an MCP server, a skill, auto-memory and a git repository. Each carries a
unique token, and ``Isolation.check`` asserts that no token reached the mock
and that no planted hook or server ran.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from spadework.executors import process
from spadework.executors.base import Event, SessionKind, SessionResult, SessionSpec
from spadework.executors.claude_cli import ClaudeCliExecutor
from spadework.records import output_schema
from spadework.sandbox.staging import Staging
from spadework.sandbox.tools import STRUCTURED_OUTPUT, TOOLS_BY_KIND
from tests.integration.mock_api import PLACEHOLDER_KEY, MockApi

ENABLE_ENV = "SPADEWORK_TEST_CLAUDE"
HERE = Path(__file__).parent


def claude_path() -> str | None:
    return shutil.which("claude")


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1" and claude_path() is not None


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "integration: runs the real claude binary against a local mock API "
        f"(opt-in: {ENABLE_ENV}=1)",
    )


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    reason = (
        f"set {ENABLE_ENV}=1 to run the real claude against a mock API"
        if os.environ.get(ENABLE_ENV) != "1"
        else "claude is not on PATH"
    )
    for item in items:
        if not item.path.is_relative_to(HERE):
            continue
        item.add_marker(pytest.mark.integration)
        if not enabled():
            item.add_marker(pytest.mark.skip(reason=reason))


# --- The mock API and the isolated environment ------------------------------


@pytest.fixture(scope="session")
def mock_server() -> Iterator[MockApi]:
    with MockApi() as api:
        yield api


@pytest.fixture
def api(mock_server: MockApi) -> Iterator[MockApi]:
    """The mock API, with no requests recorded yet. Set ``api.respond``."""
    mock_server.reset()
    yield mock_server
    assert not mock_server.errors, f"the mock's script failed: {mock_server.errors}"


@dataclass
class Isolation:
    """The isolated environment Claude Code runs in, and what was planted in it."""

    home: Path
    config_dir: Path
    temp: Path
    project: Path
    leak_log: Path
    """Planted hooks and MCP servers write here if they ever run."""
    tokens: dict[str, str] = field(default_factory=dict)
    """What each planted file says, by where it was planted."""

    def check(self, api: MockApi, *, expected_tools: set[str] | None = None) -> None:
        """Nothing of the user's environment reached the model, and nothing
        planted ran."""
        requests = api.message_requests
        assert requests, "Claude Code sent no request to the mock"
        for request in requests:
            assert request.api_key_is_placeholder, "a real API key was sent"
            assert not request.authorization_sent, "an OAuth token was sent"
            everything = request.everything
            for where, token in self.tokens.items():
                assert token not in everything, f"{where} reached the model"
            offered = request.tools
            assert not [name for name in offered if name.startswith("mcp__")]
            system = json.dumps(request.body.get("system"))
            assert "mcp__" not in system, "an MCP tool reached the system prompt"
            if request.is_main and expected_tools is not None:
                assert set(offered) == expected_tools
        leaked = self.leak_log.read_text() if self.leak_log.exists() else ""
        assert leaked == "", f"planted hooks or MCP servers ran: {leaked!r}"


@pytest.fixture
def isolation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api: MockApi
) -> Isolation:
    """Claude Code's environment for one test, cut off from the user's."""
    for name in list(os.environ):
        if name.startswith(("CLAUDE", "ANTHROPIC")):
            monkeypatch.delenv(name)
    home = tmp_path / "home"
    config_dir = tmp_path / "claude-config"
    temp = tmp_path / "tmp"
    for folder in (home, config_dir, temp):
        folder.mkdir()
    binary = claude_path()
    assert binary is not None
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("ANTHROPIC_BASE_URL", api.url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", PLACEHOLDER_KEY)
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    monkeypatch.setenv("DISABLE_AUTOUPDATER", "1")
    monkeypatch.setenv("TMPDIR", str(temp))
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    monkeypatch.setenv("SPADEWORK_CLAUDE_BIN", binary)
    for name in ("SPADEWORK_DEBUG", "SPADEWORK_EXECUTOR", "SPADEWORK_FAKE_SCRIPT"):
        monkeypatch.delenv(name, raising=False)
    # The CLI exits promptly; no need to wait long for stragglers.
    monkeypatch.setattr(process, "EXIT_GRACE_S", 5.0)
    project = Path(os.path.realpath(tmp_path / "acme-fabrication"))
    project.mkdir()
    isolation = Isolation(
        home, config_dir, temp, project, tmp_path / "leak.log", tokens={}
    )
    _plant(isolation)
    return isolation


def _token(isolation: Isolation, where: str) -> str:
    token = f"PLANTED-{secrets.token_hex(6)}"
    isolation.tokens[where] = token
    return token


def _leak_command(isolation: Isolation, tag: str) -> str:
    return f"/bin/sh -c 'echo {tag} >> {isolation.leak_log}'"


def _hooks(isolation: Isolation, tag: str) -> dict[str, Any]:
    hook = [{"hooks": [{"type": "command", "command": _leak_command(isolation, tag)}]}]
    matched = [{"matcher": "*", "hooks": hook[0]["hooks"]}]
    return {"SessionStart": hook, "UserPromptSubmit": hook, "PreToolUse": matched}


def _plant(isolation: Isolation) -> None:
    """Put everything Claude Code would normally load where it would look."""
    project, config_dir, home = isolation.project, isolation.config_dir, isolation.home

    def write(path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    # Instructions: the project's, an ancestor's, the user's, and a local one.
    write(project / "CLAUDE.md", _token(isolation, "the project CLAUDE.md") + "\n")
    write(project / "CLAUDE.local.md", _token(isolation, "CLAUDE.local.md") + "\n")
    write(project.parent / "CLAUDE.md", _token(isolation, "an ancestor CLAUDE.md"))
    write(config_dir / "CLAUDE.md", _token(isolation, "the user CLAUDE.md") + "\n")
    write(home / ".claude" / "CLAUDE.md", _token(isolation, "~/.claude/CLAUDE.md"))
    # Settings with hooks, permissions and env, for the user and the project.
    user_settings = {
        "hooks": _hooks(isolation, "USER_HOOK"),
        "permissions": {"allow": ["Bash(*)"]},
        "env": {"PLANTED_ENV": _token(isolation, "the user settings env")},
    }
    write(config_dir / "settings.json", json.dumps(user_settings))
    project_settings = {
        "hooks": _hooks(isolation, "PROJECT_HOOK"),
        "permissions": {"allow": ["Bash(*)"]},
    }
    write(project / ".claude" / "settings.json", json.dumps(project_settings))
    write(project / ".claude" / "settings.local.json", json.dumps(project_settings))
    # An MCP server for the project and one for the user.
    server = {"command": "/bin/sh", "args": ["-c", f"echo MCP >> {isolation.leak_log}"]}
    write(project / ".mcp.json", json.dumps({"mcpServers": {"planted": server}}))
    # Skills, a command and an agent, which list their descriptions.
    for base, who in ((config_dir, "user"), (project / ".claude", "project")):
        token = _token(isolation, f"a {who} skill")
        write(
            base / "skills" / "planted" / "SKILL.md",
            f"---\nname: planted\ndescription: {token}\n---\n{token}\n",
        )
        token = _token(isolation, f"a {who} agent")
        write(
            base / "agents" / "planted.md",
            f"---\nname: planted\ndescription: {token}\n---\n{token}\n",
        )
    write(
        config_dir / "commands" / "planted.md",
        _token(isolation, "a user command") + "\n",
    )
    # Auto-memory, where Claude Code keeps it for this project.
    memory = config_dir / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project))
    write(memory / "memory" / "MEMORY.md", _token(isolation, "auto-memory") + "\n")
    # A git repository, whose branch, author and commits the git block shows.
    if shutil.which("git"):
        branch = _token(isolation, "the git branch").lower()
        isolation.tokens["the git branch"] = branch
        author = _token(isolation, "the git author")
        subject = _token(isolation, "a git commit subject")
        git = ["git", "-c", f"user.name={author}", "-c", "user.email=a@example.com"]
        subprocess.run([*git, "init", "-q", "-b", branch], cwd=project, check=True)
        subprocess.run([*git, "add", "CLAUDE.md"], cwd=project, check=True)
        subprocess.run([*git, "commit", "-q", "-m", subject], cwd=project, check=True)
        changed = _token(isolation, "a changed file's name")
        write(project / f"{changed}.txt", "not committed\n")


# --- Sessions ----------------------------------------------------------------

BRIEF = "# Acme Fabrication\n\nA stainless-steel fabricator. Staged brief.\n"
SECRET = "SECRET-OUTSIDE-THE-WORKDIR-7f3a9c"
DRAFT_TOOLS = {*TOOLS_BY_KIND["draft"], STRUCTURED_OUTPUT}


def draft_result(summary: str = "Scope drafted from the brief.") -> dict[str, Any]:
    return {
        "status": "ok",
        "summary": summary,
        "key_facts": ["Acme makes stainless-steel equipment"],
        "decisions": [],
        "open_questions": [],
        "assumptions": [],
        "risks": [],
        "gaps": [],
    }


@dataclass
class Session:
    staging: Staging
    spec: SessionSpec
    secret: Path
    """A file in the project, outside the staging directory."""
    events: list[Event] = field(default_factory=list)

    @property
    def workdir(self) -> Path:
        return self.staging.paths.workdir

    @property
    def output(self) -> Path:
        return self.staging.output_path

    def run(self) -> SessionResult:
        executor = ClaudeCliExecutor()
        assert executor.restricted_supported, "this claude has no --restricted"
        return executor.run(self.spec, self.events.append)

    def log(self) -> str:
        return self.spec.log_path.read_text(encoding="utf-8")


MakeSession = Callable[..., Session]


@pytest.fixture
def make_session(isolation: Isolation) -> Iterator[MakeSession]:
    """A draft session staged in the planted project, ready to run."""
    made: list[Staging] = []

    def make(
        *,
        session_id: str = "001-scope-draft",
        max_budget_usd: float = 0.5,
        timeout_s: int = 120,
        write_policy: bool = True,
    ) -> Session:
        project = isolation.project
        secret = project / "secret.txt"
        secret.write_text(SECRET + "\n", encoding="utf-8")
        staging = Staging.create(
            project, session_id, SessionKind.DRAFT.value, output="01-scope.md"
        )
        made.append(staging)
        staging.add_text("brief.md", BRIEF)
        if write_policy:
            staging.write_policy(deadline_epoch=time.time() + timeout_s)
        spec = SessionSpec(
            session_id=session_id,
            kind=SessionKind.DRAFT,
            prompt="Write the scope document from brief.md.",
            system_append="Write for a developer. Cite nothing you did not read.",
            model="haiku",
            tools=TOOLS_BY_KIND["draft"],
            workdir=staging.paths.workdir,
            policy_path=staging.paths.policy,
            output_schema=output_schema("draft"),
            max_budget_usd=max_budget_usd,
            timeout_s=timeout_s,
            log_path=project / ".spadework" / "runs" / "r" / f"{session_id}.jsonl",
            output=staging.output_path,
            task="scope",
        )
        return Session(staging, spec, secret)

    yield make
    for staging in made:
        staging.cleanup()
