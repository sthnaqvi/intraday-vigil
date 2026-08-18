"""A tracked position whose SL disappears must be detected — every cycle.

Found in live trading: the user cancelled a resting SL in the Kite app. `reconcile()` only
ever populated `unprotected` for FIRST-SEEN positions; the qty check skips non-pending
orders; and `_replace_if_dead` only runs when a Phase 2/3 modify is attempted. So a Phase 1
position sat fully exposed with no stop while status.json still printed a stale SL price.

Default behaviour is detect-and-alarm, NOT auto-replace: that cancel was deliberate, and
silently re-placing it would have overridden a decision the user had just made.
"""
import json

from vigil import config
from vigil.broker import Broker
from vigil.events import EventLog
from vigil.monitor import MonitorLoop
from vigil.state import SessionState
from tests.mock_kite import MockKite
from tests.test_monitor import _now


def _loop(tmp_path, kite):
    events = EventLog(tmp_path / "data")
    broker = Broker(kite, events, dry_run=False)
    session = SessionState(date="2026-08-17")
    return MonitorLoop(broker, events, session, now_fn=lambda: _now(11, 30),
                       fetch_levels=False)


def _events(tmp_path, type_):
    p = next((tmp_path / "data").glob("events-*.jsonl"), None)
    if p is None:
        return []
    return [json.loads(l) for l in p.read_text().splitlines()
            if json.loads(l)["type"] == type_]


def _seed(kite, tmp_path):
    """914 short @ 1297.95 with a resting SL at 1309.60 — the real position."""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    config.RISK_FILE.write_text(json.dumps(
        {"HCLTECH": {"sl_pct": 0.009, "pdh": 1362.8, "pdl": 1325.0}}))
    kite.set_position("HCLTECH", -914, sell_price=1297.95)
    kite.set_quote("HCLTECH", 1299.0)
    return kite.add_sl_order("HCLTECH", "BUY", 1309.6, 914)


def test_cancelled_sl_on_tracked_position_is_detected(tmp_path):
    kite = MockKite()
    oid = _seed(kite, tmp_path)
    loop = _loop(tmp_path, kite)
    loop.cycle()
    assert loop.session.positions["HCLTECH"].sl_order_id == oid

    kite.cancel_order("regular", oid)          # user cancels it in the Kite app
    loop.cycle()

    lost = _events(tmp_path, "SL_LOST")
    assert lost, "a vanished SL must be detected on the very next cycle"
    assert lost[0]["data"]["status"] == "CANCELLED"
    assert lost[0]["data"]["qty"] == 914


def test_default_does_not_silently_re_place_a_deliberate_cancel(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTO_REPROTECT", False)
    kite = MockKite()
    oid = _seed(kite, tmp_path)
    loop = _loop(tmp_path, kite)
    loop.cycle()
    kite.cancel_order("regular", oid)
    before = len(kite.place_calls)

    loop.cycle()

    assert len(kite.place_calls) == before, "must not override a manual cancel"
    assert _events(tmp_path, "SL_LOST")
    assert not _events(tmp_path, "SL_REPLACED")


def test_auto_reprotect_when_enabled_preserves_phase(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "AUTO_REPROTECT", True)
    kite = MockKite()
    oid = _seed(kite, tmp_path)
    loop = _loop(tmp_path, kite)
    loop.cycle()
    loop.session.positions["HCLTECH"].phase = 2          # pretend breakeven already happened
    loop.session.positions["HCLTECH"].breakeven_done = True

    kite.cancel_order("regular", oid)
    loop.cycle()

    assert _events(tmp_path, "SL_REPLACED"), "should re-place when explicitly enabled"
    tp = loop.session.positions["HCLTECH"]
    assert tp.sl_order_id != oid, "must point at the new order"
    assert tp.phase == 2 and tp.breakeven_done, "same trade — phase history must survive"


def test_status_reports_protected_false_when_sl_is_gone(tmp_path):
    kite = MockKite()
    oid = _seed(kite, tmp_path)
    loop = _loop(tmp_path, kite)
    loop.cycle()
    kite.cancel_order("regular", oid)
    loop.cycle()

    snap = json.loads(config.STATUS_FILE.read_text())
    row = snap["positions"][0]
    assert row["protected"] is False, "status.json must not imply a stop that isn't there"
    assert row["sl_order_status"] == "CANCELLED"


def test_filled_sl_is_an_exit_not_a_lost_stop(tmp_path):
    """A COMPLETE SL means the trade closed — that must not raise a naked-position alarm."""
    kite = MockKite()
    oid = _seed(kite, tmp_path)
    loop = _loop(tmp_path, kite)
    loop.cycle()

    kite.trigger_sl(oid, 1309.6)
    loop.cycle()

    assert not _events(tmp_path, "SL_LOST"), "a filled stop is an exit, not a missing stop"
    assert _events(tmp_path, "SL_HIT")
