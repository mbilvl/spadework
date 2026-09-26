"""Reading Claude Code's ``stream-json`` output.

See docs/spec/05-executors.md, Liveness checks and Parsing. The parser is fed
one line at a time and knows nothing about processes, so recorded logs can
be replayed through it exactly as a live session is read.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, Any

from spadework.executors.base import (
    Denied,
    EventSink,
    RateLimit,
    SessionResult,
    Text,
    ToolCall,
    Usage,
    ignore_events,
)
from spadework.executors.process import Ended
from spadework.sandbox.tools import STRUCTURED_OUTPUT

_TARGET_CHARS = 80
_SNIPPET_CHARS = 120
_ERROR_CHARS = 300
# resetsAt is in seconds; a value this large (the year 5138 in seconds) can
# only be milliseconds.
_MILLISECONDS = 10**11
# Only a fallback: rate limits are read from structured fields first.
_RATE_LIMIT_TEXT = re.compile(r"\b429\b|rate[ _-]?limit|usage limit", re.IGNORECASE)


@dataclass
class _Call:
    name: str
    target: str
    finished: bool = False
    """Its tool result has arrived."""


class StreamParser:
    """Reads Claude Code's ``stream-json`` output, one line at a time.

    It writes every line to ``log``, turns the events it understands into
    normalised events, and builds the ``SessionResult``. It also checks that
    the sandbox is really in place, because ``-p`` silently ignores a
    settings file it cannot read and drops tool names it does not know:

    - a successful ``SessionStart`` hook response must arrive before
      ``init``, and ``guard_alive`` must confirm that it came from our guard;
    - ``init`` must come before anything the model does, and list exactly
      the expected tools;
    - every tool call that runs must have the guard's permission: each
      successful tool result uses up one "allow" answer for that tool. A
      result that is an error never ran, so it needs none. An answer that is
      not a decision (the guard crashed or could not start) is a violation.

    When a check fails, ``violation`` is set and the caller must stop the
    session at once. Unknown events and lines that are not JSON are ignored.
    """

    def __init__(
        self,
        *,
        expected_tools: Iterable[str] | None,
        events: EventSink = ignore_events,
        workdir: Path | None = None,
        log: IO[str] | None = None,
        require_guard: bool = True,
        guard_alive: Callable[[], bool] | None = None,
    ) -> None:
        self.expected_tools = (
            None if expected_tools is None else frozenset(expected_tools)
        )
        self.events = events
        self.workdir = workdir
        self.log = log
        self.require_guard = require_guard
        self.guard_alive = guard_alive
        self.violation: str | None = None
        self.malformed_lines = 0
        self._guard_started = False
        self._guard_start_failure: str | None = None
        self._init_seen = False
        self._started = False
        self._model_seen = False
        self._calls: dict[str, _Call] = {}
        self._tool_calls: Counter[str] = Counter()
        self._allowed: Counter[str] = Counter()
        self._refusals: dict[str, list[str]] = {}
        self._denied: set[str] = set()
        self._denials = 0
        self._rate_limited = False
        self._rate_limit_reset: dt.datetime | None = None
        self._result: dict[str, Any] | None = None

    @property
    def finished(self) -> bool:
        """A result has arrived, so the session's work is over."""
        return self._result is not None

    def feed(self, line: str) -> None:
        if self.log is not None:
            self.log.write(line if line.endswith("\n") else line + "\n")
        text = line.strip()
        if not text:
            return
        try:
            event = json.loads(text)
        except ValueError:
            self.malformed_lines += 1
            return
        if not isinstance(event, dict):
            self.malformed_lines += 1
            return
        if self.violation is not None:
            return
        kind = event.get("type")
        if kind in ("assistant", "user", "result") and not self._init_seen:
            self._model_seen = True  # so the cost is not known
            self._fail(
                f"Claude Code sent '{kind}' before 'init', "
                "so the sandbox could not be checked"
            )
            return
        if kind == "system":
            subtype = event.get("subtype")
            if subtype == "hook_response":
                self._hook_response(event)
            elif subtype == "init":
                self._init(event)
            elif subtype == "permission_denied":
                self._permission_denied(event)
        elif kind == "assistant":
            self._assistant(event)
        elif kind == "user":
            self._user(event)
        elif kind == "rate_limit_event":
            self._rate_limit(event)
        elif kind == "result":
            self._result = event

    def result(
        self,
        *,
        ended: Ended = "exited",
        duration_s: float = 0.0,
        returncode: int | None = None,
        stderr: str = "",
    ) -> SessionResult:
        """The session's result, once the stream has ended."""
        self._flush_refusals()
        base = SessionResult(
            status="error",
            duration_s=duration_s,
            tool_calls=dict(self._tool_calls),
            rate_limit_reset=self._rate_limit_reset,
            guard_denials=self._denials,
            # Stopped before init passed and before any model output: no
            # model call has been made.
            cost_known=not (self._started or self._model_seen),
        )
        event = self._result
        missing = None if event is None else _missing_result_field(event)
        if event is not None and missing is None:
            base = _with_accounts(base, event)

        if self.violation is not None:
            return replace(base, error_kind="sandbox", error=self.violation)
        if ended == "interrupted":
            return replace(base, status="interrupted", error="interrupted")
        if (event is None or missing is not None) and ended == "timeout":
            return replace(
                base,
                status="timeout",
                error="the session did not finish within its time limit",
            )
        if event is None:
            if self._rate_limited:
                return replace(base, status="rate_limited", error="rate limited")
            if not self._init_seen and returncode is not None and returncode > 0:
                # A usage error, such as an option this version lacks.
                return replace(
                    base,
                    error_kind="executor",
                    error=_exit_message(returncode, stderr, started=False),
                )
            return replace(
                base,
                error_kind="transient",
                error=_exit_message(returncode, stderr, started=self._init_seen),
            )
        if missing is not None:
            return replace(
                base,
                error_kind="executor",
                error=(
                    f"Claude Code's result has no valid '{missing}'; "
                    "this version may not be supported"
                ),
            )
        return _classify(base, event, rate_limited=self._rate_limited)

    # -- events --

    def _fail(self, reason: str) -> None:
        if self.violation is None:
            self.violation = reason

    def _init(self, event: dict[str, Any]) -> None:
        self._init_seen = True
        if self.require_guard:
            if not self._guard_started:
                reason = "the sandbox guard did not start"
                if self._guard_start_failure:
                    reason += f" ({self._guard_start_failure})"
                self._fail(reason)
                return
            if self.guard_alive is not None and not self.guard_alive():
                # A SessionStart hook answered, but not our guard.
                self._fail("the sandbox guard did not start")
                return
        tools = event.get("tools")
        if self.expected_tools is not None:
            names = (
                {tool for tool in tools if isinstance(tool, str)}
                if isinstance(tools, list)
                else set()
            )
            if names != self.expected_tools:
                missing = sorted(self.expected_tools - names)
                extra = sorted(names - self.expected_tools)
                parts = []
                if missing:
                    parts.append("missing " + ", ".join(missing))
                if extra:
                    parts.append("unexpected " + ", ".join(extra))
                self._fail(
                    "Claude Code did not load the expected tools ("
                    + "; ".join(parts)
                    + ")"
                )
                return
        self._started = True

    def _hook_response(self, event: dict[str, Any]) -> None:
        hook_name = str(event.get("hook_name", ""))
        hook = event.get("hook_event") or hook_name.partition(":")[0]
        raw_code = event.get("exit_code")
        # Only a real integer is an exit code: false is not 0.
        code = raw_code if type(raw_code) is int else None
        detail = _first_line(event.get("stderr"))
        if hook == "SessionStart":
            if code == 0:
                self._guard_started = True
            elif self._guard_start_failure is None:
                self._guard_start_failure = detail or f"exit code {raw_code!r}"
            return
        if hook != "PreToolUse":
            return
        name = hook_name.partition(":")[2]
        # The guard answers with exit code 0 (allow, or a deny in JSON) or 2
        # (deny). Anything else means it crashed or could not start, and
        # Claude Code may let the call through.
        if code not in (0, 2):
            what = f"a {name} call" if name else "a tool call"
            suffix = f": {detail}" if detail else ""
            self._fail(
                f"the sandbox guard failed on {what} (exit code {raw_code!r}){suffix}"
            )
            return
        reason = (detail or "denied by the guard") if code == 2 else _json_deny(event)
        if reason is None:
            self._allowed[name] += 1
        else:
            self._refusals.setdefault(name, []).append(reason)

    def _permission_denied(self, event: dict[str, Any]) -> None:
        call_id = event.get("tool_use_id")
        call_id = call_id if isinstance(call_id, str) else None
        call = self._calls.get(call_id) if call_id is not None else None
        name = call.name if call else str(event.get("tool_name", ""))
        reason = self._take_refusal(name, "") or "denied by the permission mode"
        self._deny(call_id, name, call.target if call else "", reason)

    def _assistant(self, event: dict[str, Any]) -> None:
        self._model_seen = True
        if event.get("error") == "rate_limit":
            self._rate_limited = True
        for block in _content(event):
            kind = block.get("type")
            if kind == "text":
                snippet = _snippet(block.get("text"))
                if snippet:
                    self.events(Text(snippet))
            elif kind == "tool_use":
                call_id, name = block.get("id"), block.get("name")
                if not isinstance(call_id, str) or not isinstance(name, str):
                    continue
                if call_id in self._calls:
                    continue
                tool_input = block.get("input")
                target = describe_target(
                    name,
                    tool_input if isinstance(tool_input, dict) else {},
                    self.workdir,
                )
                self._calls[call_id] = _Call(name, target)
                if name != STRUCTURED_OUTPUT:
                    self._tool_calls[name] += 1
                    self.events(ToolCall(name, target))

    def _user(self, event: dict[str, Any]) -> None:
        for block in _content(event):
            if block.get("type") != "tool_result":
                continue
            call_id = block.get("tool_use_id")
            call = self._calls.get(call_id) if isinstance(call_id, str) else None
            if call is not None and call.finished:
                continue
            if call is not None:
                call.finished = True
            if block.get("is_error") is True:
                # The call never ran: an unknown tool, bad input, or a denial.
                if call is not None and isinstance(call_id, str):
                    reason = self._take_refusal(call.name, _result_text(block))
                    if reason is not None:
                        self._deny(call_id, call.name, call.target, reason)
                continue
            if not self.require_guard:
                continue
            if call is None:
                self._fail("a tool call that Claude Code did not announce ran")
            elif self._allowed[call.name] > 0:
                self._allowed[call.name] -= 1
            else:
                self._fail(f"a {call.name} call ran without an answer from the guard")

    def _rate_limit(self, event: dict[str, Any]) -> None:
        info = event.get("rate_limit_info")
        if not isinstance(info, dict):
            return
        status = info.get("status")
        if not isinstance(status, str):
            return
        resets_at = _from_epoch(info.get("resetsAt"))
        self.events(RateLimit(status, resets_at))
        if status == "rejected":
            self._rate_limited = True
            if resets_at is not None:
                self._rate_limit_reset = resets_at

    # -- denials --

    def _take_refusal(self, name: str, text: str) -> str | None:
        """The guard's reason for denying a ``name`` call, preferring the one
        quoted in the call's result."""
        queue = self._refusals.get(name)
        if not queue:
            return None
        for index, reason in enumerate(queue):
            if text and reason in text:
                return queue.pop(index)
        return queue.pop(0)

    def _flush_refusals(self) -> None:
        """Report denials whose calls never got a result."""
        for name, queue in self._refusals.items():
            while queue:
                reason = queue.pop(0)
                call_id, call = next(
                    (
                        (call_id, call)
                        for call_id, call in self._calls.items()
                        if call.name == name
                        and not call.finished
                        and call_id not in self._denied
                    ),
                    (None, None),
                )
                self._deny(call_id, name, call.target if call else "", reason)

    def _deny(self, call_id: str | None, name: str, target: str, reason: str) -> None:
        if call_id is not None:
            if call_id in self._denied:
                return
            self._denied.add(call_id)
        self._denials += 1
        self.events(Denied(name, target, _shorten(reason, _ERROR_CHARS)))


