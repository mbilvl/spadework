"""Source verification, as the engine sees it: a pluggable step.

Verification runs between each research session and its draft
(docs/spec/06-records.md), and again for the whole pack with ``spadework
verify``. The engine asks a ``SourceVerifier`` for the result of each source.
``NetworkVerifier`` is the real one: it checks each quote against its page
with ``spadework.verify``, under the rules of I9. ``UncheckedVerifier``
checks nothing. With verification off (``--no-verify`` or ``[verify]
enabled = false``) no verifier is asked, and new sources stay ``unchecked``.

An ``unchecked`` source counts as not verified, so claims that rest only on
it are tagged ``[unverified]``. It is checked by the next run that drafts its
task with verification on.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal, Protocol

from spadework.models import StrictModel

if TYPE_CHECKING:
    from spadework.records import Source

VerificationStatus = Literal[
    "verified", "quote_not_found", "unreachable", "unsupported", "unchecked"
]
STATUSES: tuple[VerificationStatus, ...] = (
    "verified",
    "quote_not_found",
    "unreachable",
    "unsupported",
    "unchecked",
)

BADGES: Mapping[VerificationStatus, str] = {
    "verified": "✓ verified",
    "quote_not_found": "✗ quote not found",
    "unreachable": "? unreachable",
    "unsupported": "? could not check",
    "unchecked": "? not checked",
}
"""How each status is shown in the pack (docs/spec/06-records.md)."""


class VerificationResult(StrictModel):
    """What checking one source found, as its research record keeps it."""

    status: VerificationStatus = "unchecked"
    detail: str | None = None
    """One line on why, for people and for the draft session. It never
    repeats text from the page or the server (I9)."""
    checked_at: str | None = None
    """When it was checked, UTC, such as 2026-09-27T21:47:02Z."""
    final_url: str | None = None
    """The URL that answered, after redirects."""

    @property
    def badge(self) -> str:
        return BADGES[self.status]


UNCHECKED = VerificationResult()

Found = VerificationResult | VerificationStatus


class SourceVerifier(Protocol):
    def verify_sources(
        self, sources: Sequence[Source], *, time_limit: float | None = None
    ) -> Mapping[str, Found]:
        """The result of each source, by source id: a ``VerificationResult``,
        or just its status.

        ``time_limit`` is how many seconds the whole call may take. A source
        missing from the answer is ``unchecked``.
        """
        ...


def as_result(found: Found) -> VerificationResult:
    if isinstance(found, VerificationResult):
        return found
    return VerificationResult(status=found)


class UncheckedVerifier:
    """Verifies nothing: every source is ``unchecked``."""

    def verify_sources(
        self, sources: Sequence[Source], *, time_limit: float | None = None
    ) -> Mapping[str, Found]:
        return {source.id: "unchecked" for source in sources}


class NetworkVerifier:
    """Checks each quote against its page, over the network (I9).

    ``options`` go to ``spadework.verify.verify_sources``: tests pass a
    ``transport``, a ``resolver``, a ``probe`` and a ``reader``, so nothing
    reaches the network.
    """

    def __init__(self, **options: Any) -> None:
        self.options = options

    def verify_sources(
        self, sources: Sequence[Source], *, time_limit: float | None = None
    ) -> Mapping[str, Found]:
        from spadework.verify import verify_sources

        found = verify_sources(sources, time_limit=time_limit, **self.options)
        return {
            source_id: VerificationResult(
                status=result.status,
                detail=result.detail,
                checked_at=_timestamp(result.checked_at),
                final_url=result.final_url,
            )
            for source_id, result in found.items()
        }


def _timestamp(moment: dt.datetime) -> str:
    return moment.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def counts(statuses: Sequence[VerificationStatus]) -> dict[VerificationStatus, int]:
    """How many sources have each status, in the order of ``STATUSES``."""
    return {status: statuses.count(status) for status in STATUSES}
