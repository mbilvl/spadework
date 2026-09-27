"""Source verification, as the engine sees it: a pluggable step.

Verification runs between each research session and its draft
(docs/spec/06-records.md). The engine asks a ``SourceVerifier`` for the
status of each source. ``UncheckedVerifier`` checks nothing and marks every
source ``unchecked``: it is the engine's verifier until the real one is
plugged in (M5). With verification off (``--no-verify`` or ``[verify]
enabled = false``) no verifier is asked, and new sources stay ``unchecked``.

An ``unchecked`` source counts as not verified, so claims that rest only on
it are tagged ``[unverified]``. It is checked by the next run that drafts its
task with verification on.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from spadework.records import Source

VerificationStatus = Literal[
    "verified", "quote_not_found", "unreachable", "unsupported", "unchecked"
]


class SourceVerifier(Protocol):
    def verify_sources(
        self, sources: Sequence[Source], *, time_limit: float | None = None
    ) -> Mapping[str, VerificationStatus]:
        """The status of each source, by source id.

        ``time_limit`` is how many seconds the whole call may take. A source
        missing from the answer is ``unchecked``.
        """
        ...


class UncheckedVerifier:
    """Verifies nothing: every source is ``unchecked``."""

    def verify_sources(
        self, sources: Sequence[Source], *, time_limit: float | None = None
    ) -> Mapping[str, VerificationStatus]:
        return {source.id: "unchecked" for source in sources}