def _classify(
    base: SessionResult, event: dict[str, Any], *, rate_limited: bool
) -> SessionResult:
    """Map a complete result event to a SessionResult."""
    subtype = event["subtype"]
    succeeded = subtype == "success" and event["is_error"] is False
    structured = event.get("structured_output")
    message = _error_text(event)
    api_status = event.get("api_error_status")
    if subtype == "error_max_budget_usd":
        return replace(base, status="budget", error=message or "reached the budget cap")
    # A session that finished keeps its result even if a rate limit was hit
    # on the way; rate_limit_reset still tells the engine about it.
    if succeeded and isinstance(structured, dict):
        return replace(base, status="ok", structured=structured)
    if rate_limited or api_status == 429:
        return replace(base, status="rate_limited", error=message or "rate limited")
    if subtype == "error_max_structured_output_retries":
        return replace(
            base,
            error_kind="schema",
            error=message or "no valid structured result after retries",
        )
    if succeeded:
        return replace(
            base,
            error_kind="schema",
            error="the session ended without a structured result",
        )
    if type(api_status) is int and api_status >= 500:
        # The API is overloaded or failing: worth another try.
        return replace(
            base,
            error_kind="transient",
            error=message or f"API error {api_status}",
        )
    if _RATE_LIMIT_TEXT.search(message):
        return replace(base, status="rate_limited", error=message)
    return replace(
        base,
        error_kind="executor",
        error=message or f"Claude Code reported an error ({subtype})",
    )


