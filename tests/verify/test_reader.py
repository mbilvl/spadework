"""Reading a page's text in a child process, within a time limit.

Review items 1 and 3: text extraction is CPU work on hostile input, and
html.parser is quadratic on some pages in older Pythons (gh-135462). A page
that cannot be read within the limit ends as `unsupported`, promptly.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest

from spadework.verify import reader
from spadework.verify.fetch import Page
from spadework.verify.reader import PageRead, read_in_process, read_page
from tests.verify.conftest import Resolver, Web, probe, respond

URL = "https://example.com/"


def page(body: bytes, kind: str = "html", charset: str | None = "utf-8") -> Page:
    return Page(URL, (URL,), kind, body, charset, False)  # type: ignore[arg-type]


class TestChild:
    def test_reads_the_same_text_as_in_process(self) -> None:
        body = (
            "<html><head><title>T</title></head><body><nav>menu</nav>"
            "<p>Café \u2014 up to 20\u00a0editors.</p><script>x()</script>"
            "</body></html>"
        ).encode()
        assert read_page(page(body)) == PageRead(
            "café - up to 20 editors.", False, False
        )
        assert read_page(page(body)) == read_in_process(page(body))

    def test_plain_text_and_charset(self) -> None:
        body = "Preis: 10 €".encode("iso-8859-15")
        read = read_page(page(body, "text", "iso-8859-15"))
        assert read == PageRead("preis: 10 €", False, False)

    def test_javascript_hint(self) -> None:
        body = b"<div id=app></div><noscript>Please enable JavaScript.</noscript>"
        read = read_page(page(body))
        assert read is not None
        assert read.needs_javascript

    def test_malformed_declaration_fallback_runs_in_the_child(self) -> None:
        # Python 3.11 and 3.12 raise AssertionError here; the fallback is in
        # the child, inside the same time limit.
        read = read_page(page(b"<![foo[bar]]><p>up to 20 editors</p>"))
        assert read is not None
        assert read.text.endswith("up to 20 editors")

    def test_the_child_caps_its_own_cpu_time(self) -> None:
        code = (
            "import resource; from spadework.verify.reader import _limit_resources;"
            "_limit_resources(); print(resource.getrlimit(resource.RLIMIT_CPU))"
        )
        done = subprocess.run(
            [sys.executable, "-I", "-c", code],
            capture_output=True,
            text=True,
            check=True,
        )
        assert done.stdout.strip() == f"({reader._CPU_S}, {reader._CPU_S})"


class TestTimeLimit:
    def test_a_page_too_slow_to_read_is_given_up_on_in_time(self) -> None:
        # 2 MB of "<" takes html.parser about a second on any Python.
        body = b"<" * (2 * 1024 * 1024)
        started = time.monotonic()
        assert read_page(page(body), timeout=0.2) is None
        assert time.monotonic() - started < 2.0

    def test_the_gh_135462_page_ends_within_the_limit(
        self, web: Web, resolver: Resolver
    ) -> None:
        # Quadratic on unpatched 3.11 and 3.13; fast where it is patched.
        # Either way the verifier returns within the limit, and a page it
        # could not read is `unsupported`.
        from spadework.verify import verify_source

        web.add(URL, respond(b"<a" * 300_000))
        started = time.monotonic()
        result = verify_source(
            URL,
            "up to 20 editors",
            transport=web.transport,
            resolver=resolver,
            probe=probe,
            read_timeout=1.0,
        )
        assert time.monotonic() - started < 3.0
        # Unread (too slow) or read (no text): unsupported either way.
        assert result.status == "unsupported"

    def test_a_slow_page_is_unsupported(self, web: Web, resolver: Resolver) -> None:
        from spadework.verify import verify_source

        web.add(URL, respond(b"<" * (2 * 1024 * 1024)))
        result = verify_source(
            URL,
            "up to 20 editors",
            transport=web.transport,
            resolver=resolver,
            probe=probe,
            read_timeout=0.2,
        )
        assert result.status == "unsupported"
        assert result.detail == "the page took too long to read"


class TestChildFailures:
    @pytest.mark.parametrize(
        "executable", ["/usr/bin/false", "/bin/echo", "/no/such/python", ""]
    )
    def test_a_child_that_fails_or_talks_nonsense(
        self, executable: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(sys, "executable", executable)
        assert read_page(page(b"<p>up to 20 editors</p>")) is None

    @pytest.mark.parametrize(
        "output",
        [
            b"",
            b"not json",
            b"[]",
            b'{"text": 1, "cut": false, "needs_javascript": false}',
            b'{"text": "x", "cut": "no", "needs_javascript": false}',
            b'{"text": "x", "cut": false}',
            b"\xff\xfe",
        ],
    )
    def test_output_is_checked(self, output: bytes) -> None:
        assert reader._parse_output(output) is None

    def test_output_over_the_text_cap_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(reader, "MAX_TEXT_CHARS", 3)
        output = b'{"text": "abcd", "cut": false, "needs_javascript": false}'
        assert reader._parse_output(output) is None


def test_long_text_is_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reader, "MAX_TEXT_CHARS", 10)
    read = read_in_process(page(b"<p>up to 20 editors and more</p>"))
    assert read == PageRead("up to 20 e", True, False)
