"""A local stand-in for the Anthropic Messages API.

It lets the integration tests run the real ``claude`` binary without calling
a model or spending anything. The CLI is pointed at it with
``ANTHROPIC_BASE_URL`` and a placeholder ``ANTHROPIC_API_KEY``.

It plays a scripted model. Each request in the main conversation (one that
offers tools) is answered by ``MockApi.respond``, a function of the request,
so a script can read the prompt and the tool results it gets back. Requests
without tools, which the CLI makes for its own purposes, get a short text
answer. Every request is recorded, so a test can check exactly what reached
the "model". The API key is never stored: only whether it was the
placeholder, and whether an ``Authorization`` header was sent at all.

The CLI prices each call from the ``usage`` a step reports, so a step with a
large ``input_tokens`` makes the session expensive.
"""

from __future__ import annotations

import itertools
import json
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

PLACEHOLDER_KEY = "spadework-test-placeholder-not-a-key"

DEFAULT_USAGE = {
    "input_tokens": 50,
    "output_tokens": 10,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


@dataclass
class ApiRequest:
    """One request the CLI sent, without its credentials."""

    method: str
    path: str
    body: dict[str, Any]
    headers: dict[str, str]
    """Every header except ``x-api-key`` and ``authorization``."""
    api_key_is_placeholder: bool
    authorization_sent: bool

    @property
    def is_messages(self) -> bool:
        return (
            self.method == "POST"
            and self.path.startswith("/v1/messages")
            and "count_tokens" not in self.path
        )

    @property
    def tools(self) -> list[str]:
        tools = self.body.get("tools") or []
        return [str(tool.get("name")) for tool in tools if isinstance(tool, dict)]

    @property
    def is_main(self) -> bool:
        """A turn of the session's conversation: it offers the session's tools.

        A web search is run as a request of its own that offers only the
        server's search tool; it is not a turn.
        """
        tools = self.body.get("tools") or []
        server = [
            tool
            for tool in tools
            if isinstance(tool, dict) and str(tool.get("type", "")).startswith("web_")
        ]
        return self.is_messages and bool(tools) and len(server) < len(tools)

    @property
    def turn(self) -> int:
        """How many times the model has answered in this conversation so far."""
        messages = self.body.get("messages") or []
        return sum(1 for message in messages if message.get("role") == "assistant")

    @property
    def prompt(self) -> str:
        """The text of the first user message: the session's prompt."""
        messages = self.body.get("messages") or []
        return _text(messages[0].get("content")) if messages else ""

    def tool_results(self) -> list[dict[str, Any]]:
        """The tool results in the last user message."""
        messages = self.body.get("messages") or []
        if not messages or messages[-1].get("role") != "user":
            return []
        content = messages[-1].get("content")
        if not isinstance(content, list):
            return []
        return [
            block
            for block in content
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]

    @property
    def everything(self) -> str:
        """All the model sees: system prompt, tools and messages, as JSON."""
        return json.dumps(self.body, ensure_ascii=False)


Step = dict[str, Any]
Responder = Callable[[ApiRequest], Step]


def tool_use(name: str, tool_input: dict[str, Any], **extra: Any) -> Step:
    """A model turn that calls one tool."""
    block = {"type": "tool_use", "name": name, "input": tool_input}
    return {"content": [block], **extra}


def text(value: str, **extra: Any) -> Step:
    """A model turn that only says something, which ends the conversation."""
    return {"content": [{"type": "text", "text": value}], **extra}


def steps(*turns: Step, repeat_last: bool = False) -> Responder:
    """A model that answers each turn of the conversation with the next step."""

    def respond(request: ApiRequest) -> Step:
        index = request.turn
        if index < len(turns):
            return turns[index]
        if repeat_last and turns:
            return turns[-1]
        return text("Done.")

    return respond


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


@dataclass
class MockApi:
    """A Messages API on 127.0.0.1 that plays ``respond``. Use it as a context
    manager, or call ``start`` and ``close``."""

    respond: Responder = field(default_factory=lambda: steps())
    requests: list[ApiRequest] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    """Exceptions raised by ``respond``, so a broken script fails the test."""

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        assert self._server is not None, "the mock API is not running"
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}"

    @property
    def main_requests(self) -> list[ApiRequest]:
        return [request for request in self.requests if request.is_main]

    @property
    def message_requests(self) -> list[ApiRequest]:
        return [request for request in self.requests if request.is_messages]

    def reset(self, respond: Responder | None = None) -> None:
        with self._lock:
            self.requests.clear()
            self.errors.clear()
            if respond is not None:
                self.respond = respond

    def start(self) -> MockApi:
        handler = type("Handler", (_Handler,), {"api": self})
        self._server = _Server(("127.0.0.1", 0), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="mock-api", daemon=True
        )
        self._thread.start()
        return self

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._server = self._thread = None

    def __enter__(self) -> MockApi:
        return self.start()

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _record(self, request: ApiRequest) -> None:
        with self._lock:
            self.requests.append(request)

    def _answer(self, request: ApiRequest) -> Step:
        if not request.is_main:
            return text("ok")
        try:
            return self.respond(request)
        except Exception as exc:  # reported by the test, not lost in a thread
            with self._lock:
                self.errors.append(f"{type(exc).__name__}: {exc}")
            return text("The mock's script failed.")

    def _tool_id(self) -> str:
        return f"toolu_mock_{next(self._ids):06d}"


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        # The executor kills Claude Code's process group once a session is
        # over, which drops its keep-alive connections: not worth a traceback.
        if isinstance(sys.exc_info()[1], ConnectionError):
            return
        super().handle_error(request, client_address)


