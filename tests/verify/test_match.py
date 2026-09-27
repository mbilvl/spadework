"""Quote normalisation and fragment matching (docs/spec/06-records.md, step 3-4)."""

from __future__ import annotations

import pytest

from spadework.verify.match import Match, find, fragments, normalise

LEFT_SINGLE, RIGHT_SINGLE = "\u2018", "\u2019"
LEFT_DOUBLE, RIGHT_DOUBLE = "\u201c", "\u201d"
LOW_DOUBLE = "\u201e"
PRIME, DOUBLE_PRIME = "\u2032", "\u2033"
HYPHEN, NB_HYPHEN, FIGURE_DASH = "\u2010", "\u2011", "\u2012"
EN_DASH, EM_DASH, BAR, MINUS = "\u2013", "\u2014", "\u2015", "\u2212"
NBSP, THIN_SPACE, IDEOGRAPHIC_SPACE = "\u00a0", "\u2009", "\u3000"
SOFT_HYPHEN, ZWSP = "\u00ad", "\u200b"
ELLIPSIS = "…"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        pytest.param(f"Acme{RIGHT_SINGLE}s plan", "acme's plan", id="apostrophe"),
        pytest.param(f"{LEFT_SINGLE}free{RIGHT_SINGLE}", "'free'", id="single-quotes"),
        pytest.param(f"{LEFT_DOUBLE}free{RIGHT_DOUBLE}", '"free"', id="double-quotes"),
        pytest.param(f"{LOW_DOUBLE}frei{LEFT_DOUBLE}", '"frei"', id="low-double"),
        pytest.param(f"5{PRIME} 2{DOUBLE_PRIME}", "5' 2\"", id="primes"),
        pytest.param(
            f"a{HYPHEN}b{NB_HYPHEN}c{FIGURE_DASH}d{EN_DASH}e{EM_DASH}f{BAR}g{MINUS}h",
            "a-b-c-d-e-f-g-h",
            id="dashes",
        ),
        pytest.param(
            f"up{NBSP}to{THIN_SPACE}20{IDEOGRAPHIC_SPACE}editors",
            "up to 20 editors",
            id="spaces",
        ),
        pytest.param("  Free\n\tPlan  ", "free plan", id="whitespace"),
        pytest.param("FREE Plan", "free plan", id="case"),
        pytest.param("Straße", "strasse", id="casefold"),
        pytest.param(
            "\uff26\uff52\uff45\uff45 \uff11\uff10", "free 10", id="fullwidth"
        ),
        pytest.param("\ufb01le", "file", id="ligature"),
        pytest.param(f"extra{SOFT_HYPHEN}ordinary", "extraordinary", id="soft-hyphen"),
        pytest.param(f"zero{ZWSP}width", "zerowidth", id="zero-width"),
        pytest.param(f"wait{ELLIPSIS}", "wait...", id="ellipsis"),
        pytest.param("cafe\u0301", "café", id="combining"),
        pytest.param("\ufe58", "-", id="small-em-dash"),
    ],
)
def test_normalise(text: str, expected: str) -> None:
    assert normalise(text) == expected


@pytest.mark.parametrize(
    ("quote", "expected"),
    [
        pytest.param("Free plan", ["free plan"], id="one"),
        pytest.param(
            f"Free plan {ELLIPSIS} 20 editors",
            ["free plan", "20 editors"],
            id="ellipsis",
        ),
        pytest.param(
            "Free plan...20 editors", ["free plan", "20 editors"], id="three-dots"
        ),
        pytest.param("Free plan .... 20", ["free plan", "20"], id="four-dots"),
        pytest.param(f"Free plan [{ELLIPSIS}] 20", ["free plan", "20"], id="brackets"),
        pytest.param("Free plan (...) 20", ["free plan", "20"], id="parentheses"),
        pytest.param(f"{ELLIPSIS}free plan{ELLIPSIS}", ["free plan"], id="edges"),
        pytest.param(
            f'"Free plan{ELLIPSIS} 20"', ["free plan", "20"], id="outer-quotes"
        ),
        pytest.param(
            f"{LEFT_DOUBLE}Free plan{RIGHT_DOUBLE}", ["free plan"], id="curly-outer"
        ),
        pytest.param(f"{ELLIPSIS} ...", [], id="only-ellipses"),
    ],
)
def test_fragments(quote: str, expected: list[str]) -> None:
    assert fragments(quote) == expected


PAGE = normalise(
    f"ExampleCMS pricing. Free plan {EM_DASH} for small teams: up to 20{NBSP}editors, "
    f"10,000 documents and Acme{RIGHT_SINGLE}s "
    f"{LEFT_DOUBLE}starter{RIGHT_DOUBLE} theme."
)


@pytest.mark.parametrize(
    "quote",
    [
        pytest.param("Free plan - for small teams", id="dash"),
        pytest.param("free plan \u2014 FOR SMALL TEAMS", id="case-and-em-dash"),
        pytest.param("up to 20 editors", id="nbsp"),
        pytest.param('Acme\'s "starter" theme', id="straight-quotes"),
        pytest.param(
            f"Free plan {ELLIPSIS} up to 20 editors {ELLIPSIS} 10,000 documents",
            id="fragments",
        ),
        pytest.param("Free plan...10,000 documents", id="dots"),
        pytest.param("  up   to\n20 editors ", id="spacing"),
        pytest.param("ExampleCMS pricing.", id="start"),
        pytest.param("theme.", id="end"),
    ],
)
def test_found(quote: str) -> None:
    match = find(quote, PAGE)
    assert match.found
    assert match.missing is None


def test_a_planted_quote_is_not_found() -> None:
    match = find("Free plan: unlimited editors and documents", PAGE)
    assert match == Match(fragments=1, missing=0, out_of_order=False)
    assert not match.found


def test_fragments_must_be_in_order() -> None:
    match = find(f"10,000 documents {ELLIPSIS} up to 20 editors", PAGE)
    assert match == Match(fragments=2, missing=1, out_of_order=True)


def test_the_first_missing_fragment_is_reported() -> None:
    match = find(f"Free plan {ELLIPSIS} 50 editors {ELLIPSIS} 10,000 documents", PAGE)
    assert match == Match(fragments=3, missing=1, out_of_order=False)


def test_a_fragment_cannot_reuse_text_matched_by_the_one_before() -> None:
    page = normalise("the plan")
    assert find(f"the plan {ELLIPSIS} plan", page).missing == 1
    assert find(f"the plan {ELLIPSIS} plan", normalise("the plan and plan")).found


def test_an_empty_quote_is_never_found() -> None:
    match = find(ELLIPSIS, PAGE)
    assert match.fragments == 0
    assert not match.found


def test_words_are_not_matched_across_missing_spaces() -> None:
    # Verbatim means verbatim: no fuzzy matching (roadmap open question 4).
    assert not find("20editors", PAGE).found
