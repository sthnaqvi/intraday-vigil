"""GuardedBroker: the order-margins passthrough."""
from __future__ import annotations

from tests.mock_kite import MockKite
from vigil.events import EventLog
from vigil.guard import GuardedBroker
from vigil.kite_adapter import KiteAdapter


def test_order_margins_passthrough():
    kite = MockKite()
    kite.set_quote("KOTAKBANK", 400.0)
    kite.set_margin_leverage(5)  # 20% of order value
    broker, _ = (GuardedBroker(KiteAdapter(kite), EventLog(), dry_run=False), None)
    info = broker.order_margins("KOTAKBANK", "BUY", 100)
    assert info["total"] == 400.0 * 100 * 0.20
