"""Loop tests against the scripted mock Kite."""
import threading
import time as _time
from datetime import datetime
from zoneinfo import ZoneInfo

from tests.mock_kite import MockKite
from vigil.broker import Broker
from vigil.events import EventLog
from vigil.monitor import MonitorLoop
from vigil.state import SessionState

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


def test_unprotected_tracked_position_is_detected_independent_of_price(tmp_path):
    """The 2026-08-18 incident: a resting SL got cancelled outside the daemon while price
    sat well clear of it, so the old any_near check (price-proximity only) never sped the
    daemon up — the naked position was detected at the slow 150s cadence the whole time.
    Under the tick-driven architecture there's no variable-speed cycle to force anymore —
    reconciliation (where SL_LOST is detected) runs on its own fixed, fast, unconditional
    cadence (config.RECONCILE_INTERVAL_S) regardless of price proximity or protection
    state, which is the actual fix: detection speed no longer depends on either."""
    kite = MockKite()
    oid = seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite)
    kite.set_quote("INDIGO", 4150.0)
    loop.cycle()  # first pass: discovers + tracks the position, SL intact

    kite.cancel_order("regular", oid)  # simulate the SL vanishing outside the daemon
    kite.set_quote("INDIGO", 4150.0)   # price unchanged — nowhere near the old SL
    loop.cycle()

    events_file = next((tmp_path / "data").glob("events-*.jsonl"))
    assert "SL_LOST" in events_file.read_text(), (
        "an unprotected position must be detected even when price never moved"
    )


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


def test_squareoff_wait_until_flat_warns_instead_of_hanging_if_never_flat(tmp_path):
    """A stuck fill must not hang the daemon forever — poll for a bounded time, then warn
    and proceed with whatever the broker reports, rather than a blind sleep(2) that could
    either race a slow fill or waste time waiting past a fast one."""
    kite = MockKite()
    seed_indigo_long(kite)  # INDIGO stays open the whole time — simulates a stuck fill
    loop = make_loop(tmp_path, kite, now=(15, 11))

    loop._wait_until_flat(["INDIGO"], timeout_s=0.3, poll_s=0.1)

    events_file = next((tmp_path / "data").glob("events-*.jsonl"))
    assert "still open after" in events_file.read_text()


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


# ---------- real-time (tick-driven) decisions ----------

def test_a_tick_alone_fires_a_breakeven_modify_with_no_full_pass_involved(tmp_path):
    """The actual point of the tick-driven architecture: a price update by itself, with
    no cycle()/_tick() call at all, must be enough to trigger a decision."""
    kite = MockKite()
    oid = seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite)
    kite.set_quote("INDIGO", 4150.0)
    loop.cycle()  # discover + track the position (Phase 1, SL untouched)
    assert kite.modify_calls == []

    loop._on_price("INDIGO", 4192.0, "ws")  # +1.01R — no cycle()/_tick() call at all

    assert kite.modify_calls, "a tick alone must fire the breakeven modify"
    assert kite.modify_calls[-1]["order_id"] == oid
    assert loop.session.positions["INDIGO"].phase == 2


