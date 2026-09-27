"""Regressions for the adversarial review of the guard (R1 to R7).

R1/R2 whitespace and control characters the CLI trims or mangles after the
hook, R3 no DNS lookup before the caps are charged, R4 bounded work for any
argument, R5 one public-address answer on every Python version, R6 generic
errors and no symlink following, R7 the session-start liveness marker.
"""

from __future__ import annotations

import ipaddress
import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from spadework.sandbox import guard
from spadework.sandbox.policy import PolicyError, marker_path
from tests.security.conftest import (
    NOW,
    OUTPUT,
    Box,
    MakeBox,
    fake_resolve,
    future_deadline,
    run_guard,
)

# Every code point JavaScript's String.prototype.trim removes, as reported by
# Node 22: `("a" + ch).trim() === "a"` over all of Unicode.
JS_TRIM = [
    0x09, 0x0A, 0x0B, 0x0C, 0x0D, 0x20, 0xA0, 0x1680,
    0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008,
    0x2009, 0x200A, 0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF,
]  # fmt: skip
CONTROLS = [*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0)]


def test_the_trim_set_is_javascripts() -> None:
    assert frozenset(map(chr, JS_TRIM)) == guard._JS_SPACE


# Each argument the CLI may trim: (tool, the argument, the rest of the input).
TRIMMED: list[tuple[str, str, dict[str, Any]]] = [
    ("Read", "file_path", {}),
    ("Write", "file_path", {"content": "x"}),
    ("Edit", "file_path", {"old_string": "a", "new_string": "b"}),
    ("Glob", "path", {"pattern": "*"}),
    ("Grep", "path", {"pattern": "."}),
    ("Glob", "pattern", {}),
    ("Grep", "glob", {"pattern": "."}),
]


@pytest.mark.parametrize("code", JS_TRIM, ids=[f"U+{code:04X}" for code in JS_TRIM])
def test_every_js_trim_character_is_denied_at_either_end(
    writer: Box, code: int
) -> None:
    """R1, R2: denied, not normalised, even around an otherwise allowed value."""
    char = chr(code)
    for tool, field, rest in TRIMMED:
        values = ["/etc", "~/.aws", "<outside>"] if field != "pattern" else ["/etc/*"]
        if field in ("file_path", "path"):
            values.append(f"<w>/{OUTPUT}" if tool in ("Write", "Edit") else "<w>")
        else:
            values.append("*.md")
        for value in values:
            for raw in (char + value, value + char):
                decision = writer.decide(tool, {**rest, field: raw})
                assert not decision.allow, (tool, field, repr(raw))
                assert "\n" not in decision.reason


@pytest.mark.parametrize("code", CONTROLS, ids=[f"U+{code:04X}" for code in CONTROLS])
def test_every_control_character_is_denied(writer: Box, code: int) -> None:
    """R1: C0, DEL and C1 controls anywhere in a path, pattern or glob."""
    char = chr(code)
    for tool, field, rest in TRIMMED:
        value = "inputs/" + char + "notes.md"
        if tool in ("Write", "Edit"):
            value = OUTPUT[:3] + char + OUTPUT[3:]
        decision = writer.decide(tool, {**rest, field: value})
        assert not decision.allow, (tool, field, hex(code))


def test_the_review_repro_is_denied_by_the_real_guard(make_box: MakeBox) -> None:
    """R1: the reviewer's exact calls through the subprocess entry point."""
    box = make_box("draft", deadline=future_deadline())
    calls = [
        ("Grep", {"pattern": "SECRET", "path": f" {box.outside}"}),
        ("Grep", {"pattern": ".", "path": " ~/.ssh"}),
        ("Glob", {"pattern": "**/*", "path": " ~"}),
        ("Glob", {"pattern": "*", "path": f" {box.control}"}),
    ]
    for tool, tool_input in calls:
        completed = run_guard(box.staging.paths.policy, box.payload(tool, tool_input))
        output = json.loads(completed.stdout)["hookSpecificOutput"]
        assert output["permissionDecision"] == "deny", tool_input
        assert "whitespace" in output["permissionDecisionReason"]


# R3: DNS only after the caps


