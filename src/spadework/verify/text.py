"""The visible text of a page (docs/spec/06-records.md, Verification, step 2).

Standard library only. The HTML parser keeps the text a reader would see:
it drops `head`, `script`, `style`, `nav`, `noscript`, `template` and `svg`,
the other elements whose content browsers do not show, and anything marked
hidden (the `hidden` attribute, an inline `display: none` or `visibility:
hidden`, a closed `dialog`). It decodes entities, and puts a space at every
block boundary so that words in separate cells or paragraphs do not run
together. It also notes the signs of a page that needs JavaScript to show
its content.

Every step is linear in the size of the page. Even so, the verifier runs
this in a separate process with a time limit (`spadework.verify.reader`),
because html.parser itself is quadratic on some input in older Pythons.
"""

from __future__ import annotations

import codecs
import contextlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import TYPE_CHECKING

from spadework.verify.charsets import codec_for, decode_with

if TYPE_CHECKING:
    from spadework.verify.fetch import Kind

# Elements whose content is not shown, including fallback content for media
# and plugins. Newer Pythons parse the content of some of them as raw text;
# listing them all keeps every version the same.
_HIDDEN = frozenset(
    {
        "audio",
        "canvas",
        "datalist",
        "iframe",
        "nav",
        "noembed",
        "noframes",
        "noscript",
        "object",
        "picture",
        "script",
        "style",
        "svg",
        "template",
        "title",
        "video",
        "xmp",
    }
)
# Elements with no content and no end tag.
_VOID = frozenset(
    {
        "area",
        "base",
        "basefont",
        "bgsound",
        "br",
        "col",
        "embed",
        "frame",
        "hr",
        "img",
        "input",
        "keygen",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
# Elements allowed in <head>. Any other start tag ends it, as it does in a
# browser, since `</head>` and `<body>` may both be left out.
_HEAD_CONTENT = frozenset(
    {"head", "title", "base", "link", "meta", "style", "script", "noscript", "template"}
)
# Elements that do not break a line: text on either side runs together.
_INLINE = frozenset(
    {
        "a",
        "abbr",
        "b",
        "bdi",
        "bdo",
        "big",
        "cite",
        "code",
        "data",
        "del",
        "dfn",
        "em",
        "font",
        "i",
        "ins",
        "kbd",
        "mark",
        "nobr",
        "q",
        "s",
        "samp",
        "small",
        "span",
        "strike",
        "strong",
        "sub",
        "sup",
        "time",
        "tt",
        "u",
        "var",
        "wbr",
    }
)
_HIDING_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
# Elements open inside hidden content, at most. Deeper than this, the rest of
# the page counts as hidden: less text can only make a quote harder to find.
MAX_HIDDEN_DEPTH = 256

# What pages that cannot render without JavaScript tend to say.
_JS_HINT = re.compile(
    r"(enable|turn on|activate|allow)\s+(your\s+)?javascript"
    r"|javascript\s+(is\s+|must\s+be\s+)?(required|disabled|needed|turned off|enabled)"
    r"|(requires?|needs?)\s+javascript",
    re.IGNORECASE,
)
# Less visible text than this, and a JavaScript hint, means the page did not
# render without JavaScript.
_FEW_CHARS = 400
_MAX_NOSCRIPT_CHARS = 10_000

# WHATWG's prescan reads the first 1024 bytes for a declared encoding.
_PRESCAN_BYTES = 1024
_META_CHARSET = re.compile(
    rb"<meta[^>]+?charset\s*=\s*[\"']?\s*([A-Za-z0-9_.:+-]+)", re.IGNORECASE
)
_XML_ENCODING = re.compile(rb"^\s*<\?xml[^>]+encoding\s*=\s*[\"']([A-Za-z0-9_.:+-]+)")
# `<!…>` constructs other than comments: doctypes, CDATA and marked sections.
_DECLARATION = re.compile(r"<!(?!--)[^>]*>")


@dataclass(frozen=True)
class PageText:
    """A page's visible text, whitespace collapsed, plus rendering hints."""

    text: str
    has_noscript: bool = False
    hint: bool = False  # the page asks for JavaScript to be turned on

    def needs_javascript(self) -> bool:
        """Whether the page seems to show its content only with JavaScript."""
        return len(self.text) < _FEW_CHARS and (self.has_noscript or self.hint)


def page_text(body: bytes, kind: Kind, charset: str | None = None) -> PageText:
    """The visible text of a page's body."""
    text = decode(body, charset, html=kind == "html")
    if kind == "text":
        return PageText(" ".join(text.split()))
    parser = _parse(text)
    visible = " ".join("".join(parser.parts).split())
    hint = False
    if len(visible) < _FEW_CHARS:
        hidden = " ".join(parser.noscript)[:_MAX_NOSCRIPT_CHARS]
        hint = bool(_JS_HINT.search(visible) or _JS_HINT.search(hidden))
    return PageText(visible, has_noscript=parser.has_noscript, hint=hint)


def _parse(text: str) -> _Extractor:
    parser = _Extractor()
    try:
        # One feed for the whole page: feeding in pieces can make html.parser
        # rescan an unfinished tag again and again.
        parser.feed(text)
        parser.close()
    except AssertionError:
        # Before 3.13, html.parser raises on some malformed declarations, such
        # as `<![x[`. Browsers read them as comments: drop them and try again,
        # keeping whatever text the second pass gets to.
        parser = _Extractor()
        with contextlib.suppress(AssertionError):
            parser.feed(_DECLARATION.sub(" ", text))
            parser.close()
    return parser


def decode(body: bytes, charset: str | None, *, html: bool) -> str:
    """Decode a body as a browser would: byte-order mark, then the header's
    charset, then a `<meta>` or XML declaration, then UTF-8, then
    windows-1252. Only WHATWG labels count (`charsets.codec_for`)."""
    for bom, bom_codec in (
        (codecs.BOM_UTF8, "utf-8"),
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
    ):
        if body.startswith(bom):
            return body[len(bom) :].decode(bom_codec, "replace")
    codec = codec_for(charset)
    if codec is None and html:
        codec = codec_for(_declared_charset(body[:_PRESCAN_BYTES]))
        if codec in ("utf-16-le", "utf-16-be"):
            # A page that could be read to find this label is not UTF-16.
            codec = "utf-8"
        elif codec == "x-user-defined":
            codec = "cp1252"
    if codec is not None:
        return decode_with(body, codec)
    try:
        # Incremental, so a character cut in half by the size cap is not an error.
        return codecs.getincrementaldecoder("utf-8")().decode(body, final=False)
    except UnicodeDecodeError:
        return body.decode("cp1252", "replace")


def _declared_charset(head: bytes) -> str | None:
    match = _XML_ENCODING.match(head) or _META_CHARSET.search(head)
    return match.group(1).decode("ascii") if match else None


def _hides(tag: str, attrs: list[tuple[str, str | None]]) -> bool:
    """Whether a start tag's attributes hide the element and its content."""
    if tag == "dialog" and not any(name == "open" for name, _ in attrs):
        return True
    for name, value in attrs:
        if name == "hidden":
            return True
        if name == "style" and value and _HIDING_STYLE.search(value):
            return True
    return False


class _Extractor(HTMLParser):
    """Collects visible text. Linear: each tag costs O(1), amortised."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.noscript: list[str] = []
        self.has_noscript = False
        self._noscript_chars = 0
        self._in_head = False
        # The elements open inside hidden content, innermost last, and how
        # many of each: the counts answer "is this tag open?" in O(1).
        self._open: list[str] = []
        self._counts: dict[str, int] = {}
        self._too_deep = False

    @property
    def _hidden(self) -> bool:
        return bool(self._open) or self._too_deep

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "head":
            self._in_head = True
        elif self._in_head and tag not in _HEAD_CONTENT:
            self._in_head = False
        if tag == "noscript":
            self.has_noscript = True
        if tag in _VOID:
            if tag not in _INLINE:
                self.parts.append(" ")
            return
        if self._open or tag in _HIDDEN or _hides(tag, attrs):
            # Inside hidden content every element is tracked, so that an end
            # tag closes the element it belongs to.
            if len(self._open) >= MAX_HIDDEN_DEPTH:
                self._too_deep = True
                return
            self._open.append(tag)
            self._counts[tag] = self._counts.get(tag, 0) + 1
        elif tag not in _INLINE:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "head":
            self._in_head = False
        if self._counts.get(tag):
            # Close it and anything left open inside it. Each element is
            # popped once, so this is O(1) per tag, amortised.
            while True:
                top = self._open.pop()
                self._counts[top] -= 1
                if top == tag:
                    break
        elif tag not in _INLINE:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if self._counts.get("noscript"):
            if self._noscript_chars < _MAX_NOSCRIPT_CHARS:
                self.noscript.append(data)
                self._noscript_chars += len(data)
        elif not self._in_head and not self._hidden:
            self.parts.append(data)
