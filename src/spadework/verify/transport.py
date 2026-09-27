"""The HTTP transport verification fetches use unless a test injects one.

It is httpx's transport rebuilt on httpcore's public API, for two things
httpx does not offer:

- **No name resolution.** The fetcher sends each request to a vetted IP
  address, with the host name in the `Host` header and the `sni_hostname`
  extension (so TLS still checks the certificate against the name). The
  network backend here refuses to connect to anything that is not a public
  IP literal, so nothing below the fetcher can resolve a name or reach a
  private address, whatever URL it is given.
- **A total deadline.** httpx timeouts apply to each socket operation, so a
  server that sends one byte at a time can keep a request going for as long
  as it likes. Every connect, TLS handshake, read and write here is bounded
  by the time left before the request's deadline.

Connections are never reused: each request gets a fresh connection to the
address it was vetted for. This module imports httpx and httpcore, so import
it lazily.
"""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Iterable
from ipaddress import ip_address
from typing import TYPE_CHECKING, Any

import httpcore
import httpx

from spadework.sandbox.guard import is_public_address
from spadework.verify.fetch import DEADLINE, Deadline

if TYPE_CHECKING:
    import ssl
    from collections.abc import Iterator

# Only these request extensions are passed on to httpcore.
_EXTENSIONS = ("timeout", "sni_hostname")

# httpcore's exceptions and the httpx ones the fetcher handles, most
# specific first.
_EXCEPTIONS: tuple[tuple[type[Exception], type[httpx.TransportError]], ...] = (
    (httpcore.ConnectTimeout, httpx.ConnectTimeout),
    (httpcore.ReadTimeout, httpx.ReadTimeout),
    (httpcore.WriteTimeout, httpx.WriteTimeout),
    (httpcore.PoolTimeout, httpx.PoolTimeout),
    (httpcore.TimeoutException, httpx.TimeoutException),
    (httpcore.ConnectError, httpx.ConnectError),
    (httpcore.ReadError, httpx.ReadError),
    (httpcore.WriteError, httpx.WriteError),
    (httpcore.NetworkError, httpx.NetworkError),
    (httpcore.RemoteProtocolError, httpx.RemoteProtocolError),
    (httpcore.LocalProtocolError, httpx.LocalProtocolError),
    (httpcore.ProtocolError, httpx.ProtocolError),
    (httpcore.UnsupportedProtocol, httpx.UnsupportedProtocol),
)


@contextlib.contextmanager
def _mapped(
    other: type[httpx.TransportError] = httpx.ConnectError,
) -> Iterator[None]:
    """Turn every exception into an httpx one, so none escapes the fetcher.

    Anything that is not httpcore's (the ssl module rejecting a server name,
    say) becomes `other`.
    """
    try:
        yield
    except httpx.TransportError:
        raise
    except Exception as exc:
        for core, mapped in _EXCEPTIONS:
            if isinstance(exc, core):
                raise mapped(str(exc)) from exc
        raise other(f"{type(exc).__name__}: {exc}") from exc


class GuardedTransport(httpx.BaseTransport):
    """An HTTP/1.1 transport that connects only to public IP literals."""

    def __init__(self, *, backend: httpcore.NetworkBackend | None = None) -> None:
        self._backend = _GuardedBackend(backend or httpcore.SyncBackend())
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            max_keepalive_connections=0,
            http1=True,
            http2=False,
            retries=0,
            network_backend=self._backend,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if not isinstance(request.stream, httpx.SyncByteStream):
            raise TypeError("the request body must be a synchronous stream")
        core = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=request.url.raw_host,
                port=request.url.port,
                target=request.url.raw_path,
            ),
            headers=request.headers.raw,
            content=request.stream,
            extensions={
                key: request.extensions[key]
                for key in _EXTENSIONS
                if key in request.extensions
            },
        )
        with self._backend.deadline(request.extensions.get(DEADLINE)), _mapped():
            response = self._pool.handle_request(core)
        if not isinstance(response.stream, Iterable):
            raise TypeError("httpcore returned an asynchronous stream")
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_ResponseStream(response.stream),
            extensions=response.extensions,
        )

    def close(self) -> None:
        with _mapped():
            self._pool.close()


class _ResponseStream(httpx.SyncByteStream):
    def __init__(self, stream: Iterable[bytes]) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        with _mapped(httpx.ReadError):
            yield from self._stream

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            with _mapped(httpx.ReadError):
                close()


def _bounded(
    timeout: float | None, deadline: Deadline | None, expired: type[Exception]
) -> float | None:
    """The smaller of an operation's timeout and the time left, or raise."""
    if deadline is None:
        return timeout
    left = deadline.remaining()
    if left <= 0:
        raise expired("the deadline for this fetch has passed")
    return left if timeout is None else min(timeout, left)


class _GuardedBackend(httpcore.NetworkBackend):
    def __init__(self, inner: httpcore.NetworkBackend) -> None:
        self._inner = inner
        self._local = threading.local()

    @contextlib.contextmanager
    def deadline(self, deadline: object) -> Iterator[None]:
        """Bind a deadline to the connections opened in this block.

        Connections are opened inside `handle_request`, in the caller's
        thread, and each serves one request, so the stream keeps its deadline
        for the whole response.
        """
        previous = getattr(self._local, "deadline", None)
        self._local.deadline = deadline if isinstance(deadline, Deadline) else None
        try:
            yield
        finally:
            self._local.deadline = previous

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        try:
            address = ip_address(host)
        except ValueError:
            raise httpcore.ConnectError(
                "spadework connects only to vetted IP addresses"
            ) from None
        if not is_public_address(address):
            raise httpcore.ConnectError(
                "spadework does not connect to local or private network addresses"
            )
        deadline: Deadline | None = getattr(self._local, "deadline", None)
        stream = self._inner.connect_tcp(
            host,
            port,
            timeout=_bounded(timeout, deadline, httpcore.ConnectTimeout),
            local_address=local_address,
            socket_options=socket_options,
        )
        return _DeadlineStream(stream, deadline)

    def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        raise httpcore.ConnectError("spadework does not use Unix sockets")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class _DeadlineStream(httpcore.NetworkStream):
    def __init__(
        self, inner: httpcore.NetworkStream, deadline: Deadline | None
    ) -> None:
        self._inner = inner
        self._deadline = deadline

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        timeout = _bounded(timeout, self._deadline, httpcore.ReadTimeout)
        return self._inner.read(max_bytes, timeout)

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        timeout = _bounded(timeout, self._deadline, httpcore.WriteTimeout)
        self._inner.write(buffer, timeout)

    def close(self) -> None:
        self._inner.close()

    def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.NetworkStream:
        timeout = _bounded(timeout, self._deadline, httpcore.ConnectTimeout)
        stream = self._inner.start_tls(ssl_context, server_hostname, timeout)
        return _DeadlineStream(stream, self._deadline)

    def get_extra_info(self, info: str) -> Any:
        return self._inner.get_extra_info(info)
