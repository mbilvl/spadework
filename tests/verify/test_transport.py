"""The default transport at the socket level, with a fake network under it.

These prove the pinning where it matters: the TCP connection is made to the
address that was vetted, TLS is told the host name (for SNI and the
certificate check), and the `Host` header carries the name. Below the
fetcher nothing resolves a name or reaches a private address, and every
socket operation is bounded by the fetch's deadline.
"""

from __future__ import annotations

import ssl
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpcore
import httpx
import pytest

from spadework.verify import Verifier
from spadework.verify.fetch import DEADLINE, Deadline
from spadework.verify.reader import read_in_process
from spadework.verify.transport import GuardedTransport
from tests.verify.conftest import PUBLIC, PUBLIC_V6, Clock, Resolver, probe

BODY = b"<html><body><p>Free plan: up to 20 editors.</p></body></html>"
OK = (
    b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
    b"Content-Length: " + str(len(BODY)).encode() + b"\r\n\r\n" + BODY
)


@dataclass
class Network:
    """What the fake network saw."""

    connects: list[tuple[str, int, float | None]] = field(default_factory=list)
    server_names: list[str | None] = field(default_factory=list)
    written: bytearray = field(default_factory=bytearray)
    read_timeouts: list[float | None] = field(default_factory=list)
    closed: int = 0


class FakeStream(httpcore.NetworkStream):
    def __init__(self, backend: FakeBackend) -> None:
        self.backend = backend
        self.buffer = backend.response

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        self.backend.network.read_timeouts.append(timeout)
        if self.backend.on_read is not None:
            self.backend.on_read()
        size = min(max_bytes, self.backend.read_size)
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.backend.network.written += buffer

    def close(self) -> None:
        self.backend.network.closed += 1

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        assert ssl_context.check_hostname
        assert ssl_context.verify_mode == ssl.CERT_REQUIRED
        self.backend.network.server_names.append(server_hostname)
        return self

    def get_extra_info(self, info: str) -> Any:
        return None


class FakeBackend(httpcore.NetworkBackend):
    def __init__(self, response: bytes = OK, read_size: int = 65536) -> None:
        self.network = Network()
        self.response = response
        self.read_size = read_size
        self.on_read: Any = None
        self.refuse: set[str] = set()

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        self.network.connects.append((host, port, timeout))
        if host in self.refuse:
            raise httpcore.ConnectError("connection refused")
        return FakeStream(self)

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        raise AssertionError("never used")


def verify(backend: FakeBackend, url: str, **options: Any) -> Any:
    options.setdefault("probe", probe)
    options.setdefault("reader", read_in_process)
    with Verifier(
        transport=GuardedTransport(backend=backend), resolver=Resolver(), **options
    ) as verifier:
        return verifier.verify(url, "up to 20 editors")


class TestPinning:
    def test_https_connects_to_the_vetted_address_and_presents_the_name(self) -> None:
        backend = FakeBackend()
        result = verify(backend, "https://example.com/pricing")
        assert result.status == "verified"
        network = backend.network
        assert [(host, port) for host, port, _ in network.connects] == [(PUBLIC, 443)]
        assert network.server_names == ["example.com"]
        request = bytes(network.written)
        assert request.startswith(b"GET /pricing HTTP/1.1\r\n")
        assert b"\r\nHost: example.com\r\n" in request
        assert network.closed == 1

    def test_http_connects_to_the_vetted_address_without_tls(self) -> None:
        backend = FakeBackend()
        assert verify(backend, "http://example.com:8080/").status == "verified"
        assert [(h, p) for h, p, _ in backend.network.connects] == [(PUBLIC, 8080)]
        assert backend.network.server_names == []
        assert b"\r\nHost: example.com:8080\r\n" in bytes(backend.network.written)

    def test_ipv6(self) -> None:
        backend = FakeBackend()
        assert verify(backend, "https://v6.example.com/").status == "verified"
        assert [h for h, _, _ in backend.network.connects] == [PUBLIC_V6]
        assert backend.network.server_names == ["v6.example.com"]

    def test_falls_back_to_the_next_vetted_address(self) -> None:
        backend = FakeBackend()
        backend.refuse = {PUBLIC_V6}
        assert verify(backend, "https://dual.example.com/").status == "verified"
        assert [h for h, _, _ in backend.network.connects] == [PUBLIC_V6, PUBLIC]

    def test_every_connection_is_new(self) -> None:
        backend = FakeBackend()
        transport = GuardedTransport(backend=backend)
        with Verifier(
            transport=transport, resolver=Resolver(), probe=probe
        ) as verifier:
            verifier.verify("https://example.com/a", "x")
            verifier.verify("https://example.com/b", "x")
        assert len(backend.network.connects) == 2
        assert backend.network.closed == 2


