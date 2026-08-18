"""Everything that places, scales, protects, or closes a position."""
from __future__ import annotations

import json
import sys
import time

from .. import config, execution, rules, state as state_mod
from .. import triggers as triggers_mod
from ..monitor import MonitorLoop
from ..rules import Direction
from ..state import SessionState
from ._shared import _as_fraction, _live_broker, _open_position


def cmd_add_position(args) -> int:
    seeds = state_mod.load_risk_seeds()
    entry = seeds.get(args.symbol, {})
    entry["sl_pct"] = args.sl_pct / 100 if args.sl_pct > 0.2 else args.sl_pct
    if args.pdh:
        entry["pdh"] = args.pdh
    if args.pdl:
        entry["pdl"] = args.pdl
    seeds[args.symbol] = entry
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.RISK_FILE.write_text(json.dumps(seeds, indent=2))
    print(f"{args.symbol}: sl_pct={entry['sl_pct']:.2%} saved to {config.RISK_FILE}")
    return 0


def cmd_enter(args) -> int:
    """Open a MIS position and protect it, entirely on the daemon's own token."""
    broker, events = _live_broker(dry_run=args.dry_run)
    session = SessionState.load_or_create()

    blocked = triggers_mod.gate_block_reason(session)
    if blocked and not args.override_gate:
        print(f"REFUSED — entry gate: {blocked}", file=sys.stderr)
        return 3

    direction = Direction.LONG if args.side.upper() == "LONG" else Direction.SHORT
    sl_pct = _as_fraction(args.sl_pct)
    if sl_pct > 0.015:
        print(f"REFUSED — sl_pct {sl_pct:.2%} exceeds the 1.5% cap; wide stops make "
              "Phase 2/3 unreachable.", file=sys.stderr)
        return 3

    t = triggers_mod.Trigger(
        symbol=args.symbol.upper(), direction=direction.value, level=0.0, side="above",
        qty=args.qty, sl_pct=sl_pct, pdh=args.pdh, pdl=args.pdl, auto=True,
        note="manual vigil enter",
    )
    ltp = 0.0
    try:
        q = broker.quotes([f"NSE:{t.symbol}"])
        ltp = float(q[f"NSE:{t.symbol}"].last_price)
    except Exception:
        pass

    if not args.yes:
        est_sl = rules.initial_sl_price(ltp, sl_pct, direction,
                                        [v for v in (args.pdh, args.pdl) if v]) if ltp else 0.0
        print(f"{t.symbol} {direction.value} qty {args.qty} @ ~{ltp} — SL ~{est_sl} "
              f"({sl_pct:.2%}), risk ~Rs {abs(ltp - est_sl) * args.qty:,.0f}")
        if input("Place this order? [y/N] ").strip().lower() != "y":
            print("Aborted.")
            return 1

    ok = triggers_mod.execute(t, broker, events, session, ltp)
    print(f"{'PLACED' if ok else 'FAILED'}: {t.detail}")
    return 0 if ok else 4


def cmd_add(args) -> int:
    """Scale into an OPEN position and keep the risk seed honest.

    Adding shares moves the entry VWAP, which changes the effective sl_pct against the
    resting SL. Doing this by hand once left the seed stale and the daemon reported a
    profitable position as a loss. This command recomputes and rewrites it.
    """
    broker, events = _live_broker(dry_run=args.dry_run)
    session = SessionState.load_or_create()
    symbol = args.symbol.upper()

    blocked = triggers_mod.gate_block_reason(session)
    if blocked and not args.override_gate:
        print(f"REFUSED — entry gate: {blocked}", file=sys.stderr)
        return 3

    pos = _open_position(broker, symbol)
    if pos is None:
        print(f"No open MIS position in {symbol}. Use `vigil enter` to open one.",
              file=sys.stderr)
        return 3

    direction = state_mod.position_direction(pos)
    old_qty = abs(pos.quantity)
    old_entry = state_mod.position_entry_price(pos)

    sl_order = state_mod.find_sl_order(broker.orders(), symbol, direction)
    if sl_order is None:
        print(f"{symbol} has NO resting SL — refusing to add to an unprotected position.",
              file=sys.stderr)
        return 3
    sl_price = sl_order.trigger_price or 0.0

    if not args.yes:
        print(f"{symbol} {direction.value}: {old_qty} @ {old_entry:.2f}, SL {sl_price} "
              f"-> adding {args.qty}")
        if input("Proceed? [y/N] ").strip().lower() != "y":
            print("Aborted.")
            return 1

    execution.place_entry(broker, symbol, direction, args.qty)

    time.sleep(2)
    pos = _open_position(broker, symbol) or pos
    new_qty = abs(pos.quantity)
    new_entry = state_mod.position_entry_price(pos)
    eff = round(abs(new_entry - sl_price) / new_entry, 4) if new_entry else 0.0

    seeds = state_mod.load_risk_seeds()
    seed = dict(seeds.get(symbol, {}))
    seed["sl_pct"] = eff
    seeds[symbol] = seed
    state_mod.save_risk_seeds(seeds)

    events.emit("POSITION_SCALED", symbol, old_qty=old_qty, new_qty=new_qty,
                old_entry=old_entry, new_entry=new_entry, sl_price=sl_price, sl_pct=eff)
    print(f"{symbol}: {old_qty} -> {new_qty}, entry {old_entry:.2f} -> {new_entry:.2f}, "
          f"effective sl_pct {eff:.4f} (seed updated)")
    if eff > 0.015:
        print("WARNING: effective SL width now exceeds 1.5% — Phase 2/3 may be unreachable.")
    print("The daemon will resize the SL order to match on its next cycle (verified).")
    return 0


