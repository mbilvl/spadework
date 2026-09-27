"""Finding a quote in a page's text (docs/spec/06-records.md, Verification).

Both texts are normalised the same way: typographic quotes and dashes
become ASCII, Unicode NFKC, casefold, and runs of whitespace become one
space. The quote is split into fragments at each ellipsis (`…`, `...`, or
either in brackets), and every fragment must appear in the page, in order.
There is no fuzzy matching: a quote is verbatim or it is not found.

Some quotes prove nothing even when found, and are not checked
(`unchecked`): fragments of fewer than 3 words, more than 4 fragments (short
pieces can be stitched together from anywhere on a page), and a fragment
that is part of the page's own address (the page may only be echoing it,
like a search page does).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import unquote_plus

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

MIN_WORDS = 3
MAX_FRAGMENTS = 4

# Typographic quotes and primes, and dashes and the minus sign, by code point.
_SINGLE_QUOTES = "\u2018\u2019\u201a\u201b\u2032"
_DOUBLE_QUOTES = "\u201c\u201d\u201e\u201f\u2033"
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
# Characters a browser does not show: soft hyphen, zero-width space, joiners,
# word joiner and a stray byte-order mark.
_INVISIBLE = "\u00ad\u200b\u200c\u200d\u2060\ufeff"

_TABLE = str.maketrans(
    {
        **dict.fromkeys(map(ord, _SINGLE_QUOTES), "'"),
        **dict.fromkeys(map(ord, _DOUBLE_QUOTES), '"'),
        **dict.fromkeys(map(ord, _DASHES), "-"),
        **dict.fromkeys(map(ord, _INVISIBLE), None),
    }
)

# NFKC has already turned … into three dots.
_ELLIPSIS = re.compile(r"\s*[\[(]?\s*\.{3,}\s*[\])]?\s*")
_FRAGMENT_EDGES = " \"'"


def normalise(text: str) -> str:
    """Text in the form quotes and pages are compared in."""
    # Before NFKC, which turns a double prime into two single primes; and
    # after, since NFKC turns some compatibility characters into dashes.
    text = unicodedata.normalize("NFKC", text.translate(_TABLE))
    text = text.casefold().translate(_TABLE)
    return " ".join(text.split())


def fragments(quote: str) -> list[str]:
    """The normalised pieces of a quote, split at each ellipsis."""
    pieces = _ELLIPSIS.split(normalise(quote))
    return [
        piece.strip(_FRAGMENT_EDGES) for piece in pieces if piece.strip(_FRAGMENT_EDGES)
    ]


@dataclass(frozen=True)
class Match:
    """Where the search for a quote's fragments ended."""

    fragments: int
    missing: int | None = None  # the first fragment not found in order, from 0
    out_of_order: bool = False  # that fragment is on the page, but earlier

    @property
    def found(self) -> bool:
        return self.fragments > 0 and self.missing is None


def unchecked(parts: Sequence[str], urls: Iterable[str] = ()) -> str:
    """Why a quote's fragments cannot be checked on a page, or "" if they can.

    `urls` are the addresses the page was fetched from, redirects included.
    """
    if not parts:
        return "the quote has no text to look for"
    if len(parts) > MAX_FRAGMENTS:
        return (
            f"the quote has more than {MAX_FRAGMENTS} fragments, "
            "too many to check reliably"
        )
    if any(len(part.split()) < MIN_WORDS for part in parts):
        return (
            f"a fragment of the quote has fewer than {MIN_WORDS} words, "
            "too few to check reliably"
        )
    addresses = [normalise(unquote_plus(url)) for url in urls]
    if any(part in address for part in parts for address in addresses):
        return (
            "the quote is part of the page's address, "
            "so the page may only be repeating it"
        )
    return ""


def find(quote: str, page: str) -> Match:
    """Look for every fragment of a quote, in order, in normalised page text."""
    return locate(fragments(quote), page)


def locate(parts: Sequence[str], page: str) -> Match:
    """Look for normalised fragments, in order, in normalised page text."""
    position = 0
    for index, part in enumerate(parts):
        at = page.find(part, position)
        if at < 0:
            return Match(len(parts), index, out_of_order=part in page)
        position = at + len(part)
    return Match(len(parts))
