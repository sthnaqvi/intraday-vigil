"""Regression for a gap that undercounted a real session's P&L by more than half.

A partial exit placed outside the daemon (e.g. manually via MCP, mirroring how the skill
books profit) used to vanish into the qty-refresh branch of `reconcile()` with zero P&L
recorded — the daemon only ever saw the FINAL close and applied a blended day-average price
against the wrong (remaining) quantity. On 2026-08-20 this made `realized_pnl_today` report
₹3,672 against a true session P&L of ~₹7,299. See docs/incidents/2026-08-20-session.md.
"""
import json

from tests.mock_kite import MockKite
from tests.test_monitor import _now
from vigil.broker import Broker
from vigil.events import EventLog
from vigil.monitor import MonitorLoop
from vigil.state import SessionState


def _loop(tmp_path, kite, now=(13, 50)):
    events = EventLog(tmp_path / "data")
    broker = Broker(kite, events, dry_run=False)
    session = SessionState(date="2026-08-20")
    return MonitorLoop(broker, events, session, now_fn=lambda: _now(*now), fetch_levels=False)


def _events(tmp_path, type_):
    path = next((tmp_path / "data").glob("events-*.jsonl"), None)
    if path is None:
        return []
    return [json.loads(line) for line in path.read_text().splitlines()
            if json.loads(line)["type"] == type_]


def _seed_long(kite, qty=419, buy_price=1890.2055, sl=1872.4):
    """OBEROIRLTY long, mirroring the real position."""
    kite.set_position("OBEROIRLTY", qty, buy_price=buy_price)
    kite.set_quote("OBEROIRLTY", buy_price)
    return kite.add_sl_order("OBEROIRLTY", "SELL", sl, qty)


def test_partial_exit_is_recorded_with_its_own_pnl_not_silently_dropped(tmp_path):
    kite = MockKite()
    _seed_long(kite)
    loop = _loop(tmp_path, kite)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    from vigil import config
    config.RISK_FILE.write_text(json.dumps({"OBEROIRLTY": {"sl_pct": 0.0094}}))
    loop.cycle()
    assert loop.session.positions["OBEROIRLTY"].qty == 419

    # Sell 210 of 419 outside the daemon, at a real fill above entry — matching the actual
    # 13:50 partial exit (210 @ 1898.0686).
    kite.set_position("OBEROIRLTY", 209, buy_price=1890.2055, sell_price=1898.0686)
    kite.set_quote("OBEROIRLTY", 1900.0)
    loop.cycle()

    tp = loop.session.positions["OBEROIRLTY"]
    assert tp.qty == 209, "remaining position must reflect the reduced quantity"

    partials = _events(tmp_path, "PARTIAL_EXIT")
    assert partials, "a partial exit must be recorded, not silently dropped"
    rec = partials[0]["data"]
    assert rec["qty"] == 210
    assert rec["exit_price"] == 1898.0686
    assert round(rec["realized_pnl"]) == 1651, "must match the real 210-share P&L"

    assert loop.session.realized_pnl_today == rec["realized_pnl"], \
        "the day's P&L ledger must include the partial, not just eventual full closes"


def test_partial_exit_preserves_phase_and_breakeven_history(tmp_path):
    """A partial exit is not a new trade — phase/breakeven state on the remainder must
    survive it untouched, unlike a full close which drops the tracked position entirely."""
    kite = MockKite()
    _seed_long(kite)
    loop = _loop(tmp_path, kite)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    from vigil import config
    config.RISK_FILE.write_text(json.dumps({"OBEROIRLTY": {"sl_pct": 0.0094}}))
    loop.cycle()
    loop.session.positions["OBEROIRLTY"].phase = 2
    loop.session.positions["OBEROIRLTY"].breakeven_done = True

    kite.set_position("OBEROIRLTY", 209, buy_price=1890.2055, sell_price=1898.0686)
    kite.set_quote("OBEROIRLTY", 1900.0)
    loop.cycle()

    tp = loop.session.positions["OBEROIRLTY"]
    assert tp.phase == 2, "a partial exit must not reset phase progress on the remainder"
    assert tp.breakeven_done is True


def test_full_exit_after_a_partial_only_records_the_remaining_qty_once(tmp_path):
    """The partial and the final close must not double-count the same shares."""
    kite = MockKite()
    sl_id = _seed_long(kite)
    loop = _loop(tmp_path, kite)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)

    from vigil import config
    config.RISK_FILE.write_text(json.dumps({"OBEROIRLTY": {"sl_pct": 0.0094}}))
    loop.cycle()

    kite.set_position("OBEROIRLTY", 209, buy_price=1890.2055, sell_price=1898.0686)
    kite.set_quote("OBEROIRLTY", 1900.0)
    loop.cycle()

    # Full close of the remaining 209 (mirrors the ADMINSQF forced closure).
    kite.trigger_sl(sl_id, 1909.176)
    loop.cycle()

    total_qty_recorded = sum(c["qty"] for c in loop.session.closed)
    assert total_qty_recorded == 419, \
        f"expected all 419 original shares recorded exactly once, got {total_qty_recorded}"
