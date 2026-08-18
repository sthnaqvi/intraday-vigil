"""Regressions for two bugs that cost real money in live trading.

1. Adding to an open position left `entry` and `sl_pct` frozen at their first-discovered
   values, so R was meaningfully wrong and the reported P&L had the wrong SIGN.
2. The SL qty fix emitted SL_QTY_FIX unconditionally. Kite accepted the modify, the
   exchange rejected it (error 16448), and the audit log recorded a fix that never
   happened while most of the position sat with no stop.
"""
import json

from vigil.broker import Broker
from vigil.events import EventLog
from vigil.monitor import MonitorLoop
from vigil.state import SessionState
from tests.mock_kite import MockKite
from tests.test_monitor import _now


def _loop(tmp_path, kite, now=(11, 30)):
    events = EventLog(tmp_path / "data")
    broker = Broker(kite, events, dry_run=False)
    session = SessionState(date="2026-08-17")
    return MonitorLoop(broker, events, session, now_fn=lambda: _now(*now), fetch_levels=False)


def _events(tmp_path, type_):
    path = next((tmp_path / "data").glob("events-*.jsonl"), None)
    if path is None:
        return []
    return [json.loads(l) for l in path.read_text().splitlines()
            if json.loads(l)["type"] == type_]


def _seed_short(kite, qty=304, sell_price=1296.6, sl=1309.6):
    """HCLTECH short, mirroring the real position."""
    kite.set_position("HCLTECH", -qty, sell_price=sell_price)
    kite.set_quote("HCLTECH", sell_price)
    return kite.add_sl_order("HCLTECH", "BUY", sl, qty)


def test_adding_to_a_position_refreshes_entry_and_sl_pct(tmp_path):
    kite = MockKite()
    _seed_short(kite)
    loop = _loop(tmp_path, kite)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    from vigil import config
    config.RISK_FILE.write_text(json.dumps({"HCLTECH": {"sl_pct": 0.01}}))

    loop.cycle()
    tp = loop.session.positions["HCLTECH"]
    assert tp.entry == 1296.6 and tp.qty == 304

    # Add 610 more at a worse price -> broker VWAP moves to 1297.95
    kite.set_position("HCLTECH", -914, sell_price=1297.95)
    config.RISK_FILE.write_text(json.dumps({"HCLTECH": {"sl_pct": 0.009}}))
    kite.set_quote("HCLTECH", 1297.0)
    loop.cycle()

    tp = loop.session.positions["HCLTECH"]
    assert tp.qty == 914
    assert tp.entry == 1297.95, "entry must follow the broker VWAP after an add"
    assert tp.sl_pct == 0.009, "sl_pct must be re-read from the risk seed"
    assert _events(tmp_path, "POSITION_REFRESHED"), "the refresh must be auditable"


def test_stale_entry_would_invert_the_pnl_sign(tmp_path):
    """The concrete failure: at ltp 1297.00 the position is +Rs 868, not -Rs 731."""
    kite = MockKite()
    _seed_short(kite)
    loop = _loop(tmp_path, kite)
    loop.cycle()

    kite.set_position("HCLTECH", -914, sell_price=1297.95)
    kite.set_quote("HCLTECH", 1297.0)
    loop.cycle()

    tp = loop.session.positions["HCLTECH"]
    pnl = (tp.entry - 1297.0) * tp.qty
    assert pnl > 0, f"short entered at {tp.entry} must show a PROFIT at 1297.0, got {pnl}"
    assert round(pnl) == 868


def test_qty_fix_rejected_by_exchange_is_reported_not_claimed(tmp_path):
    kite = MockKite()
    oid = _seed_short(kite)
    loop = _loop(tmp_path, kite)
    loop.cycle()

    # Position grows to 914; the SL order stays at 304 and the exchange refuses the modify.
    kite.set_position("HCLTECH", -914, sell_price=1297.95)
    kite.set_quote("HCLTECH", 1297.0)
    kite.silent_modify_rejections = 1
    loop.cycle()

    assert kite._order(oid)["quantity"] == 304, "precondition: the modify did not take"
    assert not _events(tmp_path, "SL_QTY_FIX"), \
        "must NOT claim a fix the exchange rejected"
    rejected = _events(tmp_path, "SL_MODIFY_REJECTED")
    assert rejected, "a rejected qty fix must be recorded"
    assert rejected[0]["data"]["wanted_qty"] == 914
    assert rejected[0]["data"]["still_qty"] == 304


def test_qty_fix_that_succeeds_is_still_reported(tmp_path):
    kite = MockKite()
    oid = _seed_short(kite)
    loop = _loop(tmp_path, kite)
    loop.cycle()

    kite.set_position("HCLTECH", -914, sell_price=1297.95)
    kite.set_quote("HCLTECH", 1297.0)
    loop.cycle()

    assert kite._order(oid)["quantity"] == 914
    assert _events(tmp_path, "SL_QTY_FIX"), "a real fix must still be recorded"
    assert not _events(tmp_path, "SL_MODIFY_REJECTED")