class Recorder:
    def __init__(self) -> None:
        self.lookups: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.lookups.append(host)
        return fake_resolve(host)


def fetch(box: Box, url: str, resolver: Recorder, now: float = NOW) -> str:
    payload = json.dumps(box.payload("WebFetch", {"url": url, "prompt": "x"})).encode()
    argv = ["--policy", str(box.staging.paths.policy), "--event", "pre-tool-use"]
    code, out, err = guard.run(argv, payload, now=now, resolve=resolver)
    assert code == 0, err
    decision: str = json.loads(out)["hookSpecificOutput"]["permissionDecision"]
    return decision


def test_a_call_over_its_cap_never_looks_up_dns(make_box: MakeBox) -> None:
    box = make_box("research", max_fetches=1)
    resolver = Recorder()

    assert fetch(box, "https://example.com/", resolver) == "allow"
    for index in range(5):
        url = f"https://brief-secret-{index}.attacker.example.com/"
        assert fetch(box, url, resolver) == "deny"

    assert resolver.lookups == ["example.com"]
    assert box.staging.counters() == {"WebFetch": 1, "total": 1}
    assert all("limit of 1 WebFetch" in d["reason"] for d in box.staging.denials())


def test_a_call_over_the_total_cap_never_looks_up_dns(make_box: MakeBox) -> None:
    box = make_box("research", total_calls=1)
    resolver = Recorder()
    search = json.dumps(box.payload("WebSearch", {"query": "q"})).encode()
    argv = ["--policy", str(box.staging.paths.policy), "--event", "pre-tool-use"]
    guard.run(argv, search, now=NOW)

    assert fetch(box, "https://attacker.example.com/", resolver) == "deny"
    assert resolver.lookups == []


def test_a_fetch_denied_for_a_private_address_still_uses_a_fetch(
    make_box: MakeBox,
) -> None:
    box = make_box("research", max_fetches=2)
    resolver = Recorder()

    assert fetch(box, "http://internal.example.com/", resolver) == "deny"
    assert box.staging.counters() == {"WebFetch": 1, "total": 1}
    assert fetch(box, "https://example.com/", resolver) == "allow"
    assert fetch(box, "https://mixed.example.com/", resolver) == "deny"

    assert resolver.lookups == ["internal.example.com", "example.com"]


def test_calls_denied_offline_neither_count_nor_look_up(make_box: MakeBox) -> None:
    box = make_box("research", max_fetches=1)
    resolver = Recorder()

    assert fetch(box, "https://user:pw@example.com/", resolver) == "deny"
    assert fetch(box, "ftp://example.com/", resolver) == "deny"
    assert fetch(box, "https://example.com/", resolver, now=1e12) == "deny"  # late

    assert resolver.lookups == []
    assert box.staging.counters() == {}


def test_ip_literals_need_no_lookup(make_box: MakeBox) -> None:
    box = make_box("research")
    resolver = Recorder()

    assert fetch(box, "https://1.1.1.1/", resolver) == "allow"
    assert fetch(box, "http://[::1]/", resolver) == "deny"

    assert resolver.lookups == []


def test_offline_checks_leave_the_host_to_resolve(researcher: Box) -> None:
    payload = researcher.payload(
        "WebFetch", {"url": "https://Example.com./", "prompt": "x"}
    )

    provisional = guard.check_offline(payload, researcher.policy, now=NOW)

    assert provisional.allow
    assert provisional.host == "example.com"
    final = guard.check_host(provisional, fake_resolve)
    assert final.allow
    assert final.host is None


# R4: bounded work


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        pytest.param("Glob", {"pattern": "{" * 8000 + "a" + "}" * 8000}, id="nested"),
        pytest.param("Glob", {"pattern": "{a}" * 8000}, id="sequential"),
        pytest.param(
            "Glob", {"pattern": "{" * 2000 + "a" + "}" * 2000}, id="nested-4k"
        ),
        pytest.param("Glob", {"pattern": "{a,b}" * 800}, id="alternatives-4k"),
        pytest.param("Read", {"file_path": "a/" * 1_000_000}, id="million-components"),
        pytest.param("Grep", {"pattern": ".", "path": "/" * 1_000_000}, id="slashes"),
        pytest.param("Grep", {"pattern": ".", "glob": "," * 1_000_000}, id="commas"),
    ],
)
def test_hostile_arguments_are_denied_quickly(
    writer: Box, tool: str, tool_input: dict[str, Any]
) -> None:
    started = time.perf_counter()

    decision = writer.decide(tool, tool_input)

    assert time.perf_counter() - started < 1.0
    assert not decision.allow


