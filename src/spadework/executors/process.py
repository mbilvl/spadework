"""Running an executor's command in its own process group.

See docs/spec/05-executors.md, Process control. The child gets its prompt on
stdin, and each line it writes to stdout goes to a callback as it arrives.
The callback says whether to keep going, whether the output is complete, or
whether the child must be killed at once.

However the session ends, the whole process group is killed before
``run_process`` returns, so nothing the child started outlives the session:

- a normal end: SIGKILL to the group once the leader has exited (or has had
  ``EXIT_GRACE_S`` to exit after its work was done);
- a kill requested by the callback (a sandbox violation): SIGKILL at once;
- a timeout, a Ctrl-C or an error: SIGTERM, ``TERM_GRACE_S``, then SIGKILL.
"""

from __future__ import annotations

import contextlib
import os
import selectors
import signal
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

Ended = Literal["exited", "timeout", "interrupted", "killed"]
"""How the session ended: normally, at the deadline, by Ctrl-C, or because
the callback asked for the child to be killed."""

LineAction = Literal["continue", "done", "kill"]
"""What ``on_line`` wants: more output, no more work (the output is
complete, so the child only has to exit), or an immediate kill."""

TERM_GRACE_S = 5.0
"""How long the process group gets between SIGTERM and SIGKILL."""
EXIT_GRACE_S = 10.0
"""How long the child gets to exit once its work is done or its output
has closed. A result that arrived before the deadline is not lost to it."""

_POLL_S = 0.05
_STDERR_LINES = 20
_STDERR_PARTIAL_BYTES = 4096
_READ_CHUNK = 65536
_WRITE_CHUNK = 16384
# waitid with WNOWAIT sees that the leader exited without reaping it, so its
# group id cannot be reused before the group is killed. macOS has it from
# Python 3.13; older versions reap first, which leaves a tiny window.
_WAITID: Callable[..., Any] | None = getattr(os, "waitid", None)
_PEEK_ARGS = (
    (
        os.P_PID if hasattr(os, "P_PID") else 0,
        os.WEXITED | os.WNOHANG | os.WNOWAIT,
    )
    if _WAITID is not None and hasattr(os, "WNOWAIT")
    else None
)


@dataclass(frozen=True)
class ProcessOutcome:
    ended: Ended
    returncode: int | None
    stderr: str
    """The last lines the child wrote to stderr."""
    duration_s: float
    start_error: str | None = None


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin: str,
    timeout_s: float,
    on_line: Callable[[str], LineAction],
) -> ProcessOutcome:
    """Run ``argv`` in a new process group, passing each stdout line to ``on_line``.

    A KeyboardInterrupt is not re-raised: the outcome is ``interrupted``, so
    the caller can still record the session. Any other exception stops the
    group and is re-raised.
    """
    started = time.monotonic()
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return ProcessOutcome("exited", None, "", 0.0, start_error=str(exc))
    pipes = _Pipes(proc, stdin.encode("utf-8"))
    ended: Ended
    try:
        ended = _supervise(proc, pipes, started + timeout_s, on_line)
    except KeyboardInterrupt:
        ended = "interrupted"
    except BaseException:
        _terminate(proc)
        pipes.close()
        raise
    if ended in ("timeout", "interrupted"):
        _terminate(proc)
    else:
        _kill(proc)
    pipes.drain(on_line)
    pipes.close()
    return ProcessOutcome(
        ended, proc.returncode, pipes.stderr_text(), time.monotonic() - started
    )


def _supervise(
    proc: subprocess.Popen[bytes],
    pipes: _Pipes,
    deadline: float,
    on_line: Callable[[str], LineAction],
) -> Ended:
    """Move data until the session is over, and say how it ended.

    The deadline applies only while the work is unfinished. Once the output
    is complete or the leader has exited, the rest of the group gets
    ``EXIT_GRACE_S`` to finish and close the pipes.
    """
    wind_down: float | None = None
    output_closed: float | None = None
    while True:
        now = time.monotonic()
        exited = _has_exited(proc)
        if exited and wind_down is None:
            wind_down = now + EXIT_GRACE_S
        reading = pipes.reading()
        if wind_down is not None:
            if now >= wind_down or (exited and not reading):
                return "exited"
        elif now >= deadline:
            return "timeout"
        elif not reading:
            # The leader closed its output but is still running.
            output_closed = now if output_closed is None else output_closed
            if now >= output_closed + EXIT_GRACE_S:
                return "exited"
        if not reading:
            time.sleep(_POLL_S)
            continue
        limit = (deadline if wind_down is None else wind_down) - now
        for action in pipes.pump(min(limit, _POLL_S), on_line):
            if action == "kill":
                return "killed"
            if action == "done" and wind_down is None:
                wind_down = time.monotonic() + EXIT_GRACE_S


