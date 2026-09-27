"""Checking a source's quote against its page (docs/spec/06-records.md).

`verify_source` checks one URL and quote. `verify_sources` checks a
session's sources a page at a time over one transport, fetching each URL
once, and keeps only the page it is checking in memory. A check always
returns a `Verification`; it never raises.

Once a page has loaded, its text is read in a separate process with a time
limit (`reader`). A quote is `verified` only when it was found on the page
it cites and the check means something. A page that redirects to another
site, a quote in fragments too short or too many to prove anything, and a
quote that is part of the page's address (`match.unchecked`) are all
`unsupported`.
"""

from __future__ import annotations

import datetime as dt
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from spadework.verify.fetch import MAX_BYTES, TIMEOUT_S, Deadline, Fetcher, FetchFailed
from spadework.verify.match import fragments, locate, unchecked
from spadework.verify.reader import READ_TIMEOUT_S, read_page

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence
    from types import TracebackType

    import httpx

    from spadework.records import Source
    from spadework.sandbox.guard import Probe, Resolver
    from spadework.verify.reader import Reader

Status = Literal["verified", "quote_not_found", "unreachable", "unsupported"]

BADGES: Mapping[Status, str] = {
    "verified": "✓ verified",
    "quote_not_found": "✗ quote not found",
    "unreachable": "? unreachable",
    "unsupported": "? could not check",
}

_DEFAULT_PORTS = {"http": 80, "https": 443}
_SHOWN_HOST = 60


@dataclass(frozen=True)
class Verification:
    """The outcome of checking one quote against its page."""

    status: Status
    detail: str  # one line, for people and for the draft session
    checked_at: dt.datetime  # UTC, to the second
    final_url: str | None = None  # the URL that answered, after redirects

    @property
    def badge(self) -> str:
        return BADGES[self.status]


@dataclass(frozen=True)
class _Loaded:
    urls: tuple[str, ...]  # every URL requested, the page's own last
    text: str  # normalised, and cut if too long
    body_cut: bool  # the page was over MAX_BYTES
    text_cut: bool  # its text was over reader.MAX_TEXT_CHARS
    needs_javascript: bool

    @property
    def url(self) -> str:
        return self.urls[-1]


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Verifier:
    """Checks quotes against pages over one transport.

    Sequential: use it from one thread. Close it, or use it as a context
    manager, to close the transport it made. `reader` reads a page's text
    (`reader.read_page`, in a child process, unless a test swaps it) and
    `probe` finds the local source address for a destination
    (`guard.local_address_for`).
    """

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver | None = None,
        probe: Probe | None = None,
        reader: Reader | None = None,
        now: Callable[[], dt.datetime] | None = None,
        clock: Callable[[], float] | None = None,
        timeout: float = TIMEOUT_S,
        read_timeout: float = READ_TIMEOUT_S,
    ) -> None:
        self._fetcher = Fetcher(transport=transport, resolver=resolver, probe=probe)
        self._reader = reader or read_page
        self._now = now or _utc_now
        self._clock = clock or time.monotonic
        self._timeout = timeout
        self._read_timeout = read_timeout

    def __enter__(self) -> Verifier:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        self._fetcher.close()

    def verify(
        self, url: str, quote: str, *, until: float | None = None
    ) -> Verification:
        """Check that `quote` is on the page at `url`.

        `until` is a time on the verifier's clock by which the work must end,
        such as the end of a run's time for verification.
        """
        return self.verify_quotes(url, [quote], until=until)[0]

    def verify_quotes(
        self, url: str, quotes: Sequence[str], *, until: float | None = None
    ) -> list[Verification]:
        """Check several quotes against one page, which is loaded once."""
        loaded = self._load(url, until)
        if isinstance(loaded, FetchFailed):
            return [
                self._result(loaded.status, loaded.detail, loaded.final_url)
                for _ in quotes
            ]
        return [self._check(quote, loaded) for quote in quotes]

    def _load(self, url: str, until: float | None) -> _Loaded | FetchFailed:
        try:
            seconds = self._timeout
            if until is not None:
                seconds = min(seconds, until - self._clock())
                if seconds <= 0:
                    detail = "not checked: the time for verification ran out"
                    return FetchFailed("unreachable", detail)
            page = self._fetcher.fetch(url, Deadline.after(seconds, self._clock))
            read_timeout = self._read_timeout
            if until is not None:
                # Reading may overrun the run's time by a second at most.
                read_timeout = min(read_timeout, max(1.0, until - self._clock()))
            read = self._reader(page, read_timeout)
            if read is None:
                return FetchFailed(
                    "unsupported", "the page took too long to read", page.url
                )
            return _Loaded(
                page.urls, read.text, page.truncated, read.cut, read.needs_javascript
            )
        except FetchFailed as failed:
            return failed
        except Exception:
            # Verification never raises, whatever the page or the network does.
            return FetchFailed(
                "unreachable", "network error: the page could not be checked"
            )

    def _check(self, quote: str, page: _Loaded) -> Verification:
        other = _other_site(page.urls[0], page.url)
        if other:
            detail = (
                f"the page redirects to another site ({other}), "
                "so the page cited could not be checked"
            )
            return self._result("unsupported", detail, page.url)
        parts = fragments(quote)
        problem = unchecked(parts, page.urls)
        if problem:
            return self._result("unsupported", problem, page.url)
        match = locate(parts, page.text)
        if match.found:
            if match.fragments == 1:
                return self._result("verified", "the quote is on the page", page.url)
            detail = f"all {match.fragments} fragments of the quote are on the page"
            return self._result("verified", detail, page.url)
        if page.needs_javascript:
            detail = "the page seems to need JavaScript to show its text"
            return self._result("unsupported", detail, page.url)
        if not page.text:
            detail = "the page has no visible text; it may need JavaScript to show it"
            return self._result("unsupported", detail, page.url)
        if page.body_cut:
            size = f"{MAX_BYTES // 2**20} MB"
            detail = f"the page is over {size}; the quote is not in the first {size}"
            return self._result("unsupported", detail, page.url)
        if page.text_cut:
            detail = "the page's text is too long; the quote is not in the part read"
            return self._result("unsupported", detail, page.url)
        return self._result(
            "quote_not_found",
            _not_found(match.fragments, match.missing, match.out_of_order),
            page.url,
        )

    def _result(
        self, status: Status, detail: str, final_url: str | None
    ) -> Verification:
        checked_at = self._now().astimezone(dt.UTC).replace(microsecond=0)
        return Verification(status, detail, checked_at, final_url)


