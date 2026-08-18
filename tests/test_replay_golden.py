"""Session-replay characterization test — the safety net for the open-source rewrite.

This drives one scripted trading day through the real MonitorLoop and asserts the exact
ordered event stream it produces: symbol discovery, phase transitions, breakeven, two
trail steps, an SL hit, and a square-off of a second position. Every later refactor stage
(rename, src/ layout, domain models, broker port, market profile, feed abstraction) must
reproduce this stream byte-for-byte, modulo the renames each stage is explicitly doing.

If this test's assertions change, that is a behavior change and must be justified in the
commit message — not an incidental side effect of moving code around.
"""
import json

from algo.broker import Broker
from algo.events import EventLog
from algo.monitor import MonitorLoop
from algo.state import SessionState
from tests.mock_kite import MockKite
from tests.test_monitor import _now


def _events(tmp_path):
    path = next((tmp_path / "data").glob("events-*.jsonl"))
    return [json.loads(l) for l in path.read_text().splitlines()]


def _stream(tmp_path):
    """(type, symbol, stable-subset-of-data) tuples, in emission order."""
    out = []
    for e in _events(tmp_path):
        data = e["data"]
        # Drop fields that are allowed to vary across refactors without being a
        # regression: free-text messages/reasons and anything float-precision-sensitive
        # that isn't the point of the assertion.
        keep = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in data.items()
                if k not in ("message", "reason", "error")}
        out.append((e["type"], e["symbol"], keep))
    return out


