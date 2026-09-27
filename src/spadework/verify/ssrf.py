"""Which URLs a verification fetch may reach (docs/spec/07-security.md, I9).

`vet` turns a URL into a `Target`: the host name to send in the `Host` header
and to check the TLS certificate against, and the public addresses to
connect to. It resolves the name itself, and every address must be public.
The fetcher then connects to one of those exact addresses, so a second DNS
answer can never change where the request goes.

The address rules are the guard's (`spadework.sandbox.guard`), so a URL the
guard would let a session fetch is judged the same way here: an IP literal
is read the way a browser's URL parser reads it (`127.1`, `0x7f000001`), and
an address must pass `is_public_address`, which refuses loopback, private,
link-local, carrier-grade NAT, multicast, reserved and every other
special-purpose block, and judges IPv4-mapped and NAT64 addresses by the
IPv4 address inside them. `is_own_network` then refuses a public address
that leads back to this machine: the source address the kernel would use to
reach it, or, for IPv6, that address's /64.

A host name must have at least two labels, and it is resolved as an
absolute name (with a trailing dot), so DNS search domains never apply.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import IPv6Address, ip_address
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from spadework.sandbox.guard import (
    Resolver,
    is_own_network,
    is_public_address,
    system_resolve,
    whatwg_ipv4,
)

if TYPE_CHECKING:
    from ipaddress import IPv4Address

    import httpx

    from spadework.sandbox.guard import Probe

DEFAULT_PORTS = {"http": 80, "https": 443}

# Lower-case ASCII labels of at most 63 characters, which is what httpx
# leaves after IDNA encoding, and what TLS will accept as a server name.
_HOST_NAME = re.compile(r"[a-z0-9_-]{1,63}(?:\.[a-z0-9_-]{1,63})*")
# How much of a host name a message shows.
_SHOWN_HOST = 60


class Blocked(Exception):
    """The URL breaks a rule of I9. Nothing was fetched."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Unresolvable(Exception):
    """The host name has no addresses."""

    def __init__(self, host: str) -> None:
        super().__init__(f"could not resolve {host}")
        self.host = host


@dataclass(frozen=True)
class Target:
    """A vetted URL: where to connect, and the name to present there."""

    url: httpx.URL
    host: str  # lower-case ASCII, no trailing dot; an IPv6 address has no brackets
    port: int
    addresses: tuple[str, ...]  # public, in the resolver's order

    @property
    def host_header(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port == DEFAULT_PORTS[self.url.scheme]:
            return host
        return f"{host}:{self.port}"

    def request_url(self, address: str) -> httpx.URL:
        """The URL with its host replaced by one of the vetted addresses."""
        if address not in self.addresses:
            raise ValueError("not one of the target's vetted addresses")
        return self.url.copy_with(host=address, fragment=None)


def vet(
    raw: str, resolve: Resolver = system_resolve, probe: Probe | None = None
) -> Target:
    """Check a URL against I9 and resolve it to public addresses.

    Raises `Blocked` when a rule forbids the URL and `Unresolvable` when its
    host has no addresses. Neither makes a connection. `probe` finds the
    local source address for a destination (`guard.local_address_for`).
    """
    import httpx

    if not raw:
        raise Blocked("the URL is empty")
    if "\\" in raw or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in raw):
        raise Blocked("the URL has spaces, control characters or backslashes")
    try:
        url = httpx.URL(raw)
    except (httpx.InvalidURL, ValueError, UnicodeError):
        raise Blocked("the URL is not valid") from None
    if url.scheme not in DEFAULT_PORTS:
        raise Blocked("only http and https URLs are allowed")
    if url.userinfo:
        raise Blocked("URLs with a user name or password are not allowed")
    try:
        host = url.raw_host.decode("ascii").removesuffix(".")
    except UnicodeError:
        raise Blocked("the host name is not valid") from None
    if not host:
        raise Blocked("the URL has no host")
    if "%" in host:
        raise Blocked("the host name must not be percent-encoded")
    port = DEFAULT_PORTS[url.scheme] if url.port is None else url.port
    if not 0 < port < 65536:
        raise Blocked("the port is not valid")
    if ":" in host:  # httpx keeps an IPv6 literal's brackets out of raw_host
        try:
            literal: IPv4Address | IPv6Address | None = IPv6Address(host)
        except ValueError:
            raise Blocked("the host is not a valid IPv6 address") from None
    else:
        if len(host) > 253 or not _HOST_NAME.fullmatch(host):
            raise Blocked("the host name is not valid")
        if host == "localhost" or host.endswith(".localhost"):
            raise Blocked(f"{_short(host)} is a local address")
        try:
            literal = whatwg_ipv4(host)
        except ValueError:
            raise Blocked("the host is not a valid IP address") from None
        if literal is None and "." not in host:
            # A single label is looked up in the DNS search domains, so it
            # names a machine on the local network.
            raise Blocked("the host must be a full domain name, such as example.com")
    if literal is not None:
        problem = _problem(literal, probe)
        if problem:
            raise Blocked(f"{_short(host)} is {problem}")
        return Target(url, str(literal), port, (str(literal),))
    return Target(url, host, port, _resolve_public(host, resolve, probe))


def _resolve_public(
    host: str, resolve: Resolver, probe: Probe | None
) -> tuple[str, ...]:
    try:
        # Absolute, so that the resolver does not try search domains.
        found = list(resolve(host + "."))
    except (OSError, UnicodeError):
        raise Unresolvable(_short(host)) from None
    if not found:
        raise Unresolvable(_short(host))
    addresses: list[str] = []
    for value in found:
        try:
            # A scoped address (fe80::1%en0) is link-local, so never public.
            address = ip_address(str(value).split("%", 1)[0])
        except ValueError:
            raise Blocked(f"{_short(host)} did not resolve to an IP address") from None
        problem = _problem(address, probe)
        if problem:
            raise Blocked(f"{_short(host)} resolves to {problem}")
        if str(address) not in addresses:
            addresses.append(str(address))
    return tuple(addresses)


def _problem(address: IPv4Address | IPv6Address, probe: Probe | None) -> str:
    """Why an address may not be fetched, or "" if it may."""
    if not is_public_address(address):
        return "a local or private network address"
    if is_own_network(address, probe):
        return "this computer's own address or network"
    return ""


def _short(host: str) -> str:
    return host if len(host) <= _SHOWN_HOST else host[: _SHOWN_HOST - 1] + "…"


def shown(raw: str, limit: int = 60) -> str:
    """A URL for a message: no credentials, query or fragment, one short line."""
    try:
        parts = urlsplit(raw)
        port = f":{parts.port}" if parts.port is not None else ""
        text = f"{parts.scheme}://{parts.hostname or ''}{port}{parts.path}"
    except ValueError:
        text = "(an invalid URL)"
    text = "".join(
        "?" if ord(char) < 0x20 or ord(char) == 0x7F else char for char in text
    )
    return text if len(text) <= limit else text[: limit - 1] + "…"