def replay(
    lines: Iterable[str],
    *,
    expected_tools: Iterable[str] | None,
    events: EventSink = ignore_events,
    workdir: Path | None = None,
    log: IO[str] | None = None,
    require_guard: bool = True,
) -> SessionResult:
    """Feed a recorded ``stream-json`` log through the parser, as a live
    session would. Stops where the real executor would stop the process."""
    parser = StreamParser(
        expected_tools=expected_tools,
        events=events,
        workdir=workdir,
        log=log,
        require_guard=require_guard,
    )
    for line in lines:
        parser.feed(line)
        if parser.violation is not None:
            break
    return parser.result()


def describe_target(
    name: str, tool_input: Mapping[str, Any], workdir: Path | None = None
) -> str:
    """What a tool call is about, shortened for the live view."""
    if name == "WebSearch":
        value = tool_input.get("query")
    elif name == "WebFetch":
        value = tool_input.get("url")
    elif name in ("Glob", "Grep"):
        value = tool_input.get("pattern")
    else:
        value = next(
            (
                tool_input[key]
                for key in ("file_path", "notebook_path", "path")
                if isinstance(tool_input.get(key), str)
            ),
            None,
        )
        if isinstance(value, str) and workdir is not None:
            value = _relative(value, workdir)
    return (
        _shorten(" ".join(value.split()), _TARGET_CHARS)
        if isinstance(value, str)
        else ""
    )