def _has_exited(proc: subprocess.Popen[bytes]) -> bool:
    """Whether the leader has exited, without reaping it when possible."""
    if proc.returncode is not None:
        return True
    if _WAITID is None or _PEEK_ARGS is None:
        return proc.poll() is not None
    idtype, options = _PEEK_ARGS
    try:
        info = _WAITID(idtype, proc.pid, options)
    except ChildProcessError:
        return proc.poll() is not None
    return info is not None


def _kill(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the whole group, then reap the leader."""
    _signal_group(proc, signal.SIGKILL)
    _reap(proc)


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM the group, give it ``TERM_GRACE_S``, then SIGKILL it."""
    _signal_group(proc, signal.SIGTERM)
    grace_until = time.monotonic() + TERM_GRACE_S
    # A second Ctrl-C skips the grace period.
    with contextlib.suppress(KeyboardInterrupt):
        while time.monotonic() < grace_until and not _has_exited(proc):
            time.sleep(_POLL_S)
    _kill(proc)


def _reap(proc: subprocess.Popen[bytes]) -> None:
    while True:
        try:
            proc.wait()
            return
        except KeyboardInterrupt:
            continue


def _signal_group(proc: subprocess.Popen[bytes], sig: signal.Signals) -> None:
    # A group whose only member is an unreaped leader gives EPERM on macOS.
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, sig)


class _Pipes:
    """Non-blocking I/O with the child: the prompt in, lines and stderr out."""

    def __init__(self, proc: subprocess.Popen[bytes], data: bytes) -> None:
        assert proc.stdin is not None
        assert proc.stdout is not None
        assert proc.stderr is not None
        self._proc = proc
        self._selector = selectors.DefaultSelector()
        self._pending = memoryview(data)
        self._line = b""
        self._stderr: deque[str] = deque(maxlen=_STDERR_LINES)
        self._stderr_partial = b""
        for stream, role in ((proc.stdout, "out"), (proc.stderr, "err")):
            os.set_blocking(stream.fileno(), False)
            self._selector.register(stream, selectors.EVENT_READ, role)
        if data:
            os.set_blocking(proc.stdin.fileno(), False)
            self._selector.register(proc.stdin, selectors.EVENT_WRITE, "in")
        else:
            proc.stdin.close()

    def reading(self) -> bool:
        """Whether stdout or stderr is still open."""
        return any(key.data != "in" for key in self._selector.get_map().values())

    def pump(
        self, timeout: float, on_line: Callable[[str], LineAction]
    ) -> list[LineAction]:
        """Wait up to ``timeout`` for the pipes, move data, and return what
        ``on_line`` asked for."""
        actions: list[LineAction] = []
        for key, _ in self._selector.select(max(timeout, 0.0)):
            if key.data == "in":
                self._write(key.fileobj)
            else:
                actions += self._read(key.fileobj, key.data, on_line)
        return actions

    def drain(self, on_line: Callable[[str], LineAction]) -> None:
        """Pass on whatever is left in the pipes after the group was killed."""
        for key in list(self._selector.get_map().values()):
            stream: Any = key.fileobj
            if key.data in ("out", "err"):
                # Stops at the end of the output, or if a process that left
                # the group still holds the pipe open.
                with contextlib.suppress(BlockingIOError):
                    while not stream.closed:
                        self._read(stream, key.data, on_line)
        if self._line:
            on_line(self._line.decode("utf-8", errors="replace"))
            self._line = b""

    def close(self) -> None:
        for key in list(self._selector.get_map().values()):
            self._selector.unregister(key.fileobj)
        self._selector.close()
        for stream in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()

    def stderr_text(self) -> str:
        lines = list(self._stderr)
        if self._stderr_partial:
            lines.append(self._stderr_partial.decode("utf-8", errors="replace"))
        return "\n".join(lines)

    def _write(self, stream: Any) -> None:
        try:
            written = os.write(stream.fileno(), self._pending[:_WRITE_CHUNK])
        except BlockingIOError:
            return
        except BrokenPipeError:
            written = len(self._pending)
        self._pending = self._pending[written:]
        if not self._pending:
            self._selector.unregister(stream)
            with contextlib.suppress(BrokenPipeError):
                stream.close()

    def _read(
        self, stream: Any, role: str, on_line: Callable[[str], LineAction]
    ) -> list[LineAction]:
        chunk = os.read(stream.fileno(), _READ_CHUNK)
        if not chunk:
            self._selector.unregister(stream)
            stream.close()
            if role == "out" and self._line:
                line, self._line = self._line, b""
                return [on_line(line.decode("utf-8", errors="replace"))]
            return []
        if role == "err":
            *lines, partial = (self._stderr_partial + chunk).split(b"\n")
            self._stderr.extend(
                line.decode("utf-8", errors="replace") for line in lines
            )
            self._stderr_partial = partial[-_STDERR_PARTIAL_BYTES:]
            return []
        *lines, self._line = (self._line + chunk).split(b"\n")
        return [
            on_line(line.decode("utf-8", errors="replace") + "\n") for line in lines
        ]
