"""Regression cover for the IPv4 pin.

Guards the failure in docs/incidents/verification-gaps.md: a broker that whitelists a
single IPv4 address for order placement, on a dual-stack host where IPv6 wins the default
route — every read succeeds, every order is rejected.
"""
from __future__ import annotations

import socket

import pytest

from vigil import netconfig


@pytest.fixture(autouse=True)
def _restore_resolver():
    """Save/restore the resolver actually installed — the suite runs under pytest-socket's
    --disable-socket guard, and clobbering that would disarm it for later tests."""
    before = socket.getaddrinfo
    yield
    socket.getaddrinfo = before
    netconfig._replaced = None


def test_unspecified_family_is_pinned_to_ipv4(monkeypatch):
    seen = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: seen.append(a))
    netconfig.force_ipv4()
    socket.getaddrinfo("api.kite.trade", 443)
    assert seen[0][2] == socket.AF_INET


def test_explicit_ipv6_request_is_not_rewritten(monkeypatch):
    seen = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: seen.append(a))
    netconfig.force_ipv4()
    socket.getaddrinfo("api.kite.trade", 443, socket.AF_INET6)
    assert seen[0][2] == socket.AF_INET6, "an explicit AF_INET6 caller must be left alone"


def test_ipv6_only_network_falls_back_instead_of_failing(monkeypatch):
    """No IPv4 route at all must not make vigil unusable — the whitelist problem this
    guards against cannot arise there anyway."""
    calls = []

    def fake(host, port, family=0, *a):
        calls.append(family)
        if family == socket.AF_INET:
            raise socket.gaierror("no A record")
        return ["v6-result"]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    netconfig.force_ipv4()
    assert socket.getaddrinfo("api.kite.trade", 443) == ["v6-result"]
    assert calls == [socket.AF_INET, 0], "should try IPv4 first, then fall back"


def test_pin_chains_to_the_installed_resolver_rather_than_replacing_it(monkeypatch):
    """The suite's own network guard must survive the pin — this is why force_ipv4 chains
    to whatever is installed instead of to the interpreter's import-time original."""
    guard_hits = []

    def guard(*a, **k):
        guard_hits.append(a)
        raise RuntimeError("network disabled")

    monkeypatch.setattr(socket, "getaddrinfo", guard)
    netconfig.force_ipv4()
    with pytest.raises(RuntimeError, match="network disabled"):
        socket.getaddrinfo("api.kite.trade", 443)
    assert guard_hits, "the pin must delegate to the resolver it replaced, not bypass it"


def test_force_ipv4_is_idempotent(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: [])
    netconfig.force_ipv4()
    first = socket.getaddrinfo
    netconfig.force_ipv4()
    assert socket.getaddrinfo is first, "double-pinning would stack a second wrapper"


def test_undo_restores_exactly_what_was_replaced(monkeypatch):
    sentinel = lambda *a: []  # noqa: E731
    monkeypatch.setattr(socket, "getaddrinfo", sentinel)
    netconfig.force_ipv4()
    assert socket.getaddrinfo is not sentinel
    netconfig.undo()
    assert socket.getaddrinfo is sentinel


def test_undo_is_a_noop_when_not_pinned(monkeypatch):
    sentinel = lambda *a: []  # noqa: E731
    monkeypatch.setattr(socket, "getaddrinfo", sentinel)
    netconfig.undo()
    assert socket.getaddrinfo is sentinel


def test_apply_respects_the_config_flag(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a: [])
    unpinned = socket.getaddrinfo

    monkeypatch.setattr(netconfig.config, "FORCE_IPV4", False)
    assert netconfig.apply() is False
    assert socket.getaddrinfo is unpinned, "disabled flag must leave resolution untouched"

    monkeypatch.setattr(netconfig.config, "FORCE_IPV4", True)
    assert netconfig.apply() is True
    assert socket.getaddrinfo is not unpinned


def test_log_egress_family_warns_on_ipv6(monkeypatch, caplog):
    monkeypatch.setattr(netconfig, "connect_family", lambda *a, **k: socket.AF_INET6)
    with caplog.at_level("WARNING"):
        assert netconfig.log_egress_family() == socket.AF_INET6
    assert "IPv6" in caplog.text and "REJECTED" in caplog.text


def test_log_egress_family_never_raises_when_host_unreachable(monkeypatch, caplog):
    monkeypatch.setattr(netconfig, "connect_family", lambda *a, **k: None)
    with caplog.at_level("WARNING"):
        assert netconfig.log_egress_family() is None
    assert "could not reach" in caplog.text


def test_connect_family_swallows_non_oserror(monkeypatch):
    """A startup diagnostic must never be able to abort startup. pytest-socket's own guard
    raises a non-OSError, and so can a proxy or a sandbox — this is why the handler there
    is deliberately broad rather than `except OSError`."""

    class Blocked(Exception):
        pass

    def boom(*a, **k):
        raise Blocked("network disabled")

    monkeypatch.setattr(socket, "create_connection", boom)
    assert netconfig.connect_family("api.kite.trade") is None
