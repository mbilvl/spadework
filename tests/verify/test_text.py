"""Visible text and charsets (docs/spec/06-records.md, Verification, step 2)."""

from __future__ import annotations

import codecs
import time

import pytest

from spadework.verify.charsets import LABELS, REPLACEMENT, X_USER_DEFINED, codec_for
from spadework.verify.text import MAX_HIDDEN_DEPTH, PageText, decode, page_text


def visible(html: str) -> str:
    return page_text(html.encode("utf-8"), "html", "utf-8").text


class TestVisibleText:
    def test_drops_what_a_reader_does_not_see(self) -> None:
        html = """<!doctype html>
        <html><head><title>Pricing | ExampleCMS</title>
        <style>p { color: red }</style><script>var price = "fake";</script></head>
        <body>
          <nav><a href="/">Home</a> <a href="/pricing">Pricing</a></nav>
          <noscript>Please enable JavaScript.</noscript>
          <template><p>Hidden template</p></template>
          <svg viewBox="0 0 10 10"><title>Logo</title><text>SVG text</text></svg>
          <iframe>Iframe fallback</iframe>
          <h1>Pricing</h1>
          <p>Free plan: up to 20 editors.</p>
          <script type="application/ld+json">{"price": "0"}</script>
        </body></html>"""
        assert visible(html) == "Pricing Free plan: up to 20 editors."

    def test_decodes_entities(self) -> None:
        html = (
            "<p>Tom &amp; Jerry&nbsp;&mdash; &#8220;quoted&#x201D; "
            "&lt;tag&gt; &eacute;</p>"
        )
        # A no-break space is whitespace too, so it collapses to a space.
        assert visible(html) == "Tom & Jerry \u2014 \u201cquoted\u201d <tag> \u00e9"

    def test_blocks_are_separated_and_inline_elements_are_not(self) -> None:
        html = (
            "<table><tr><td>10</td><td>GB</td></tr></table>"
            "<p>Ex<b>ample</b><span>CMS</span></p><p>next</p>line<br>break"
            "<ul><li>one</li><li>two</li></ul>"
        )
        assert visible(html) == "10 GB ExampleCMS next line break one two"

    def test_collapses_whitespace(self) -> None:
        assert visible("<p>  up\n\n  to \t 20 </p>") == "up to 20"

    def test_head_can_end_without_its_end_tag(self) -> None:
        html = "<html><head><title>T</title><meta charset=utf-8><p>Body text</p>"
        assert visible(html) == "Body text"

    def test_body_start_ends_head(self) -> None:
        assert visible("<head><title>T</title><body>Body text</body>") == "Body text"

    def test_title_outside_head_is_not_text(self) -> None:
        assert visible("<title>Title</title><p>Body</p>") == "Body"

    def test_hidden_element_left_open_inside_another_is_closed_with_it(self) -> None:
        assert visible("<nav>menu <svg><g>icon</nav><p>after</p>") == "after"

    def test_self_closing_hidden_element_hides_nothing_else(self) -> None:
        assert visible("<p>before<svg/>after</p>") == "beforeafter"

    def test_stray_end_tags_are_harmless(self) -> None:
        assert visible("</nav></script><p>text</p>") == "text"

    def test_unfinished_page_keeps_the_text_it_has(self) -> None:
        assert visible("<p>Free plan: up to 20 edi") == "Free plan: up to 20 edi"

    @pytest.mark.parametrize(
        "html",
        [
            "<![foo[bar]]><p>after</p>",
            "<p>before</p><![if !IE]><p>after</p><![endif]>",
            "<!DOCTYPE html [ <!ENTITY x 'y'> ]><p>after</p>",
        ],
    )
    def test_malformed_declarations_do_not_stop_the_parse(self, html: str) -> None:
        # Python 3.11 and 3.12 raise AssertionError on the first of these.
        assert visible(html).endswith("after")

    def test_depth_is_capped_and_the_rest_counts_as_hidden(self) -> None:
        deep = "<svg>" * (MAX_HIDDEN_DEPTH + 5) + "</svg>" * (MAX_HIDDEN_DEPTH + 5)
        assert visible(f"<p>before</p>{deep}<p>after</p>") == "before"
        shallow = "<svg>" * 50 + "</svg>" * 50
        assert visible(f"<p>before</p>{shallow}<p>after</p>") == "before after"

    def test_hidden_content_closes_with_its_own_element(self) -> None:
        html = "<div hidden><div>inner</div>still hidden<p>x</div><p>shown</p>"
        assert visible(html) == "shown"

    def test_hidden_void_elements_hide_nothing_else(self) -> None:
        assert visible("<p>a<img hidden><br hidden>b</p>") == "a b"

    @pytest.mark.parametrize(
        ("unit", "tail"),
        [
            ("<svg>", "</b>"),  # the reviewer's case: 346 s at 2 MB before
            ("<template>", "</p>"),
            ("<div hidden>", "a<i>"),
            ("<nav>", "</nav>"),
        ],
    )
    def test_nesting_costs_linear_time(self, unit: str, tail: str) -> None:
        def cost(count: int) -> float:
            html = (unit * count + tail * count).encode()
            started = time.process_time()
            page_text(html, "html")
            return time.process_time() - started

        small, large = cost(5_000), cost(20_000)
        # Linear: about 4 times as long. Quadratic would be about 16.
        assert large < 8 * small + 0.05

    def test_comments_are_not_text(self) -> None:
        assert visible("<p>a<!-- secret -->b</p>") == "ab"

    def test_plain_text_is_kept_as_is(self) -> None:
        body = b"Free plan <b>not a tag</b>\n\nup to 20"
        assert page_text(body, "text").text == "Free plan <b>not a tag</b> up to 20"


