"""BrokerClient contract, parametrized over every adapter this codebase ships.

This tests the ADAPTERS directly (not through GuardedBroker — that wrapper is already
covered by monitor/triggers tests), because the question here is narrower and more
important for a multi-broker codebase: does a fresh adapter actually honor the same
observable behavior the daemon already depends on? A new broker that fails this suite is
not safe to point the daemon at, no matter how correct it looks reading the diff.

Deliberately NOT tested here: price-driven auto-fill of a resting stop. MockKite is a
scripted fake (explicit kite.trigger_sl() control, matching real Kite's own quirks —
modify-without-qty silently resets to 1, a 200 response that hides an exchange-side
rejection) rather than a price simulator, so there is no shared "set a price and watch a
stop fill" behavior to assert across both adapters. PaperAdapter's simulation of that is
tested on its own in test_paper_adapter.py.
"""
from __future__ import annotations

import pytest

from tests.mock_kite import MockKite
from vigil.kite_adapter import KiteAdapter
from vigil.paper_adapter import PaperAdapter


def _kite_case():
    kite = MockKite()
    return KiteAdapter(kite), kite.set_quote


def _paper_case():
    adapter = PaperAdapter()
    return adapter, adapter.set_price


CASES = [
    pytest.param(_kite_case, id="kite"),
    pytest.param(_paper_case, id="paper"),
]


@pytest.fixture(params=CASES)
def adapter(request):
    built, set_price = request.param()
    built.set_price = set_price  # type: ignore[attr-defined]
    return built


def _position(adapter, symbol):
    return next((p for p in adapter.positions_day() if p.symbol == symbol), None)


def _order(adapter, order_id):
    return next(o for o in adapter.orders() if o.order_id == order_id)


def test_market_order_opens_a_long_position(adapter):
    adapter.set_price("RELIANCE", 1300.0)
    adapter.place_market_order("RELIANCE", "BUY", 100)

    pos = _position(adapter, "RELIANCE")
    assert pos is not None
    assert pos.quantity == 100
    assert pos.buy_price == 1300.0


def test_market_order_opens_a_short_position(adapter):
    adapter.set_price("HCLTECH", 1500.0)
    adapter.place_market_order("HCLTECH", "SELL", 50)

    pos = _position(adapter, "HCLTECH")
    assert pos is not None
    assert pos.quantity == -50
    assert pos.sell_price == 1500.0


def test_scaling_in_blends_the_entry_vwap(adapter):
    adapter.set_price("RELIANCE", 1300.0)
    adapter.place_market_order("RELIANCE", "BUY", 100)
    adapter.set_price("RELIANCE", 1320.0)
    adapter.place_market_order("RELIANCE", "BUY", 100)

    pos = _position(adapter, "RELIANCE")
    assert pos.quantity == 200
    assert pos.buy_price == pytest.approx(1310.0)


def test_market_order_closes_a_position(adapter):
    adapter.set_price("RELIANCE", 1300.0)
    adapter.place_market_order("RELIANCE", "BUY", 100)
    adapter.set_price("RELIANCE", 1310.0)
    adapter.place_market_order("RELIANCE", "SELL", 100)

    pos = _position(adapter, "RELIANCE")
    assert pos is None or pos.quantity == 0


def test_stop_order_rests_pending(adapter):
    adapter.set_price("RELIANCE", 1300.0)
    adapter.place_market_order("RELIANCE", "BUY", 100)
    oid = adapter.place_stop_order("RELIANCE", "SELL", 1280.0, 100)

    o = _order(adapter, oid)
    assert o.status == "TRIGGER PENDING"
    assert o.trigger_price == 1280.0
    assert o.quantity == 100


def test_modify_stop_order_changes_trigger_and_quantity(adapter):
    adapter.set_price("RELIANCE", 1300.0)
    adapter.place_market_order("RELIANCE", "BUY", 150)
    oid = adapter.place_stop_order("RELIANCE", "SELL", 1280.0, 150)

    adapter.modify_stop_order(oid, 1290.0, 150)

    o = _order(adapter, oid)
    assert o.status == "TRIGGER PENDING"
    assert o.trigger_price == 1290.0
    assert o.quantity == 150


def test_cancel_order_leaves_it_no_longer_pending(adapter):
    adapter.set_price("RELIANCE", 1300.0)
    adapter.place_market_order("RELIANCE", "BUY", 100)
    oid = adapter.place_stop_order("RELIANCE", "SELL", 1280.0, 100)

    adapter.cancel_order(oid)

    o = _order(adapter, oid)
    assert o.status != "TRIGGER PENDING"


def test_quotes_reports_the_price_that_was_set(adapter):
    adapter.set_price("RELIANCE", 1345.5)
    q = adapter.quotes(["NSE:RELIANCE"])
    assert q["NSE:RELIANCE"].last_price == 1345.5
