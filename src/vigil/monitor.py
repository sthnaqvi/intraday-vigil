"""The monitor loop: reconcile -> qty check -> SL lifecycle -> time rules -> status.

Design invariants:
- Only ratchets: SL moves are decided in rules.py and verified after every modify
  before state advances, so a crash between modify and persist self-heals.
- A cycle crash never kills the process.
- The resting SL-M at the exchange does the actual exiting; this loop only adjusts
  triggers.

Cadence: SL decisions (breakeven, trail) are tick-driven — see _on_price /
_apply_position_decision — not polled. The four concerns that genuinely have no push
alternative (broker-truth reconciliation, qty-drift verification, time actions, the
tick-feed-down safety net) each run on their own cadence via config.py's
RECONCILE_INTERVAL_S / QTY_VERIFY_INTERVAL_S / LOOP_TICK_S / TICK_STALE_AFTER_S, instead
of sharing one interval the way a single polling cycle used to.
"""
from __future__ import annotations

import os
import threading
import time as _time
import traceback as _traceback
from collections.abc import Callable
from datetime import datetime

from . import clock, config, execution, levels, rules
from . import state as state_mod
from .claudelink import enqueue as _claudelink_enqueue
from .events import EventLog, logger
from .feed import KiteTickerFeed, PollingFeed
from .guard import GuardedBroker, TokenException
from .notify import alert_dialog, notify
from .rules import Direction, ModifyIntent
from .state import SessionState, TrackedPosition
from .triggers import TriggerEngine


