"""The ``claude-cli`` executor: the user's installed Claude Code, run headless.

See docs/spec/05-executors.md. Claude Code's flags and settings file are
known here, and its ``stream-json`` format in ``claude_stream``; nothing else
in spadework depends on either. This module builds the command line, the
environment and the settings file for a session, and runs it with
``process.run_process``, which stops the whole process group on a timeout,
an interrupt or a sandbox violation.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping
from decimal import ROUND_DOWN, Decimal
from functools import cached_property
from pathlib import Path
from typing import Any

from spadework.executors.base import (
    Diagnostic,
    EventSink,
    Finished,
    Notice,
    SessionResult,
    SessionSpec,
)
from spadework.executors.claude_stream import StreamParser
from spadework.executors.process import LineAction, run_process
from spadework.sandbox.policy import GuardEvent, guard_command, marker_path
from spadework.sandbox.tools import STRUCTURED_OUTPUT, TOOLS_BY_KIND

MIN_VERSION = (2, 1, 278)
"""The oldest Claude Code this release was tested against."""

BIN_ENV = "SPADEWORK_CLAUDE_BIN"

# Either of these makes Claude Code skip every hook, including the guard.
REMOVED_ENV = ("CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_SAFE_MODE")
# Auto-memory and the git status block reach the session even with no
# settings loaded. The settings file turns them off too; these also cover a
# settings file that Claude Code silently ignored.
ADDED_ENV = {
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
}

HOOK_TIMEOUT_S = 600
PROBE_TIMEOUT_S = 20.0
"""For ``--version``, ``--help`` and ``auth status``."""

_NO_RESTRICTED = (
    "Claude Code has no --restricted, so sessions run without restricted "
    "mode and rely on the guard alone"
)
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")
_RESTRICTED = re.compile(r"(?<![\w-])--restricted(?![\w-])")


# --- Finding and probing the binary -----------------------------------------


def find_claude(
    configured: str | None = None, environ: Mapping[str, str] | None = None
) -> Path | None:
    """The ``claude`` binary, as an absolute path.

    ``SPADEWORK_CLAUDE_BIN`` comes first, then ``configured`` (the
    ``[executor].bin`` setting), then ``claude`` on PATH, as environment
    variables beat the config file (docs/spec/01-cli.md). A name that was
    given is not replaced by a later one when it cannot be found.

    The path is made absolute, because sessions run in another directory,
    but symlinks are kept: an update that moves the target keeps working.
    """
    environ = os.environ if environ is None else environ
    name = environ.get(BIN_ENV) or configured or "claude"
    if os.sep in name or name.startswith("~"):
        found: str | None = os.path.expanduser(name)
    else:
        found = shutil.which(name, path=environ.get("PATH"))
    if found is None:
        return None
    path = Path(os.path.abspath(found))
    return path if path.is_file() and os.access(path, os.X_OK) else None


def parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION.search(text)
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch


def child_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for Claude Code: the user's, so authentication and
    proxies work, minus what would disable the guard."""
    source = os.environ if environ is None else environ
    env = {key: value for key, value in source.items() if key not in REMOVED_ENV}
    env.update(ADDED_ENV)
    return env


# --- The session's command line and settings --------------------------------


def expected_tools(spec: SessionSpec) -> frozenset[str]:
    """The exact tool set Claude Code must report in its ``init`` event."""
    return frozenset(spec.expected_tools or (*spec.tools, STRUCTURED_OUTPUT))


def settings_path(spec: SessionSpec) -> Path:
    """The per-session settings file, next to the policy in the control plane."""
    return spec.policy_path.parent / f"{spec.session_id}.settings.json"


def settings_for(policy_path: Path) -> dict[str, Any]:
    """Claude Code settings that run the guard, and nothing else of the user's."""

    def guard(event: GuardEvent) -> dict[str, Any]:
        command = shlex.join(guard_command(policy_path, event))
        return {"type": "command", "command": command, "timeout": HOOK_TIMEOUT_S}

    return {
        "hooks": {
            # Runs before the init event and before any model call; the
            # parser treats its answer as proof that the settings loaded.
            "SessionStart": [{"hooks": [guard("session-start")]}],
            "PreToolUse": [{"matcher": "*", "hooks": [guard("pre-tool-use")]}],
        },
        "autoMemoryEnabled": False,
        "includeGitInstructions": False,
    }


