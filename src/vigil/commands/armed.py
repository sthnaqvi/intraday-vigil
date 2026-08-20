"""Armed price-trigger management: arm, list, disarm.

Named armed.py rather than triggers.py to avoid shadowing vigil.triggers, the WebSocket
trigger engine these commands configure.
"""
from __future__ import annotations

import sys

from .. import levels
from .. import triggers as triggers_mod
from ..rules import Direction
from ._shared import _as_fraction, _daemon_pid, _live_broker


def cmd_arm(args) -> int:
    """Arm a price trigger. The daemon watches it over the tick WebSocket."""
    broker, events = _live_broker()
    symbol = args.symbol.upper()
    if (args.above is None) == (args.below is None):
        print("Specify exactly one of --above / --below", file=sys.stderr)
        return 2
    side = "above" if args.above is not None else "below"
    level = args.above if args.above is not None else args.below
    direction = Direction.LONG if args.side.upper() == "LONG" else Direction.SHORT
    sl_pct = _as_fraction(args.sl_pct)
    if sl_pct > 0.015:
        print(f"REFUSED — sl_pct {sl_pct:.2%} exceeds the 1.5% cap.", file=sys.stderr)
        return 3

    pdh, pdl = args.pdh, args.pdl
    if pdh is None or pdl is None:
        tokens = levels.instrument_tokens(broker, [symbol])
        if symbol in tokens:
            got = levels.fetch_pdh_pdl(broker, symbol, tokens[symbol], events)
            if got:
                pdh, pdl = pdh or got[0], pdl or got[1]

    all_t = triggers_mod.load()
    all_t.append(triggers_mod.Trigger(
        symbol=symbol, direction=direction.value, level=float(level), side=side,
        qty=args.qty, sl_pct=sl_pct, auto=args.auto, pdh=pdh, pdl=pdl, note=args.note or "",
    ))
    triggers_mod.save(all_t)
    events.emit("TRIGGER_ARMED", symbol, level=float(level), side=side, qty=args.qty,
                sl_pct=sl_pct, auto=args.auto, direction=direction.value)
    mode = "AUTO-EXECUTE" if args.auto else "alert only"
    print(f"Armed: {symbol} {direction.value} when price goes {side} {level} "
          f"— qty {args.qty}, SL {sl_pct:.2%}, {mode}")
    if not _daemon_pid():
        print("NOTE: daemon is not running — start it so the WebSocket watches this.")
    else:
        print("Restart the daemon (`vigil stop && vigil start`) to subscribe to this symbol.")
    return 0


def cmd_triggers(args) -> int:
    all_t = triggers_mod.load()
    if not all_t:
        print("No triggers.")
        return 0
    print(f"{'SYMBOL':<12}{'DIR':<6}{'SIDE':<7}{'LEVEL':>10}{'QTY':>7}{'SL%':>7}  "
          f"{'AUTO':<6}{'STATUS':<10}DETAIL")
    for t in all_t:
        print(f"{t.symbol:<12}{t.direction:<6}{t.side:<7}{t.level:>10.2f}{t.qty:>7}"
              f"{t.sl_pct * 100:>6.2f}%  {'yes' if t.auto else 'no':<6}{t.status:<10}{t.detail}")
    return 0


def cmd_disarm(args) -> int:
    all_t = triggers_mod.load()
    n = 0
    for t in all_t:
        if t.status == triggers_mod.ARMED and (args.symbol is None
                                               or t.symbol == args.symbol.upper()):
            t.status = triggers_mod.CANCELLED
            t.detail = "disarmed manually"
            n += 1
    triggers_mod.save(all_t)
    print(f"Disarmed {n} trigger(s).")
    return 0


def cmd_arm_exit(args) -> int:
    """Arm an exit trigger on an existing (or expected) position, independent of its
    resting SL. Fires by cancelling the SL and market-exiting the moment the level
    breaks — no confirmation, no copy-paste, the daemon does it off the price feed."""
    symbol = args.symbol.upper()
    if (args.above is None) == (args.below is None):
        print("Specify exactly one of --above / --below", file=sys.stderr)
        return 2
    side = "above" if args.above is not None else "below"
    level = args.above if args.above is not None else args.below

    all_t = triggers_mod.load_exit_triggers()
    all_t.append(triggers_mod.ExitTrigger(
        symbol=symbol, level=float(level), side=side, note=args.note or "",
    ))
    triggers_mod.save_exit_triggers(all_t)
    print(f"Armed exit: {symbol} closes when price goes {side} {level} "
          f"— fires automatically, no confirmation.")
    if not _daemon_pid():
        print("NOTE: daemon is not running — start it so the price feed watches this.")
    else:
        print("Restart the daemon (`vigil stop && vigil start`) to subscribe to this symbol.")
    return 0


def cmd_exit_triggers(args) -> int:
    all_t = triggers_mod.load_exit_triggers()
    if not all_t:
        print("No exit triggers.")
        return 0
    print(f"{'SYMBOL':<12}{'SIDE':<7}{'LEVEL':>10}  {'STATUS':<10}DETAIL")
    for t in all_t:
        print(f"{t.symbol:<12}{t.side:<7}{t.level:>10.2f}  {t.status:<10}{t.detail}")
    return 0


def cmd_disarm_exit(args) -> int:
    all_t = triggers_mod.load_exit_triggers()
    n = 0
    for t in all_t:
        if t.status == triggers_mod.ARMED and (args.symbol is None
                                               or t.symbol == args.symbol.upper()):
            t.status = triggers_mod.CANCELLED
            t.detail = "disarmed manually"
            n += 1
    triggers_mod.save_exit_triggers(all_t)
    print(f"Disarmed {n} exit trigger(s).")
    return 0
