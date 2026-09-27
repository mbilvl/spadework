"""T4: public addresses that lead back to this machine or its network.

A machine with a public IPv4 address, or any machine with global IPv6, can
reach itself and its /64 neighbours through addresses that look public. The
guard asks the kernel which source address it would use for the target (a
UDP connect, which sends nothing) and refuses the target when it is that
address or, for IPv6, in its /64. These tests inject the probe.
"""

from __future__ import annotations

from ipaddress import IPv4Address, IPv6Address, ip_address

import pytest

from spadework.sandbox import guard
from tests.security.conftest import NOW, Box

PRIVATE = "cannot fetch local or private network addresses"
OWN_V4 = "93.184.215.14"  # this machine's public IPv4 address, in these tests
OWN_V6 = "2606:4700:10::5"  # and its global IPv6 address


def fake_probe(address: IPv4Address | IPv6Address) -> IPv4Address | IPv6Address:
    return ip_address(OWN_V4 if address.version == 4 else OWN_V6)


@pytest.mark.parametrize(
    ("address", "own"),
    [
        (OWN_V4, True),
        ("93.184.215.15", False),
        (OWN_V6, True),
        ("2606:4700:10::1", True),  # a neighbour on the same /64
        ("2606:4700:10:0:ffff::1", True),
        ("2606:4700:11::5", False),  # the next /64
        (f"::ffff:{OWN_V4}", True),
        ("::ffff:8.8.8.8", False),
    ],
)
def test_is_own_network(address: str, own: bool) -> None:
    assert guard.is_own_network(ip_address(address), fake_probe) is own


def test_a_failed_probe_does_not_block() -> None:
    assert not guard.is_own_network(ip_address("8.8.8.8"), lambda address: None)


def test_the_real_probe_finds_the_loopback_route() -> None:
    # Offline: the kernel picks a source address without sending anything.
    assert guard.local_address_for(ip_address("127.0.0.1")) == ip_address("127.0.0.1")


@pytest.mark.parametrize(
    ("url", "allow"),
    [
        (f"https://{OWN_V4}/", False),
        (f"https://[{OWN_V6}]/", False),
        ("https://[2606:4700:10::1]:8080/admin", False),
        (f"https://[::ffff:{OWN_V4}]/", False),
        ("https://self.example.com/", False),
        ("https://neighbour.example.com/", False),
        ("https://example.com/", True),
        ("https://8.8.8.8/", True),
    ],
)
def test_webfetch_refuses_this_machines_own_network(
    url: str, allow: bool, researcher: Box, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(guard, "local_address_for", fake_probe)
    names = {
        "self.example.com": [OWN_V4],
        "neighbour.example.com": ["93.184.215.20", "2606:4700:10::99"],
        "example.com": ["93.184.215.20"],
    }
    payload = researcher.payload("WebFetch", {"url": url, "prompt": "Summarise"})
    decision = guard.decide(
        payload, researcher.policy, now=NOW, resolve=lambda host: names[host]
    )
    assert decision.allow is allow
    if not allow:
        assert PRIVATE in decision.reason
