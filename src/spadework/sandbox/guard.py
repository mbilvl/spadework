"""The guard: the hook that allows or denies every tool call a session makes.

Run by the CLI as `python -I -m spadework.sandbox.guard --policy P --event E`
(see `policy.guard_command`). docs/spec/07-security.md is the specification.

`--event pre-tool-use` reads a PreToolUse payload on stdin and answers on
stdout with a permission decision, exit code 0. The checks run in this order,
and the first failure denies:

    parse the payload -> load the policy -> deadline -> tool allowlist ->
    argument checks that need no network -> caps -> WebFetch DNS -> allow

The caps are charged before any DNS lookup, so a call over its cap never
makes one, and a WebFetch later denied for a private address still uses up
one of the session's fetches. Every denial is appended to the denials log.

`--event session-start` checks that the policy loads and that the session runs
in its staging directory, then writes the liveness marker (`marker_path`) so
the executor can tell that this guard, and not some other hook, started. It
prints nothing on stdout, because SessionStart output would be added to the
model's context.

It fails closed: when the payload or the policy is unusable, or anything else
goes wrong, the guard prints one line on stderr and exits 2, which blocks the
call. Messages never contain control-plane paths or exception details,
because the model may see them. Standard library only, so it starts fast and
can be audited in one read.
"""

from __future__ import annotations

import fcntl
import functools
import json
import os
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import TYPE_CHECKING, Any

from spadework.sandbox.policy import (
    TOTAL,
    Policy,
    PolicyError,
    is_within,
    load,
    marker_path,
    write_atomic,
)
from spadework.sandbox.tools import KNOWN_TOOLS, STRUCTURED_OUTPUT

if TYPE_CHECKING:
    from collections.abc import Mapping
    from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network

    # Checks one tool's arguments. Returns a WebFetch host still to resolve.
    Check = Callable[[dict[str, Any], Policy], str | None]
    # The source address this machine would use to reach an address, or None.
    Probe = Callable[[IPv4Address | IPv6Address], IPv4Address | IPv6Address | None]

# Resolves a host name to its IP addresses, or raises OSError. Defined at run
# time so other modules can import it; `typing` loads collections.abc anyway.
Resolver = Callable[[str], Sequence[str]]

PREFIX = "spadework: "
ALLOWED = PREFIX + "allowed by the session policy"
MAX_PAYLOAD_BYTES = 64 * 1024 * 1024
MAX_ARGUMENT_BYTES = 4096  # a path, pattern or glob
MAX_URL_BYTES = 8192
MAX_BRACE_DEPTH = 16
DNS_TIMEOUT_S = 3.0
EXIT_BLOCK = 2

_EVENTS = {"session-start": "SessionStart", "pre-tool-use": "PreToolUse"}
_HOST_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-._")
_GREP_FLAGS = (
    "output_mode",
    "-A",
    "-B",
    "-C",
    "context",
    "-n",
    "-i",
    "-o",
    "only_matching",
    "type",
    "head_limit",
    "offset",
    "multiline",
)
_MAX_BRACE_EXPANSIONS = 64
_MAX_BRACE_STEPS = 256

# What JavaScript's String.prototype.trim removes. The CLI trims Glob and Grep
# paths after the hook has seen them, so a path the guard reads as relative
# (" /etc") can become absolute. Such arguments are denied, not normalised.
_JS_SPACE = frozenset(
    "\t\n\v\f\r "
    + "".join(map(chr, (0x00A0, 0x1680, *range(0x2000, 0x200B))))
    + "".join(map(chr, (0x2028, 0x2029, 0x202F, 0x205F, 0x3000, 0xFEFF)))
)
_LINE_SEPARATORS = frozenset(map(chr, (0x2028, 0x2029)))
# Separators between globs in one pattern: commas and anything JS calls space.
_GLOB_SEPARATORS = str.maketrans(dict.fromkeys([*_JS_SPACE, ","], " "))


class GuardError(Exception):
    """The call cannot be judged. The guard blocks it with exit code 2."""