class TestCharset:
    def test_header_charset(self) -> None:
        body = "Preis: 10 €".encode("iso-8859-15")
        assert decode(body, "iso-8859-15", html=True) == "Preis: 10 €"

    def test_quoted_header_charset(self) -> None:
        assert decode("é".encode(), '"UTF-8"', html=False) == "é"

    def test_meta_charset(self) -> None:
        body = '<meta charset="windows-1251"><p>Цена</p>'.encode("cp1251")
        assert "Цена" in decode(body, None, html=True)

    def test_meta_http_equiv(self) -> None:
        body = (
            '<meta http-equiv="Content-Type" content="text/html; charset=shift_jis">'
            "<p>価格</p>"
        ).encode("shift_jis")
        assert "価格" in decode(body, None, html=True)

    def test_xml_declaration(self) -> None:
        body = '<?xml version="1.0" encoding="iso-8859-7"?><p>τιμή</p>'.encode(
            "iso-8859-7"
        )
        assert "τιμή" in decode(body, None, html=True)

    def test_meta_is_ignored_for_plain_text(self) -> None:
        body = '<meta charset="windows-1251"> café'.encode()
        assert decode(body, None, html=False).endswith("café")

    def test_bom_wins_over_the_header(self) -> None:
        body = codecs.BOM_UTF8 + "café".encode()
        assert decode(body, "iso-8859-1", html=True) == "café"

    @pytest.mark.parametrize(
        ("bom", "codec"),
        [(codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")],
    )
    def test_utf16_bom(self, bom: bytes, codec: str) -> None:
        assert decode(bom + "café".encode(codec), None, html=True) == "café"

    def test_latin1_label_means_windows_1252(self) -> None:
        # As in browsers: 0x93 and 0x94 are curly quotes, not control codes.
        assert (
            decode(b"\x93quoted\x94", "iso-8859-1", html=False) == "\u201cquoted\u201d"
        )

    def test_meta_utf16_means_utf8(self) -> None:
        body = '<meta charset="utf-16"><p>café</p>'.encode()
        assert "café" in decode(body, None, html=True)

    @pytest.mark.parametrize(
        "label",
        [
            "no-such-charset",
            "rot13",
            "base64",
            "idna",
            "punycode",
            "utf-7",
            "unicode-1-1-utf-7",
            "unicode_escape",
            "raw_unicode_escape",
            "zlib",
            "hex",
            "utf-32",
            "mbcs",
            "",
            "utf_8\x00",
        ],
    )
    def test_labels_browsers_ignore_fall_back(self, label: str) -> None:
        assert codec_for(label) is None
        assert decode("café".encode(), label, html=False) == "café"
        body = f'<meta charset="{label}"><p>café</p>'.encode()
        assert "café" in decode(body, None, html=True)

    def test_utf7_cannot_spell_hidden_text(self) -> None:
        # In UTF-7, "+AEY-AKE" is "FAKE". Browsers do not support UTF-7.
        for charset, body in [
            ("utf-7", b"<p>+AEY-AKE QUOTE</p>"),
            (None, b"<meta charset=utf-7><p>+AEY-AKE QUOTE</p>"),
        ]:
            assert page_text(body, "html", charset).text == "+AEY-AKE QUOTE"

    def test_punycode_is_not_used_so_decoding_stays_linear(self) -> None:
        # Python's punycode decoder is quadratic: 180 s for 2 MB.
        body = b"<meta charset=punycode>" + b"b" * 200_000 + b"-" + b"a" * 200_000
        started = time.process_time()
        page_text(body, "html")
        assert time.process_time() - started < 1.0

    def test_labels_are_trimmed_and_case_insensitive(self) -> None:
        assert codec_for(" \tShift_JIS\n") == "cp932"
        assert codec_for("ISO-8859-1") == "cp1252"
        assert codec_for("iso-8859-1 x") is None

    def test_every_whatwg_codec_is_a_python_text_codec(self) -> None:
        for codec in set(LABELS.values()) - {REPLACEMENT, X_USER_DEFINED}:
            assert codecs.lookup(codec)._is_text_encoding, codec
            assert "plain text".encode(codec).decode(codec) == "plain text", codec

    @pytest.mark.parametrize("label", ["iso-2022-kr", "hz-gb-2312", "replacement"])
    def test_replacement_encodings_show_nothing(self, label: str) -> None:
        # As in browsers: these can hide markup in ASCII, so the page is one
        # replacement character.
        assert decode(b"~{<p>FAKE QUOTE</p>", label, html=True) == "\ufffd"
        assert decode(b"", label, html=True) == ""

    def test_x_user_defined(self) -> None:
        assert decode(b"ab\x80\xff", "x-user-defined", html=False) == "ab\uf780\uf7ff"
        body = b'<meta charset="x-user-defined">\x80'
        assert decode(body, None, html=True).endswith("\u20ac")

    def test_utf8_is_the_default(self) -> None:
        assert decode("café".encode(), None, html=True) == "café"

    def test_utf8_cut_in_the_middle_of_a_character(self) -> None:
        body = "café €".encode()[:-1]
        assert decode(body, None, html=True) == "café "

    def test_not_utf8_falls_back_to_windows_1252(self) -> None:
        assert decode(b"caf\xe9 \x80", None, html=True) == "café €"


class TestNeedsJavaScript:
    def test_empty_shell_with_noscript(self) -> None:
        html = (
            '<div id="root"></div><noscript>You need to enable JavaScript to run '
            'this app.</noscript><script src="/app.js"></script>'
        )
        text = page_text(html.encode(), "html")
        assert text.text == ""
        assert text.has_noscript
        assert text.hint
        assert text.needs_javascript()

    def test_bot_wall(self) -> None:
        html = (
            "<h1>Just a moment...</h1><p>Enable JavaScript and cookies to continue</p>"
        )
        assert page_text(html.encode(), "html").needs_javascript()

    def test_short_page_with_a_noscript_element(self) -> None:
        html = "<p>Loading…</p><noscript><img src=pixel.gif></noscript>"
        assert page_text(html.encode(), "html").needs_javascript()

    def test_a_full_page_with_a_tracking_noscript_does_not(self) -> None:
        html = (
            "<noscript><iframe src=tracker></iframe></noscript>"
            + "<p>Plenty of real content here.</p>" * 30
        )
        text = page_text(html.encode(), "html")
        assert text.has_noscript
        assert not text.needs_javascript()

    def test_a_short_page_without_hints_does_not(self) -> None:
        text = page_text(b"<p>Short but complete.</p><script>x()</script>", "html")
        assert not text.needs_javascript()

    def test_plain_text_never_does(self) -> None:
        assert not page_text(b"please enable javascript", "text").needs_javascript()

    def test_page_text_is_a_value(self) -> None:
        assert PageText("a") == PageText("a", has_noscript=False, hint=False)
