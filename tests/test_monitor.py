"""Loop tests against the scripted mock Kite."""
from datetime import datetime
from zoneinfo import ZoneInfo

from vigil.broker import Broker
from vigil.events import EventLog
from vigil.monitor import MonitorLoop
from vigil.state import SessionState
from tests.mock_kite import MockKite

IST = ZoneInfo("Asia/Kolkata")


def _now(h=10, m=30):
    return datetime(2026, 8, 17, h, m, tzinfo=IST)  # a Monday, market open


def make_loop(tmp_path, kite, now=(10, 30), dry_run=False):
    events = EventLog(tmp_path / "data")
    broker = Broker(kite, events, dry_run=dry_run)
    session = SessionState(date="2026-08-17")
    return MonitorLoop(broker, events, session, now_fn=lambda: _now(*now), fetch_levels=False)


def seed_indigo_long(kite):
    """INDIGO long 100 @ 4150, sl_pct 1% → SL 4108.5, R=41.5."""
    kite.set_position("INDIGO", 100, buy_price=4150.0)
    return kite.add_sl_order("INDIGO", "SELL", 4108.5, 100)


def test_full_lifecycle_p1_p2_p3_and_profitable_trail_exit(tmp_path):
    kite = MockKite()
    oid = seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite)

    # Cycle 1 — flat: Phase 1, SL untouched
    kite.set_quote("INDIGO", 4150.0, open_=4140, high=4155, low=4138)
    loop.cycle()
    tp = loop.session.positions["INDIGO"]
    assert tp.phase == 1 and kite.modify_calls == []

    # Cycle 2 — +1.01R: breakeven fires once, with full quantity
    # (day low kept well clear of entry so the stop-hunt guard stays out of the way)
    kite.set_quote("INDIGO", 4192.0, open_=4140, high=4193, low=4100)
    loop.cycle()
    tp = loop.session.positions["INDIGO"]
    assert tp.phase == 2 and tp.breakeven_done
    assert kite.modify_calls[-1] == {"order_id": oid, "trigger_price": 4150.0, "quantity": 100}
    assert kite._order(oid)["trigger_price"] == 4150.0

    # Cycle 2b — still Phase 2: no further modify
    n = len(kite.modify_calls)
    kite.set_quote("INDIGO", 4200.0, open_=4140, high=4201, low=4100)
    loop.cycle()
    assert len(kite.modify_calls) == n

    # Cycle 3 — +2.89R: Phase 3, first trail exempt from min-move, trail = 4270*0.98
    kite.set_quote("INDIGO", 4270.0, open_=4140, high=4271, low=4100)
    loop.cycle()
    tp = loop.session.positions["INDIGO"]
    assert tp.phase == 3 and tp.trail_started
    assert kite.modify_calls[-1]["trigger_price"] == 4184.60
    assert kite.modify_calls[-1]["quantity"] == 100

    # Cycle 4 — trail SL fires at a PROFIT; exit must be detected via COMPLETE status
    kite.trigger_sl(oid, 4184.6)
    kite.set_quote("INDIGO", 4180.0)
    loop.cycle()
    assert "INDIGO" not in loop.session.positions
    rec = loop.session.closed[0]
    assert rec["exit_reason"] == "TRAIL_EXIT"
    assert rec["realized_pnl"] > 0
    assert loop.session.realized_r_today > 0


def test_breakeven_near_day_low_gets_stop_hunt_guarded(tmp_path):
    """Entry within 0.3% of the day low: breakeven SL must be pushed below the level
    (all phases, all orders — per sl-rules.md), not parked in the stop-hunt zone."""
    kite = MockKite()
    seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite)
    kite.set_quote("INDIGO", 4192.0, open_=4140, high=4193, low=4138)  # 4138 is 0.29% from 4150
    loop.cycle()
    trigger = kite.modify_calls[-1]["trigger_price"]
    assert trigger < 4138.0
    assert abs(trigger - 4138.0) / 4138.0 >= 0.0029


def test_qty_mismatch_fixed_same_cycle(tmp_path):
    kite = MockKite()
    oid = seed_indigo_long(kite)
    kite._order(oid)["quantity"] = 1  # the silent Kite default-qty bug
    loop = make_loop(tmp_path, kite)
    kite.set_quote("INDIGO", 4150.0)
    loop.cycle()
    assert kite._order(oid)["quantity"] == 100
    fix = next(c for c in kite.modify_calls if c["quantity"] == 100)
    assert fix["order_id"] == oid


def test_rejected_modify_replaces_dead_sl(tmp_path):
    kite = MockKite()
    seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite)

    kite.set_quote("INDIGO", 4192.0)  # +1R → breakeven attempt
    kite.fail_next_modify = Exception("Trigger price already crossed")  # order → REJECTED
    loop.cycle()

    tp = loop.session.positions["INDIGO"]
    # a fresh SL-M was placed and is now the tracked order
    assert kite.place_calls[-1]["order_type"] == "SL-M"
    assert kite.place_calls[-1]["transaction_type"] == "SELL"
    assert kite.place_calls[-1]["quantity"] == 100
    assert kite._order(tp.sl_order_id)["status"] == "TRIGGER PENDING"
    assert tp.breakeven_done  # intent was breakeven; replacement carries it


def test_unprotected_position_gets_sl_from_seed(tmp_path):
    import json

    from vigil import config

    kite = MockKite()
    kite.set_position("SBIN", 200, buy_price=800.0)  # no SL order at all
    kite.set_quote("SBIN", 800.0)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.RISK_FILE.write_text(json.dumps({"SBIN": {"sl_pct": 0.01}}))

    loop = make_loop(tmp_path, kite)
    loop.cycle()

    tp = loop.session.positions["SBIN"]
    assert kite.place_calls[-1]["order_type"] == "SL-M"
    assert kite.place_calls[-1]["trigger_price"] == 792.0
    assert tp.sl_price == 792.0 and tp.sl_pct == 0.01


def test_squareoff_at_1510_cancels_and_exits_everything(tmp_path):
    kite = MockKite()
    oid = seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite, now=(15, 11))
    kite.set_quote("INDIGO", 4160.0)
    loop.cycle()

    assert loop.session.squareoff_done
    assert oid in kite.cancel_calls
    market_exits = [c for c in kite.place_calls if c["order_type"] == "MARKET"]
    assert market_exits and market_exits[-1]["transaction_type"] == "SELL"
    assert loop.session.closed and loop.session.closed[0]["exit_reason"] == "SQUAREOFF"


def test_dry_run_never_mutates(tmp_path):
    kite = MockKite()
    seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite, dry_run=True)
    kite.set_quote("INDIGO", 4192.0)  # would trigger breakeven
    loop.cycle()
    assert kite.modify_calls == [] and kite.place_calls == []
    # intent was logged instead
    events_file = next((tmp_path / "data").glob("events-*.jsonl"))
    assert "DRY_RUN_INTENT" in events_file.read_text()


def test_status_snapshot_contract(tmp_path):
    from vigil import config

    kite = MockKite()
    seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite)
    kite.set_quote("INDIGO", 4150.0)
    loop.cycle()

    import json

    snap = json.loads(config.STATUS_FILE.read_text())
    assert snap["no_new_entries"] is False
    assert snap["kill_switch"] is False
    pos = snap["positions"][0]
    for key in ("symbol", "direction", "entry", "qty", "ltp", "profit_r", "phase",
                "sl_price", "sl_pct", "near_sl"):
        assert key in pos
    assert pos["symbol"] == "INDIGO"