def test_worst_case_patterns_within_the_limits_stay_fast(writer: Box) -> None:
    patterns = [
        "{" * 16 + "a," * 2000 + "}" * 16,
        "{a,b}" * 6 + "x" * 4000,
        "{a,{b,{c,{d,{e,{f,{g,{h,{i}}}}}}}}}" + "y" * 4000,
    ]
    for pattern in patterns:
        started = time.perf_counter()
        writer.decide("Glob", {"pattern": pattern[:4096]})
        assert time.perf_counter() - started < 1.0, pattern[:40]


def test_deeply_nested_json_is_invalid_not_a_crash(writer: Box) -> None:
    deep = b'{"a":' * 100_000 + b"1" + b"}" * 100_000
    argv = ["--policy", str(writer.staging.paths.policy), "--event", "pre-tool-use"]

    code, out, err = guard.run(argv, deep, now=NOW)

    assert (code, out) == (2, "")
    # Python 3.11 raises RecursionError, which counts as invalid JSON; newer
    # versions parse it and find no hook event. Either way, one clean line.
    assert err in (
        "spadework guard: the hook payload is not valid JSON\n",
        "spadework guard: expected a PreToolUse hook payload\n",
    )


# R5: one answer on every Python version


PUBLIC = [
    "1.1.1.1",
    "8.8.8.8",
    "93.184.215.14",
    "2606:4700:4700::1111",
    "::ffff:8.8.8.8",
]
NOT_PUBLIC = [
    "0.0.0.0", "10.1.2.3", "100.64.0.1", "127.0.0.1", "169.254.169.254",
    "172.31.255.255", "192.0.0.8", "192.0.0.9", "192.0.0.10", "192.0.0.170",
    "192.0.2.1", "192.31.196.1", "192.52.193.1", "192.88.99.1", "192.168.0.1",
    "192.175.48.1", "198.18.0.1", "198.51.100.1", "203.0.113.1", "224.0.0.1",
    "240.0.0.1", "255.255.255.255",
    "::", "::1", "::7f00:1", "::ffff:127.0.0.1", "::ffff:0:7f00:1", "64:ff9b::a00:1",
    "64:ff9b:1::a00:1", "100::1", "100:0:0:1::1", "2001::1", "2001:1::1",
    "2001:1::3", "2001:2::1", "2001:3::1", "2001:4:112::1", "2001:20::1",
    "2001:30::1", "2001:db8::1", "2002:808:808::", "2620:4f:8000::1", "3fff::1",
    "5f00::1", "fc00::1", "fd00:ec2::254", "fe80::1", "fec0::1", "ff02::1",
]  # fmt: skip


@pytest.mark.parametrize("address", PUBLIC + NOT_PUBLIC)
def test_public_addresses(address: str) -> None:
    assert guard.is_public_address(ipaddress.ip_address(address)) is (address in PUBLIC)


def test_the_old_name_still_works() -> None:
    """The verifier imports `_is_public`."""
    assert guard._is_public is guard.is_public_address
    assert guard._is_public(ipaddress.ip_address("1.1.1.1"))


