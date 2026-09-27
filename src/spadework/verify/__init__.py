"""Source verification: is each quote really on its page?

See docs/spec/06-records.md (Verification) and docs/spec/07-security.md (I9).
Plain code, no model. Importing this package does not import httpx; the
first fetch does.
"""

from spadework.verify.fetch import MAX_BYTES, MAX_REDIRECTS, TIMEOUT_S, USER_AGENT
from spadework.verify.verifier import (
    BADGES,
    Status,
    Verification,
    Verifier,
    verify_source,
    verify_sources,
)

__all__ = [
    "BADGES",
    "MAX_BYTES",
    "MAX_REDIRECTS",
    "TIMEOUT_S",
    "USER_AGENT",
    "Status",
    "Verification",
    "Verifier",
    "verify_source",
    "verify_sources",
]
