"""Reconciliation and restart-recovery tests."""
import json

from vigil import config, state as state_mod
from vigil.models import Order, Position
from vigil.state import SessionState, TrackedPosition


def _pos_row(symbol, qty, buy=0.0, sell=0.0):
    return Position(symbol=symbol, exchange="NSE", product="MIS", quantity=qty,
                    buy_price=buy, sell_price=sell, average_price=buy or sell)


def _sl_order(oid, symbol, txn, trigger, qty, status="TRIGGER PENDING", average_price=0.0):
    return Order(order_id=oid, symbol=symbol, product="MIS", order_type="SL-M",
                transaction_type=txn, status=status, trigger_price=trigger,
                quantity=qty, average_price=average_price)


def test_discovery_with_seed_and_derived():
    s = SessionState(date="2026-08-17")
    positions = [_pos_row("INDIGO", 100, buy=4150.0), _pos_row("TATASTEEL", -50, sell=160.0)]
    orders = [
        _sl_order("O1", "INDIGO", "SELL", 4108.5, 100),
        _sl_order("O2", "TATASTEEL", "BUY", 162.0, 50),
    ]
    seeds = {"INDIGO": {"sl_pct": 0.01, "pdh": 4205.0}}
    report = state_mod.reconcile(s, positions, orders, seeds)

    assert sorted(report.new_tracked) == ["INDIGO", "TATASTEEL"]
    indigo = s.positions["INDIGO"]
    assert indigo.sl_pct == 0.01 and not indigo.sl_pct_derived and indigo.pdh == 4205.0
    assert indigo.r == 41.5 and indigo.trail_pct == 0.02
    tata = s.positions["TATASTEEL"]
    assert tata.direction == "SHORT" and tata.entry == 160.0
    assert tata.sl_pct_derived  # derived (162-160)/160 = 1.25%
    assert abs(tata.sl_pct - 0.0125) < 1e-6
    assert any("derived" in w for w in report.warnings)


def test_unprotected_position_flagged_not_tracked():
    s = SessionState(date="2026-08-17")
    report = state_mod.reconcile(s, [_pos_row("SBIN", 200, buy=800.0)], [], {})
    assert report.unprotected == ["SBIN"]
    assert "SBIN" not in s.positions


def test_profitable_trail_exit_detected_via_complete_status():
    s = SessionState(date="2026-08-17")
    s.positions["INDIGO"] = TrackedPosition(
        symbol="INDIGO", direction="LONG", entry=4150.0, qty=100,
        sl_order_id="O1", sl_price=4184.6, sl_pct=0.01, phase=3,
        breakeven_done=True, trail_started=True,
    )
    positions = [_pos_row("INDIGO", 0, buy=4150.0, sell=4184.6)]
    orders = [_sl_order("O1", "INDIGO", "SELL", 4184.6, 100, status="COMPLETE",
                        average_price=4184.6)]
    report = state_mod.reconcile(s, positions, orders, {})

    assert len(report.exited) == 1
    rec = report.exited[0]
    assert rec["exit_reason"] == "TRAIL_EXIT"       # NOT inferred from P&L sign
    assert rec["realized_pnl"] > 0                   # profitable exit still detected
    assert rec["realized_r"] > 0
    assert "INDIGO" not in s.positions
    assert s.realized_pnl_today == rec["realized_pnl"]


def test_manual_exit_cancels_orphan_sl():
    s = SessionState(date="2026-08-17")
    s.positions["SBIN"] = TrackedPosition(
        symbol="SBIN", direction="LONG", entry=800.0, qty=100,
        sl_order_id="O9", sl_price=792.0, sl_pct=0.01,
    )
    positions = [_pos_row("SBIN", 0, buy=800.0, sell=805.0)]
    orders = [_sl_order("O9", "SBIN", "SELL", 792.0, 100)]  # still pending → orphan
    report = state_mod.reconcile(s, positions, orders, {})
    assert report.orphan_sl_cancelled == ["O9"]
    assert report.exited[0]["exit_reason"] == "MANUAL_EXIT"
    assert report.exited[0]["exit_price"] == 805.0


def test_kill_switch_at_minus_2R():
    s = SessionState(date="2026-08-17")
    for i, r in enumerate([-1.0, -1.1]):
        s.closed.append({"symbol": f"X{i}", "realized_r": r, "realized_pnl": -1000.0})
    state_mod.reconcile(s, [], [], {})
    assert s.kill_switch


def test_session_roundtrip_preserves_lifecycle_flags(isolated_dirs):
    s = SessionState.load_or_create()
    s.positions["INDIGO"] = TrackedPosition(
        symbol="INDIGO", direction="LONG", entry=4150.0, qty=100,
        sl_order_id="O1", sl_price=4150.0, sl_pct=0.01, phase=3,
        breakeven_done=True, trail_started=True, pdh=4205.0,
    )
    s.fired.append("alert_1400")
    s.save()

    s2 = SessionState.load_or_create()
    tp = s2.positions["INDIGO"]
    assert tp.phase == 3 and tp.breakeven_done and tp.trail_started and tp.pdh == 4205.0
    assert "alert_1400" in s2.fired


def test_risk_seed_file_roundtrip(isolated_dirs):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.RISK_FILE.write_text(json.dumps({"INDIGO": {"sl_pct": 0.012}}))
    assert state_mod.load_risk_seeds()["INDIGO"]["sl_pct"] == 0.012