def test_public_addresses_do_not_depend_on_the_stdlib_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5: the answer comes from the fixed registry, so Python versions agree."""

    def boom(self: object) -> bool:
        raise AssertionError("ipaddress flags must not be used")

    for cls in (ipaddress.IPv4Address, ipaddress.IPv6Address):
        for flag in (
            "is_global",
            "is_private",
            "is_reserved",
            "is_multicast",
            "is_loopback",
            "is_link_local",
            "is_unspecified",
        ):
            monkeypatch.setattr(cls, flag, property(boom))
    monkeypatch.setattr(ipaddress.IPv6Address, "is_site_local", property(boom))

    for address in PUBLIC + NOT_PUBLIC:
        is_public = guard.is_public_address(ipaddress.ip_address(address))
        assert is_public is (address in PUBLIC)


# R6: generic errors, no symlinks followed


def test_internal_errors_name_no_exception(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=future_deadline())
    box.staging.paths.lock.mkdir()

    completed = run_guard(
        box.staging.paths.policy, box.payload("Read", {"file_path": "x"})
    )

    assert completed.returncode == 2
    assert completed.stderr == "spadework guard: internal error\n"
    assert box.staging.denials()[0]["reason"] == "guard error: IsADirectoryError"


def test_counters_symlink_is_not_followed(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=future_deadline())
    secret = box.outside / "secret.txt"
    box.staging.paths.counters.symlink_to(secret)

    completed = run_guard(
        box.staging.paths.policy, box.payload("Read", {"file_path": "x"})
    )

    assert completed.returncode == 2
    assert "counters are unreadable" in completed.stderr
    assert secret.read_text(encoding="utf-8") == "SECRET\n"
    assert box.staging.paths.counters.is_symlink()


def test_lock_symlink_is_not_followed(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=future_deadline())
    secret = box.outside / "secret.txt"
    box.staging.paths.lock.symlink_to(secret)

    completed = run_guard(
        box.staging.paths.policy, box.payload("Read", {"file_path": "x"})
    )

    assert completed.returncode == 2
    assert completed.stderr == "spadework guard: internal error\n"
    assert secret.read_text(encoding="utf-8") == "SECRET\n"


# R7: the liveness marker


def session_start(box: Box) -> Any:
    payload = {"hook_event_name": "SessionStart", "cwd": str(box.workdir)}
    return run_guard(box.staging.paths.policy, payload, event="session-start")


def test_marker_path() -> None:
    policy = Path("/project/.spadework/control/003-x.policy.json")

    assert marker_path(policy, "003-x") == Path(
        "/project/.spadework/control/003-x.alive"
    )
    assert marker_path(str(policy), "003-x") == marker_path(policy, "003-x")
    with pytest.raises(PolicyError, match="session_id"):
        marker_path(policy, "../escape")


def test_session_start_writes_the_marker(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=future_deadline())
    marker = box.staging.paths.marker

    completed = session_start(box)

    assert (completed.returncode, completed.stdout, completed.stderr) == (0, "", "")
    assert marker == box.control / "001-scope-draft.alive"
    assert marker.read_bytes() == b"001-scope-draft"
    assert marker.stat().st_mode & 0o777 == 0o600
    assert not [p.name for p in box.control.iterdir() if p.name.endswith(".tmp")]


def test_session_start_replaces_a_stale_marker(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=future_deadline())
    box.staging.paths.marker.write_text("someone-else", encoding="utf-8")

    assert session_start(box).returncode == 0
    assert box.staging.paths.marker.read_text(encoding="utf-8") == "001-scope-draft"


def test_session_start_does_not_follow_a_symlinked_marker(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=future_deadline())
    secret = box.outside / "secret.txt"
    box.staging.paths.marker.symlink_to(secret)

    assert session_start(box).returncode == 0

    assert secret.read_text(encoding="utf-8") == "SECRET\n"
    assert not box.staging.paths.marker.is_symlink()
    assert box.staging.paths.marker.read_text(encoding="utf-8") == "001-scope-draft"


def test_no_marker_when_the_session_start_check_fails(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=1.0)

    completed = session_start(box)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert not box.staging.paths.marker.exists()


def test_failing_to_write_the_marker_blocks(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=future_deadline())
    box.staging.paths.marker.mkdir()
    (box.staging.paths.marker / "x").touch()  # a non-empty directory cannot be replaced

    completed = session_start(box)

    assert completed.returncode == 2
    assert completed.stdout == ""
    assert (
        completed.stderr
        == "spadework guard: cannot write the session's liveness marker\n"
    )


def test_cleanup_removes_the_marker(make_box: MakeBox) -> None:
    box = make_box("draft", deadline=future_deadline())
    assert session_start(box).returncode == 0

    assert box.staging.cleanup()

    assert not box.staging.paths.marker.exists()
    assert not os.listdir(box.control)