def _other_site(first: str, last: str) -> str:
    """The host a page redirected to, if it is another site; else "".

    The same site allows a change of `www.` and an upgrade from http to https.
    """
    try:
        before, after = urlsplit(first), urlsplit(last)
        ports = (
            before.port or _DEFAULT_PORTS.get(before.scheme),
            after.port or _DEFAULT_PORTS.get(after.scheme),
        )
    except ValueError:
        return "an invalid address"
    host = _site_host(after.hostname)
    if _site_host(before.hostname) != host:
        shown = host if len(host) <= _SHOWN_HOST else host[: _SHOWN_HOST - 1] + "…"
        return shown or "an unknown host"
    if (before.scheme, ports[0]) == (after.scheme, ports[1]):
        return ""
    if (before.scheme, ports[0], after.scheme, ports[1]) == ("http", 80, "https", 443):
        return ""
    return f"{after.scheme}://{host}:{ports[1]}"


def _site_host(host: str | None) -> str:
    return (host or "").removesuffix(".").removeprefix("www.")


def _not_found(count: int, missing: int | None, out_of_order: bool) -> str:
    if count == 1:
        return "the page loaded, but the quote is not in its text"
    position = (missing or 0) + 1
    if out_of_order:
        return (
            f"fragment {position} of {count} of the quote is on the page, "
            f"but not after fragment {position - 1}"
        )
    return f"fragment {position} of {count} of the quote is not in the page's text"


def verify_source(
    url: str,
    quote: str,
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    probe: Probe | None = None,
    reader: Reader | None = None,
    now: Callable[[], dt.datetime] | None = None,
    clock: Callable[[], float] | None = None,
    timeout: float = TIMEOUT_S,
    read_timeout: float = READ_TIMEOUT_S,
) -> Verification:
    """Check one quote against its page. See `Verifier` for the arguments."""
    with Verifier(
        transport=transport,
        resolver=resolver,
        probe=probe,
        reader=reader,
        now=now,
        clock=clock,
        timeout=timeout,
        read_timeout=read_timeout,
    ) as verifier:
        return verifier.verify(url, quote)


def verify_sources(
    sources: Iterable[Source],
    *,
    transport: httpx.BaseTransport | None = None,
    resolver: Resolver | None = None,
    probe: Probe | None = None,
    reader: Reader | None = None,
    now: Callable[[], dt.datetime] | None = None,
    clock: Callable[[], float] | None = None,
    timeout: float = TIMEOUT_S,
    read_timeout: float = READ_TIMEOUT_S,
    time_limit: float | None = None,
) -> dict[str, Verification]:
    """Check each source's quote, keyed by source id.

    Sources that cite the same URL are checked together, when the first of
    them comes up, so each page is loaded once and only one page's text is
    held at a time. `timeout` bounds each page, redirects included, and
    `read_timeout` the reading of its text. `time_limit` bounds the whole
    call: once it has passed, the pages left are not fetched, and their
    sources are `unreachable`.
    """
    listed = list(sources)
    by_url: dict[str, list[Source]] = {}
    for source in listed:
        by_url.setdefault(source.url, []).append(source)
    tick = clock or time.monotonic
    until = None if time_limit is None else tick() + time_limit
    found: dict[str, Verification] = {}
    with Verifier(
        transport=transport,
        resolver=resolver,
        probe=probe,
        reader=reader,
        now=now,
        clock=tick,
        timeout=timeout,
        read_timeout=read_timeout,
    ) as verifier:
        for url, group in by_url.items():
            quotes = [source.quote for source in group]
            results = verifier.verify_quotes(url, quotes, until=until)
            for source, result in zip(group, results, strict=True):
                found[source.id] = result
    return {source.id: found[source.id] for source in listed}
