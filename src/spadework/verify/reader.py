"""Reading a page's text in a separate process, under time and memory limits.

Decoding, parsing and normalising a page is CPU work on untrusted input, and
html.parser is quadratic on some pages in older Pythons (gh-135462). So the
verifier runs `python -I -m spadework.verify.reader`, writes the page on
stdin, and reads back JSON: the normalised text, cut to `MAX_TEXT_CHARS`,
and whether the page seems to need JavaScript. The parent kills the child
after `READ_TIMEOUT_S`; the child also caps its own CPU time and, where the
OS enforces it (Linux), its memory.

On stdin: one line of JSON, `{"kind": "html" | "text", "charset": str |
null}`, then the body. On stdout: `{"text": str, "cut": bool,
"needs_javascript": bool}`.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from spadework.verify.fetch import MAX_BYTES

if TYPE_CHECKING:
    from collections.abc import Callable

    from spadework.verify.fetch import Page

    # Reads a page within a time limit in seconds; None if it could not.
    Reader = Callable[[Page, float], "PageRead | None"]

READ_TIMEOUT_S = 10.0
MAX_TEXT_CHARS = 4_000_000
_CPU_S = 10
_MEMORY_BYTES = 1 << 30
_MAX_HEADER_BYTES = 1024
_MAX_CHARSET_CHARS = 64


@dataclass(frozen=True)
class PageRead:
    """What the verifier keeps of a page: its normalised text."""

    text: str  # normalised (match.normalise), at most MAX_TEXT_CHARS
    cut: bool  # the text was longer, and the rest was dropped
    needs_javascript: bool


def read_page(page: Page, timeout: float = READ_TIMEOUT_S) -> PageRead | None:
    """Read a page's text in a child process. None if it took longer than
    `timeout`, or the child failed for any reason."""
    if not sys.executable:
        return None
    header = json.dumps({"kind": page.kind, "charset": page.charset})
    try:
        done = subprocess.run(
            [sys.executable, "-I", "-m", "spadework.verify.reader"],
            input=header.encode("utf-8") + b"\n" + page.body,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        # subprocess.run has already killed and reaped a child that ran late.
        return None
    if done.returncode != 0:
        return None
    return _parse_output(done.stdout)


def read_in_process(page: Page, timeout: float = READ_TIMEOUT_S) -> PageRead | None:
    """Read a page's text in this process, with no limits. For tests."""
    return _read(page.kind, page.charset, page.body)


def _read(kind: str, charset: str | None, body: bytes) -> PageRead:
    from spadework.verify.match import normalise
    from spadework.verify.text import page_text

    text = page_text(body, "html" if kind == "html" else "text", charset)
    normalised = normalise(text.text)
    return PageRead(
        normalised[:MAX_TEXT_CHARS],
        len(normalised) > MAX_TEXT_CHARS,
        text.needs_javascript(),
    )


def _parse_output(data: bytes) -> PageRead | None:
    try:
        output: Any = json.loads(data)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(output, dict):
        return None
    text, cut, needs_javascript = (
        output.get("text"),
        output.get("cut"),
        output.get("needs_javascript"),
    )
    if (
        not isinstance(text, str)
        or len(text) > MAX_TEXT_CHARS
        or not isinstance(cut, bool)
        or not isinstance(needs_javascript, bool)
    ):
        return None
    return PageRead(text, cut, needs_javascript)


def _limit_resources() -> None:
    """Cap this process's CPU time and memory, as far as the OS allows."""
    try:
        import resource
    except ImportError:  # not on POSIX
        return
    limits = [(resource.RLIMIT_CPU, _CPU_S)]
    if hasattr(resource, "RLIMIT_AS"):
        limits.append((resource.RLIMIT_AS, _MEMORY_BYTES))
    for which, value in limits:
        try:
            _soft, hard = resource.getrlimit(which)
            if hard != resource.RLIM_INFINITY:
                value = min(value, hard)
            resource.setrlimit(which, (value, value))
        except (ValueError, OSError):
            continue  # macOS does not let a process set RLIMIT_AS


def main() -> int:
    """The child: read a page from stdin, write its text to stdout."""
    _limit_resources()
    request = json.loads(sys.stdin.buffer.readline(_MAX_HEADER_BYTES))
    kind, charset = request["kind"], request["charset"]
    if kind not in ("html", "text"):
        return 2
    if charset is not None and (
        not isinstance(charset, str) or len(charset) > _MAX_CHARSET_CHARS
    ):
        charset = None
    body = sys.stdin.buffer.read(MAX_BYTES)
    read = _read(kind, charset, body)
    output = {
        "text": read.text,
        "cut": read.cut,
        "needs_javascript": read.needs_javascript,
    }
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