def write_settings(spec: SessionSpec) -> Path:
    path = settings_path(spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(settings_for(spec.policy_path), indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


def build_argv(
    binary: Path, spec: SessionSpec, settings: Path, *, restricted: bool
) -> list[str]:
    """The command line for one session. The prompt goes to stdin."""
    argv = [
        str(binary),
        "-p",
        "--model",
        spec.model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-hook-events",
        "--json-schema",
        json.dumps(spec.output_schema, separators=(",", ":")),
        "--tools",
        ",".join(spec.tools),
    ]
    if restricted:
        argv.append("--restricted")
    argv += [
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--settings",
        str(settings),
        # The empty string, as its own argument, loads no user, project or
        # local settings. `none` is rejected.
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--exclude-dynamic-system-prompt-sections",
    ]
    if spec.system_append:
        argv += ["--append-system-prompt", spec.system_append]
    argv += ["--max-budget-usd", format_usd(spec.max_budget_usd)]
    return argv


def format_usd(amount: float) -> str:
    """A cap for ``--max-budget-usd``: rounded down, so never above ``amount``.

    SessionSpec refuses caps below 0.0001, so this is never zero.
    """
    exact = Decimal(repr(amount)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    return str(exact)


def guard_marker(spec: SessionSpec) -> Path:
    """The file the guard writes when it starts for this session."""
    return marker_path(spec.policy_path, spec.session_id)


def guard_marker_ok(spec: SessionSpec) -> bool:
    """Whether our guard, not some other SessionStart hook, has started."""
    try:
        text = guard_marker(spec).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return text.strip() == spec.session_id


def _check_tools(spec: SessionSpec) -> None:
    allowed = TOOLS_BY_KIND[spec.kind.value]
    if set(spec.tools) != set(allowed):
        raise ValueError(
            f"a {spec.kind} session gets the tools {', '.join(allowed)}, "
            f"not {', '.join(spec.tools)}"
        )


# --- The executor -----------------------------------------------------------


class ClaudeCliExecutor:
    """Runs sessions with the user's installed Claude Code (``claude -p``)."""

    name = "claude-cli"

    def __init__(self, claude_bin: str | None = None) -> None:
        self._configured_bin = claude_bin
        self._restricted: bool | None = None

    @cached_property
    def binary(self) -> Path | None:
        return find_claude(self._configured_bin)

    @cached_property
    def _version(self) -> str | None:
        done = self._probe("--version")
        if done is None or done.returncode != 0:
            return None
        parsed = parse_version(done.stdout)
        return None if parsed is None else ".".join(str(part) for part in parsed)

    @property
    def restricted_supported(self) -> bool | None:
        """Whether this Claude Code has ``--restricted``; older ones do not.

        None when ``claude --help`` failed, timed out or crashed, so nobody
        knows. Only an answer is cached: a failed probe is tried again.
        """
        if self._restricted is None:
            done = self._probe("--help")
            if done is not None and done.returncode == 0:
                self._restricted = bool(_RESTRICTED.search(done.stdout + done.stderr))
        return self._restricted

    def version(self) -> str | None:
        return self._version

    def diagnose(self) -> list[Diagnostic]:
        binary = self.binary
        if binary is None:
            return [
                Diagnostic(
                    "Claude Code",
                    False,
                    "not found",
                    "Install Claude Code, or set [executor].bin in "
                    f"spadework.toml or {BIN_ENV} to its path.",
                )
            ]
        checks = [
            Diagnostic("Claude Code", True, str(binary)),
            self._version_check(),
            self._restricted_check(),
            self._login_check(),
        ]
        if os.environ.get("CLAUDECODE"):
            checks.append(
                Diagnostic(
                    "Nested session",
                    False,
                    "spadework is running inside a Claude Code session",
                    "Sessions started from inside Claude Code can fail to "
                    "authenticate. Run spadework from a normal terminal.",
                    required=False,
                )
            )
        return checks

    def run(self, spec: SessionSpec, events: EventSink) -> SessionResult:
        result = self._run(spec, events)
        events(Finished(result))
        return result

    def _run(self, spec: SessionSpec, events: EventSink) -> SessionResult:
        binary = self.binary
        if binary is None:
            return SessionResult(
                status="error",
                error_kind="executor",
                error=f"Claude Code was not found; set {BIN_ENV} or [executor].bin",
            )
        _check_tools(spec)
        restricted = self.restricted_supported
        if restricted is None:
            # Running without it on a guess would fail open.
            return SessionResult(
                status="error",
                error_kind="executor",
                error=(
                    "could not tell whether Claude Code supports --restricted "
                    "(`claude --help` failed); run `spadework doctor`"
                ),
            )
        if not restricted:
            events(Notice(_NO_RESTRICTED))
        argv = build_argv(binary, spec, write_settings(spec), restricted=restricted)
        # A marker left by an earlier attempt must not vouch for this one.
        guard_marker(spec).unlink(missing_ok=True)
        spec.log_path.parent.mkdir(parents=True, exist_ok=True)
        with spec.log_path.open("w", encoding="utf-8", buffering=1) as log:
            parser = StreamParser(
                expected_tools=expected_tools(spec),
                events=events,
                workdir=spec.workdir,
                log=log,
                guard_alive=lambda: guard_marker_ok(spec),
            )

            def on_line(line: str) -> LineAction:
                parser.feed(line)
                if parser.violation is not None:
                    return "kill"
                return "done" if parser.finished else "continue"

            outcome = run_process(
                argv,
                cwd=spec.workdir,
                env=child_env(),
                stdin=spec.prompt,
                timeout_s=spec.timeout_s,
                on_line=on_line,
            )
        if outcome.start_error is not None:
            return SessionResult(
                status="error",
                error_kind="executor",
                error=f"could not start Claude Code: {outcome.start_error}",
            )
        return parser.result(
            ended=outcome.ended,
            duration_s=outcome.duration_s,
            returncode=outcome.returncode,
            stderr=outcome.stderr,
        )

    def _version_check(self) -> Diagnostic:
        minimum = ".".join(str(part) for part in MIN_VERSION)
        version = self.version()
        parsed = None if version is None else parse_version(version)
        if version is None or parsed is None:
            return Diagnostic(
                "Claude Code version",
                False,
                "`claude --version` did not report a version",
                "Check that Claude Code runs: claude --version",
            )
        if parsed < MIN_VERSION:
            return Diagnostic(
                "Claude Code version",
                False,
                f"{version}, older than {minimum}",
                "Update Claude Code: claude update",
            )
        return Diagnostic("Claude Code version", True, f"{version} (minimum {minimum})")

    def _restricted_check(self) -> Diagnostic:
        restricted = self.restricted_supported
        if restricted:
            return Diagnostic("Restricted mode", True, "available")
        if restricted is None:
            return Diagnostic(
                "Restricted mode",
                False,
                "`claude --help` failed, so it is not known whether "
                "--restricted is available",
                "Check that Claude Code runs: claude --help. "
                "Sessions are refused until it does.",
            )
        return Diagnostic(
            "Restricted mode",
            False,
            "this Claude Code has no --restricted",
            _NO_RESTRICTED + ". Update Claude Code to add restricted mode.",
            required=False,
        )

    def _login_check(self) -> Diagnostic:
        hint = (
            "Run `claude` and log in. A session started from inside another "
            "Claude Code session can fail to authenticate; use a normal terminal."
        )
        done = self._probe("auth", "status")
        if done is None:
            return Diagnostic(
                "Claude Code login", False, "`claude auth status` did not run", hint
            )
        try:
            status = json.loads(done.stdout)
        except ValueError:
            status = None
        if isinstance(status, dict) and status.get("loggedIn") is True:
            return Diagnostic("Claude Code login", True, "logged in")
        return Diagnostic("Claude Code login", False, "not logged in", hint)

    def _probe(self, *args: str) -> subprocess.CompletedProcess[str] | None:
        if self.binary is None:
            return None
        try:
            return subprocess.run(
                [str(self.binary), *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=child_env(),
                timeout=PROBE_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
