"""I9: which URLs a verification fetch may reach (docs/spec/07-security.md).

`vet` is the gate every hop passes through. These rows mirror the guard's
WebFetch rows (tests/security/test_guard_table.py), since a source URL is
judged the same way whether a session or the verifier fetches it.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address, ip_address

import pytest

from spadework.verify.ssrf import Blocked, Target, Unresolvable, shown, vet
from tests.verify.conftest import PUBLIC, PUBLIC_2, PUBLIC_V6, Resolver

# For the local-address tests: this machine has a public IPv4 address and a
# global IPv6 address.
OWN_V4 = "93.184.215.99"
OWN_V6 = "2606:4700:10::5"


def public_probe(address: IPv4Address | IPv6Address) -> IPv4Address | IPv6Address:
    return ip_address(OWN_V4 if address.version == 4 else OWN_V6)


PRIVATE = "local or private network address"
LOCAL = "is a local address"
INVALID = "not valid"
SCHEME = "only http and https"
USERINFO = "user name or password"
SPACES = "spaces, control characters or backslashes"
SPECIAL = "^special-use domain$"

BLOCKED_LITERALS = [
    # IPv4 in every spelling a browser accepts.
    pytest.param("http://127.0.0.1/", PRIVATE, id="loopback"),
    pytest.param("http://127.0.0.1:22/", PRIVATE, id="loopback-port"),
    pytest.param("http://127.1/", PRIVATE, id="short-127.1"),
    pytest.param("http://127.0.1/", PRIVATE, id="short-127.0.1"),
    pytest.param("http://2130706433/", PRIVATE, id="decimal"),
    pytest.param("http://0x7f000001/", PRIVATE, id="hex"),
    pytest.param("http://0x7F.0.0.1/", PRIVATE, id="hex-label"),
    pytest.param("http://0x7f.1/", PRIVATE, id="hex-short"),
    pytest.param("http://0177.0.0.1/", INVALID, id="octal"),
    pytest.param("http://0/", PRIVATE, id="zero"),
    pytest.param("http://0.0.0.0/", PRIVATE, id="unspecified"),
    pytest.param("http://10.0.0.1/", PRIVATE, id="private-10"),
    pytest.param("http://172.16.5.4/", PRIVATE, id="private-172"),
    pytest.param("http://192.168.1.1/", PRIVATE, id="private-192"),
    pytest.param("http://169.254.169.254/latest/meta-data/", PRIVATE, id="metadata"),
    pytest.param("http://100.64.0.1/", PRIVATE, id="cgnat"),
    pytest.param("http://224.0.0.1/", PRIVATE, id="multicast"),
    pytest.param("http://240.0.0.1/", PRIVATE, id="reserved"),
    pytest.param("http://255.255.255.255/", PRIVATE, id="broadcast"),
    pytest.param("http://198.51.100.7/", PRIVATE, id="documentation"),
    # IPv6, including IPv4 addresses carried inside it.
    pytest.param("http://[::1]/", PRIVATE, id="v6-loopback"),
    pytest.param("http://[::]/", PRIVATE, id="v6-unspecified"),
    pytest.param("http://[::ffff:127.0.0.1]/", PRIVATE, id="v6-mapped-loopback"),
    pytest.param("http://[::ffff:7f00:1]/", PRIVATE, id="v6-mapped-hex"),
    pytest.param("http://[::ffff:169.254.169.254]/", PRIVATE, id="v6-mapped-metadata"),
    pytest.param("http://[::ffff:10.0.0.1]/", PRIVATE, id="v6-mapped-private"),
    pytest.param("http://[::7f00:1]/", PRIVATE, id="v6-compatible"),
    pytest.param("http://[64:ff9b::a00:1]/", PRIVATE, id="v6-nat64"),
    pytest.param("http://[2002:7f00:1::]/", PRIVATE, id="v6-6to4"),
    pytest.param("http://[fe80::1]/", PRIVATE, id="v6-link-local"),
    pytest.param("http://[fc00::1]/", PRIVATE, id="v6-unique-local"),
    pytest.param("http://[fec0::1]/", PRIVATE, id="v6-site-local"),
    pytest.param("http://[ff02::1]/", PRIVATE, id="v6-multicast"),
    pytest.param("http://[fe80::1%25en0]/", "percent-encoded", id="v6-zone"),
    # Names that are local without asking DNS.
    pytest.param("http://localhost/", LOCAL, id="localhost"),
    pytest.param("http://LOCALHOST:8080/", LOCAL, id="localhost-upper"),
    pytest.param("http://localhost./", LOCAL, id="localhost-dot"),
    pytest.param("http://api.localhost/", LOCAL, id="localhost-sub"),
    # Hosts that are not what they seem.
    pytest.param("http://%6c%6f%63%61%6c%68%6f%73%74/", "percent-encoded", id="pct"),
    pytest.param(
        "http://\uff4c\uff4f\uff43\uff41\uff4c\uff48\uff4f\uff53\uff54/",
        INVALID,
        id="fullwidth",
    ),
    pytest.param("http://1.2.3.4.5/", "valid IP address", id="five-parts"),
    pytest.param("http://256.0.0.1/", INVALID, id="octet-too-big"),
    pytest.param("http://1.2.3.08/", INVALID, id="bad-octal"),
    pytest.param("http://example.123/", "valid IP address", id="numeric-tld"),
    pytest.param("http://0x" + "f" * 70 + "/", "host name is not valid", id="huge-hex"),
    pytest.param("http://0x" + "f" * 60 + "/", "valid IP address", id="big-hex"),
    pytest.param("http://a..b.example.com/", "not valid", id="empty-label"),
    pytest.param("http://a!b.example.com/", "not valid", id="bad-char"),
    pytest.param("http://[127.0.0.1]/", INVALID, id="v4-in-brackets"),
    pytest.param("http://[::1::2]/", INVALID, id="bad-v6"),
    # Schemes and shapes that are not an absolute http(s) URL.
    pytest.param("file:///etc/passwd", SCHEME, id="file"),
    pytest.param("ftp://example.com/x", SCHEME, id="ftp"),
    pytest.param("gopher://example.com/", SCHEME, id="gopher"),
    pytest.param("javascript:alert(1)", SCHEME, id="javascript"),
    pytest.param("data:text/html,hello", SCHEME, id="data"),
    pytest.param("//example.com/x", SCHEME, id="scheme-relative"),
    pytest.param("//127.0.0.1/x", SCHEME, id="scheme-relative-private"),
    pytest.param("example.com/x", SCHEME, id="no-scheme"),
    pytest.param("https:///127.0.0.1/", "no host", id="empty-host"),
    pytest.param("", "empty", id="empty"),
    # Credentials, and the parser tricks that hide a host behind them.
    pytest.param("http://user:pass@example.com/", USERINFO, id="userinfo"),
    pytest.param("http://user@example.com/", USERINFO, id="user"),
    pytest.param("http://:@example.com/", USERINFO, id="empty-userinfo"),
    pytest.param("http://example.com@127.0.0.1/", USERINFO, id="at-trick"),
    pytest.param("http://example.com\\@127.0.0.1/", SPACES, id="backslash-trick"),
    # Whitespace and control characters, which parsers disagree about.
    pytest.param("http://exa mple.com/", SPACES, id="space"),
    pytest.param(" http://example.com/", SPACES, id="leading-space"),
    pytest.param("http://example.com/\n", SPACES, id="newline"),
    pytest.param("http://example.com/\t", SPACES, id="tab"),
    pytest.param("http://example.com/\x7f", SPACES, id="delete"),
    # Ports.
    pytest.param("http://example.com:99999/", "port", id="port-too-big"),
    pytest.param("http://example.com:0/", "port", id="port-zero"),
    # Special-use names: refused unresolved, whatever DNS would say.
    pytest.param("https://cms.example/pricing", SPECIAL, id="special-example"),
    pytest.param("https://example/", SPECIAL, id="special-bare-example"),
    pytest.param("https://nothing.invalid/", SPECIAL, id="special-invalid"),
    pytest.param("https://invalid./", SPECIAL, id="special-bare-invalid"),
    pytest.param("https://site.test/", SPECIAL, id="special-test"),
    pytest.param("https://test/", SPECIAL, id="special-bare-test"),
    pytest.param("https://printer.local/", SPECIAL, id="special-local"),
    pytest.param("https://PRINTER.LOCAL./", SPECIAL, id="special-local-case"),
    pytest.param("https://wiki.corp.internal/", SPECIAL, id="special-internal"),
    pytest.param("https://router.home.arpa/", SPECIAL, id="special-home-arpa"),
    pytest.param("http://abcdef.onion/", SPECIAL, id="special-onion"),
    pytest.param("https://name.alt/", SPECIAL, id="special-alt"),
]


@pytest.mark.parametrize(("url", "reason"), BLOCKED_LITERALS)
def test_blocked_without_asking_dns(url: str, reason: str, resolver: Resolver) -> None:
    with pytest.raises(Blocked, match=reason):
        vet(url, resolver)
    assert resolver.calls == []


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        pytest.param("http://internal.example.com/", PRIVATE, id="private"),
        pytest.param("http://mixed.example.com/", PRIVATE, id="mixed"),
        pytest.param("http://metadata.example.com/", PRIVATE, id="metadata"),
        pytest.param("http://mapped.example.com/", PRIVATE, id="v6-mapped"),
        pytest.param("http://nat64.example.com/", PRIVATE, id="nat64-metadata"),
        pytest.param("http://scoped.example.com/", PRIVATE, id="scoped"),
        pytest.param("http://cgnat.example.com/", PRIVATE, id="cgnat"),
        pytest.param("http://unique-local.example.com/", PRIVATE, id="unique-local"),
        pytest.param("http://127.0.0.1.nip.io/", PRIVATE, id="wildcard-dns"),
        pytest.param(
            "http://garbage.example.com/", "did not resolve to an IP", id="junk"
        ),
    ],
)
def test_names_must_resolve_only_to_public_addresses(
    url: str, reason: str, resolver: Resolver
) -> None:
    with pytest.raises(Blocked, match=reason):
        vet(url, resolver)
    assert len(resolver.calls) == 1


@pytest.mark.parametrize(
    "url", ["http://unknown.example.com/", "http://empty.example.com/"]
)
def test_a_name_without_addresses_is_unresolvable(url: str, resolver: Resolver) -> None:
    with pytest.raises(Unresolvable):
        vet(url, resolver)


@pytest.mark.parametrize(
    ("url", "host", "port", "addresses", "host_header"),
    [
        ("https://example.com/", "example.com", 443, (PUBLIC,), "example.com"),
        ("http://example.com/a/b?q=1#top", "example.com", 80, (PUBLIC,), "example.com"),
        ("https://EXAMPLE.com/", "example.com", 443, (PUBLIC,), "example.com"),
        ("HTTPS://example.com/", "example.com", 443, (PUBLIC,), "example.com"),
        ("https://example.com./", "example.com", 443, (PUBLIC,), "example.com"),
        ("https://example.com:443/", "example.com", 443, (PUBLIC,), "example.com"),
        (
            "https://example.com:8443/",
            "example.com",
            8443,
            (PUBLIC,),
            "example.com:8443",
        ),
        (
            "https://www.example.com/",
            "www.example.com",
            443,
            (PUBLIC_2,),
            "www.example.com",
        ),
        (
            "https://v6.example.com/",
            "v6.example.com",
            443,
            (PUBLIC_V6,),
            "v6.example.com",
        ),
        (
            "https://dual.example.com/",
            "dual.example.com",
            443,
            (PUBLIC_V6, PUBLIC),
            "dual.example.com",
        ),
        (
            "https://bücher.example.com/",
            "xn--bcher-kva.example.com",
            443,
            (PUBLIC,),
            "xn--bcher-kva.example.com",
        ),
        ("https://8.8.8.8/", "8.8.8.8", 443, ("8.8.8.8",), "8.8.8.8"),
        ("http://0x08080808/", "8.8.8.8", 80, ("8.8.8.8",), "8.8.8.8"),
        (
            f"https://[{PUBLIC_V6}]:8443/",
            PUBLIC_V6,
            8443,
            (PUBLIC_V6,),
            f"[{PUBLIC_V6}]:8443",
        ),
    ],
)
def test_public_urls_are_allowed(
    url: str,
    host: str,
    port: int,
    addresses: tuple[str, ...],
    host_header: str,
    resolver: Resolver,
) -> None:
    target = vet(url, resolver)
    assert (target.host, target.port, target.addresses) == (host, port, addresses)
    assert target.host_header == host_header


def test_ipv4_mapped_public_address_is_allowed(resolver: Resolver) -> None:
    target = vet("https://[::ffff:8.8.8.8]/", resolver)
    assert len(target.addresses) == 1


def test_duplicate_addresses_are_tried_once() -> None:
    target = vet("https://example.com/", Resolver({"example.com": [PUBLIC, PUBLIC]}))
    assert target.addresses == (PUBLIC,)


def test_resolver_errors_are_unresolvable() -> None:
    def broken(host: str) -> list[str]:
        raise UnicodeError("label too long")

    with pytest.raises(Unresolvable):
        vet("https://example.com/", broken)


class TestTarget:
    def test_request_url_puts_the_vetted_address_in_place_of_the_name(
        self, resolver: Resolver
    ) -> None:
        target = vet("https://example.com:8443/a/b?q=1#top", resolver)
        url = target.request_url(PUBLIC)
        assert str(url) == f"https://{PUBLIC}:8443/a/b?q=1"

    def test_request_url_brackets_ipv6(self, resolver: Resolver) -> None:
        target = vet("https://v6.example.com/x", resolver)
        assert str(target.request_url(PUBLIC_V6)) == f"https://[{PUBLIC_V6}]/x"

    def test_request_url_refuses_an_address_that_was_not_vetted(
        self, resolver: Resolver
    ) -> None:
        target = vet("https://example.com/", resolver)
        with pytest.raises(ValueError, match="vetted"):
            target.request_url("127.0.0.1")

    def test_is_a_value(self, resolver: Resolver) -> None:
        target = vet("https://example.com/", resolver)
        assert isinstance(target, Target)
        assert target == vet("https://example.com/", resolver)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/a?token=secret#frag", "https://example.com/a"),
        ("http://user:pass@example.com:8080/x", "http://example.com:8080/x"),
        ("http://example.com:99999/", "(an invalid URL)"),
        ("https://example.com/" + "a" * 200, "https://example.com/" + "a" * 39 + "…"),
    ],
)
def test_shown_drops_credentials_query_and_fragment(url: str, expected: str) -> None:
    assert shown(url) == expected


class TestLocalAddresses:
    """Review item 4: public-looking addresses that are really local."""

    def test_azure_wireserver_is_blocked(self, resolver: Resolver) -> None:
        with pytest.raises(Blocked, match=PRIVATE):
            vet("http://168.63.129.16/?comp=versions", resolver)

    @pytest.mark.parametrize(
        "url", ["http://intranet/", "http://intranet./", "http://WIKI:8080/x"]
    )
    def test_single_label_names_are_blocked(self, url: str, resolver: Resolver) -> None:
        with pytest.raises(Blocked, match="full domain name"):
            vet(url, resolver)
        assert resolver.calls == []

    def test_a_single_number_is_an_ip_address_not_a_name(
        self, resolver: Resolver
    ) -> None:
        assert vet("http://134744072/", resolver).addresses == ("8.8.8.8",)

    def test_names_are_resolved_as_absolute_names(self, resolver: Resolver) -> None:
        vet("https://example.com./", resolver)
        vet("https://www.example.com/", resolver)
        # A trailing dot stops the resolver trying DNS search domains.
        assert resolver.calls == ["example.com.", "www.example.com."]

    @pytest.mark.parametrize(
        ("url", "reason"),
        [
            (f"https://{OWN_V4}/", "is this computer's own address or network"),
            (f"https://[{OWN_V6}]/", "is this computer's own address or network"),
            ("https://[2606:4700:10::99]:8080/", "own address or network"),
            ("https://[2606:4700:10:0:ffff::1]/", "own address or network"),
            (f"https://[::ffff:{OWN_V4}]/", "own address or network"),
            ("https://self.example.com/", "resolves to this computer's own"),
            ("https://neighbour.example.com/", "resolves to this computer's own"),
        ],
    )
    def test_this_computers_own_network_is_blocked(self, url: str, reason: str) -> None:
        resolver = Resolver(
            {
                "self.example.com": [OWN_V4],
                "neighbour.example.com": [PUBLIC, "2606:4700:10::abcd"],
            }
        )
        with pytest.raises(Blocked, match=reason):
            vet(url, resolver, public_probe)

    @pytest.mark.parametrize(
        "url",
        ["https://example.com/", "https://[2606:4700:11::5]/", "https://8.8.8.8/"],
    )
    def test_other_public_addresses_are_allowed(
        self, url: str, resolver: Resolver
    ) -> None:
        assert vet(url, resolver, public_probe).addresses

    def test_a_failed_probe_does_not_block(self, resolver: Resolver) -> None:
        assert vet(f"https://{OWN_V4}/", resolver, lambda address: None).addresses


class TestHostLabels:
    """Review item 6: TLS refuses labels over 63 characters, so they are
    refused here, before anything is sent."""

    def test_a_63_character_label_is_fine(self) -> None:
        name = "a" * 63 + ".example.com"
        assert vet(f"https://{name}/", Resolver({name: [PUBLIC]})).host == name

    def test_a_64_character_label_is_not(self, resolver: Resolver) -> None:
        with pytest.raises(Blocked, match="host name is not valid"):
            vet("https://" + "a" * 64 + ".test/", resolver)
        assert resolver.calls == []


def test_long_hosts_are_shortened_in_messages(resolver: Resolver) -> None:
    name = ".".join(["a" * 60] * 4) + ".localhost"
    with pytest.raises(Blocked) as blocked:
        vet(f"http://{name}/", resolver)
    assert len(blocked.value.reason) < 100