class MonitorLoop:
    def __init__(
        self,
        broker: GuardedBroker,
        events: EventLog,
        session: SessionState,
        now_fn: Callable = clock.now_ist,
        fetch_levels: bool = True,
    ):
        self.broker = broker
        self.events = events
        self.session = session
        self.now_fn = now_fn
        self.fetch_levels = fetch_levels
        self.failed_cycles = 0
        self.cycles_run = 0
        self._tokens: dict[str, int] = {}

        # One lock for everything that mutates tracked position/trigger state or talks to
        # the broker about them — shared with TriggerEngine, since a symbol can carry both
        # an open position and an armed exit trigger touching the same broker order, and
        # both a WebSocket tick and the periodic pass can arrive concurrently.
        self._lock = threading.Lock()
        self.trigger_engine = TriggerEngine(broker, events, session, lock=self._lock)
        self.feed: KiteTickerFeed | None = None  # push feed, when anything needs watching
        self._polling_feed = PollingFeed(broker)

        self._live_ltp: dict[str, float] = {}
        self._last_tick_at: dict[str, float] = {}  # symbol -> time.monotonic()
        self._day_hilo: dict[str, tuple[float, float]] = {}
        self._tick_cache: dict[str, float] = {}  # symbol -> exchange tick size
        self._last_feed_attempt = 0.0  # time.monotonic() of the last (re)subscribe attempt

    # ---------- level helpers ----------

    def _ensure_pdh_pdl(self, tp: TrackedPosition) -> None:
        if tp.pdh is not None or not self.fetch_levels:
            return
        if tp.symbol not in self._tokens:
            try:
                self._tokens.update(levels.instrument_tokens(self.broker, [tp.symbol]))
            except Exception as e:
                self.events.emit("WARNING", tp.symbol,
                                message=f"instrument token lookup failed: {e}")
                return
        token = self._tokens.get(tp.symbol)
        if token:
            hl = levels.fetch_pdh_pdl(self.broker, tp.symbol, token, self.events)
            if hl:
                tp.pdh, tp.pdl = hl

    def _levels_for(self, tp: TrackedPosition) -> list[float]:
        """Stop-hunt guard levels available right now: pdh/pdl plus the last known
        day-high/day-low. Day-H/L is refreshed by the periodic pass (a tick carries LTP
        only, not a full quote) — PDH/PDL don't move intraday, and day-H/L moves slowly
        enough that lagging it by up to RECONCILE_INTERVAL_S doesn't matter for a guard
        whose whole job is staying clear of a level by a comfortable buffer."""
        hi, lo = self._day_hilo.get(tp.symbol, (None, None))
        return [float(v) for v in (tp.pdh, tp.pdl, hi, lo) if v]

    # ---------- SL execution ----------

    def _execute_intent(self, tp: TrackedPosition, intent: ModifyIntent) -> None:
        """Send the modify, verify it landed, only then advance state.
        If the order died (rejected/cancelled) while the position is open, re-place."""
        try:
            self.broker.modify_stop_order(intent.order_id, intent.trigger_price, intent.quantity)
        except TokenException:
            raise
        except Exception as e:
            self.events.emit("SL_MODIFY_REJECTED", tp.symbol, error=str(e),
                            intended_trigger=intent.trigger_price, reason=intent.reason)
            self._replace_if_dead(tp, intent)
            return

        self.events.emit(
            "SL_MODIFY", tp.symbol,
            from_trigger=tp.sl_price, to_trigger=intent.trigger_price,
            quantity=intent.quantity, reason=intent.reason, guard_applied=intent.guard_applied,
        )

        if self.broker.dry_run:
            verified = True  # nothing to verify against
        else:
            orders = self.broker.orders()
            o = state_mod.order_by_id(orders, intent.order_id)
            verified = (
                o is not None
                and o.status in state_mod.PENDING_STATUSES
                and abs((o.trigger_price or 0) - intent.trigger_price) < config.NSE_TICK
                and o.quantity == intent.quantity
            )
            if (o is not None and o.status not in state_mod.PENDING_STATUSES
                    and o.status != "COMPLETE"):
                self._replace_if_dead(tp, intent)
                return

        if verified:
            tp.sl_price = intent.trigger_price
            if intent.reason == "breakeven_+1R":
                tp.breakeven_done = True
                notify(f"{tp.symbol} SL moved to breakeven ({intent.trigger_price})")
            elif intent.reason == "trail_2x_slpct":
                tp.trail_started = True
            self.events.emit("SL_MODIFY_VERIFIED", tp.symbol, trigger=intent.trigger_price,
                            quantity=intent.quantity)
        else:
            self.events.emit("WARNING", tp.symbol,
                            message="modify not verified — will retry on the next check")

    def _replace_if_dead(self, tp: TrackedPosition, intent: ModifyIntent) -> None:
        """SL order rejected/cancelled with the position still open = unprotected. Re-place now."""
        if not self.broker.dry_run:
            orders = self.broker.orders()
            o = state_mod.order_by_id(orders, tp.sl_order_id)
            if o is not None and o.status in state_mod.PENDING_STATUSES:
                return  # original SL survived the failed modify; nothing to do
            if o is not None and o.status == "COMPLETE":
                return  # SL fired during the race; reconcile will pick up the exit
        new_id = execution.place_sl(self.broker, tp.symbol, tp.dir, intent.trigger_price, tp.qty)
        tp.sl_order_id = new_id
        tp.sl_price = intent.trigger_price
        if intent.reason == "breakeven_+1R":
            tp.breakeven_done = True
        elif intent.reason == "trail_2x_slpct":
            tp.trail_started = True
        self.events.emit("SL_REPLACED", tp.symbol, new_order_id=new_id,
                        trigger=intent.trigger_price, quantity=tp.qty)
        notify(f"{tp.symbol}: SL order was dead — re-placed at {intent.trigger_price}", sound=True)

    def _auto_enqueue(self, question: str, context: dict) -> None:
        """Fire-and-forget: claudelink.enqueue() blocks on a subprocess for up to 180s
        when a `claude` CLI is reachable — calling it directly from this loop would stall
        SL decisions, reconciliation, and tick processing for as long as Claude takes to
        answer. Runs on its own daemon thread instead, so a slow or hung response never
        blocks the loop that's actually managing stop-loss orders. The request is written
        to the queue file synchronously inside enqueue() before the subprocess is even
        attempted, so if the thread gets killed mid-flight (daemon restart, etc.) the
        request still survives and can be picked up later with `vigil ask --pending`."""
        if not config.AUTO_ENQUEUE_ENABLED:
            return

        def _run() -> None:
            # An uncaught exception here (disk full, a bad permission on DATA_DIR, ...)
            # would otherwise just dump a traceback to stderr from a background thread —
            # invisible in the one place this daemon's failures are actually watched
            # (logs/algo.log via events.emit). Recorded as a WARNING instead, same as
            # every other best-effort side path in this loop (levels refresh, resubscribe).
            try:
                _claudelink_enqueue(question, context=context)
            except Exception as e:
                self.events.emit("WARNING", message=f"auto-enqueue failed: {e!r}")

        threading.Thread(target=_run, daemon=True).start()

    # ---------- real-time position decisions (tick-driven) ----------

    def _apply_position_decision(self, tp: TrackedPosition, ltp: float) -> None:
        """Recompute phase and fire a breakeven/trail intent if one is due. This is the
        one decision path — called the instant a price update arrives (WebSocket tick, or
        the stale-tick poll fallback), not on a fixed interval. Ratchet-only guards
        (rules.trail_decision's min-move, breakeven's one-shot) already make this safe to
        call redundantly for a symbol whose price barely moved — it just returns None."""
        self._live_ltp[tp.symbol] = ltp
        pr = rules.profit_r(ltp, tp.entry, tp.r, tp.dir)
        new_phase = max(tp.phase, rules.target_phase(pr))
        if new_phase != tp.phase:
            tp.phase = new_phase
            self.events.emit("PHASE_CHANGE", tp.symbol, phase=new_phase, profit_r=round(pr, 2))
            notify(f"{tp.symbol} -> Phase {new_phase} ({pr:+.2f}R)")
            if new_phase == 3:
                # The significant transition, not every phase change — reaching phase 3
                # (trailing) means a real trend is underway and is worth a proactive look.
                # REASSESS, not MONITOR: this is exactly the "bigger move" case the plan
                # calls out for it — a position that has moved this far is worth checking
                # against sector rank/thesis-decay, not just a protection-status read.
                # REASSESS can only ever propose; it still can't place anything unattended.
                self._auto_enqueue(
                    f"Run /intraday-vigil reassess — {tp.symbol} just reached phase 3 "
                    f"(trailing) at {pr:+.2f}R. Re-check its sector rank and whether the "
                    f"thesis still holds this far into the move.",
                    context={"symbol": tp.symbol, "phase": new_phase,
                             "profit_r": round(pr, 2), "ltp": ltp, "entry": tp.entry,
                             "sl_price": tp.sl_price})

        lvls = self._levels_for(tp)
        tick_size = self._tick_cache.get(tp.symbol, config.NSE_TICK)
        intent = None
        if tp.phase >= 2 and not tp.breakeven_done:
            intent = rules.breakeven_decision(
                tp.entry, tp.sl_price, tp.dir, lvls, tp.sl_order_id, tp.qty, tick_size)
        elif tp.phase == 3:
            intent = rules.trail_decision(
                ltp, tp.trail_pct, tp.sl_price, tp.dir, lvls, tp.sl_order_id, tp.qty,
                require_min_move=tp.trail_started, tick=tick_size)
        if intent:
            self._execute_intent(tp, intent)

    def _on_position_price(self, symbol: str, ltp: float, source: str) -> None:
        with self._lock:
            tp = self.session.positions.get(symbol)
            if tp is None:
                return
            self._apply_position_decision(tp, ltp)

    def _on_price(self, symbol: str, ltp: float, source: str) -> None:
        """Single entry point for every price update, whichever transport delivered it —
        WebSocket tick or the stale-tick polling fallback. Routes to both trigger-firing
        (TriggerEngine, unchanged) and position decisions, exactly mirroring triggers.py's
        own "both transports call the same handler, through the same lock" design."""
        self._last_tick_at[symbol] = _time.monotonic()
        self.trigger_engine.on_price(symbol, ltp, source)
        self._on_position_price(symbol, ltp, source)

    # ---------- squareoff ----------

    def _squareoff(self) -> None:
        self.events.emit("SQUAREOFF_START")
        notify(f"{config.SQUAREOFF_AT.strftime('%H:%M')} — squaring off all MIS positions "
              f"(ahead of the broker's own {config.BROKER_SQUAREOFF_AT.strftime('%H:%M')})",
              sound=True)
        self.session.squareoff_done = True
        symbols = list(self.session.positions)
        for tp in list(self.session.positions.values()):
            try:
                self.broker.cancel_order(tp.sl_order_id)
            except Exception as e:
                self.events.emit("WARNING", tp.symbol, message=f"SL cancel failed: {e}")
            execution.place_market_exit(self.broker, tp.symbol, tp.dir, tp.qty)
            self.events.emit("SQUAREOFF_FILL", tp.symbol, quantity=tp.qty)
        # final reconcile records the exits with reason SQUAREOFF
        if not self.broker.dry_run:
            self._wait_until_flat(symbols)
        report = state_mod.reconcile(
            self.session, self.broker.positions_day(), self.broker.orders(),
            state_mod.load_risk_seeds(),
        )
        for rec in report.exited:
            self.events.emit(
                "SL_HIT" if rec["exit_reason"] != "SQUAREOFF" else "SQUAREOFF_FILL",
                rec["symbol"], **{k: v for k, v in rec.items() if k != "symbol"})
        self.events.emit(
            "DAEMON_STOP",
            realized_r=self.session.realized_r_today,
            realized_pnl=self.session.realized_pnl_today,
        )
        notify(
            f"Session done. Realised: Rs {self.session.realized_pnl_today} "
            f"({self.session.realized_r_today}R)"
        )

    def _wait_until_flat(self, symbols: list[str], timeout_s: float = 10.0,
                         poll_s: float = 1.0) -> None:
        """Poll positions_day() until every symbol just market-exited shows flat, instead
        of trusting a fixed sleep(2). A market order accepted by the API is not the same
        moment as the fill landing in positions() — reconciling against a fixed sleep would
        either race a slow fill (still shows open, gets mis-read) or waste time waiting
        past a fast one every single day."""
        deadline = _time.monotonic() + timeout_s
        while _time.monotonic() < deadline:
            open_now = state_mod.open_mis_positions(self.broker.positions_day())
            if not any(s in open_now for s in symbols):
                return
            _time.sleep(poll_s)
        self.events.emit("WARNING", message=f"squareoff: positions still open after "
                         f"{timeout_s:.0f}s poll — reconciling with whatever the broker "
                         "reports now")

    # ---------- periodic passes (each on its own cadence, see _run_loop) ----------

    def _reconcile_pass(self, positions_day, orders, seeds) -> None:
        with self._lock:
            report = state_mod.reconcile(self.session, positions_day, orders, seeds)
        for oid in report.orphan_sl_cancelled:
            try:
                self.broker.cancel_order(oid)
                self.events.emit("ORPHAN_SL_CANCELLED", data_order_id=oid)
            except Exception as e:
                self.events.emit("WARNING", message=f"orphan SL cancel failed: {e}")
        for rec in report.partial_exits:
            self.events.emit("PARTIAL_EXIT", rec["symbol"],
                            **{k: v for k, v in rec.items() if k != "symbol"})
            sign = "+" if rec["realized_pnl"] >= 0 else ""
            notify(
                f"{rec['symbol']} partial exit: {rec['qty']} @ {rec['exit_price']} — "
                f"{sign}Rs {rec['realized_pnl']} ({rec['realized_r']}R) recorded"
            )
        for rec in report.exited:
            self._live_ltp.pop(rec["symbol"], None)
            self.events.emit("SL_HIT", rec["symbol"],
                            **{k: v for k, v in rec.items() if k != "symbol"})
            sign = "+" if rec["realized_pnl"] >= 0 else ""
            notify(
                f"{rec['symbol']} exited ({rec['exit_reason']}) at {rec['exit_price']} — "
                f"{sign}Rs {rec['realized_pnl']} ({rec['realized_r']}R)",
                sound=True,
            )
        for symbol in report.new_tracked:
            tp = self.session.positions[symbol]
            self.events.emit("POSITION_DISCOVERED", symbol, entry=tp.entry, qty=tp.qty,
                            sl_pct=tp.sl_pct, derived=tp.sl_pct_derived, sl_price=tp.sl_price)
            notify(f"Tracking {symbol}: entry {tp.entry}, SL {tp.sl_price} ({tp.sl_pct:.2%})")
        for r in report.refreshed:
            self.events.emit("POSITION_REFRESHED", r["symbol"], **{k: v for k, v in r.items()
                                                                   if k != "symbol"})
            notify(f"{r['symbol']} re-read after qty change: entry {r['entry'][0]} -> "
                   f"{r['entry'][1]}, sl_pct {r['sl_pct'][0]} -> {r['sl_pct'][1]}")
        for w in report.warnings:
            self.events.emit("WARNING", message=w)

        # Unprotected positions: place a fresh SL if we have a seed; otherwise scream
        for symbol in report.unprotected:
            seed = seeds.get(symbol, {})
            pos_row = state_mod.open_mis_positions(positions_day)[symbol]
            direction = state_mod.position_direction(pos_row)
            entry = state_mod.position_entry_price(pos_row)
            qty = abs(pos_row.quantity)
            if "sl_pct" in seed:
                sl_pct = float(seed["sl_pct"])
                lvls = [v for v in (seed.get("pdh"), seed.get("pdl")) if v]
                trigger = rules.initial_sl_price(entry, sl_pct, direction, lvls,
                                                 self._tick_cache.get(symbol, config.NSE_TICK))
                new_id = execution.place_sl(self.broker, symbol, direction, trigger, qty)
                with self._lock:
                    self.session.positions[symbol] = TrackedPosition(
                        symbol=symbol, direction=direction.value, entry=entry, qty=qty,
                        sl_order_id=new_id, sl_price=trigger, sl_pct=sl_pct,
                        pdh=seed.get("pdh"), pdl=seed.get("pdl"),
                    )
                self.events.emit("SL_REPLACED", symbol, new_order_id=new_id, trigger=trigger,
                                quantity=qty, note="position had no SL — placed one")
                notify(f"{symbol} had NO SL — placed SL-M at {trigger}", sound=True)
            else:
                self.events.emit("WARNING", symbol,
                                message="position has NO SL and no sl_pct seed — cannot protect")
                alert_dialog(f"{symbol} is UNPROTECTED (no SL order, no risk.json seed). "
                             "Add one: vigil add-position " + symbol + " --sl-pct 1.0")

        # Tracked position whose SL vanished. Alert every pass — this is the loudest
        # condition the daemon has, because the position is naked until someone acts.
        # The whole re-protect attempt (including the broker call) runs under the same
        # lock a tick-driven decision would use — without it, a tick for this exact
        # symbol arriving mid-reprotect could read/write tp.sl_order_id concurrently with
        # this loop, the same class of race the shared lock exists to rule out everywhere
        # else a position's SL gets touched.
        for lost in report.lost_sl:
            symbol = lost["symbol"]
            self.events.emit("SL_LOST", symbol, **{k: v for k, v in lost.items()
                                                   if k != "symbol"})
            with self._lock:
                tp = self.session.positions.get(symbol)
                if config.AUTO_REPROTECT and tp is not None:
                    seed = seeds.get(symbol, {})
                    lvls = [v for v in (seed.get("pdh"), seed.get("pdl")) if v]
                    trigger = rules.initial_sl_price(
                        tp.entry, tp.sl_pct, tp.dir, lvls,
                        self._tick_cache.get(symbol, config.NSE_TICK))
                    try:
                        new_id = execution.place_sl(self.broker, symbol, tp.dir, trigger, tp.qty)
                    except Exception as e:
                        self.events.emit("ERROR", symbol, message=f"re-protect failed: {e!r}")
                        alert_dialog(f"{symbol} is UNPROTECTED and re-protect FAILED: {e}")
                        self._auto_enqueue(
                            f"Run /intraday-vigil monitor — {symbol} is UNPROTECTED and "
                            f"the automatic re-protect attempt FAILED ({e}). Needs an "
                            f"immediate decision.",
                            context={"symbol": symbol, "qty": lost["qty"],
                                     "direction": lost["direction"], "reprotect_error": str(e)})
                        continue
                    # keep phase/breakeven history — this is the same trade, not a new one
                    tp.sl_order_id, tp.sl_price = new_id, trigger
                    self.events.emit("SL_REPLACED", symbol, new_order_id=new_id, trigger=trigger,
                                     quantity=tp.qty, note="SL had vanished — auto re-protected")
                    notify(f"{symbol}: SL was gone — re-placed at {trigger}", sound=True)
                else:
                    alert_dialog(
                        f"{symbol} is UNPROTECTED — {lost['qty']} {lost['direction']} with no "
                        f"stop (order {lost['sl_order_id']} is {lost['status']}).\n\n"
                        f"Re-place it:  vigil protect {symbol}\n"
                        f"Or exit now:  vigil exit {symbol}"
                    )
                    notify(f"{symbol} HAS NO STOP — {lost['qty']} shares naked", sound=True)
                    self._auto_enqueue(
                        f"Run /intraday-vigil monitor — {symbol} has NO STOP, "
                        f"{lost['qty']} {lost['direction']} shares naked. Needs an "
                        f"immediate decision: re-protect or exit.",
                        context={"symbol": symbol, "qty": lost["qty"],
                                 "direction": lost["direction"],
                                 "sl_order_status": lost.get("status")})

        if self.session.kill_switch and "kill_switch_announced" not in self.session.fired:
            self.session.fired.append("kill_switch_announced")
            self.events.emit("KILL_SWITCH", realized_r=self.session.realized_r_today)
            notify(f"KILL SWITCH: day at {self.session.realized_r_today}R — no new entries",
                   sound=True)
            # REASSESS, not MONITOR: the day going wrong enough to trip the kill switch is
            # exactly the "bigger move" case worth a full re-look (sector rank, thesis
            # decay across every open position), not just a protection-status read. Still
            # propose-only — no new entries are possible anyway with the switch tripped.
            self._auto_enqueue(
                f"Run /intraday-vigil reassess — KILL SWITCH triggered, day at "
                f"{self.session.realized_r_today}R. No new entries allowed. Re-check every "
                f"open position's sector rank and thesis, and decide whether any should be "
                f"exited early.",
                context={"realized_r_today": self.session.realized_r_today,
                         "realized_pnl_today": self.session.realized_pnl_today})

    def _qty_verify_pass(self, orders) -> None:
        """SL order qty verification. The modify MUST be re-read and confirmed: Kite can
        accept the request and the exchange still reject it (e.g. "16448: difference
        between limit price and trigger price is beyond permissible range"), which leaves
        the position part-unprotected. Emitting SL_QTY_FIX without verifying once made the
        audit log claim a fix that never happened, leaving a majority of shares naked.

        Locked per-position, same as a tick-driven decision — a qty fix and a concurrent
        breakeven/trail modify for the same order must never interleave (one could
        silently clobber the other's price or quantity), but a fix on one symbol must not
        block a tick's decision on an unrelated one. `orders` is only used as a cheap
        first filter to skip positions with nothing to fix — a fresh, lock-protected
        re-read happens right before actually acting, since a concurrent tick could have
        already modified this exact order in the gap between that filter and the lock."""
        for tp in list(self.session.positions.values()):
            o = state_mod.order_by_id(orders, tp.sl_order_id)
            if not (o and o.status in state_mod.PENDING_STATUSES and o.quantity != tp.qty):
                continue
            with self._lock:
                fresh_o = state_mod.order_by_id(self.broker.orders(), tp.sl_order_id)
                if not (fresh_o and fresh_o.status in state_mod.PENDING_STATUSES
                        and fresh_o.quantity != tp.qty):
                    continue
                self._fix_qty_mismatch(tp, fresh_o)

    def _fix_qty_mismatch(self, tp: TrackedPosition, o) -> None:
        was = o.quantity
        trigger = o.trigger_price or tp.sl_price
        if self.broker.dry_run:
            self.broker.modify_stop_order(tp.sl_order_id, trigger, tp.qty)
            self.events.emit("SL_QTY_FIX", tp.symbol, was=was, now=tp.qty)
            return
        try:
            self.broker.modify_stop_order(tp.sl_order_id, trigger, tp.qty)
        except Exception as e:
            self.events.emit("SL_MODIFY_REJECTED", tp.symbol, reason=repr(e),
                             wanted_qty=tp.qty, still_qty=was, context="qty_fix")
            alert_dialog(
                f"{tp.symbol}: SL qty fix FAILED ({was} of {tp.qty} protected). {e}")
            return
        after = state_mod.order_by_id(self.broker.orders(), tp.sl_order_id)
        if after is not None and after.quantity == tp.qty \
                and after.status in state_mod.PENDING_STATUSES:
            self.events.emit("SL_QTY_FIX", tp.symbol, was=was, now=tp.qty)
            notify(f"{tp.symbol} SL qty mismatch fixed: {was} -> {tp.qty}")
        else:
            self.events.emit(
                "SL_MODIFY_REJECTED", tp.symbol, context="qty_fix",
                wanted_qty=tp.qty,
                still_qty=(after.quantity if after else None),
                exchange_message=(after.status_message if after else None),
            )
            unprotected = tp.qty - ((after.quantity if after else 0) or 0)
            alert_dialog(
                f"{tp.symbol}: SL qty fix REJECTED — {unprotected} shares UNPROTECTED. "
                f"{(after.status_message if after else None) or 'no exchange message'}"
            )
            notify(f"{tp.symbol} SL qty fix REJECTED — {unprotected} shares unprotected",
                   sound=True)

    def _refresh_levels_cache(self) -> None:
        """Periodic refresh of what a tick alone can't carry: each symbol's day-high/low
        (for the stop-hunt guard) and exchange tick size. Also the moment a freshly
        discovered position gets its PDH/PDL fetched."""
        if not self.session.positions:
            return
        self._tick_cache.update(levels.tick_sizes(self.broker, list(self.session.positions)))
        symbols = [f"NSE:{s}" for s in self.session.positions]
        try:
            quotes = self.broker.quotes(symbols)
        except Exception as e:
            self.events.emit("WARNING", message=f"levels refresh failed: {e!r}")
            return
        for tp in list(self.session.positions.values()):
            self._ensure_pdh_pdl(tp)
            q = quotes.get(f"NSE:{tp.symbol}")
            if not q:
                continue
            self._day_hilo[tp.symbol] = (float(q.ohlc.high), float(q.ohlc.low))
            # Belt-and-suspenders: feed the freshest polled price through the same
            # decision path ticks use, for every position, not just ones with no LTP
            # yet. Ratchet-only guards (min-move, one-shot breakeven) make this safe to
            # call redundantly for a symbol whose tick already handled this — it's a
            # no-op then, not a duplicate action.
            self._on_price(tp.symbol, float(q.last_price), "poll")

    def _poll_prices(self) -> None:
        """Fallback for whichever symbols' ticks have gone stale (dropped socket) — same
        safety-net role the old cycle-cadence trigger poll always played, now covering
        position decisions too, and now scoped to only the symbols that actually need it
        instead of everyone every pass. Routes through the same _on_price dispatcher and
        lock the WebSocket feed uses, so a stale-tick catch-up can never race a fresh tick
        for the same symbol.

        When no push feed is running at all — paper mode (no real WebSocket exists for a
        simulated broker), or a live account where the ticker failed to start — there is
        no tick source to go stale relative to: every watched symbol needs polling every
        pass, not just ones whose (poll-derived) _last_tick_at happens to be older than
        TICK_STALE_AFTER_S. Gating on staleness alone here would let a paper/no-feed
        symbol go quiet for up to TICK_STALE_AFTER_S after its first poll, since a
        poll-derived update would otherwise count as "recently fresh" the same as a real
        tick would.
        """
        if self.broker.dry_run:
            return
        try:
            from . import triggers as triggers_mod
            watched = triggers_mod.all_armed_symbols() | set(self.session.positions)
            if not watched:
                return
            if self.feed is None:
                stale = sorted(watched)
            else:
                now_mono = _time.monotonic()
                stale = sorted(
                    s for s in watched
                    if now_mono - self._last_tick_at.get(s, 0.0) >= config.TICK_STALE_AFTER_S
                )
            if stale:
                self._polling_feed.poll(stale, self._on_price)
        except Exception as e:
            self.events.emit("WARNING", message=f"price poll failed: {e!r}")

    def _build_position_views(self, orders) -> list[dict]:
        """Locked for the whole pass — cheap (no broker calls, just reading already-fetched
        data), and it's what keeps a snapshot from reading tp.phase after a concurrent
        tick updated it but tp.sl_price before that same tick did — every other place a
        TrackedPosition gets read-or-written serializes through this same lock, and a
        snapshot is exactly the kind of multi-field read that a partial interleave would
        make momentarily self-contradictory."""
        with self._lock:
            return self._build_position_views_locked(orders)

    def _build_position_views_locked(self, orders) -> list[dict]:
        views = []
        for tp in list(self.session.positions.values()):
            ltp = self._live_ltp.get(tp.symbol)
            if ltp is None:
                self.events.emit("WARNING", tp.symbol, message="no price yet this session")
                continue
            pr = rules.profit_r(ltp, tp.entry, tp.r, tp.dir)
            is_near = rules.near_sl(ltp, tp.sl_price)
            unrealized = round(
                (ltp - tp.entry) * tp.qty if tp.dir == Direction.LONG
                else (tp.entry - ltp) * tp.qty, 2)
            # `protected` is read back from the broker, never assumed. status.json once
            # showed a live SL price for an order that had been cancelled.
            sl_now = state_mod.order_by_id(orders, tp.sl_order_id)
            protected = (sl_now is not None
                         and sl_now.status in state_mod.PENDING_STATUSES
                         and sl_now.quantity == tp.qty)
            views.append(
                {"symbol": tp.symbol, "direction": tp.direction, "entry": tp.entry,
                 "qty": tp.qty, "ltp": ltp, "profit_r": round(pr, 2), "phase": tp.phase,
                 "unrealized_pnl": unrealized,
                 "protected": protected,
                 "sl_order_status": sl_now.status if sl_now else "MISSING",
                 "sl_order_qty": sl_now.quantity if sl_now else None,
                 "sl_order_id": tp.sl_order_id, "sl_price": tp.sl_price,
                 "sl_pct": tp.sl_pct, "trail_pct": tp.trail_pct,
                 "pdh": tp.pdh, "pdl": tp.pdl, "near_sl": is_near}
            )
        return views

    # ---------- one pass of the run loop ----------

    def cycle(self) -> None:
        """One full pass — reconcile, qty-verify, a price refresh/decision pass, time
        actions, snapshot — everything _tick can do, in one call. A convenience for tests
        and one-off manual invocation; production code (_run_loop) calls _tick directly so
        each concern runs on its own cadence instead of being bundled every time."""
        self._tick(self.now_fn(), do_reconcile=True, do_qty_verify=True)

    def _tick(self, now: datetime, do_reconcile: bool, do_qty_verify: bool) -> None:
        seeds = state_mod.load_risk_seeds()
        positions_day = self.broker.positions_day()
        orders = self.broker.orders()

        if do_reconcile:
            self._reconcile_pass(positions_day, orders, seeds)
            self._refresh_levels_cache()
            orders = self.broker.orders()  # positions/SLs may have just changed above

        if do_qty_verify:
            self._qty_verify_pass(orders)

        # Keep the socket subscription in step with what's armed/tracked, so `vigil arm`
        # or a freshly discovered position takes effect without a daemon restart, then
        # catch up anything whose ticks have gone stale.
        self._sync_trigger_subscriptions()
        self._poll_prices()

        position_views = self._build_position_views(orders)

        for key in rules.due_time_actions(now, set(self.session.fired)):
            self.session.fired.append(key)
            if key == "squareoff":
                self._squareoff()
            else:
                msg = config.TIME_ALERTS[key][1]
                self.events.emit("TIME_ALERT", alert=key, message=msg)
                notify(msg, sound=True)
                self._auto_enqueue(f"Run /intraday-vigil monitor — time alert: {msg}",
                                   context={"alert": key, "message": msg})

        blocked, why = rules.no_new_entries(now, self.session.kill_switch)
        snapshot = {
            "as_of": now.isoformat(),
            "daemon": {"pid": os.getpid(), "mode": "dry_run" if self.broker.dry_run else "live",
                       "broker": self.broker.adapter_kind,
                       "cycle_seconds": config.LOOP_TICK_S, "cycles_run": self.cycles_run},
            "no_new_entries": blocked,
            "no_new_entries_reason": why,
            "kill_switch": self.session.kill_switch,
            "realized_r_today": self.session.realized_r_today,
            "realized_pnl_today": self.session.realized_pnl_today,
            "positions": position_views,
            "closed_today": self.session.closed,
        }
        self.events.write_status(snapshot)
        self.session.save()
        self._print_table(now, position_views)
        self.cycles_run += 1

    def _print_table(self, now, views: list[dict]) -> None:
        lines = [f"\n[{now.strftime('%H:%M:%S')}] check {self.cycles_run}"
                 f"{' (DRY RUN)' if self.broker.dry_run else ''}"]
        phase_tag = {1: "P1 hold", 2: "P2 breakeven", 3: "P3 trailing"}
        for v in views:
            arrow = "^" if v["direction"] == "LONG" else "v"
            near = "  << NEAR SL" if v["near_sl"] else ""
            sl_txt = (f"SL {v['sl_price']:.2f}" if v.get("protected", True)
                      else f"*** NO STOP ({v.get('sl_order_status', '?')}) ***")
            lines.append(
                f"  {v['symbol']:<12}{arrow} ltp {v['ltp']:>10.2f}  {v['profit_r']:+.2f}R  "
                f"Rs {v['unrealized_pnl']:>+9.2f}  {phase_tag[v['phase']]:<13} "
                f"{sl_txt}{near}"
            )
        if not views:
            lines.append("  (no open MIS positions)")
        lines.append(
            f"  day: Rs {self.session.realized_pnl_today} ({self.session.realized_r_today}R)"
            f"{'  KILL-SWITCH' if self.session.kill_switch else ''}"
        )
        logger.info("\n".join(lines))

    def _sync_trigger_subscriptions(self) -> None:
        """Restart the push feed's subscription when the watched set changes — armed
        triggers or tracked positions. Without this, `vigil arm` or a freshly discovered
        position only got real-time coverage after a full daemon restart.

        Two things this guards against, now that the run loop wakes up every
        LOOP_TICK_S (5s) instead of every 150s: emitting TICKER_RESUBSCRIBED only when a
        feed actually attaches, not on every failed attempt (the event name should mean
        what it says); and not hammering a persistently failing connection attempt 30x
        more often than the old cadence would have — backed off to RECONCILE_INTERVAL_S.
        """
        if self.broker.dry_run:
            return
        try:
            from . import triggers as triggers_mod
            want = triggers_mod.all_armed_symbols() | set(self.session.positions)
            have = set(getattr(self.feed, "token_to_symbol", {}).values())
            if want == have:
                return
            now_mono = _time.monotonic()
            if now_mono - self._last_feed_attempt < config.RECONCILE_INTERVAL_S:
                return
            self._last_feed_attempt = now_mono
            if self.feed is not None:
                self.feed.stop()
                self.feed = None
            if want:
                self._start_trigger_feed()
                if self.feed is not None:
                    self.events.emit("TICKER_RESUBSCRIBED", symbols=sorted(want))
        except Exception as e:
            self.events.emit("WARNING", message=f"trigger resubscribe failed: {e!r}")

    # ---------- run ----------

    def run(self, force: bool = False, sleep_fn: Callable = _time.sleep) -> None:
        self.events.emit("DAEMON_START", mode="dry_run" if self.broker.dry_run else "live")
        holidays = clock.load_holidays()
        self._start_trigger_feed()
        try:
            self._run_loop(force, sleep_fn, holidays)
        finally:
            if self.feed is not None:
                self.feed.stop()

    def _start_trigger_feed(self) -> None:
        """Bring up the push feed if anything needs watching. Never fatal — the periodic
        poll fallback still catches breaks, just with up to TICK_STALE_AFTER_S latency."""
        if self.broker.dry_run:
            return
        try:
            from . import triggers as triggers_mod
            symbols = sorted(triggers_mod.all_armed_symbols() | set(self.session.positions))
            if not symbols:
                return
            feed = KiteTickerFeed(self.broker, self.events)
            if feed.start(symbols, self._on_price):
                self.feed = feed
        except Exception as e:
            self.events.emit("WARNING", message=f"price feed failed to start: {e!r}")

    def _run_loop(self, force: bool, sleep_fn: Callable, holidays) -> None:
        last_reconcile = 0.0
        last_qty_verify = 0.0
        last_auto_monitor = 0.0
        while True:
            now = self.now_fn()
            # started before the bell on a market day: wait for the open
            if (clock.is_market_day(now.date(), holidays)
                    and now.time() < config.MARKET_OPEN and not force):
                logger.info("[%s] Market opens at %s — waiting.",
                           now.strftime("%H:%M:%S"), config.MARKET_OPEN.strftime("%H:%M"))
                sleep_fn(60)
                continue
            if not clock.is_market_open(now, holidays) and not force:
                logger.info("Market is closed (%s). Use --force to override.", now)
                break
            interval = config.LOOP_TICK_S
            try:
                now_mono = _time.monotonic()
                do_reconcile = now_mono - last_reconcile >= config.RECONCILE_INTERVAL_S
                do_qty_verify = now_mono - last_qty_verify >= config.QTY_VERIFY_INTERVAL_S
                self._tick(now, do_reconcile, do_qty_verify)
                # LOOP_TICK_S is already short, but a scheduled squareoff/alert still
                # deserves the same "don't sleep past it" precision the old flat-interval
                # loop needed — cheap to keep, and it's what beats the broker's own
                # force-square by SQUAREOFF_AT's lead time, not by luck.
                next_action_s = rules.seconds_until_next_action(now, set(self.session.fired))
                if next_action_s is not None:
                    interval = min(interval, max(next_action_s, 1))
                if do_reconcile:
                    last_reconcile = now_mono
                if do_qty_verify:
                    last_qty_verify = now_mono
                # Decoupled from every other cadence here, and off by default
                # (AUTO_MONITOR_INTERVAL_S == 0) — this is the only auto-enqueue trigger
                # that fires on a schedule rather than only when something actually
                # happened, so it's the one with a real, ongoing token cost to opt into.
                if (config.AUTO_MONITOR_INTERVAL_S > 0
                        and now_mono - last_auto_monitor >= config.AUTO_MONITOR_INTERVAL_S):
                    last_auto_monitor = now_mono
                    self._auto_enqueue(
                        "Run /intraday-vigil monitor — render the daemon snapshot, check "
                        "protection, and run the thesis-decay check.",
                        context={
                            "positions": [tp.symbol for tp in self.session.positions.values()],
                            "realized_pnl_today": self.session.realized_pnl_today,
                            "realized_r_today": self.session.realized_r_today,
                            "kill_switch": self.session.kill_switch,
                        })
                self.failed_cycles = 0
            except TokenException as e:
                self.events.emit("ERROR", message=f"token expired mid-session: {e}")
                if "token_alerted" not in self.session.fired:
                    self.session.fired.append("token_alerted")
                    alert_dialog(
                        "Kite token expired mid-session. Broker SLs still protect positions. "
                        "Run `vigil login` in another terminal — the daemon will keep retrying."
                    )
                interval = 60
            except KeyboardInterrupt:
                self.events.emit("DAEMON_STOP", reason="keyboard interrupt")
                logger.info("Stopped. Broker SL orders remain active.")
                break
            except Exception as e:
                self.failed_cycles += 1
                # Always keep the traceback: a bare repr() once cost real time hunting for the
                # cause while the loop was crash-looping and SLs went unmanaged.
                self.events.emit("ERROR", message=f"check failed: {e!r}",
                                 tb=_traceback.format_exc()[-2000:],
                                 consecutive_failures=self.failed_cycles)
                if self.failed_cycles >= config.MAX_FAILED_CYCLES_BEFORE_ALERT:
                    notify(f"{self.failed_cycles} consecutive failures: {e}", sound=True)
                    # A toast is missable. If the loop is wedged, SLs are not being managed —
                    # that warrants a modal the user has to dismiss.
                    alert_dialog(
                        f"Daemon has failed {self.failed_cycles} checks in a row.\n\n{e}\n\n"
                        "SL lifecycle is NOT running. Check logs/algo.log."
                    )
            if self.session.squareoff_done:
                break
            try:
                sleep_fn(interval)
            except KeyboardInterrupt:
                self.events.emit("DAEMON_STOP", reason="keyboard interrupt")
                logger.info("Stopped. Broker SL orders remain active.")
                break