class _Denied(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str  # one line, shown to the model
    target: str = ""  # the call's path, URL or query, shortened, for the log
    # For an allowed WebFetch to a host name: the name `check_host` must still
    # resolve. Such a decision is provisional.
    host: str | None = None


def decide(
    payload: Mapping[str, Any],
    policy: Policy,
    *,
    now: float,
    resolve: Resolver | None = None,
) -> Decision:
    """Judge one PreToolUse call against the policy, except for the caps.

    For callers that do not count calls. The guard itself runs
    `check_offline`, then `charge`, then `check_host`.
    """
    return check_host(check_offline(payload, policy, now=now), resolve)


def check_offline(
    payload: Mapping[str, Any], policy: Policy, *, now: float
) -> Decision:
    """Every check that needs no network: deadline, allowlist, arguments."""
    tool = payload.get("tool_name")
    args = payload.get("tool_input")
    if not isinstance(tool, str) or not tool or not isinstance(args, dict):
        raise GuardError("the hook payload has no tool call")
    target = _target(tool, args)
    try:
        host = _check(tool, args, policy, now)
    except _Denied as denied:
        return Decision(False, PREFIX + denied.reason, target)
    return Decision(True, ALLOWED, target, host)


def check_host(decision: Decision, resolve: Resolver | None = None) -> Decision:
    """Finish a provisional WebFetch decision: every address must be public."""
    if not decision.allow or decision.host is None:
        return decision
    try:
        _resolves_to_public(decision.host, resolve or system_resolve)
    except _Denied as denied:
        return Decision(False, PREFIX + denied.reason, decision.target)
    return Decision(True, decision.reason, decision.target)


def _check(tool: str, args: dict[str, Any], policy: Policy, now: float) -> str | None:
    if now >= policy.deadline_epoch:
        raise _Denied("the session's time is up. Stop now.")
    if tool.startswith("mcp__"):
        raise _Denied("MCP tools are not available in spadework sessions.")
    if tool not in policy.allowed_tools or tool not in KNOWN_TOOLS:
        available = ", ".join(policy.allowed_tools)
        raise _Denied(
            f"{_one_line(tool, 60)} is not available in this session. "
            f"Available tools: {available}."
        )
    return _CHECKS[tool](args, policy)


# Per-tool argument checks. Unknown arguments are denied, so a CLI update that
# adds a path argument cannot slip past the guard.


def _check_read(args: dict[str, Any], policy: Policy) -> None:
    _fields(args, "Read", ("file_path",), ("offset", "limit", "pages"))
    _real_path(args["file_path"], policy, "Read", "file_path")


def _check_write(args: dict[str, Any], policy: Policy) -> None:
    _fields(args, "Write", ("file_path", "content"), ())
    _writable(args["file_path"], policy, "Write")


def _check_edit(args: dict[str, Any], policy: Policy) -> None:
    _fields(args, "Edit", ("file_path", "old_string", "new_string"), ("replace_all",))
    _writable(args["file_path"], policy, "Edit")


def _check_glob(args: dict[str, Any], policy: Policy) -> None:
    _fields(args, "Glob", ("pattern",), ("path",))
    _pattern(args["pattern"], "Glob", "pattern")
    if args.get("path") is not None:
        _real_path(args["path"], policy, "Glob", "path")


def _check_grep(args: dict[str, Any], policy: Policy) -> None:
    _fields(args, "Grep", ("pattern",), ("path", "glob", *_GREP_FLAGS))
    # `pattern` is a regular expression over file contents, not a path.
    if not isinstance(args["pattern"], str):
        raise _Denied("Grep needs 'pattern' as a string.")
    if args.get("path") is not None:
        _real_path(args["path"], policy, "Grep", "path")
    if args.get("glob") is not None:
        _pattern(args["glob"], "Grep", "glob")


def _check_web_fetch(args: dict[str, Any], policy: Policy) -> str | None:
    _fields(args, "WebFetch", ("url", "prompt"), ())
    return _checked_url(args["url"])


def _check_web_search(args: dict[str, Any], policy: Policy) -> None:
    _fields(args, "WebSearch", ("query",), ("allowed_domains", "blocked_domains"))
    query = args["query"]
    if not isinstance(query, str) or not query.strip():
        raise _Denied("WebSearch needs 'query' as a non-empty string.")


def _check_structured_output(args: dict[str, Any], policy: Policy) -> None:
    # Its input is the session's result, which the engine validates.
    return


_CHECKS: dict[str, Check] = {
    "Read": _check_read,
    "Write": _check_write,
    "Edit": _check_edit,
    "Glob": _check_glob,
    "Grep": _check_grep,
    "WebFetch": _check_web_fetch,
    "WebSearch": _check_web_search,
    STRUCTURED_OUTPUT: _check_structured_output,
}


def _fields(
    args: dict[str, Any],
    tool: str,
    required: tuple[str, ...],
    optional: tuple[str, ...],
) -> None:
    for name in required:
        if name not in args:
            raise _Denied(f"{tool} needs the argument '{name}'.")
    for name in args:
        if name not in required and name not in optional:
            raise _Denied(
                f"{tool} does not take the argument {_one_line(repr(name), 60)}."
            )


def _plain_text(raw: object, tool: str, field: str) -> str:
    """A path, pattern or glob: a short string with no control characters and
    nothing the CLI would trim off either end."""
    if not isinstance(raw, str) or not raw:
        raise _Denied(f"{tool} needs '{field}' as a non-empty string.")
    # The length check comes first, so nothing below does unbounded work.
    if (
        len(raw) > MAX_ARGUMENT_BYTES
        or len(raw.encode("utf-8", "surrogatepass")) > MAX_ARGUMENT_BYTES
    ):
        raise _Denied(f"{tool}: '{field}' is longer than {MAX_ARGUMENT_BYTES} bytes.")
    if raw[0] in _JS_SPACE or raw[-1] in _JS_SPACE:
        raise _Denied(f"{tool}: '{field}' must not start or end with whitespace.")
    if _has_control(raw):
        raise _Denied(f"{tool}: '{field}' contains a control character.")
    return raw


def _has_control(text: str) -> bool:
    """C0 controls, DEL and C1 controls."""
    return any(ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F for char in text)


def _real_path(raw: object, policy: Policy, tool: str, field: str) -> str:
    """Resolve a path argument the way the OS will, and require it in the workdir."""
    path = _plain_text(raw, tool, field)
    if path.startswith("~"):
        # The CLI expands a leading ~ to the home directory.
        raise _Denied(
            f"{tool}: '~' paths are not allowed. Use paths in {policy.workdir}."
        )
    joined = path if os.path.isabs(path) else os.path.join(policy.workdir, path)
    try:
        # realpath resolves symlinks and `..` exactly as the kernel will on open.
        real = os.path.realpath(joined)
    except (ValueError, UnicodeError):
        raise _Denied(f"{tool}: '{field}' is not a valid path.") from None
    if not is_within(real, policy.workdir):
        raise _Denied(
            f"{tool} can only use paths inside the working directory {policy.workdir}."
        )
    return real


def _writable(raw: object, policy: Policy, tool: str) -> None:
    real = _real_path(raw, policy, tool, "file_path")
    if real not in policy.writable:
        if not policy.writable:
            raise _Denied(f"{tool} is not allowed: this session writes no files.")
        raise _Denied(f"{tool} is allowed only on {policy.writable[0]}.")


def _pattern(raw: object, tool: str, field: str) -> None:
    """Require a glob to stay relative to the search directory."""
    pattern = _plain_text(raw, tool, field)
    if "\\" in pattern:
        raise _Denied(f"{tool}: '{field}' must not contain backslashes.")
    expansions = _expand_braces(pattern)
    if expansions is None:
        raise _Denied(
            f"{tool}: '{field}' has unbalanced, too deeply nested or too many braces."
        )
    for expansion in expansions:
        # Globs can be listed with spaces or commas and negated with `!`.
        for piece in expansion.translate(_GLOB_SEPARATORS).split():
            piece = piece.removeprefix("!")
            if piece.startswith(("/", "~")) or ".." in piece:
                raise _Denied(
                    f"{tool}: '{field}' must be relative to the working directory "
                    "and must not contain '..'."
                )


def _expand_braces(pattern: str) -> list[str] | None:
    """Every alternative of a `{a,b}` pattern, or None if unbalanced, nested
    deeper than MAX_BRACE_DEPTH, or too many.

    `{a}` expands to `a`, a superset of what glob engines do, so that nothing
    a brace group could produce escapes the checks. The depth, step and
    expansion limits keep the work small for any input.
    """
    done: list[str] = []
    pending = [pattern]
    steps = 0
    while pending:
        steps += 1
        if steps > _MAX_BRACE_STEPS:
            return None
        current = pending.pop()
        start = current.find("{")
        if start < 0:
            done.append(current)
            continue
        depth = 0
        end = -1
        splits: list[int] = []
        for index in range(start, len(current)):
            char = current[index]
            if char == "{":
                depth += 1
                if depth > MAX_BRACE_DEPTH:
                    return None
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index
                    break
            elif char == "," and depth == 1:
                splits.append(index)
        if end < 0:
            return None
        bounds = [start, *splits, end]
        for left, right in pairwise(bounds):
            pending.append(
                current[:start] + current[left + 1 : right] + current[end + 1 :]
            )
        if len(done) + len(pending) > _MAX_BRACE_EXPANSIONS:
            return None
    return done


# WebFetch


def _checked_url(raw: object) -> str | None:
    """Check a URL without the network.

    Returns the host name still to be resolved, or None when the host is an
    IP literal, which is checked here. Hosts are read the way the WHATWG URL
    parser, which the CLI uses, reads them. Anything this cannot interpret the
    same way is denied.
    """
    # Imported here to keep file-tool calls fast.
    from ipaddress import IPv6Address
    from urllib.parse import urlsplit

    if not isinstance(raw, str) or not raw:
        raise _Denied("WebFetch needs 'url' as a non-empty string.")
    if len(raw) > MAX_URL_BYTES:
        raise _Denied(f"WebFetch: the URL is longer than {MAX_URL_BYTES} bytes.")
    if (
        "\\" in raw
        or " " in raw
        or raw[0] in _JS_SPACE
        or raw[-1] in _JS_SPACE
        or _has_control(raw)
    ):
        raise _Denied(
            "WebFetch: the URL has spaces, control characters or backslashes."
        )
    try:
        parts = urlsplit(raw)
        parts.port  # noqa: B018 - raises ValueError for an invalid port
    except ValueError:
        raise _Denied("WebFetch: the URL is not valid.") from None
    if parts.scheme not in ("http", "https"):
        raise _Denied("WebFetch allows only http and https URLs.")
    if "@" in parts.netloc:
        raise _Denied("WebFetch: URLs with a user name or password are not allowed.")
    bracketed = parts.netloc.startswith("[")
    port = (parts.netloc.rpartition("]")[2] if bracketed else parts.netloc).partition(
        ":"
    )[2]
    if port and not (port.isascii() and port.isdigit()):
        # Python accepts "+80" and non-ASCII digits; the WHATWG parser does not.
        raise _Denied("WebFetch: the URL is not valid.")
    host = parts.hostname
    if not host:
        raise _Denied("WebFetch: the URL has no host.")
    if not host.isascii():
        raise _Denied(
            "WebFetch: the host name must be ASCII. Use its xn-- (punycode) form."
        )
    if "%" in host:
        raise _Denied("WebFetch: the host name must not be percent-encoded.")
    if bracketed:
        try:
            _require_public([IPv6Address(host)])
        except ValueError:
            raise _Denied("WebFetch: the host is not a valid IPv6 address.") from None
        return None
    name = host.removesuffix(".")
    if len(name) > 253 or "" in name.split(".") or not set(name) <= _HOST_CHARS:
        raise _Denied("WebFetch: the host name is not valid.")
    if name == "localhost" or name.endswith(".localhost"):
        raise _Denied("WebFetch cannot fetch local or private network addresses.")
    if is_special_use(name):
        raise _Denied(
            "WebFetch cannot fetch special-use domain names, such as .example, "
            ".test, .local or .internal."
        )
    address = _whatwg_ipv4(name)
    if address is not None:
        _require_public([address])
        return None
    if "." not in name:
        # A single label is looked up in the DNS search domains, so it names
        # a machine on the local network.
        raise _Denied(
            "WebFetch: the host must be a full domain name, such as example.com."
        )
    return name


# Special-use domain names (RFC 6761, RFC 8375, RFC 7686, RFC 9476, and
# .internal, which ICANN reserved for private use). They never name a public
# site, so they are refused without a DNS lookup: a resolver may answer them
# with a local address, or leak the lookup to the local network.
SPECIAL_USE_DOMAINS = (
    "example",
    "invalid",
    "test",
    "localhost",
    "local",
    "internal",
    "home.arpa",
    "onion",
    "alt",
)


def is_special_use(host: str) -> bool:
    """Whether a lower-case host name is, or is under, a special-use name."""
    name = host.removesuffix(".")
    return any(
        name == domain or name.endswith("." + domain) for domain in SPECIAL_USE_DOMAINS
    )


def _resolves_to_public(name: str, resolve: Resolver) -> None:
    try:
        resolved = resolve(name)
    except (OSError, UnicodeError):
        raise _Denied("WebFetch: the host name could not be resolved.") from None
    _require_public([_ip(value) for value in resolved])


def _ip(value: str) -> IPv4Address | IPv6Address:
    from ipaddress import ip_address

    try:
        return ip_address(value.split("%", 1)[0])
    except ValueError:
        raise _Denied(
            "WebFetch: the host name did not resolve to an IP address."
        ) from None


def _require_public(addresses: Sequence[IPv4Address | IPv6Address]) -> None:
    if (
        not addresses
        or not all(is_public_address(address) for address in addresses)
        or any(is_own_network(address) for address in addresses)
    ):
        raise _Denied("WebFetch cannot fetch local or private network addresses.")


# The IANA IPv4 and IPv6 Special-Purpose Address Registries, plus multicast.
# Every listed block is refused, including the few the registry marks as
# globally reachable (AS112, AMT, anycast services), which there is no reason
# to fetch. A fixed list gives the same answer on every Python version, which
# `ipaddress.is_global` does not.
_SPECIAL_IPV4 = (
    "0.0.0.0/8",  # "this network"
    "168.63.129.16/32",  # Azure WireServer, reachable from every Azure VM
    "10.0.0.0/8",  # private use
    "100.64.0.0/10",  # shared address space (carrier-grade NAT)
    "127.0.0.0/8",  # loopback
    "169.254.0.0/16",  # link local, including cloud metadata services
    "172.16.0.0/12",  # private use
    "192.0.0.0/24",  # IETF protocol assignments
    "192.0.2.0/24",  # documentation (TEST-NET-1)
    "192.31.196.0/24",  # AS112-v4
    "192.52.193.0/24",  # AMT
    "192.88.99.0/24",  # deprecated 6to4 relay anycast
    "192.168.0.0/16",  # private use
    "192.175.48.0/24",  # direct delegation AS112 service
    "198.18.0.0/15",  # benchmarking
    "198.51.100.0/24",  # documentation (TEST-NET-2)
    "203.0.113.0/24",  # documentation (TEST-NET-3)
    "224.0.0.0/4",  # multicast
    "240.0.0.0/4",  # reserved, including 255.255.255.255 limited broadcast
)
# Only 2000::/3 is allocated for global unicast, so everything outside it is
# refused: loopback, unspecified, discard, SRv6 SIDs, unique local, link local,
# site local, multicast, and the reserved space. These are the special blocks
# inside it. The IPv4-mapped (::ffff:0:0/96) and NAT64 (64:ff9b::/96) forms
# are judged by the IPv4 address they carry.
_GLOBAL_UNICAST = "2000::/3"
_SPECIAL_IPV6 = (
    "2001::/23",  # IETF protocol assignments: Teredo, ORCHIDv2, AMT, AS112...
    "2001:db8::/32",  # documentation
    "2002::/16",  # 6to4
    "2620:4f:8000::/48",  # direct delegation AS112 service
    "3fff::/20",  # documentation
)
_IPV4_MAPPED = "::ffff:0:0/96"
_NAT64 = "64:ff9b::/96"


@functools.cache
def _networks() -> tuple[
    tuple[IPv4Network, ...],
    IPv6Network,
    tuple[IPv6Network, ...],
    IPv6Network,
    IPv6Network,
]:
    from ipaddress import IPv4Network, IPv6Network

    return (
        tuple(IPv4Network(block) for block in _SPECIAL_IPV4),
        IPv6Network(_GLOBAL_UNICAST),
        tuple(IPv6Network(block) for block in _SPECIAL_IPV6),
        IPv6Network(_IPV4_MAPPED),
        IPv6Network(_NAT64),
    )


def is_public_address(address: IPv4Address | IPv6Address) -> bool:
    """Whether an address may be fetched: unicast, globally routable, and in
    no IANA special-purpose block. The same answer on every Python version."""
    from ipaddress import IPv4Address

    special_v4, global_unicast, special_v6, mapped, nat64 = _networks()
    if isinstance(address, IPv4Address):
        return not any(address in block for block in special_v4)
    if address in mapped or address in nat64:
        return is_public_address(IPv4Address(int(address) & 0xFFFFFFFF))
    if address not in global_unicast:
        return False
    return not any(address in block for block in special_v6)


# The earlier name, kept for callers that import it.
_is_public = is_public_address


def local_address_for(
    address: IPv4Address | IPv6Address,
) -> IPv4Address | IPv6Address | None:
    """The source address this machine would use to reach `address`, or None.

    Connecting a UDP socket sends no packet: the kernel only picks a route.
    """
    import socket
    from ipaddress import ip_address

    family = socket.AF_INET if address.version == 4 else socket.AF_INET6
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as probe:
            probe.connect((str(address), 9))
            local = str(probe.getsockname()[0])
        return ip_address(local.split("%", 1)[0])
    except (OSError, ValueError):
        return None


def is_own_network(
    address: IPv4Address | IPv6Address, probe: Probe | None = None
) -> bool:
    """Whether a public address leads back to this machine: it is the source
    address the machine would use to reach it, or, for IPv6, in that
    address's /64, the machine's own network. False when the probe fails."""
    from ipaddress import IPv6Address, IPv6Network

    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    local = (probe or local_address_for)(address)
    if isinstance(local, IPv6Address) and local.ipv4_mapped is not None:
        local = local.ipv4_mapped
    if local is None:
        return False
    if isinstance(address, IPv6Address) and isinstance(local, IPv6Address):
        return address in IPv6Network((local, 64), strict=False)
    return address == local


def _whatwg_ipv4(host: str) -> IPv4Address | None:
    """The IPv4 address a host means to a WHATWG URL parser, or None.

    `127.1`, `0x7f000001`, `2130706433` and `0177.0.0.1` all mean 127.0.0.1.
    A host that ends in a number but is not a valid address is denied.
    """
    from ipaddress import IPv4Address

    labels = host.split(".")
    last = labels[-1]
    if not (last.isdigit() or (last.startswith("0x") and _number(last) is not None)):
        return None
    numbers = [_number(label) for label in labels]
    if len(numbers) > 4 or None in numbers:
        raise _Denied("WebFetch: the host is not a valid IP address.")
    values = [number for number in numbers if number is not None]
    *head, tail = values
    if any(value > 255 for value in head) or tail >= 256 ** (5 - len(values)):
        raise _Denied("WebFetch: the host is not a valid IP address.")
    for index, value in enumerate(head):
        tail += value << (8 * (3 - index))
    return IPv4Address(tail)


def whatwg_ipv4(host: str) -> IPv4Address | None:
    """`_whatwg_ipv4` for other modules: a lower-case ASCII host's IPv4
    address, or None for a name. Raises ValueError for a numeric host that is
    not a valid address, such as `1.2.3.4.5` or `example.123`."""
    try:
        return _whatwg_ipv4(host)
    except _Denied:
        raise ValueError("the host is not a valid IP address") from None


def _number(label: str) -> int | None:
    """A WHATWG IPv4 number: decimal, 0x hexadecimal, or 0-prefixed octal."""
    if not label:
        return None
    base, digits = 10, label
    if label.startswith("0x"):
        base, digits = 16, label[2:]
    elif len(label) > 1 and label.startswith("0"):
        base, digits = 8, label[1:]
    if not digits:
        return 0
    if not set(digits) <= set("0123456789abcdef"[:base]):
        return None
    return int(digits, base)


def system_resolve(host: str) -> list[str]:
    """Resolve a host with the system resolver, giving up after a few seconds."""
    import socket
    import threading

    found: list[list[str]] = []

    def lookup() -> None:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
        except (OSError, UnicodeError):
            return
        found.append([str(info[4][0]) for info in infos])

    # getaddrinfo has no timeout of its own. The thread is a daemon, so a slow
    # lookup is abandoned when the guard exits.
    thread = threading.Thread(target=lookup, daemon=True)
    thread.start()
    thread.join(DNS_TIMEOUT_S)
    if not found:
        raise OSError("the host name could not be resolved")
    return found[0]


# Caps


def read_counters(path: str) -> dict[str, int]:
    """The tool calls counted so far. A missing file means none."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError:
        raise GuardError("the tool-call counters are unreadable") from None
    try:
        with os.fdopen(fd, "rb") as file:
            data = json.loads(file.read())
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        raise GuardError("the tool-call counters are unreadable") from None
    if not isinstance(data, dict) or not all(
        isinstance(key, str) and type(value) is int and value >= 0
        for key, value in data.items()
    ):
        raise GuardError("the tool-call counters are invalid")
    return data


def charge(policy: Policy, tool: str) -> str | None:
    """Count one allowed call. Returns a deny reason when a cap is used up.

    Calls can run in parallel, so the read-modify-write happens under an
    exclusive lock on a separate lock file.
    """
    fd = os.open(policy.lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        counters = read_counters(policy.counters_path)
        cap = policy.caps.get(tool)
        if cap is not None and counters.get(tool, 0) >= cap:
            return f"the limit of {cap} {tool} calls for this session is used up."
        total = policy.caps[TOTAL]
        if counters.get(TOTAL, 0) >= total:
            return f"the limit of {total} tool calls for this session is used up."
        counters[tool] = counters.get(tool, 0) + 1
        counters[TOTAL] = counters.get(TOTAL, 0) + 1
        write_atomic(policy.counters_path, json.dumps(counters).encode("utf-8"))
        return None
    finally:
        os.close(fd)  # releases the lock


# Session start


def check_session_start(
    payload: Mapping[str, Any], policy: Policy, policy_path: str, *, now: float
) -> str | None:
    """A problem that means the session must not run, or None."""
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd or "\x00" in cwd:
        return "the SessionStart payload has no cwd"
    workdir = policy.workdir
    if os.path.realpath(workdir) != workdir:
        return "the policy's workdir is not a fully resolved path"
    if not os.path.isdir(workdir):
        return "the policy's workdir is not a directory"
    if os.path.realpath(cwd) != workdir:
        return "the session is not running in its staging directory"
    if now >= policy.deadline_epoch:
        return "the session's deadline has already passed"
    for path in policy.writable:
        if os.path.realpath(path) != path:
            return "the writable path is not a fully resolved path"
        if os.path.lexists(path) and not os.path.isfile(path):
            return "the writable path is not a regular file"
    control = (policy_path, policy.counters_path, policy.denials_path, policy.lock_path)
    if any(is_within(os.path.realpath(path), workdir) for path in control):
        return "the control files must be outside the staging directory"
    read_counters(policy.counters_path)
    return None


# I/O


def parse_payload(data: bytes, event: str) -> dict[str, Any]:
    """The hook payload for an event name such as `PreToolUse`."""
    if len(data) > MAX_PAYLOAD_BYTES:
        raise GuardError("the hook payload is too large")
    try:
        payload = json.loads(data)
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise GuardError("the hook payload is not valid JSON") from None
    if not isinstance(payload, dict):
        raise GuardError("the hook payload is not a JSON object")
    if payload.get("hook_event_name") != event:
        raise GuardError(f"expected a {event} hook payload")
    if event == "PreToolUse":
        tool = payload.get("tool_name")
        if not isinstance(tool, str) or not tool:
            raise GuardError("the hook payload has no tool_name")
        if not isinstance(payload.get("tool_input"), dict):
            raise GuardError("the hook payload has no tool_input object")
    return payload


def log_denial(policy: Policy, tool: str, decision: Decision, now: float) -> None:
    """Append one line to the denials log. Failing to log never allows a call."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "tool_name": _one_line(tool, 100),
        "reason": decision.reason,
        "target": decision.target,
    }
    line = (json.dumps(record) + "\n").encode("utf-8")
    try:
        fd = os.open(
            policy.denials_path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def run(
    argv: Sequence[str],
    stdin: bytes,
    *,
    now: float | None = None,
    resolve: Resolver | None = None,
) -> tuple[int, str, str]:
    """The guard with its I/O made explicit. Returns (exit code, stdout, stderr)."""
    when = time.time() if now is None else now
    try:
        policy_path, event = _parse_args(argv)
        if event == "session-start":
            return _session_start(policy_path, stdin, when)
        return _pre_tool_use(policy_path, stdin, when, resolve)
    except GuardError as exc:
        return EXIT_BLOCK, "", _error_line(str(exc))
    except Exception:  # fail closed, with one generic line and no traceback
        return EXIT_BLOCK, "", _error_line("internal error")


def _parse_args(argv: Sequence[str]) -> tuple[str, str]:
    usage = "usage: python -m spadework.sandbox.guard --policy PATH --event EVENT"
    if len(argv) != 4:
        raise GuardError(usage)
    options = dict(zip(argv[::2], argv[1::2], strict=True))
    if set(options) != {"--policy", "--event"}:
        raise GuardError(usage)
    if options["--event"] not in _EVENTS:
        raise GuardError("--event must be session-start or pre-tool-use")
    if not os.path.isabs(options["--policy"]):
        raise GuardError("--policy must be an absolute path")
    return options["--policy"], options["--event"]


def _load(policy_path: str) -> Policy:
    try:
        return load(policy_path)
    except PolicyError as exc:
        raise GuardError(f"the session policy is unusable: {exc}") from None


def _session_start(policy_path: str, stdin: bytes, now: float) -> tuple[int, str, str]:
    payload = parse_payload(stdin, _EVENTS["session-start"])
    policy = _load(policy_path)
    problem = check_session_start(payload, policy, policy_path, now=now)
    if problem is not None:
        raise GuardError(problem)
    marker = marker_path(policy_path, policy.session_id)
    try:
        write_atomic(str(marker), policy.session_id.encode("utf-8"))
    except OSError:
        raise GuardError("cannot write the session's liveness marker") from None
    return 0, "", ""


def _pre_tool_use(
    policy_path: str, stdin: bytes, now: float, resolve: Resolver | None
) -> tuple[int, str, str]:
    payload_error: GuardError | None = None
    try:
        payload = parse_payload(stdin, _EVENTS["pre-tool-use"])
    except GuardError as exc:
        payload_error = exc
    try:
        policy = _load(policy_path)
    except GuardError:
        if payload_error is not None:
            raise payload_error from None
        raise
    if payload_error is not None:
        log_denial(policy, "(unknown)", Decision(False, str(payload_error)), now)
        raise payload_error
    tool = str(payload["tool_name"])
    try:
        decision = check_offline(payload, policy, now=now)
        if decision.allow and tool != STRUCTURED_OUTPUT:
            # StructuredOutput carries the session's result, so it is never
            # capped: a session that used up its calls can still finish.
            over = charge(policy, tool)
            if over is not None:
                decision = Decision(False, PREFIX + over, decision.target)
        decision = check_host(decision, resolve)
    except Exception as exc:
        # The detail stays in the local log; the model sees a generic line.
        reason = str(exc) if isinstance(exc, GuardError) else type(exc).__name__
        log_denial(policy, tool, Decision(False, f"guard error: {reason}"), now)
        raise
    if not decision.allow:
        log_denial(policy, tool, decision, now)
    return 0, _hook_output(decision), ""


def _hook_output(decision: Decision) -> str:
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow" if decision.allow else "deny",
            "permissionDecisionReason": _one_line(decision.reason, 500),
        }
    }
    return json.dumps(output) + "\n"


