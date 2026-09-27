"""Fetching a source page under the rules of I9 (docs/spec/07-security.md).

Every hop, the first request and each redirect, is vetted by `ssrf.vet`
before anything is sent, and the request goes to one of the addresses that
were vetted. Redirects are followed here, at most 5. Nothing else is sent
but the headers below: no cookies, no credentials, no proxy settings from
the environment. The body is read as a stream and reading stops at 2 MB,
and the whole fetch, redirects included, has one deadline.

httpx is imported lazily, so importing this module stays cheap.
"""

from __future__ import annotations

import codecs
import contextlib
import re
import threading
import time
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from spadework import __version__
from spadework.sandbox.guard import DNS_TIMEOUT_S, system_resolve
from spadework.verify.ssrf import SPECIAL_USE, Blocked, Unresolvable, shown, vet

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from zlib import _Decompress

    import httpx

    from spadework.sandbox.guard import Probe, Resolver
    from spadework.verify.ssrf import Target

MAX_BYTES = 2 * 1024 * 1024
MAX_REDIRECTS = 5
TIMEOUT_S = 10.0
# When a name has several addresses, how long to try each one but the last.
CONNECT_ATTEMPT_S = 3.0

USER_AGENT = f"spadework/{__version__}"
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html, text/plain",
    "Accept-Encoding": "gzip, deflate",
    "Connection": "close",
}

# A request extension carrying the fetch's `Deadline` to the transport.
DEADLINE = "spadework.deadline"

_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
# A media type or content coding is shown in a detail only if it is a plain
# token: the detail reaches a model, so no free text from the server.
_MEDIA_TYPE = re.compile(r"[a-z0-9!#$&^_.+-]{1,30}/[a-z0-9!#$&^_.+-]{1,40}")
_CODINGS = re.compile(r"[a-z0-9._-]{1,20}(?:\s*,\s*[a-z0-9._-]{1,20}){0,3}")

Kind = Literal["html", "text"]
FailureStatus = Literal["unreachable", "unsupported"]