def test_position_decisions_are_serialized_across_concurrent_ticks(tmp_path):
    """Two near-simultaneous ticks for the same symbol must never run the decision path
    concurrently — proves the shared lock (the same one TriggerEngine uses) actually
    serializes them, not just that it exists."""
    kite = MockKite()
    seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite)
    kite.set_quote("INDIGO", 4150.0)
    loop.cycle()  # discover + track

    state = {"inside": 0, "max_concurrent": 0}
    orig = loop._apply_position_decision

    def spy(tp, ltp):
        state["inside"] += 1
        state["max_concurrent"] = max(state["max_concurrent"], state["inside"])
        _time.sleep(0.05)  # force a real window where an unguarded race would show up
        orig(tp, ltp)
        state["inside"] -= 1

    loop._apply_position_decision = spy

    threads = [threading.Thread(target=loop._on_price, args=("INDIGO", 4192.0, "ws"))
              for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert state["max_concurrent"] == 1, "the lock must fully serialize position decisions"
    # And the ratchet guard means 5 identical ticks still only produce one real modify.
    assert len(kite.modify_calls) == 1


def test_stale_tick_falls_back_to_a_poll_driven_decision(tmp_path):
    """If a symbol's tick has gone stale (dropped socket, or nothing pushed a price in a
    while), the periodic poll fallback must still drive the same decision path — the
    daemon's SL management can never silently stall just because the socket did."""
    from vigil import config

    kite = MockKite()
    oid = seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite)
    kite.set_quote("INDIGO", 4150.0)
    loop.cycle()  # discover + track

    loop._last_tick_at["INDIGO"] = _time.monotonic() - config.TICK_STALE_AFTER_S - 1
    kite.set_quote("INDIGO", 4192.0, open_=4140, high=4193, low=4100)  # +1.01R
    assert kite.modify_calls == []

    loop._poll_prices()

    assert kite.modify_calls, "a stale tick must fall back to a poll-driven decision"
    assert kite.modify_calls[-1]["order_id"] == oid


def test_fresh_ticks_are_left_alone_by_the_poll_fallback_when_a_feed_is_attached(tmp_path):
    """With a real push feed actually running, a symbol with a recent tick must not also
    get re-polled every pass — the fallback is scoped to genuinely stale symbols there,
    not everyone. (Distinct from the no-feed-at-all case below, which polls everything —
    a feed being attached is what makes "recent tick" a meaningful freshness signal.)"""
    kite = MockKite()
    seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite)
    kite.set_quote("INDIGO", 4150.0)
    loop.cycle()
    loop.feed = object()  # simulate an attached push feed, without a real WebSocket
    loop._last_tick_at["INDIGO"] = _time.monotonic()  # just ticked

    polled: list[str] = []
    orig_quote = kite.quote

    def spy_quote(symbols):
        polled.extend(symbols)
        return orig_quote(symbols)

    kite.quote = spy_quote

    loop._poll_prices()

    assert polled == [], "a fresh-tick symbol must not be re-polled once a feed is attached"


def test_no_feed_at_all_polls_every_watched_symbol_every_pass(tmp_path):
    """Paper mode (there's no real WebSocket for a simulated broker) and any live account
    whose ticker failed to start have no tick source to go "stale" relative to. Without a
    feed attached, every watched symbol must be polled every pass — not gated on
    _last_tick_at, which would otherwise let a paper-mode symbol go quiet for up to
    TICK_STALE_AFTER_S after its first poll."""
    kite = MockKite()
    seed_indigo_long(kite)
    loop = make_loop(tmp_path, kite)
    kite.set_quote("INDIGO", 4150.0)
    loop.cycle()
    assert loop.feed is None  # no real feed in this test environment
    loop._last_tick_at["INDIGO"] = _time.monotonic()  # "just ticked" via the poll above

    polled: list[str] = []
    orig_quote = kite.quote

    def spy_quote(symbols):
        polled.extend(symbols)
        return orig_quote(symbols)

    kite.quote = spy_quote

    loop._poll_prices()

    assert polled == ["NSE:INDIGO"], (
        "with no feed attached, every pass must poll, not just stale ones")


def test_reconcile_only_runs_when_due(tmp_path):
    """Decoupled cadence: a _tick() pass with do_reconcile=False must not discover a new
    broker-side position — that's reconcile's job, and it only runs on its own schedule
    now, not on every pass."""
    kite = MockKite()
    kite.set_position("SBIN", 200, buy_price=800.0)
    kite.add_sl_order("SBIN", "SELL", 792.0, 200)
    kite.set_quote("SBIN", 800.0)
    loop = make_loop(tmp_path, kite)

    loop._tick(loop.now_fn(), do_reconcile=False, do_qty_verify=False)
    assert "SBIN" not in loop.session.positions

    loop._tick(loop.now_fn(), do_reconcile=True, do_qty_verify=False)
    assert "SBIN" in loop.session.positions
