"""SSRF connect-time guard tests for kb/target_safety.py.

These exercise the gap that the literal-IP `validate_public_https_url` check
cannot close on its own: a public *hostname* whose DNS record points at an
internal address. The resolver is monkeypatched so the tests never touch the
network.
"""

from __future__ import annotations

import httpx
import pytest

import gestaltworkframe.kb.target_safety as target_safety
from gestaltworkframe.kb.target_safety import (
    SsrfGuardTransport,
    assert_destination_is_global,
)


@pytest.fixture
def fake_dns(monkeypatch):
    """Return a setter that maps every hostname to a fixed list of IPs."""

    def _set(addresses):
        monkeypatch.setattr(target_safety, "resolve_host_addresses", lambda host: list(addresses))

    return _set


# ---------------------------------------------------------------------------
# assert_destination_is_global
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "ip",
    ["127.0.0.1", "10.0.0.1", "192.168.1.10", "169.254.169.254", "172.16.0.1", "::1", "fc00::1"],
)
def test_literal_private_ips_are_refused(ip):
    with pytest.raises(ValueError):
        assert_destination_is_global(ip)


@pytest.mark.parametrize("ip", ["1.1.1.1", "8.8.8.8", "2606:4700:4700::1111"])
def test_literal_public_ips_are_allowed(ip):
    assert_destination_is_global(ip)  # no raise


def test_hostname_resolving_to_private_is_refused(fake_dns):
    fake_dns(["10.0.0.5"])
    with pytest.raises(ValueError, match="non-global"):
        assert_destination_is_global("internal.attacker.example")


def test_hostname_resolving_to_metadata_endpoint_is_refused(fake_dns):
    fake_dns(["169.254.169.254"])
    with pytest.raises(ValueError, match="non-global"):
        assert_destination_is_global("metadata.attacker.example")


def test_hostname_with_mixed_records_is_refused_if_any_is_private(fake_dns):
    # One public, one internal: must still refuse.
    fake_dns(["1.1.1.1", "10.0.0.5"])
    with pytest.raises(ValueError):
        assert_destination_is_global("mixed.attacker.example")


def test_hostname_resolving_to_public_is_allowed(fake_dns):
    fake_dns(["93.184.216.34"])
    assert_destination_is_global("example.com")  # no raise


def test_blocked_hosts_and_suffixes_are_refused(fake_dns):
    fake_dns(["1.1.1.1"])  # even if DNS would say public, the name is blocked
    for host in ["localhost", "db.internal", "svc.local", "box.lan"]:
        with pytest.raises(ValueError):
            assert_destination_is_global(host)


def test_resolution_failure_is_fail_closed(monkeypatch):
    def boom(host):
        raise OSError("nxdomain")

    monkeypatch.setattr(target_safety, "resolve_host_addresses", boom)
    with pytest.raises(ValueError, match="could not be resolved"):
        assert_destination_is_global("does-not-resolve.example")


def test_empty_host_is_refused():
    with pytest.raises(ValueError):
        assert_destination_is_global("")


# ---------------------------------------------------------------------------
# SsrfGuardTransport
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transport_refuses_private_host_before_connecting(fake_dns):
    fake_dns(["10.0.0.5"])
    reached = []

    inner = httpx.MockTransport(lambda req: reached.append(req) or httpx.Response(200, text="ok"))
    transport = SsrfGuardTransport(inner=inner)

    async with httpx.AsyncClient(transport=transport) as client:
        with pytest.raises(ValueError, match="non-global"):
            await client.get("https://internal.attacker.example/")

    # The guard fired before the inner transport was ever invoked.
    assert reached == []


@pytest.mark.asyncio
async def test_transport_allows_public_host(fake_dns):
    fake_dns(["93.184.216.34"])
    reached = []

    inner = httpx.MockTransport(lambda req: reached.append(req) or httpx.Response(200, text="ok"))
    transport = SsrfGuardTransport(inner=inner)

    async with httpx.AsyncClient(transport=transport) as client:
        resp = await client.get("https://example.com/feed")

    assert resp.status_code == 200
    assert len(reached) == 1


@pytest.mark.asyncio
async def test_transport_revalidates_each_redirect_hop(fake_dns, monkeypatch):
    # First host is public; it 302-redirects to an internal host. The guard must
    # refuse the second hop even though the first was allowed.
    def resolver(host):
        return ["93.184.216.34"] if host == "public.example" else ["169.254.169.254"]

    monkeypatch.setattr(target_safety, "resolve_host_addresses", resolver)

    def handle(req: httpx.Request) -> httpx.Response:
        if req.url.host == "public.example":
            return httpx.Response(302, headers={"Location": "https://metadata.attacker.example/latest"})
        return httpx.Response(200, text="SECRET")

    transport = SsrfGuardTransport(inner=httpx.MockTransport(handle))
    async with httpx.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(ValueError, match="non-global"):
            await client.get("https://public.example/start")
