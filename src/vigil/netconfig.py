"""Pin outbound connections to IPv4.

Why this exists: Zerodha applies its static-IP whitelist to **order** endpoints only — read
endpoints ignore it entirely. On a dual-stack host where IPv6 wins the default route, every
API call egresses from the IPv6 address. Quotes, margins and historical data keep working
perfectly, so the daemon logs in, reports itself live and renders a healthy status — while
every `place_order` / `modify_order` / `cancel_order` is rejected with a PermissionException
naming an address that was never on the whitelist.

That failure is invisible until the first order is attempted. On a session with an open
position it means a daemon reporting LIVE while unable to move a stop to breakeven, trail
it, or square off — the position falls through to the exchange's own forced close. See
`docs/incidents/verification-gaps.md`.

Pinning to IPv4 makes the egress address deterministic, and therefore matchable against a
broker whitelist. Set `VIGIL_FORCE_IPV4=0` to disable.

This *chains* to whatever resolver is installed rather than to the interpreter's own: the
test suite runs under pytest-socket's `--disable-socket`, which patches `getaddrinfo` to
block network access. Capturing the interpreter's original at import time and restoring it
later would silently disarm that guard for the rest of the session.
"""
from __future__ import annotations

import logging
import socket
from collections.abc import Callable
from typing import Any

from . import config

log = logging.getLogger(__name__)

_MARKER = "_vigil_ipv4_pin"
_replaced: Callable[..., Any] | None = None


def _is_pinned() -> bool:
    return getattr(socket.getaddrinfo, _MARKER, False)


def force_ipv4() -> None:
    """Idempotent. Chains to the resolver currently installed, whatever that is."""
    global _replaced
    if _is_pinned():
        return
    previous = socket.getaddrinfo

    def _ipv4_first(host, port, family=0, type=0, proto=0, flags=0):  # noqa: A002
        """Resolve to IPv4 when the caller left the family unspecified.

        An explicit AF_INET6 request passes through untouched: this narrows a default, it
        does not remove the ability to ask for IPv6 deliberately.
        """
        if family == 0:
            try:
                return previous(host, port, socket.AF_INET, type, proto, flags)
            except socket.gaierror:
                # IPv6-only network: the A-record lookup can fail outright. Falling back is
                # better than making vigil unusable there — and the whitelist problem this
                # guards against cannot arise with no IPv4 route at all.
                pass
        return previous(host, port, family, type, proto, flags)

    setattr(_ipv4_first, _MARKER, True)
    _replaced = previous
    socket.getaddrinfo = _ipv4_first


def undo() -> None:
    """Restore the resolver this module replaced. No-op if the pin isn't installed."""
    global _replaced
    if _is_pinned() and _replaced is not None:
        socket.getaddrinfo = _replaced
    _replaced = None


def apply() -> bool:
    """Honour config.FORCE_IPV4. Returns whether the pin is in effect."""
    if not config.FORCE_IPV4:
        return False
    force_ipv4()
    return True


def connect_family(host: str, port: int = 443, timeout: float = 5.0) -> int | None:
    """The address family a real connection to `host` actually uses, or None if unreachable.

    Deliberately opens a socket rather than trusting resolution: what matters is the family
    of the connection the broker will see, not what DNS returned.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            return sock.family
    except Exception:
        # Deliberately broad. This is a diagnostic, never a precondition: a check that can
        # abort `vigil start` is worse than no check at all. Anything that can refuse a
        # connection counts as "unknown family" — a DNS failure, a proxy, a sandbox, or the
        # test suite's own pytest-socket guard, which raises a non-OSError of its own.
        return None


def log_egress_family(host: str = "api.kite.trade") -> int | None:
    """Record which family outbound broker traffic leaves on. Never raises.

    Called at daemon start so a whitelist mismatch is visible in the log immediately,
    instead of surfacing hours later as a rejected order.
    """
    fam = connect_family(host)
    if fam is None:
        log.warning("egress check: could not reach %s — skipping IP-family check", host)
    elif fam == socket.AF_INET6:
        log.warning(
            "egress check: outbound traffic to %s is using IPv6. If your broker whitelists "
            "a single IPv4 address for order placement, orders will be REJECTED while "
            "quotes and margins keep working. Set VIGIL_FORCE_IPV4=1 (default) or whitelist "
            "the IPv6 address.", host,
        )
    else:
        log.info("egress check: outbound traffic to %s is using IPv4", host)
    return fam
