"""The verification statuses of docs/spec/06-records.md, end to end."""

from __future__ import annotations

import datetime as dt
from typing import Any

import httpx
import pytest

from spadework.records import Source
from spadework.verify import (
    BADGES,
    Verification,
    Verifier,
    verify_source,
    verify_sources,
)
from spadework.verify.reader import PageRead, read_in_process
from tests.verify.conftest import (
    NOW,
    PUBLIC,
    Check,
    Clock,
    Resolver,
    Web,
    probe,
    redirect,
    respond,
)

# A fictional pricing page, as a research session might cite it.
PRICING = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Pricing | ExampleCMS</title>
  <script>window.dataLayer = [{"plan": "Unlimited editors for free"}];</script>
</head>
<body>
  <nav><a href="/">Home</a><a href="/pricing">Pricing</a></nav>
  <main>
    <h1>Pricing</h1>
    <section>
      <h2>Free</h2>
      <p>Our Free plan is for small teams &mdash; up to 20&nbsp;editors and
         10,000 documents, with community support.</p>
      <p>Acme Fabrication&rsquo;s catalogue team uses the
         &ldquo;Starter&rdquo; theme.</p>
    </section>
  </main>
</body>
</html>"""

URL = "https://cms.example.com/pricing"


@pytest.fixture
def pricing(web: Web) -> Web:
    web.add(URL, respond(PRICING))
    return web


class TestStatuses:
    @pytest.mark.parametrize(
        "quote",
        [
            "Our Free plan is for small teams - up to 20 editors and 10,000 documents",
            "Our Free plan … up to 20 editors … 10,000 documents, with",
            "free plan is ... UP TO 20 EDITORS",
            "Acme Fabrication\u2019s catalogue team uses the \u201cStarter\u201d",
            'Acme Fabrication\'s catalogue team uses the "Starter" theme.',
        ],
    )
    def test_verified(self, quote: str, pricing: Web, check: Check) -> None:
        result = check(URL, quote)
        assert result.status == "verified"
        assert result.final_url == URL
        assert result.badge == "✓ verified"

    def test_a_planted_fake_quote_is_quote_not_found(
        self, pricing: Web, check: Check
    ) -> None:
        result = check(URL, "The Free plan includes unlimited editors and documents")
        assert result.status == "quote_not_found"
        assert result.detail == "the page loaded, but the quote is not in its text"
        assert result.badge == "✗ quote not found"

    def test_a_plausible_quote_with_one_changed_number_is_not_found(
        self, pricing: Web, check: Check
    ) -> None:
        result = check(URL, "up to 25 editors and 10,000 documents")
        assert result.status == "quote_not_found"

    def test_text_only_in_a_script_does_not_count(
        self, pricing: Web, check: Check
    ) -> None:
        assert check(URL, "Unlimited editors for free").status == "quote_not_found"

    def test_text_only_in_nav_or_title_does_not_count(
        self, pricing: Web, check: Check
    ) -> None:
        assert check(URL, "Pricing | ExampleCMS").status == "quote_not_found"
        assert check(URL, "Home Pricing Pricing Free").status == "quote_not_found"

    def test_fragments_out_of_order(self, pricing: Web, check: Check) -> None:
        result = check(URL, "and 10,000 documents … up to 20 editors")
        assert result.status == "quote_not_found"
        assert result.detail == (
            "fragment 2 of 2 of the quote is on the page, but not after fragment 1"
        )

    def test_a_missing_fragment(self, pricing: Web, check: Check) -> None:
        result = check(URL, "Our Free plan … up to 50 editors … with community support")
        assert result.detail == "fragment 2 of 3 of the quote is not in the page's text"

    def test_all_fragments_found(self, pricing: Web, check: Check) -> None:
        result = check(URL, "Our Free plan … up to 20 editors")
        assert result.detail == "all 2 fragments of the quote are on the page"

    def test_unreachable(self, web: Web, check: Check) -> None:
        result = check("https://cms.example.com/gone", "anything at all here")
        assert (result.status, result.detail) == ("unreachable", "HTTP 404 Not Found")
        assert result.badge == "? unreachable"

    def test_blocked_is_unreachable(self, web: Web, check: Check) -> None:
        result = check("http://169.254.169.254/latest/meta-data/", "anything")
        assert result.status == "unreachable"
        assert result.detail == (
            "blocked: 169.254.169.254 is a local or private network address"
        )

    def test_pdf_is_unsupported(self, web: Web, check: Check) -> None:
        web.add(URL, respond(b"%PDF-1.7", content_type="application/pdf"))
        result = check(URL, "anything at all here")
        assert result.status == "unsupported"
        assert result.badge == "? could not check"

    def test_a_page_that_needs_javascript_is_unsupported(
        self, web: Web, check: Check
    ) -> None:
        web.add(
            URL,
            respond(
                '<div id="app"></div><noscript>Please enable JavaScript to view '
                "this site.</noscript><script src=app.js></script>"
            ),
        )
        result = check(URL, "up to 20 editors")
        assert result.status == "unsupported"
        assert result.detail == "the page seems to need JavaScript to show its text"

    def test_a_bot_wall_is_unsupported(self, web: Web, check: Check) -> None:
        web.add(
            URL,
            respond(
                "<h1>Just a moment...</h1>"
                "<p>Enable JavaScript and cookies to continue</p>"
            ),
        )
        assert check(URL, "up to 20 editors").status == "unsupported"

    def test_a_page_with_no_text_is_unsupported(self, web: Web, check: Check) -> None:
        web.add(URL, respond('<div id="root"></div><script src="/app.js"></script>'))
        result = check(URL, "up to 20 editors")
        assert result.status == "unsupported"
        assert "no visible text" in result.detail

    def test_a_quote_on_a_javascript_page_is_still_verified(
        self, web: Web, check: Check
    ) -> None:
        web.add(
            URL,
            respond("<p>up to 20 editors</p><noscript>Enable JavaScript</noscript>"),
        )
        assert check(URL, "up to 20 editors").status == "verified"

    def test_a_short_page_without_the_quote_is_quote_not_found(
        self, web: Web, check: Check
    ) -> None:
        web.add(URL, respond("<p>Contact us for pricing.</p><script>track()</script>"))
        assert check(URL, "up to 20 editors").status == "quote_not_found"


class TestQuotesThatProveNothing:
    """Review item 5: a fake quote that would be found is never `verified`."""

    def test_a_page_that_echoes_its_query(self, web: Web, check: Check) -> None:
        fake = "the minister admitted the trial data was falsified"
        url = "https://news.example.com/search?q=" + fake.replace(" ", "+")

        def search(request: httpx.Request) -> httpx.Response:
            query = request.url.params.get("q", "")
            return respond(
                f"<h1>Search</h1><p>No results for \u201c{query}\u201d.</p>"
            )(request)

        web.add(url, search)
        result = check(url, fake)
        assert result.status == "unsupported"
        assert result.detail == (
            "the quote is part of the page's address, "
            "so the page may only be repeating it"
        )

    def test_an_echo_through_a_redirect(self, web: Web, check: Check) -> None:
        fake = "the minister admitted the trial data was falsified"
        web.add(
            "https://news.example.com/go",
            redirect("/results/" + fake.replace(" ", "%20")),
        )
        web.add(
            "https://news.example.com/results/" + fake.replace(" ", "%20"),
            respond(f"<p>Nothing found for {fake}</p>"),
        )
        result = check("https://news.example.com/go", fake)
        assert result.status == "unsupported"
        assert "address" in result.detail

    def test_short_fragments_can_be_stitched_from_anywhere(
        self, web: Web, check: Check
    ) -> None:
        # The page says the opposite; ellipses turn it around.
        web.add(
            URL,
            respond(
                "<p>We did not find evidence that the drug is safe for children.</p>"
            ),
        )
        result = check(URL, "We did [...] find evidence that the drug is safe")
        assert result.status == "unsupported"
        assert result.detail == (
            "a fragment of the quote has fewer than 3 words, too few to check reliably"
        )
        assert check(URL, "W … e … d … r … u … g").status == "unsupported"

    def test_more_than_four_fragments(self, pricing: Web, check: Check) -> None:
        quote = (
            "Our Free plan is … for small teams … up to 20 editors … "
            "and 10,000 documents … with community support."
        )
        result = check(URL, quote)
        assert result.status == "unsupported"
        assert result.detail == (
            "the quote has more than 4 fragments, too many to check reliably"
        )

    def test_four_fragments_of_three_words_are_fine(
        self, pricing: Web, check: Check
    ) -> None:
        quote = "Our Free plan … for small teams … up to 20 … with community support."
        assert check(URL, quote).status == "verified"

    def test_a_quote_with_no_words(self, pricing: Web, check: Check) -> None:
        result = check(URL, "…")
        assert result.status == "unsupported"
        assert result.detail == "the quote has no text to look for"

    def test_a_redirect_to_another_site(self, web: Web, check: Check) -> None:
        web.add("https://example.com/pricing", redirect(URL))
        web.add(URL, respond(PRICING))
        result = check("https://example.com/pricing", "up to 20 editors")
        assert result.status == "unsupported"
        assert result.detail == (
            "the page redirects to another site (cms.example.com), "
            "so the page cited could not be checked"
        )
        assert result.final_url == URL

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            ("https://example.com/a", "https://www.example.com/b"),
            ("https://www.example.com/a", "https://example.com/b"),
            ("http://example.com/a", "https://example.com/b"),
            ("http://example.com/a", "https://www.example.com/b"),
            ("https://example.com/a", "https://example.com./b"),
        ],
    )
    def test_the_same_site(self, start: str, end: str, web: Web, check: Check) -> None:
        web.add(start, redirect(end))
        web.add(end.replace("example.com./", "example.com/"), respond(PRICING))
        assert check(start, "up to 20 editors").status == "verified"

    @pytest.mark.parametrize(
        ("start", "end", "shown"),
        [
            ("https://example.com/a", "http://example.com/b", "http://example.com:80"),
            (
                "https://example.com/a",
                "https://example.com:8443/b",
                "https://example.com:8443",
            ),
            ("https://example.com/a", "https://evil.example.com/b", "evil.example.com"),
        ],
    )
    def test_another_site(
        self, start: str, end: str, shown: str, web: Web, check: Check
    ) -> None:
        web.add(start, redirect(end))
        web.add(end, respond(PRICING))
        result = check(start, "up to 20 editors")
        assert result.status == "unsupported"
        assert f"another site ({shown})" in result.detail

    @pytest.mark.parametrize(
        "snippet",
        [
            "<div hidden>FAKE QUOTE HERE</div>",
            "<div hidden><div>inner</div>FAKE QUOTE HERE</div>",
            "<div style='display:none'>FAKE QUOTE HERE</div>",
            "<p style='color: red; DISPLAY : NONE !important'>FAKE QUOTE HERE</p>",
            "<span style='visibility:hidden'>FAKE QUOTE HERE</span>",
            "<video src=x.mp4>FAKE QUOTE HERE</video>",
            "<audio src=x.mp3>FAKE QUOTE HERE</audio>",
            "<object data=x.swf>FAKE QUOTE HERE</object>",
            "<canvas>FAKE QUOTE HERE</canvas>",
            "<picture><source srcset=x.webp>FAKE QUOTE HERE</picture>",
            "<dialog>FAKE QUOTE HERE</dialog>",
            "<datalist><option>FAKE QUOTE HERE</option></datalist>",
            "<img alt='FAKE QUOTE HERE'>",
            "<!-- FAKE QUOTE HERE -->",
            "<title>FAKE QUOTE HERE</title>",
            "<svg><text>FAKE QUOTE HERE</text></svg>",
            "<script>'FAKE QUOTE HERE'</script>",
            "<template>FAKE QUOTE HERE</template>",
            "<noscript>FAKE QUOTE HERE</noscript>",
            "<meta charset=utf-7>+AEY-AKE QUOTE HERE",
        ],
    )
    def test_text_a_browser_does_not_show(
        self, snippet: str, web: Web, check: Check
    ) -> None:
        page = f"<html><body><p>{'Article text. ' * 40}</p>{snippet}</body></html>"
        web.add(URL, respond(page, content_type="text/html"))
        assert check(URL, "fake quote here").status == "quote_not_found"

    @pytest.mark.parametrize(
        "snippet",
        [
            "<dialog open>FAKE QUOTE HERE</dialog>",
            "<div hidden></div><p>FAKE QUOTE HERE</p>",
            "<img hidden src=x.png><p>FAKE QUOTE HERE</p>",
            "<div style='display:block'>FAKE QUOTE HERE</div>",
        ],
    )
    def test_text_a_browser_shows(self, snippet: str, web: Web, check: Check) -> None:
        web.add(URL, respond(f"<html><body>{snippet}</body></html>"))
        assert check(URL, "fake quote here").status == "verified"


class TestNeverRaises:
    """Review item 6: whatever goes wrong, the result is a Verification."""

    def test_a_reader_that_raises(self, pricing: Web, resolver: Resolver) -> None:
        def broken(page: Any, timeout: float) -> PageRead | None:
            raise RuntimeError("boom")

        result = verify_source(
            URL,
            "up to 20 editors",
            transport=pricing.transport,
            resolver=resolver,
            probe=probe,
            reader=broken,
        )
        assert result.status == "unreachable"
        assert result.detail == "network error: the page could not be checked"

    def test_a_transport_that_raises(self, resolver: Resolver) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise ValueError("not an httpx error")

        result = verify_source(
            URL,
            "up to 20 editors",
            transport=httpx.MockTransport(handler),
            resolver=resolver,
            probe=probe,
        )
        assert result.status == "unreachable"

    def test_a_reader_that_gives_up(self, pricing: Web, resolver: Resolver) -> None:
        result = verify_source(
            URL,
            "up to 20 editors",
            transport=pricing.transport,
            resolver=resolver,
            probe=probe,
            reader=lambda page, timeout: None,
        )
        assert result.status == "unsupported"
        assert result.detail == "the page took too long to read"
        assert result.final_url == URL


class TestResult:
    def test_checked_at_is_utc_to_the_second(self, pricing: Web, check: Check) -> None:
        assert check(URL, "Our Free plan").checked_at == dt.datetime(
            2026, 9, 27, 21, 47, 2, tzinfo=dt.UTC
        )

    def test_checked_at_is_converted_to_utc(self, pricing: Web, check: Check) -> None:
        paris = dt.timezone(dt.timedelta(hours=2))
        result = check(
            URL,
            "Our Free plan",
            now=lambda: dt.datetime(2026, 9, 27, 23, 47, 2, tzinfo=paris),
        )
        assert result.checked_at == dt.datetime(2026, 9, 27, 21, 47, 2, tzinfo=dt.UTC)

    def test_default_clock_is_now(self, pricing: Web, resolver: Resolver) -> None:
        before = dt.datetime.now(dt.UTC).replace(microsecond=0)
        result = verify_source(
            URL,
            "Our Free plan",
            transport=pricing.transport,
            resolver=resolver,
            probe=probe,
            reader=read_in_process,
        )
        assert before <= result.checked_at <= dt.datetime.now(dt.UTC)

    def test_final_url_follows_redirects(self, web: Web, check: Check) -> None:
        web.add("https://cms.example.com/p", redirect(URL, 301))
        web.add(URL, respond(PRICING))
        assert check("https://cms.example.com/p", "Our Free plan").final_url == URL

    def test_badges_cover_every_status(self) -> None:
        assert set(BADGES) == {
            "verified",
            "quote_not_found",
            "unreachable",
            "unsupported",
        }

    def test_is_a_value(self) -> None:
        a = Verification("verified", "x", NOW)
        assert a == Verification("verified", "x", NOW, None)
        with pytest.raises(AttributeError):
            a.status = "unreachable"  # type: ignore[misc]


def source(id: str, url: str, quote: str) -> Source:
    return Source(
        id=id,
        url=url,
        title="Pricing | ExampleCMS",
        publisher="ExampleCMS",
        accessed="2026-09-27",
        quote=quote,
        claim="ExampleCMS's free plan allows 20 editors",
    )


class TestVerifySources:
    def run(
        self, web: Web, resolver: Resolver, sources: list[Source], **options: Any
    ) -> dict[str, Verification]:
        options.setdefault("reader", read_in_process)
        return verify_sources(
            sources,
            transport=web.transport,
            resolver=resolver,
            probe=probe,
            now=lambda: NOW,
            **options,
        )

    def test_results_by_source_id(self, pricing: Web, resolver: Resolver) -> None:
        pricing.add("https://example.com/", redirect("http://127.0.0.1/"))
        results = self.run(
            pricing,
            resolver,
            [
                source("S1", URL, "up to 20 editors"),
                source("S2", URL, "unlimited editors and documents"),
                source("S3", "https://cms.example.com/missing", "one two three"),
                source("S4", "https://example.com/", "one two three"),
            ],
        )
        assert {id: r.status for id, r in results.items()} == {
            "S1": "verified",
            "S2": "quote_not_found",
            "S3": "unreachable",
            "S4": "unreachable",
        }
        assert results["S4"].detail.startswith("blocked: the page redirects to")

    def test_each_url_is_fetched_once_and_order_is_kept(
        self, pricing: Web, resolver: Resolver
    ) -> None:
        pricing.add("https://example.com/", respond(status=500))
        results = self.run(
            pricing,
            resolver,
            [
                source("S1", URL, "up to 20 editors"),
                source("S2", "https://example.com/", "one two three"),
                source("S3", URL, "and 10,000 documents"),
                source("S4", URL, "a made up quote"),
                source("S5", "https://example.com/", "four five six"),
            ],
        )
        assert list(results) == ["S1", "S2", "S3", "S4", "S5"]
        assert [r.status for r in results.values()] == [
            "verified",
            "unreachable",
            "verified",
            "quote_not_found",
            "unreachable",
        ]
        assert pricing.visited == [URL, "https://example.com/"]

    def test_only_one_pages_text_is_held_at_a_time(
        self, web: Web, resolver: Resolver
    ) -> None:
        # Review item 9: no cache across pages. Each page is read once.
        reads: list[str] = []

        def reader(page: Any, timeout: float) -> PageRead | None:
            reads.append(page.url)
            return read_in_process(page, timeout)

        for path in ("a", "b"):
            web.add(f"https://example.com/{path}", respond(PRICING))
        self.run(
            web,
            resolver,
            [
                source("S1", "https://example.com/a", "up to 20 editors"),
                source("S2", "https://example.com/b", "up to 20 editors"),
                source("S3", "https://example.com/a", "and 10,000 documents"),
            ],
            reader=reader,
        )
        assert reads == ["https://example.com/a", "https://example.com/b"]

    def test_no_sources(self, web: Web, resolver: Resolver) -> None:
        assert self.run(web, resolver, []) == {}
        assert web.requests == []

    def test_the_run_time_limit(
        self, web: Web, resolver: Resolver, clock: Clock
    ) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            clock.advance(8)
            return respond(PRICING)(request)

        for path in ("a", "b", "c"):
            web.add(f"https://example.com/{path}", slow)
        results = self.run(
            web,
            resolver,
            [
                source("S1", "https://example.com/a", "up to 20 editors"),
                source("S2", "https://example.com/b", "up to 20 editors"),
                source("S3", "https://example.com/c", "up to 20 editors"),
                source("S4", "https://example.com/a", "and 10,000 documents"),
            ],
            clock=clock,
            time_limit=12,
        )
        # S4 cites S1's page, so it is checked with S1.
        assert results["S1"].status == "verified"
        assert results["S4"].status == "verified"
        # S2 had only the 4 s left of the run, and its page took 8.
        assert results["S2"].status == "unreachable"
        assert results["S2"].detail == "timed out: the page did not load within 4 s"
        assert results["S3"].detail == "not checked: the time for verification ran out"
        assert len(web.requests) == 2

    def test_the_per_page_timeout(
        self, web: Web, resolver: Resolver, clock: Clock
    ) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            clock.advance(3)
            return respond(PRICING)(request)

        web.add(URL, slow)
        results = self.run(
            web, resolver, [source("S1", URL, "Our Free plan")], clock=clock, timeout=2
        )
        assert results["S1"].detail == "timed out: the page did not load within 2 s"

    def test_the_real_reader_by_default(self, pricing: Web, resolver: Resolver) -> None:
        results = verify_sources(
            [
                source("S1", URL, "up to 20 editors"),
                source("S2", URL, "a quote that is not on it"),
            ],
            transport=pricing.transport,
            resolver=resolver,
            probe=probe,
        )
        assert [r.status for r in results.values()] == ["verified", "quote_not_found"]


def test_a_verifier_closes_the_transport_it_made() -> None:
    verifier = Verifier(resolver=Resolver())
    verifier.close()


def test_a_verifier_leaves_a_transport_it_was_given_open(web: Web) -> None:
    class Transport(httpx.MockTransport):
        closed = False

        def close(self) -> None:
            self.closed = True

    transport = Transport(web)
    with Verifier(transport=transport, resolver=Resolver()):
        pass
    assert not transport.closed


def test_public_address_used_in_these_tests_is_not_local() -> None:
    assert str(probe(__import__("ipaddress").ip_address(PUBLIC))) != PUBLIC