def test_golden_session_replay(tmp_path):
    kite = MockKite()

    # --- position 1: INDIGO long, seeded with an existing SL already at the broker ---
    kite.set_position("INDIGO", 100, buy_price=4150.0)
    indigo_sl = kite.add_sl_order("INDIGO", "SELL", 4108.5, 100)  # sl_pct 1%, R=41.5

    events = EventLog(tmp_path / "data")
    broker = Broker(kite, events, dry_run=False)
    session = SessionState(date="2026-08-17")
    loop = MonitorLoop(broker, events, session, now_fn=lambda: _now(10, 30),
                       fetch_levels=False)

    # Cycle 1 (10:30) — flat: INDIGO discovered, Phase 1, SL untouched.
    kite.set_quote("INDIGO", 4150.0, open_=4140, high=4160, low=4000)
    loop.cycle()

    # Cycle 2 (10:45) — +1.08R: breakeven fires, Phase 2.
    loop.now_fn = lambda: _now(10, 45)
    kite.set_quote("INDIGO", 4195.0, open_=4140, high=4200, low=4000)
    loop.cycle()

    # Cycle 3 (11:00) — +2.41R: Phase 3, first trail (no min-move gate on the first one).
    loop.now_fn = lambda: _now(11, 0)
    kite.set_quote("INDIGO", 4250.0, open_=4140, high=4255, low=4000)
    loop.cycle()

    # Cycle 4 (11:15) — +3.61R: second trail, clears the 0.5% min-move gate.
    loop.now_fn = lambda: _now(11, 15)
    kite.set_quote("INDIGO", 4300.0, open_=4140, high=4305, low=4000)
    loop.cycle()

    # --- position 2: SBIN long, discovered with NO resting SL — seeded via risk.json ---
    from algo import config
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.RISK_FILE.write_text(json.dumps({"SBIN": {"sl_pct": 0.01}}))
    kite.set_position("SBIN", 200, buy_price=800.0)
    kite.set_quote("SBIN", 800.0, open_=795, high=805, low=793)

    # Cycle 5 (11:30) — SBIN discovered unprotected, SL placed from the seed.
    loop.now_fn = lambda: _now(11, 30)
    loop.cycle()

    # The exchange reverses hard on INDIGO and its resting stop fires.
    kite.trigger_sl(indigo_sl, 4214.0)

    # Cycle 6 (11:45) — reconcile sees the fill: SL_HIT for INDIGO.
    loop.now_fn = lambda: _now(11, 45)
    kite.set_quote("SBIN", 802.0, open_=795, high=805, low=793)
    loop.cycle()

    # Cycle 7 (15:06) — past SQUAREOFF_AT (15:05): SBIN gets force-flattened.
    loop.now_fn = lambda: _now(15, 6)
    kite.set_quote("SBIN", 803.0, open_=795, high=805, low=793)
    loop.cycle()

    stream = _stream(tmp_path)

    expected = [
        # INDIGO: a resting SL already at the broker when first seen -> POSITION_DISCOVERED.
        ("POSITION_DISCOVERED", "INDIGO", {"entry": 4150.0, "qty": 100,
                                           "sl_pct": 0.01, "derived": True,
                                           "sl_price": 4108.5}),
        ("PHASE_CHANGE", "INDIGO", {"phase": 2, "profit_r": 1.08}),
        ("SL_MODIFY", "INDIGO", {"from_trigger": 4108.5, "to_trigger": 4150.0,
                                 "quantity": 100, "guard_applied": False}),
        ("SL_MODIFY_VERIFIED", "INDIGO", {"trigger": 4150.0, "quantity": 100}),
        ("PHASE_CHANGE", "INDIGO", {"phase": 3, "profit_r": 2.41}),
        ("SL_MODIFY", "INDIGO", {"from_trigger": 4150.0, "to_trigger": 4165.0,
                                 "quantity": 100, "guard_applied": False}),
        ("SL_MODIFY_VERIFIED", "INDIGO", {"trigger": 4165.0, "quantity": 100}),
        ("SL_MODIFY", "INDIGO", {"from_trigger": 4165.0, "to_trigger": 4214.0,
                                 "quantity": 100, "guard_applied": False}),
        ("SL_MODIFY_VERIFIED", "INDIGO", {"trigger": 4214.0, "quantity": 100}),
        # SBIN: no resting SL at all -> goes through the "unprotected" branch, which places
        # a fresh SL and tracks the position WITHOUT a POSITION_DISCOVERED event (that event
        # is reserved for symbols that already had a broker-side SL when first seen).
        ("SL_REPLACED", "SBIN", {"trigger": 792.0, "quantity": 200}),
        ("SL_HIT", "INDIGO", {"direction": "LONG", "entry": 4150.0, "exit_price": 4214.0,
                              "qty": 100, "realized_r": 1.54, "realized_pnl": 6400.0,
                              "exit_reason": "TRAIL_EXIT", "phase_at_exit": 3}),
        ("SQUAREOFF_START", None, {}),
        # Squareoff emits FILL twice per symbol: once when the market exit is sent (qty
        # only), and again after the post-squareoff reconcile confirms the fill and computes
        # realised P&L. Both are real, both matter — collapsing them would hide a squareoff
        # that sends an order but never confirms the fill.
        ("SQUAREOFF_FILL", "SBIN", {"quantity": 200}),
        ("SQUAREOFF_FILL", "SBIN", {"direction": "LONG", "entry": 800.0, "exit_price": 803.0,
                                    "qty": 200, "realized_r": 0.38, "realized_pnl": 600.0,
                                    "exit_reason": "SQUAREOFF", "phase_at_exit": 1}),
        ("DAEMON_STOP", None, {"realized_r": 1.92, "realized_pnl": 7000.0}),
    ]

    got = [(t, s, d) for t, s, d in stream if t not in ("WARNING", "TIME_ALERT")]

    import os
    if os.environ.get("REPLAY_DEBUG"):
        for row in got:
            print(row)

    assert len(got) == len(expected), (
        f"event count drifted: got {len(got)}, expected {len(expected)}\n"
        f"got types: {[g[0] for g in got]}\n"
        f"expected types: {[e[0] for e in expected]}"
    )
    for i, ((gt, gs, gd), (et, es, ed)) in enumerate(zip(got, expected)):
        assert gt == et, f"event {i}: type {gt!r} != expected {et!r}"
        assert gs == es, f"event {i} ({gt}): symbol {gs!r} != expected {es!r}"
        for k, v in ed.items():
            assert gd.get(k) == v, f"event {i} ({gt}.{k}): {gd.get(k)!r} != expected {v!r}"

    # The session ended flat, both positions closed, correct realised total.
    assert session.positions == {}
    assert round(session.realized_r_today, 2) == 1.92
    assert round(session.realized_pnl_today, 2) == 7000.0
