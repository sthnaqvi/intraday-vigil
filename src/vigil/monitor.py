"""The monitor loop: reconcile -> qty check -> SL lifecycle -> time rules -> status.

Design invariants:
- Only ratchets: SL moves are decided in rules.py and verified after every modify
  before state advances, so a crash between modify and persist self-heals.
- A cycle crash never kills the process.
- The resting SL-M at the exchange does the actual exiting; this loop only
  adjusts triggers, so polling cadence (150s/90s) is enough.
"""
from __future__ import annotations

import os
import time as _time
import traceback as _traceback
from collections.abc import Callable

from . import clock, config, execution, levels, rules
from . import state as state_mod
from .events import EventLog, logger
from .feed import KiteTickerFeed, PollingFeed
from .guard import GuardedBroker, TokenException
from .models import Quote
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
        self.trigger_engine = TriggerEngine(broker, events, session)
        self.feed: KiteTickerFeed | None = None  # push feed, when anything is armed
        self._polling_feed = PollingFeed(broker)

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

    @staticmethod
    def _levels_for(tp: TrackedPosition, quote: Quote) -> list[float]:
        candidates = [tp.pdh, tp.pdl, quote.ohlc.high, quote.ohlc.low]
        return [float(lv) for lv in candidates if lv]

    # ---------- SL execution ----------

    def _execute_intent(self, tp: TrackedPosition, intent: ModifyIntent, quote: Quote) -> None:
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
                            message="modify not verified — will retry next cycle")

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

    # ---------- one cycle ----------

    def cycle(self) -> int:
        """Runs one full cycle; returns the sleep interval in seconds."""
        now = self.now_fn()
        seeds = state_mod.load_risk_seeds()
        positions_day = self.broker.positions_day()
        orders = self.broker.orders()

        # 1. Reconcile broker truth vs tracked state
        report = state_mod.reconcile(self.session, positions_day, orders, seeds)
        for oid in report.orphan_sl_cancelled:
            try:
                self.broker.cancel_order(oid)
                self.events.emit("ORPHAN_SL_CANCELLED", data_order_id=oid)
            except Exception as e:
                self.events.emit("WARNING", message=f"orphan SL cancel failed: {e}")
        for rec in report.exited:
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
                trigger = rules.initial_sl_price(entry, sl_pct, direction, lvls)
                new_id = execution.place_sl(self.broker, symbol, direction, trigger, qty)
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

        # 1b. Tracked position whose SL vanished. Alert EVERY cycle — this is the loudest
        # condition the daemon has, because the position is naked until someone acts.
        for lost in report.lost_sl:
            symbol = lost["symbol"]
            tp = self.session.positions.get(symbol)
            self.events.emit("SL_LOST", symbol, **{k: v for k, v in lost.items()
                                                   if k != "symbol"})
            if config.AUTO_REPROTECT and tp is not None:
                seed = seeds.get(symbol, {})
                lvls = [v for v in (seed.get("pdh"), seed.get("pdl")) if v]
                trigger = rules.initial_sl_price(tp.entry, tp.sl_pct, tp.dir, lvls)
                try:
                    new_id = execution.place_sl(self.broker, symbol, tp.dir, trigger, tp.qty)
                except Exception as e:
                    self.events.emit("ERROR", symbol, message=f"re-protect failed: {e!r}")
                    alert_dialog(f"{symbol} is UNPROTECTED and re-protect FAILED: {e}")
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

        # 2. Kill switch announcement (state set inside reconcile)
        if self.session.kill_switch and "kill_switch_announced" not in self.session.fired:
            self.session.fired.append("kill_switch_announced")
            self.events.emit("KILL_SWITCH", realized_r=self.session.realized_r_today)
            notify(f"KILL SWITCH: day at {self.session.realized_r_today}R — no new entries",
                   sound=True)

        # 3. SL order qty verification — every cycle, before lifecycle decisions.
        # The modify MUST be re-read and confirmed: Kite can accept the request and the
        # exchange still reject it (e.g. "16448: difference between limit price and trigger
        # price is beyond permissible range"), which leaves the position part-unprotected.
        # Emitting SL_QTY_FIX without verifying once made the audit log claim a fix that
        # never happened, leaving a majority of shares naked.
        for tp in self.session.positions.values():
            o = state_mod.order_by_id(orders, tp.sl_order_id)
            if o and o.status in state_mod.PENDING_STATUSES and o.quantity != tp.qty:
                was = o.quantity
                trigger = o.trigger_price or tp.sl_price
                if self.broker.dry_run:
                    self.broker.modify_stop_order(tp.sl_order_id, trigger, tp.qty)
                    self.events.emit("SL_QTY_FIX", tp.symbol, was=was, now=tp.qty)
                    continue
                try:
                    self.broker.modify_stop_order(tp.sl_order_id, trigger, tp.qty)
                except Exception as e:
                    self.events.emit("SL_MODIFY_REJECTED", tp.symbol, reason=repr(e),
                                     wanted_qty=tp.qty, still_qty=was, context="qty_fix")
                    alert_dialog(
                        f"{tp.symbol}: SL qty fix FAILED ({was} of {tp.qty} protected). {e}")
                    continue
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

        # 3b. Armed triggers. Keep the socket subscription in step with what is armed,
        # so `vigil arm` takes effect without a daemon restart, then run the poll as the
        # fallback for a dropped socket.
        self._sync_trigger_subscriptions()
        self._poll_triggers()

        # 4. Lifecycle decisions per open position
        any_near = False
        position_views = []
        if self.session.positions:
            symbols = [f"NSE:{s}" for s in self.session.positions]
            quotes = self.broker.quotes(symbols)
            for tp in list(self.session.positions.values()):
                q = quotes.get(f"NSE:{tp.symbol}")
                if not q:
                    self.events.emit("WARNING", tp.symbol, message="no quote this cycle")
                    continue
                self._ensure_pdh_pdl(tp)
                ltp = q.last_price
                pr = rules.profit_r(ltp, tp.entry, tp.r, tp.dir)
                new_phase = max(tp.phase, rules.target_phase(pr))
                if new_phase != tp.phase:
                    tp.phase = new_phase
                    self.events.emit("PHASE_CHANGE", tp.symbol, phase=new_phase,
                                    profit_r=round(pr, 2))
                    notify(f"{tp.symbol} -> Phase {new_phase} ({pr:+.2f}R)")

                lvls = self._levels_for(tp, q)
                intent = None
                if tp.phase >= 2 and not tp.breakeven_done:
                    intent = rules.breakeven_decision(
                        tp.entry, tp.sl_price, tp.dir, lvls, tp.sl_order_id, tp.qty)
                elif tp.phase == 3:
                    intent = rules.trail_decision(
                        ltp, tp.trail_pct, tp.sl_price, tp.dir, lvls, tp.sl_order_id, tp.qty,
                        require_min_move=tp.trail_started)
                if intent:
                    self._execute_intent(tp, intent, q)

                is_near = rules.near_sl(ltp, tp.sl_price)
                unrealized = round(
                    (ltp - tp.entry) * tp.qty if tp.dir == Direction.LONG
                    else (tp.entry - ltp) * tp.qty, 2)
                # `protected` is read back from the broker, never assumed. status.json
                # once showed a live SL price for an order that had been cancelled.
                sl_now = state_mod.order_by_id(orders, tp.sl_order_id)
                protected = (sl_now is not None
                             and sl_now.status in state_mod.PENDING_STATUSES
                             and sl_now.quantity == tp.qty)
                # An unprotected position is at least as urgent as a "near SL" one — the
                # 2026-08-18 incident sat naked for 37 minutes partly because detection ran
                # at the slow 150s cadence the whole time, since price never got near the
                # (already-cancelled) stop. Missing protection forces the fast cycle too.
                any_near = any_near or is_near or not protected
                position_views.append(
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

        # 5. Time actions
        for key in rules.due_time_actions(now, set(self.session.fired)):
            self.session.fired.append(key)
            if key == "squareoff":
                self._squareoff()
            else:
                msg = config.TIME_ALERTS[key][1]
                self.events.emit("TIME_ALERT", alert=key, message=msg)
                notify(msg, sound=True)

        # 6. Status snapshot + console table
        interval = rules.cycle_interval(any_near)
        next_action_s = rules.seconds_until_next_action(now, set(self.session.fired))
        if next_action_s is not None:
            # A flat cycle interval knows nothing about the clock — it can sleep straight
            # past a scheduled alert or squareoff if a cycle happens to finish just before
            # one is due. Clamping to whichever is sooner is what actually guarantees the
            # daemon wakes up in time to act, not just "usually does".
            interval = min(interval, max(next_action_s, 1))
        blocked, why = rules.no_new_entries(now, self.session.kill_switch)
        snapshot = {
            "as_of": now.isoformat(),
            "daemon": {"pid": os.getpid(), "mode": "dry_run" if self.broker.dry_run else "live",
                       "broker": self.broker.adapter_kind,
                       "cycle_seconds": interval, "cycles_run": self.cycles_run},
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
        self._print_table(now, position_views, interval)
        self.cycles_run += 1
        return interval

    def _print_table(self, now, views: list[dict], interval: int) -> None:
        lines = [f"\n[{now.strftime('%H:%M:%S')}] cycle {self.cycles_run}"
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
            f"{'  KILL-SWITCH' if self.session.kill_switch else ''} | next in {interval}s"
        )
        logger.info("\n".join(lines))

    def _sync_trigger_subscriptions(self) -> None:
        """Restart the push feed's subscription when the armed set changes.

        Without this, `vigil arm` only took effect after a full daemon restart — and a
        restart with a live position is exactly what we want to avoid.
        """
        if self.broker.dry_run:
            return
        try:
            from . import triggers as triggers_mod
            want = triggers_mod.all_armed_symbols()
            have = set(getattr(self.feed, "token_to_symbol", {}).values())
            if want == have:
                return
            if self.feed is not None:
                self.feed.stop()
                self.feed = None
            if want:
                self._start_trigger_feed()
                self.events.emit("TICKER_RESUBSCRIBED", symbols=sorted(want))
        except Exception as e:
            self.events.emit("WARNING", message=f"trigger resubscribe failed: {e!r}")

    def _poll_triggers(self) -> None:
        """Cycle-cadence safety net for armed triggers when ticks are not flowing. Feeds
        the same TriggerEngine the WebSocket feed does, through the same lock."""
        if self.broker.dry_run:
            return
        try:
            from . import triggers as triggers_mod
            symbols = sorted(triggers_mod.all_armed_symbols())
            self._polling_feed.poll(symbols, self.trigger_engine.on_price)
        except Exception as e:
            self.events.emit("WARNING", message=f"trigger poll failed: {e!r}")

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
        """Bring up the push feed if anything is armed. Never fatal — the cycle-level
        poll in step 3b still catches breaks, just with up to one cycle of latency."""
        if self.broker.dry_run:
            return
        try:
            from . import triggers as triggers_mod
            symbols = sorted(triggers_mod.all_armed_symbols())
            if not symbols:
                return
            feed = KiteTickerFeed(self.broker, self.events)
            if feed.start(symbols, self.trigger_engine.on_price):
                self.feed = feed
        except Exception as e:
            self.events.emit("WARNING", message=f"trigger feed failed to start: {e!r}")

    def _run_loop(self, force: bool, sleep_fn: Callable, holidays) -> None:
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
            try:
                interval = self.cycle()
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
                # cause while the cycle was crash-looping and SLs went unmanaged.
                self.events.emit("ERROR", message=f"cycle failed: {e!r}",
                                 tb=_traceback.format_exc()[-2000:],
                                 consecutive_failures=self.failed_cycles)
                if self.failed_cycles >= config.MAX_FAILED_CYCLES_BEFORE_ALERT:
                    notify(f"{self.failed_cycles} consecutive cycle failures: {e}", sound=True)
                    # A toast is missable. If the loop is wedged, SLs are not being managed —
                    # that warrants a modal the user has to dismiss.
                    alert_dialog(
                        f"Daemon has failed {self.failed_cycles} cycles in a row.\n\n{e}\n\n"
                        "SL lifecycle is NOT running. Check logs/algo.log."
                    )
                interval = config.CYCLE_NEAR_SL_S
            if self.session.squareoff_done:
                break
            try:
                sleep_fn(interval)
            except KeyboardInterrupt:
                self.events.emit("DAEMON_STOP", reason="keyboard interrupt")
                logger.info("Stopped. Broker SL orders remain active.")
                break