class _Handler(BaseHTTPRequestHandler):
    api: MockApi
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    def do_HEAD(self) -> None:
        self._json(200, {})

    def do_GET(self) -> None:
        self.api._record(self._request({}))
        self._json(404, _error("not_found_error", "not in the mock"))

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"unparsed": raw[:2000].decode("utf-8", errors="replace")}
        if not isinstance(body, dict):
            body = {"unparsed": body}
        request = self._request(body)
        self.api._record(request)
        if "count_tokens" in self.path:
            self._json(200, {"input_tokens": 100})
            return
        if not request.is_messages:
            self._json(404, _error("not_found_error", "not in the mock"))
            return
        step = self.api._answer(request)
        if "status" in step:
            error = step.get("error") or _error("api_error", "mock error")
            self._json(int(step["status"]), error, step.get("headers"))
            return
        self._message(body, step)

    def _request(self, body: dict[str, Any]) -> ApiRequest:
        secret = {"x-api-key", "authorization"}
        headers = {
            key.lower(): value
            for key, value in self.headers.items()
            if key.lower() not in secret
        }
        return ApiRequest(
            method=self.command,
            path=self.path,
            body=body,
            headers=headers,
            api_key_is_placeholder=self.headers.get("x-api-key") == PLACEHOLDER_KEY,
            authorization_sent=self.headers.get("authorization") is not None,
        )

    def _json(
        self, status: int, document: Any, headers: dict[str, str] | None = None
    ) -> None:
        data = json.dumps(document).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.send_header("request-id", "req_mock")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _message(self, body: dict[str, Any], step: Step) -> None:
        usage = {**DEFAULT_USAGE, **step.get("usage", {})}
        content = []
        for block in step["content"]:
            block = dict(block)
            if block["type"] == "tool_use":
                block.setdefault("id", self.api._tool_id())
            content.append(block)
        stop = step.get("stop_reason") or (
            "tool_use" if content and content[-1]["type"] == "tool_use" else "end_turn"
        )
        message = {
            "id": f"msg_mock_{self.api._tool_id()[-6:]}",
            "type": "message",
            "role": "assistant",
            "model": body.get("model", "mock-model"),
            "content": content,
            "stop_reason": stop,
            "stop_sequence": None,
            "usage": usage,
        }
        if not body.get("stream"):
            self._json(200, message, step.get("headers"))
            return
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("request-id", "req_mock")
        for key, value in (step.get("headers") or {}).items():
            self.send_header(key, value)
        self.send_header("connection", "close")
        self.end_headers()
        self.close_connection = True

        def event(name: str, data: dict[str, Any]) -> None:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()

        started = {**message, "content": [], "stop_reason": None}
        started["usage"] = {**usage, "output_tokens": 1}
        event("message_start", {"type": "message_start", "message": started})
        for index, block in enumerate(content):
            if block["type"] == "text":
                start = {"type": "text", "text": ""}
                delta = {"type": "text_delta", "text": block["text"]}
            else:
                start = {**block, "input": {}}
                delta = {
                    "type": "input_json_delta",
                    "partial_json": json.dumps(block["input"]),
                }
            event(
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": start},
            )
            event(
                "content_block_delta",
                {"type": "content_block_delta", "index": index, "delta": delta},
            )
            event("content_block_stop", {"type": "content_block_stop", "index": index})
        event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": usage["output_tokens"]},
            },
        )
        event("message_stop", {"type": "message_stop"})


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}