@dataclass(frozen=True)
class Deadline:
    """When a fetch must be finished, on a monotonic clock."""

    at: float
    seconds: float  # the time it allowed, for messages
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def after(
        cls, seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> Deadline:
        return cls(clock() + seconds, seconds, clock)

    def remaining(self) -> float:
        return self.at - self.clock()

    def message(self) -> str:
        return f"timed out: the page did not load within {self.seconds:.3g} s"


@dataclass(frozen=True)
class Page:
    """A page that loaded, not yet parsed."""

    url: str  # the URL that returned it, after redirects
    urls: tuple[str, ...]  # every URL requested on the way, this one last
    kind: Kind
    body: bytes  # with any content encoding undone, at most MAX_BYTES
    charset: str | None  # from the Content-Type header
    truncated: bool  # the page is longer than MAX_BYTES


class FetchFailed(Exception):
    """The page could not be loaded, or is not something we can check."""

    def __init__(
        self, status: FailureStatus, detail: str, final_url: str | None = None
    ) -> None:
        super().__init__(detail)
        self.status: FailureStatus = status
        self.detail = detail
        self.final_url = final_url


class Fetcher:
    """Fetches pages over one transport. Not thread-safe; use it sequentially.

    With no transport it uses `GuardedTransport`, which connects only to the
    vetted address. A transport passed in (a test's `httpx.MockTransport`)
    receives the same pinned requests: an IP address in the URL, the name in
    the `Host` header and the `sni_hostname` extension.
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        probe: Probe | None = None,
    ) -> None:
        if transport is None:
            from spadework.verify.transport import GuardedTransport

            transport = GuardedTransport()
            self._owns_transport = True
        else:
            self._owns_transport = False
        self._transport = transport
        self._resolve = resolver or system_resolve
        self._probe = probe

    def close(self) -> None:
        if self._owns_transport:
            self._transport.close()

    def fetch(self, url: str, deadline: Deadline) -> Page:
        """Load a page, following redirects. Raises `FetchFailed`."""
        import httpx

        current = url
        final_url: str | None = None
        seen: set[str] = set()
        hops: list[str] = []
        for hop in range(MAX_REDIRECTS + 1):
            target = self._vet(current, hop, deadline, final_url)
            key = str(target.url.copy_with(fragment=None))
            if key in seen:
                raise FetchFailed(
                    "unreachable", "the page redirects in a loop", final_url
                )
            seen.add(key)
            try:
                response = self._send(target, deadline)
                final_url = str(target.url)
                hops.append(final_url)
                try:
                    location = _location(response)
                    if location is None:
                        return _page(response, tuple(hops), deadline)
                finally:
                    response.close()
            except httpx.TimeoutException:
                raise FetchFailed(
                    "unreachable", deadline.message(), final_url
                ) from None
            except httpx.TransportError as exc:
                detail = f"network error: {_describe(exc)}"
                raise FetchFailed("unreachable", detail, final_url) from None
            try:
                current = str(target.url.join(location))
            except (httpx.InvalidURL, ValueError, UnicodeError):
                detail = "network error: the page redirects to an invalid URL"
                raise FetchFailed("unreachable", detail, final_url) from None
        detail = f"too many redirects (more than {MAX_REDIRECTS})"
        raise FetchFailed("unreachable", detail, final_url)

    def _vet(
        self, url: str, hop: int, deadline: Deadline, final_url: str | None
    ) -> Target:
        left = deadline.remaining()
        if left <= 0:
            raise FetchFailed("unreachable", deadline.message(), final_url)
        resolve = _within(self._resolve, min(DNS_TIMEOUT_S, left))
        try:
            target = vet(url, resolve, self._probe)
        except Blocked as exc:
            if exc.reason == SPECIAL_USE:
                where = f" (the page redirects to {shown(url)})" if hop else ""
                detail = f"blocked: {SPECIAL_USE}{where}"
            else:
                where = f"the page redirects to {shown(url)}, but " if hop else ""
                detail = f"blocked: {where}{exc.reason}"
            raise FetchFailed("unreachable", detail, final_url) from None
        except Unresolvable as exc:
            if deadline.remaining() <= 0:
                raise FetchFailed(
                    "unreachable", deadline.message(), final_url
                ) from None
            detail = f"network error: could not resolve {exc.host}"
            raise FetchFailed("unreachable", detail, final_url) from None
        if deadline.remaining() <= 0:
            raise FetchFailed("unreachable", deadline.message(), final_url)
        return target

    def _send(self, target: Target, deadline: Deadline) -> httpx.Response:
        """Send the request to the first vetted address that accepts it."""
        import httpx

        *others, last = target.addresses
        for address in others:
            try:
                return self._send_to(target, address, deadline, CONNECT_ATTEMPT_S)
            except (httpx.ConnectError, httpx.ConnectTimeout):
                continue
        return self._send_to(target, last, deadline, None)

    def _send_to(
        self,
        target: Target,
        address: str,
        deadline: Deadline,
        connect_limit: float | None,
    ) -> httpx.Response:
        import httpx

        left = deadline.remaining()
        if left <= 0:
            raise httpx.ConnectTimeout(deadline.message())
        connect = left if connect_limit is None else min(left, connect_limit)
        request = httpx.Request(
            "GET",
            target.request_url(address),
            headers={"Host": target.host_header, **HEADERS},
            extensions={
                "timeout": httpx.Timeout(left, connect=connect).as_dict(),
                "sni_hostname": target.host,
                DEADLINE: deadline,
            },
        )
        return self._transport.handle_request(request)


def _within(resolve: Resolver, seconds: float) -> Resolver:
    """`resolve`, giving up after `seconds`: getaddrinfo has no timeout.

    The lookup runs in a daemon thread, which is abandoned if it is late.
    """

    def lookup(host: str) -> Sequence[str]:
        found: list[Sequence[str]] = []

        def run() -> None:
            # Any failure, whatever its type, means no addresses.
            with contextlib.suppress(Exception):
                found.append(resolve(host))

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(max(seconds, 0.0))
        if not found:
            raise OSError("the host name could not be resolved in time")
        return found[0]

    return lookup


def _location(response: httpx.Response) -> str | None:
    if response.status_code in _REDIRECTS and "location" in response.headers:
        return str(response.headers["location"])
    return None


def _page(response: httpx.Response, hops: tuple[str, ...], deadline: Deadline) -> Page:
    import httpx

    url = hops[-1]
    status = response.status_code
    if status >= 400:
        # The standard phrase, never the one the server sent.
        phrase = httpx.codes.get_reason_phrase(status)
        reason = f" {phrase}" if phrase else ""
        raise FetchFailed("unreachable", f"HTTP {status}{reason}", url)
    if not 200 <= status < 300:
        raise FetchFailed("unreachable", f"HTTP {status}: the server sent no page", url)
    media, charset = _content_type(response.headers.get("content-type", ""))
    kind = _kind(media, url)
    decoder = _Decoder.for_encoding(response.headers.get("content-encoding", ""), url)
    body, truncated = _read(response, decoder, deadline, url)
    return Page(url, hops, kind or _sniff(body, url), body, charset, truncated)


def _read(
    response: httpx.Response, decoder: _Decoder, deadline: Deadline, url: str
) -> tuple[bytes, bool]:
    """The body, stopping after MAX_BYTES on the wire or once decoded."""
    if response.is_stream_consumed:
        # The transport read the body already (an httpx.Response built with
        # content=), and httpx undid any content encoding.
        return response.content[:MAX_BYTES], len(response.content) > MAX_BYTES
    body = bytearray()
    received = 0
    truncated = False
    for chunk in response.iter_raw():
        received += len(chunk)
        if received > MAX_BYTES:
            chunk = chunk[: len(chunk) - (received - MAX_BYTES)]
            truncated = True
        body += decoder.decode(chunk, MAX_BYTES + 1 - len(body), url)
        if truncated or len(body) > MAX_BYTES:
            break
        if deadline.remaining() <= 0:
            raise FetchFailed("unreachable", deadline.message(), url)
    else:
        body += decoder.flush(MAX_BYTES + 1 - len(body), url)
    if len(body) > MAX_BYTES:
        truncated = True
        del body[MAX_BYTES:]
    return bytes(body), truncated


class _Decoder:
    """Undoes a content encoding without producing more than it is asked for.

    httpx's own decoders have no output limit, so a small compressed body
    could expand to gigabytes before the size cap saw it.
    """

    def __init__(self, encoding: str) -> None:
        self._encoding = encoding
        self._started = False
        self._zlib: _Decompress | None = None
        if encoding == "gzip":
            self._zlib = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            self._zlib = zlib.decompressobj()

    @classmethod
    def for_encoding(cls, header: str, url: str) -> _Decoder:
        codings = [
            coding.strip().lower()
            for coding in header.split(",")
            if coding.strip().lower() not in ("", "identity")
        ]
        if not codings:
            return cls("")
        if len(codings) == 1 and codings[0] in ("gzip", "x-gzip", "deflate"):
            return cls("gzip" if codings[0] == "x-gzip" else codings[0])
        shown = header.strip().lower()
        named = f" ({shown})" if _CODINGS.fullmatch(shown) else ""
        detail = f"the page uses a content encoding spadework cannot read{named}"
        raise FetchFailed("unsupported", detail, url)

    def decode(self, data: bytes, limit: int, url: str) -> bytes:
        if self._zlib is None:
            return data[:limit]
        try:
            # max_length must be positive: 0 means no limit at all.
            output = self._zlib.decompress(data, max(limit, 1))
        except zlib.error:
            if self._encoding != "deflate" or self._started:
                raise FetchFailed("unreachable", _CORRUPT, url) from None
            # Some servers send raw deflate data without the zlib header.
            self._zlib = zlib.decompressobj(-zlib.MAX_WBITS)
            self._started = True
            return self.decode(data, limit, url)
        self._started = True
        return output

    def flush(self, limit: int, url: str) -> bytes:
        if self._zlib is None:
            return b""
        try:
            return self._zlib.flush()[: max(limit, 0)]
        except zlib.error:
            raise FetchFailed("unreachable", _CORRUPT, url) from None


_CORRUPT = "network error: the compressed response is corrupt"


def _content_type(value: str) -> tuple[str, str | None]:
    """The media type, lower case, and the charset parameter if there is one."""
    media, *params = value.split(";")
    charset = None
    for param in params:
        name, _, raw = param.partition("=")
        if name.strip().lower() == "charset":
            charset = raw.strip().strip("\"'") or None
    return media.strip().lower(), charset


def _kind(media: str, url: str) -> Kind | None:
    """What the Content-Type says the page is, or None to look at the body."""
    if not media:
        return None
    if media in _HTML_TYPES:
        return "html"
    if media.startswith("text/"):
        return "text"
    if media == "application/pdf":
        raise FetchFailed("unsupported", _PDF, url)
    shown = media if _MEDIA_TYPE.fullmatch(media) else "of an unknown type"
    detail = f"the page is {shown}, not HTML or text"
    raise FetchFailed("unsupported", detail, url)


_PDF = "the page is a PDF, which spadework cannot check yet"


def _sniff(body: bytes, url: str) -> Kind:
    """The kind of a page served without a Content-Type."""
    if body.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "text"
    head = body[:1024].removeprefix(codecs.BOM_UTF8).lstrip().lower()
    if head.startswith(b"%pdf-"):
        raise FetchFailed("unsupported", _PDF, url)
    if b"\x00" in head:
        raise FetchFailed("unsupported", "the page is not HTML or text", url)
    if head.startswith(b"<!doctype html") or b"<html" in head or b"<body" in head:
        return "html"
    return "text"


def _describe(exc: httpx.TransportError) -> str:
    import httpx

    # Only errors from the operating system or TLS carry their message. A
    # protocol error's message can quote the server's bytes.
    if isinstance(exc, httpx.ConnectError):
        what, message = "could not connect", _one_line(str(exc), 100)
    elif isinstance(exc, httpx.ReadError):
        what = "the connection failed while reading the page"
        message = _one_line(str(exc), 100)
    elif isinstance(exc, httpx.RemoteProtocolError):
        what, message = "the server's response is not valid HTTP", ""
    else:
        what, message = "the request failed", ""
    return f"{what} ({message})" if message else what


def _one_line(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
