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


# ---------- persistence across separate instances (simulating separate CLI processes) ----------

def test_load_or_create_returns_a_fresh_book_when_nothing_persisted(tmp_path):
    p = PaperAdapter.load_or_create(tmp_path / "paper_book.json")
    assert p.positions_day() == []
    assert p.orders() == []


def test_a_mutation_is_visible_to_a_new_instance_loading_the_same_path(tmp_path):
    path = tmp_path / "paper_book.json"
    p1 = PaperAdapter.load_or_create(path)
    p1.set_price("RELIANCE", 1300.0)
    p1.place_market_order("RELIANCE", "BUY", 100)
    oid = p1.place_stop_order("RELIANCE", "SELL", 1280.0, 100)

    # A fresh instance loading the same path — stands in for a separate CLI process.
    p2 = PaperAdapter.load_or_create(path)
    pos = next(x for x in p2.positions_day() if x.symbol == "RELIANCE")
    assert pos.quantity == 100 and pos.buy_price == 1300.0
    order = next(o for o in p2.orders() if o.order_id == oid)
    assert order.status == "TRIGGER PENDING" and order.trigger_price == 1280.0


def test_a_long_lived_instance_sees_a_separate_process_mutation_on_its_next_read(tmp_path):
    """The exact daemon scenario: MonitorLoop constructs its broker ONCE at startup and
    keeps calling positions_day()/orders() on that SAME instance for the rest of its life
    — it never calls load_or_create() again. If reads didn't reload from disk, a daemon
    that had already started before `vigil enter` ran in a separate process would never
    see the position that command placed, for as long as the daemon kept running."""
    path = tmp_path / "paper_book.json"
    daemon_adapter = PaperAdapter.load_or_create(path)  # constructed once, held long-term
    assert daemon_adapter.positions_day() == []

    # A separate process (a `vigil enter` invocation) loads its own instance and mutates.
    cli_adapter = PaperAdapter.load_or_create(path)
    cli_adapter.set_price("RELIANCE", 1300.0)
    cli_adapter.place_market_order("RELIANCE", "BUY", 100)

    # The daemon's own long-lived instance — never reconstructed — must see it too.
    pos = next(x for x in daemon_adapter.positions_day() if x.symbol == "RELIANCE")
    assert pos.quantity == 100 and pos.buy_price == 1300.0


def test_price_and_fills_persist_across_instances_too(tmp_path):
    path = tmp_path / "paper_book.json"
    p1 = PaperAdapter.load_or_create(path)
    p1.set_price("RELIANCE", 1300.0)
    p1.place_market_order("RELIANCE", "BUY", 100)
    p1.place_stop_order("RELIANCE", "SELL", 1280.0, 100)

    p2 = PaperAdapter.load_or_create(path)
    p2.set_price("RELIANCE", 1280.0)  # fills the stop placed by p1

    p3 = PaperAdapter.load_or_create(path)
    pos = next(x for x in p3.positions_day() if x.symbol == "RELIANCE")
    assert pos.quantity == 0  # the stop p1 placed fired under p2, visible to p3


def test_a_corrupt_persisted_file_falls_back_to_a_fresh_book(tmp_path):
    path = tmp_path / "paper_book.json"
    path.write_text("{not valid json")
    p = PaperAdapter.load_or_create(path)
    assert p.positions_day() == []