def _error_line(message: str) -> str:
    return f"spadework guard: {_one_line(message, 300)}\n"


def _target(tool: str, args: Mapping[str, Any]) -> str:
    if tool == "WebFetch":
        return _redacted_url(args.get("url"))
    for key in ("file_path", "path", "pattern", "glob", "query"):
        value = args.get(key)
        if isinstance(value, str):
            return _one_line(value, 200)
    return ""


def _redacted_url(value: object) -> str:
    """A URL without credentials, query or fragment, for the local log."""
    from urllib.parse import urlsplit

    if not isinstance(value, str):
        return ""
    try:
        parts = urlsplit(value[:MAX_URL_BYTES])
        port = f":{parts.port}" if parts.port is not None else ""
    except ValueError:
        return "(invalid URL)"
    return _one_line(f"{parts.scheme}://{parts.hostname or ''}{port}{parts.path}", 200)


def _one_line(text: str, limit: int) -> str:
    """At most `limit` characters, with control characters and line breaks
    replaced. Truncates first, so a huge argument costs nothing."""
    flat = "".join(
        " "
        if ord(char) < 0x20 or 0x7F <= ord(char) <= 0x9F or char in _LINE_SEPARATORS
        else char
        for char in text[:limit]
    )
    return flat if len(text) <= limit else flat[: limit - 1] + "…"


def main(argv: Sequence[str] | None = None) -> int:
    try:
        stdin = sys.stdin.buffer.read(MAX_PAYLOAD_BYTES + 1)
    except Exception:
        sys.stderr.write(_error_line("cannot read the hook payload"))
        return EXIT_BLOCK
    code, out, err = run(sys.argv[1:] if argv is None else argv, stdin)
    try:
        sys.stdout.write(out)
        sys.stdout.flush()
        sys.stderr.write(err)
    except OSError:
        return EXIT_BLOCK
    return code


if __name__ == "__main__":
    raise SystemExit(main())
