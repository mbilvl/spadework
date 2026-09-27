"""A fake web and fake DNS for the verifier's tests. Nothing here uses the network.

`Web` is the handler of an `httpx.MockTransport`. Pages are keyed by the
logical URL (scheme, the `Host` header and the path), because the requests it
receives carry the vetted IP address in their URL, not the name. It records
every request, so a test can check that a blocked URL was never fetched.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Iterator, Mapping, Sequence
from ipaddress import IPv4Address, IPv6Address, ip_address

import httpx
import pytest

from spadework.verify import Verification, verify_source
from spadework.verify.reader import read_in_process

PUBLIC = "93.184.215.14"
PUBLIC_2 = "93.184.215.15"
PUBLIC_V6 = "2606:4700::1111"
NOW = dt.datetime(2026, 9, 27, 21, 47, 2, 123456, tzinfo=dt.UTC)

DNS: dict[str, list[str]] = {
    "example.com": [PUBLIC],
    "www.example.com": [PUBLIC_2],
    "cms.example.com": [PUBLIC_2],
    "news.example.com": [PUBLIC_2],
    "evil.example.com": [PUBLIC_2],
    "v6.example.com": [PUBLIC_V6],
    "dual.example.com": [PUBLIC_V6, PUBLIC],
    "xn--bcher-kva.example.com": [PUBLIC],
    "internal.example.com": ["10.0.0.5"],
    "mixed.example.com": [PUBLIC, "127.0.0.1"],
    "metadata.example.com": ["169.254.169.254"],
    "mapped.example.com": ["::ffff:10.0.0.1"],
    "nat64.example.com": ["64:ff9b::a9fe:a9fe"],
    "scoped.example.com": ["fe80::1%en0"],
    "cgnat.example.com": ["100.64.0.1"],
    "unique-local.example.com": ["fd00::1"],
    "127.0.0.1.nip.io": ["127.0.0.1"],
    "garbage.example.com": ["not-an-address"],
    "empty.example.com": [],
}

Handler = Callable[[httpx.Request], httpx.Response]


class Resolver:
    """Stands in for DNS, and records every lookup, as asked (the verifier
    asks for absolute names, with a trailing dot)."""

    def __init__(self, table: Mapping[str, Sequence[str]] = DNS) -> None:
        self.table = {name: list(addresses) for name, addresses in table.items()}
        self.calls: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        name = host.removesuffix(".")
        if name not in self.table:
            raise OSError("no such host")
        return self.table[name]


# This machine's addresses, as the fake probe reports them: none of the
# addresses in DNS above.
LOCAL_V4 = "192.168.1.10"
LOCAL_V6 = "2001:db8:1::10"


def probe(address: IPv4Address | IPv6Address) -> IPv4Address | IPv6Address:
    """Stands in for `guard.local_address_for`."""
    return ip_address(LOCAL_V4 if address.version == 4 else LOCAL_V6)


class Clock:
    """A monotonic clock that moves only when told to."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def respond(
    body: bytes | str = b"",
    *,
    status: int = 200,
    content_type: str | None = "text/html; charset=utf-8",
    headers: Mapping[str, str] | None = None,
) -> Handler:
    """A handler that answers with a fresh streamed response each time."""
    data = body.encode("utf-8") if isinstance(body, str) else body
    all_headers = dict(headers or {})
    if content_type is not None:
        all_headers["Content-Type"] = content_type

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status, headers=all_headers, stream=httpx.ByteStream(data)
        )

    return handler


def redirect(location: str, status: int = 302) -> Handler:
    return respond(status=status, content_type=None, headers={"Location": location})


def stream(
    chunks: Iterator[bytes],
    content_type: str = "text/html",
    headers: Mapping[str, str] | None = None,
) -> Handler:
    """A handler whose body is read from an iterator, chunk by chunk."""

    class Body(httpx.SyncByteStream):
        def __iter__(self) -> Iterator[bytes]:
            yield from chunks

    def handler(request: httpx.Request) -> httpx.Response:
        all_headers = {"Content-Type": content_type, **(headers or {})}
        return httpx.Response(200, headers=all_headers, stream=Body())

    return handler


class Web:
    """Pages by logical URL, served through an httpx.MockTransport."""

    def __init__(self) -> None:
        self.pages: dict[str, Handler] = {}
        self.requests: list[httpx.Request] = []

    def add(self, url: str, handler: Handler) -> None:
        self.pages[url] = handler

    def logical_url(self, request: httpx.Request) -> str:
        path = request.url.raw_path.decode("ascii")
        return f"{request.url.scheme}://{request.headers['host']}{path}"

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.pages.get(self.logical_url(request))
        if handler is None:
            return httpx.Response(404, stream=httpx.ByteStream(b"not found"))
        return handler(request)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    @property
    def visited(self) -> list[str]:
        return [self.logical_url(request) for request in self.requests]


@pytest.fixture
def resolver() -> Resolver:
    return Resolver()


@pytest.fixture
def web() -> Web:
    return Web()


@pytest.fixture
def clock() -> Clock:
    return Clock()


Check = Callable[..., Verification]


@pytest.fixture
def check(web: Web, resolver: Resolver) -> Check:
    """Verify a quote against the fake web."""

    def run(url: str, quote: str, **options: object) -> Verification:
        options.setdefault("probe", probe)
        options.setdefault("reader", read_in_process)
        options.setdefault("now", lambda: NOW)
        return verify_source(
            url, quote, transport=web.transport, resolver=resolver, **options
        )

    return run