def send(backend: FakeBackend, url: str, **extensions: Any) -> httpx.Response:
    request = httpx.Request("GET", url, extensions=extensions)
    return GuardedTransport(backend=backend).handle_request(request)


class TestRefusals:
    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/",
            "http://localhost/",
            "http://127.1/",
            "http://127.0.0.1/",
            "http://10.0.0.1/",
            "http://169.254.169.254/",
            "http://[::1]/",
            "http://[::ffff:127.0.0.1]/",
            "http://[fe80::1]/",
        ],
    )
    def test_nothing_but_a_public_ip_literal_is_connected_to(self, url: str) -> None:
        backend = FakeBackend()
        with pytest.raises(httpx.ConnectError, match="spadework"):
            send(backend, url)
        assert backend.network.connects == []

    def test_unix_sockets_are_refused(self) -> None:
        backend = FakeBackend()
        guarded = GuardedTransport(backend=backend)._backend
        with pytest.raises(httpcore.ConnectError, match="Unix sockets"):
            guarded.connect_unix_socket("/var/run/docker.sock")

    def test_errors_are_httpx_errors(self) -> None:
        backend = FakeBackend(response=b"NOT HTTP\r\n\r\n")
        with pytest.raises(httpx.RemoteProtocolError):
            send(backend, f"http://{PUBLIC}/")

    def test_close(self) -> None:
        GuardedTransport(backend=FakeBackend()).close()


class TestDeadline:
    def test_socket_timeouts_never_outlast_the_deadline(self) -> None:
        clock = Clock()
        backend = FakeBackend()
        response = send(
            backend,
            f"http://{PUBLIC}/",
            timeout=httpx.Timeout(30).as_dict(),
            **{DEADLINE: Deadline(clock() + 4, 4, clock)},
        )
        response.read()
        assert backend.network.connects[0][2] == 4
        assert backend.network.read_timeouts[0] == 4

    def test_a_server_that_trickles_its_headers_is_cut_off(self) -> None:
        # One byte every 4 s: each read is within its own timeout, but the
        # whole response is not within the 10 s the fetch has.
        clock = Clock()
        backend = FakeBackend(read_size=1)
        backend.on_read = lambda: clock.advance(4)
        result = verify(backend, "https://example.com/", clock=clock)
        assert result.status == "unreachable"
        assert result.detail == "timed out: the page did not load within 10 s"
        assert backend.network.read_timeouts == [10, 6, 2]

    def test_without_a_deadline_timeouts_pass_through(self) -> None:
        backend = FakeBackend()
        send(backend, f"http://{PUBLIC}/", timeout=httpx.Timeout(7).as_dict()).read()
        assert backend.network.connects[0][2] == 7
        assert backend.network.read_timeouts[0] == 7


class TestNothingEscapes:
    """Review item 6: an exception that is not httpcore's becomes an httpx one."""

    def test_tls_refusing_a_server_name(self) -> None:
        class Refusing(FakeStream):
            def start_tls(
                self,
                ssl_context: ssl.SSLContext,
                server_hostname: str | None = None,
                timeout: float | None = None,
            ) -> httpcore.NetworkStream:
                # What the ssl module does with a label over 63 characters.
                raise UnicodeError("encoding with 'idna' codec failed")

        class Backend(FakeBackend):
            def connect_tcp(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
                return Refusing(self)

        result = verify(Backend(), "https://example.com/")
        assert result.status == "unreachable"
        assert result.detail.startswith(
            "network error: could not connect (UnicodeError"
        )

    def test_a_failure_while_reading(self) -> None:
        class Failing(FakeStream):
            def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
                if self.buffer != self.backend.response:
                    raise ValueError("unexpected")
                return super().read(len(OK) - 10, timeout)

        class Backend(FakeBackend):
            def connect_tcp(self, *args: Any, **kwargs: Any) -> httpcore.NetworkStream:
                return Failing(self)

        with pytest.raises(httpx.TransportError):
            send(Backend(), f"http://{PUBLIC}/").read()