def cmd_exit(args) -> int:
    """Exit ONE symbol: cancel its SL, then market-exit. `squareoff` does everything."""
    broker, events = _live_broker(dry_run=args.dry_run)
    symbol = args.symbol.upper()
    pos = _open_position(broker, symbol)
    if pos is None:
        print(f"No open MIS position in {symbol}.", file=sys.stderr)
        return 3
    direction = state_mod.position_direction(pos)
    qty = abs(pos.quantity)

    if not args.yes:
        print(f"Exit {symbol} {direction.value} {qty} at market (cancels its SL first)?")
        if input("[y/N] ").strip().lower() != "y":
            print("Aborted.")
            return 1

    sl_order = state_mod.find_sl_order(broker.orders(), symbol, direction)
    if sl_order is not None:
        try:
            broker.cancel_order(sl_order.order_id)
        except Exception as e:
            events.emit("WARNING", symbol, message=f"SL cancel failed before exit: {e}")
            print(f"WARNING: could not cancel the SL ({e}) — it may fill alongside the exit.",
                  file=sys.stderr)
    execution.place_market_exit(broker, symbol, direction, qty)
    events.emit("MANUAL_EXIT", symbol, qty=qty, direction=direction.value)
    time.sleep(2)
    still = _open_position(broker, symbol)
    if still is None:
        print(f"{symbol}: exit filled — position FLAT.")
    else:
        print(f"{symbol}: exit sent, still showing {abs(still.quantity)} open. "
              "Re-check with `vigil positions`.")
    return 0


def cmd_protect(args) -> int:
    """Re-place a missing SL on an open position, preserving its phase history."""
    broker, events = _live_broker(dry_run=args.dry_run)
    session = SessionState.load_or_create()
    symbol = args.symbol.upper()

    pos = _open_position(broker, symbol)
    if pos is None:
        print(f"No open MIS position in {symbol}.", file=sys.stderr)
        return 3
    direction = state_mod.position_direction(pos)
    qty = abs(pos.quantity)
    entry = state_mod.position_entry_price(pos)

    existing = state_mod.find_sl_order(broker.orders(), symbol, direction)
    if existing is not None and not args.force:
        print(f"{symbol} already has a resting SL (order {existing.order_id}, "
              f"trigger {existing.trigger_price}, qty {existing.quantity}). "
              "Use --force to place another anyway.", file=sys.stderr)
        return 3

    tp = session.positions.get(symbol)
    seed = state_mod.load_risk_seeds().get(symbol, {})
    sl_pct = args.sl_pct and _as_fraction(args.sl_pct)
    if sl_pct is None:
        sl_pct = float(seed["sl_pct"]) if "sl_pct" in seed else (tp.sl_pct if tp else None)
    if sl_pct is None:
        print(f"No sl_pct for {symbol} — pass --sl-pct or seed it with `vigil add-position`.",
              file=sys.stderr)
        return 3
    if sl_pct > 0.015:
        print(f"REFUSED — sl_pct {sl_pct:.2%} exceeds the 1.5% cap.", file=sys.stderr)
        return 3

    lvls = [v for v in (seed.get("pdh"), seed.get("pdl")) if v]
    trigger = args.trigger or rules.initial_sl_price(entry, sl_pct, direction, lvls)

    if not args.yes:
        print(f"{symbol} {direction.value} {qty} @ {entry:.2f} — place SL-M at {trigger} "
              f"({abs(entry - trigger) / entry:.2%}, risk Rs {abs(entry - trigger) * qty:,.0f})")
        if input("Place it? [y/N] ").strip().lower() != "y":
            print("Aborted.")
            return 1

    new_id = execution.place_sl(broker, symbol, direction, trigger, qty)
    if tp is not None:
        tp.sl_order_id, tp.sl_price = new_id, trigger   # same trade: keep phase/breakeven
        session.save()
    events.emit("SL_REPLACED", symbol, new_order_id=new_id, trigger=trigger, quantity=qty,
                note="manual vigil protect")
    print(f"{symbol}: SL-M placed, order {new_id}, trigger {trigger}, qty {qty}")
    return 0


def cmd_squareoff(args) -> int:
    if not args.yes:
        confirm = input("Cancel all MIS SL orders and market-exit every position? [y/N] ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            return 1
    broker, events = _live_broker(dry_run=args.dry_run)
    session = SessionState.load_or_create()
    loop = MonitorLoop(broker, events, session)
    loop.cycle()  # reconcile first so we exit what actually exists
    loop._squareoff()
    session.save()
    return 0
