"""GuardedBroker: dry-run gating and the order-margins passthrough.

Regression coverage for a bug found live: `_dry()` passed `symbol` both positionally and
via **params, which raised "emit() got multiple values for argument 'symbol'" the moment
any --dry-run mutation fired. Discovered when a dry-run `vigil add` was attempted
mid-session and found broken instead of quietly working as the safety net it's meant to be.
"""
from __future__ import annotations

from tests.mock_kite import MockKite
from vigil.events import EventLog
from vigil.guard import GuardedBroker
from vigil.kite_adapter import KiteAdapter


def _guarded(dry_run: bool = False) -> tuple[GuardedBroker, EventLog]:
    events = EventLog()
    return GuardedBroker(KiteAdapter(MockKite()), events, dry_run=dry_run), events


def test_dry_run_market_order_does_not_crash_and_logs_symbol():
    broker, events = _guarded(dry_run=True)
    order_id = broker.place_market_order("INFY", "BUY", 10)
    assert order_id.startswith("DRY-")


def test_dry_run_stop_order_does_not_crash():
    broker, events = _guarded(dry_run=True)
    order_id = broker.place_stop_order("INFY", "SELL", 1400.0, 10)
    assert order_id.startswith("DRY-")


def test_dry_run_modify_does_not_crash():
    broker, events = _guarded(dry_run=True)
    order_id = broker.modify_stop_order("O1", 1400.0, 10)
    assert order_id.startswith("DRY-")


def test_dry_run_cancel_does_not_crash():
    broker, events = _guarded(dry_run=True)
    order_id = broker.cancel_order("O1")
    assert order_id.startswith("DRY-")


def test_order_margins_passthrough():
    kite = MockKite()
    kite.set_quote("KOTAKBANK", 400.0)
    kite.set_margin_leverage(5)  # 20% of order value
    broker, _ = (GuardedBroker(KiteAdapter(kite), EventLog(), dry_run=False), None)
    info = broker.order_margins("KOTAKBANK", "BUY", 100)
    assert info["total"] == 400.0 * 100 * 0.20
