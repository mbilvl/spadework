import json
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from spadework import lock as lock_module
from spadework.errors import ExitCode
from spadework.lock import LockedError, LockOwner, ProjectLock, read_lock

HOST = socket.gethostname()
STARTED = "2026-09-27T21:04:11Z"


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / ".spadework" / "lock"


def write_lock(path: Path, pid: int, host: str = HOST) -> bytes:
    data = LockOwner(pid, host, STARTED).to_bytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data


def dead_pid() -> int:
    """The pid of a process that has exited."""
    done = subprocess.run(
        [sys.executable, "-c", "import os; print(os.getpid())"],
        capture_output=True,
        text=True,
        check=True,
    )
    return int(done.stdout)


def test_the_lock_names_this_process(path: Path) -> None:
    with ProjectLock(path) as held:
        assert held.warning is None
        document = json.loads(path.read_text())
        assert document["pid"] == os.getpid()
        assert document["host"] == HOST
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", document["started_at"])
        assert read_lock(path) == held.owner
    assert not path.exists()
    assert read_lock(path) is None


def test_the_lock_is_released_on_an_error(path: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"), ProjectLock(path):
        raise RuntimeError("boom")
    assert not path.exists()


def test_a_live_run_holds_the_lock(path: Path) -> None:
    # The parent of this process is alive for as long as the test runs.
    data = write_lock(path, os.getppid())

    with pytest.raises(LockedError) as caught:
        ProjectLock(path).acquire()

    error = caught.value
    assert error.exit_code == ExitCode.LOCKED == 7
    assert error.message == (
        f"another run holds the project lock (pid {os.getppid()} on host {HOST}, "
        f"started {STARTED})"
    )
    assert "`spadework status`" in error.hint
    assert "delete .spadework/lock" in error.hint
    assert path.read_bytes() == data


def test_a_second_lock_in_this_process_is_refused(path: Path) -> None:
    with ProjectLock(path):
        with pytest.raises(LockedError, match=f"pid {os.getpid()}"):
            ProjectLock(path).acquire()
        assert path.exists()
    assert not path.exists()


def test_a_lock_from_another_host_is_never_reclaimed(path: Path) -> None:
    data = write_lock(path, dead_pid(), host="build-server")

    with pytest.raises(LockedError) as caught:
        ProjectLock(path).acquire()

    assert "on host build-server" in caught.value.message
    assert caught.value.hint.startswith(
        "The lock was taken on another computer (build-server)."
    )
    assert path.read_bytes() == data


def test_a_dead_run_s_lock_is_reclaimed_with_a_warning(path: Path) -> None:
    pid = dead_pid()
    write_lock(path, pid)

    with ProjectLock(path) as held:
        assert held.warning == (
            "reclaimed the project lock left by a run that is no longer running "
            f"(pid {pid} on host {HOST}, started {STARTED})"
        )
        assert read_lock(path) == held.owner
    assert not path.exists()


def test_acquire_returns_the_warning(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_lock(path, 4242)
    monkeypatch.setattr(lock_module, "_running", lambda pid: False)
    held = ProjectLock(path)
    warning = held.acquire()
    assert warning is not None
    assert "pid 4242" in warning
    held.release()


def test_our_own_pid_left_by_an_earlier_process_is_reclaimed(path: Path) -> None:
    # In a container, each run can start with the same pid.
    write_lock(path, os.getpid())
    with ProjectLock(path) as held:
        assert held.warning is not None
        assert f"pid {os.getpid()}" in held.warning


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"not json",
        b"[]",
        b'{"pid": 1}',
        b'{"pid": "1", "host": "h", "started_at": "s"}',
        b'{"pid": true, "host": "h", "started_at": "s"}',
        b'{"pid": 0, "host": "h", "started_at": "s"}',
        b'{"pid": 1, "host": "h", "started_at": "s", "extra": 1}',
        b"\xff\xfe",
    ],
)
def test_a_corrupt_lock_is_left_alone(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True)
    path.write_bytes(content)

    with pytest.raises(LockedError) as caught:
        ProjectLock(path).acquire()

    assert caught.value.exit_code == ExitCode.LOCKED
    assert caught.value.message == (
        "the project lock .spadework/lock is unreadable, so spadework cannot "
        "tell whether another run holds it"
    )
    assert caught.value.hint == (
        "If no other spadework run is active in this project, delete "
        ".spadework/lock and try again."
    )
    assert path.read_bytes() == content
    assert read_lock(path) is None


def test_release_leaves_a_lock_that_is_not_ours(path: Path) -> None:
    held = ProjectLock(path)
    held.acquire()
    other = write_lock(path, os.getppid())

    held.release()
    held.release()

    assert path.read_bytes() == other


def test_a_lock_cannot_be_taken_twice(path: Path) -> None:
    held = ProjectLock(path)
    held.acquire()
    with pytest.raises(RuntimeError, match="already held"):
        held.acquire()
    held.release()


def test_reclaim_removes_only_the_lock_it_found_dead(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_lock(path, 4242)
    live = LockOwner(os.getppid(), HOST, STARTED).to_bytes()

    def running(pid: int) -> bool:
        if pid == 4242:
            # Another run reclaims the dead lock first and takes it.
            path.write_bytes(live)
            return False
        return True

    monkeypatch.setattr(lock_module, "_running", running)

    with pytest.raises(LockedError, match=f"pid {os.getppid()}"):
        ProjectLock(path).acquire()
    assert path.read_bytes() == live


def test_a_lock_released_meanwhile_is_taken(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_lock(path, os.getppid())
    real_read = lock_module._read
    calls = 0

    def read(target: Path) -> bytes | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            target.unlink()  # the other run finishes as we look
        return real_read(target)

    monkeypatch.setattr(lock_module, "_read", read)

    with ProjectLock(path) as held:
        assert held.warning is None


def test_giving_up_when_others_keep_taking_the_lock(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ProjectLock, "_create", lambda self, me: False)
    monkeypatch.setattr(lock_module, "_read", lambda target: None)
    with pytest.raises(LockedError, match="another run took the project lock"):
        ProjectLock(path).acquire()


def test_a_failed_write_removes_the_new_lock(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(fd: int) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(OSError, match="No space"):
        ProjectLock(path).acquire()
    assert not path.exists()


def test_an_unreadable_lock_is_not_absent(path: Path) -> None:
    path.mkdir(parents=True)  # a folder: it exists, but cannot be read
    with pytest.raises(LockedError, match="unreadable"):
        ProjectLock(path).acquire()


def test_the_pid_check(monkeypatch: pytest.MonkeyPatch) -> None:
    assert lock_module._running(os.getppid())
    assert not lock_module._running(dead_pid())

    def denied(pid: int, signal: int) -> None:
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(os, "kill", denied)
    assert lock_module._running(1)