def _relative(path: str, workdir: Path) -> str:
    for base in (workdir, workdir.resolve()):
        with contextlib.suppress(ValueError):
            return str(Path(path).relative_to(base))
    return path


def _content(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [block for block in content if isinstance(block, dict)]


def _result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return ""


def _json_deny(event: dict[str, Any]) -> str | None:
    """The reason, if a hook that exited 0 denied the call in its JSON."""
    try:
        answer = json.loads(str(event.get("stdout") or "").strip() or "null")
    except ValueError:
        return None
    output = answer.get("hookSpecificOutput") if isinstance(answer, dict) else None
    if isinstance(output, dict) and output.get("permissionDecision") == "deny":
        reason = output.get("permissionDecisionReason")
        return reason if isinstance(reason, str) and reason else "denied by the guard"
    return None


def _missing_result_field(event: dict[str, Any]) -> str | None:
    if not isinstance(event.get("subtype"), str):
        return "subtype"
    if not isinstance(event.get("is_error"), bool):
        return "is_error"
    if _number(event.get("total_cost_usd")) is None:
        return "total_cost_usd"
    return None


def _with_accounts(base: SessionResult, event: dict[str, Any]) -> SessionResult:
    duration_ms = _number(event.get("duration_ms"))
    denials = event.get("permission_denials")
    return replace(
        base,
        cost_usd=float(event["total_cost_usd"]),
        cost_known=True,
        usage=_usage(event.get("usage")),
        duration_s=base.duration_s if duration_ms is None else duration_ms / 1000,
        guard_denials=len(denials) if isinstance(denials, list) else base.guard_denials,
    )


def _usage(raw: Any) -> Usage:
    usage = raw if isinstance(raw, dict) else {}
    server = usage.get("server_tool_use")
    server = server if isinstance(server, dict) else {}

    def count(source: dict[str, Any], key: str) -> int:
        value = source.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        return 0

    return Usage(
        input_tokens=count(usage, "input_tokens"),
        output_tokens=count(usage, "output_tokens"),
        cache_read_tokens=count(usage, "cache_read_input_tokens"),
        cache_creation_tokens=count(usage, "cache_creation_input_tokens"),
        web_search_requests=count(server, "web_search_requests"),
        web_fetch_requests=count(server, "web_fetch_requests"),
    )


def _number(value: Any) -> float | None:
    """A non-negative JSON number, or None."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return None
    return float(value)


def _error_text(event: dict[str, Any]) -> str:
    errors = event.get("errors")
    if isinstance(errors, list) and errors:
        text = "; ".join(str(error) for error in errors)
    elif event.get("is_error") and isinstance(event.get("result"), str):
        text = event["result"]
    else:
        return ""
    return _shorten(" ".join(text.split()), _ERROR_CHARS)


def _exit_message(returncode: int | None, stderr: str, *, started: bool) -> str:
    message = (
        "Claude Code exited without a result"
        if started
        else "Claude Code exited before the session started"
    )
    if returncode not in (None, 0):
        message += f" (exit code {returncode})"
    last = next((line for line in reversed(stderr.splitlines()) if line.strip()), "")
    if last:
        message += f": {_shorten(last.strip(), _ERROR_CHARS)}"
    return message


def _from_epoch(value: Any) -> dt.datetime | None:
    seconds = _number(value)
    if seconds is None:
        return None
    if seconds > _MILLISECONDS:
        seconds /= 1000
    try:
        return dt.datetime.fromtimestamp(seconds, dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _first_line(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return next((line.strip() for line in value.splitlines() if line.strip()), "")


def _snippet(value: Any) -> str:
    return _shorten(" ".join(_first_line(value).split()), _SNIPPET_CHARS)


def _shorten(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"
