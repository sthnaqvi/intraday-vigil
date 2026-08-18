"""PaperAdapter's own behavior: price-driven fills. Not shared with KiteAdapter/MockKite
(a scripted fake, not a simulator), so this lives outside the conformance suite."""
from vigil.paper_adapter import PaperAdapter


def _order(adapter, order_id):
    return next(o for o in adapter.orders() if o.order_id == order_id)


def test_long_stop_fills_when_price_falls_to_trigger():
    p = PaperAdapter()
    p.set_price("RELIANCE", 1300.0)
    p.place_market_order("RELIANCE", "BUY", 100)
    oid = p.place_stop_order("RELIANCE", "SELL", 1280.0, 100)

    p.set_price("RELIANCE", 1290.0)
    assert _order(p, oid).status == "TRIGGER PENDING"

    p.set_price("RELIANCE", 1280.0)
    o = _order(p, oid)
    assert o.status == "COMPLETE"
    assert o.average_price == 1280.0
    pos = next(x for x in p.positions_day() if x.symbol == "RELIANCE")
    assert pos.quantity == 0


def test_short_stop_fills_when_price_rises_to_trigger():
    p = PaperAdapter()
    p.set_price("HCLTECH", 1500.0)
    p.place_market_order("HCLTECH", "SELL", 50)
    oid = p.place_stop_order("HCLTECH", "BUY", 1520.0, 50)

    p.set_price("HCLTECH", 1510.0)
    assert _order(p, oid).status == "TRIGGER PENDING"

    p.set_price("HCLTECH", 1520.0)
    o = _order(p, oid)
    assert o.status == "COMPLETE"
    pos = next(x for x in p.positions_day() if x.symbol == "HCLTECH")
    assert pos.quantity == 0


def test_cancelled_stop_never_fills_on_price_cross():
    p = PaperAdapter()
    p.set_price("RELIANCE", 1300.0)
    p.place_market_order("RELIANCE", "BUY", 100)
    oid = p.place_stop_order("RELIANCE", "SELL", 1280.0, 100)
    p.cancel_order(oid)

    p.set_price("RELIANCE", 1270.0)  # well past the trigger

    assert _order(p, oid).status == "CANCELLED"


def test_modify_before_fill_uses_the_new_trigger():
    """Move the stop DOWN (1280 -> 1270, harder to reach for a SELL stop) and prove the
    new trigger is what's actually being watched: a price that would have fired the OLD
    trigger must not fire the modified one."""
    p = PaperAdapter()
    p.set_price("RELIANCE", 1300.0)
    p.place_market_order("RELIANCE", "BUY", 100)
    oid = p.place_stop_order("RELIANCE", "SELL", 1280.0, 100)
    p.modify_stop_order(oid, 1270.0, 100)

    p.set_price("RELIANCE", 1275.0)  # below the OLD trigger, still above the new one
    assert _order(p, oid).status == "TRIGGER PENDING"

    p.set_price("RELIANCE", 1270.0)
    assert _order(p, oid).status == "COMPLETE"


def test_flat_position_still_appears_with_zero_quantity():
    """Matches Kite: a symbol stays in positions_day() after going flat, day P&L is
    tracked per symbol regardless of current holding."""
    p = PaperAdapter()
    p.set_price("RELIANCE", 1300.0)
    p.place_market_order("RELIANCE", "BUY", 100)
    p.set_price("RELIANCE", 1310.0)
    p.place_market_order("RELIANCE", "SELL", 100)

    pos = next(x for x in p.positions_day() if x.symbol == "RELIANCE")
    assert pos.quantity == 0
