"""Read-only views: positions, status.json, quotes."""
from __future__ import annotations

import json
from datetime import datetime

from .. import clock, config, state as state_mod
from .. import triggers as triggers_mod
from ._shared import _daemon_pid, _live_broker


def cmd_positions(args) -> int:
    broker, _ = _live_broker()
    rows = broker.positions_day()
    orders = broker.orders()
    open_pos = state_mod.open_mis_positions(rows)
    if not open_pos:
        print("No open MIS positions.")
        return 0
    for symbol, row in open_pos.items():
        direction = state_mod.position_direction(row)
        entry = state_mod.position_entry_price(row)
        sl = state_mod.find_sl_order(orders, symbol, direction)
        sl_desc = (
            f"SL {sl.trigger_price} x{sl.quantity} ({sl.order_id})"
            if sl else "NO SL ORDER!"
        )
        print(f"{symbol:<14}{direction.value:<6}qty {abs(row.quantity):<6}"
              f"entry {entry:<10}{sl_desc}")
    return 0


def cmd_status(args) -> int:
    if not config.STATUS_FILE.exists():
        print("No status.json yet — start the daemon with: vigil start")
        return 1
    raw = config.STATUS_FILE.read_text()
    if args.json:
        print(raw)
        return 0
    snap = json.loads(raw)
    as_of = datetime.fromisoformat(snap["as_of"])
    age = (clock.now_ist() - as_of).total_seconds()
    cycle_s = snap["daemon"].get("cycle_seconds", 150)
    pid = _daemon_pid()
    fresh = age < 2 * cycle_s

    running = f"running (pid {pid})" if pid else "NOT RUNNING"
    freshness = f"{int(age)}s ago" + ("" if fresh else "  ** STALE **")
    print(f"Daemon:  {running} | mode {snap['daemon']['mode']} | snapshot {freshness}")
    flags = []
    if snap.get("kill_switch"):
        flags.append("KILL-SWITCH ACTIVE")
    if snap.get("no_new_entries"):
        flags.append(f"no new entries ({snap.get('no_new_entries_reason')})")
    if flags:
        print("Flags:   " + " | ".join(flags))

    naked = [p for p in snap.get("positions", []) if p.get("protected") is False]
    if naked:
        for p in naked:
            print(f"*** UNPROTECTED: {p['symbol']} {p['qty']} {p['direction']} — SL order "
                  f"{p.get('sl_order_status')}. `vigil protect {p['symbol']}` or "
                  f"`vigil exit {p['symbol']}` ***")

    live = triggers_mod.armed(triggers_mod.load())
    if live:
        print("Armed:   " + " | ".join(
            f"{t.symbol} {t.side} {t.level} x{t.qty}{' AUTO' if t.auto else ''}" for t in live))

    print()
    if snap["positions"]:
        print(f"{'SYMBOL':<12}{'DIR':<6}{'QTY':>5}{'ENTRY':>10}{'LTP':>10}"
              f"{'R':>7}{'P&L':>11}{'PH':>4}{'SL':>10}")
        for v in snap["positions"]:
            near = " <SL" if v.get("near_sl") else ""
            # Never print a remembered SL price for an order that is no longer resting.
            sl_col = (f"{v['sl_price']:>10.2f}" if v.get("protected", True)
                      else f"{'NO STOP':>10}")
            print(f"{v['symbol']:<12}{v['direction']:<6}{v['qty']:>5}{v['entry']:>10.2f}"
                  f"{v['ltp']:>10.2f}{v['profit_r']:>+7.2f}"
                  f"{v.get('unrealized_pnl', 0):>+11.2f}{v['phase']:>4}"
                  f"{sl_col}{near}")
    else:
        print("No open positions.")

    if snap.get("closed_today"):
        print("\nClosed today:")
        for c in snap["closed_today"]:
            print(f"  {c['symbol']:<12}{c['exit_reason']:<12}exit {c['exit_price']:<10}"
                  f"{c['realized_r']:+.2f}R  Rs {c['realized_pnl']:+.2f}")
    print(f"\nDay realised: Rs {snap['realized_pnl_today']:+.2f} "
          f"({snap['realized_r_today']:+.2f}R)")
    return 0


def cmd_paths(args) -> int:
    """Where this install keeps its state — the skill and any tooling should resolve
    paths through this instead of hardcoding a filesystem location, since VIGIL_HOME/
    XDG_STATE_HOME make that location a per-install choice, not a constant."""
    paths = {
        "state_dir": str(config.STATE_DIR),
        "data_dir": str(config.DATA_DIR),
        "logs_dir": str(config.LOGS_DIR),
        "status_file": str(config.STATUS_FILE),
        "risk_file": str(config.RISK_FILE),
        "events_glob": str(config.DATA_DIR / "events-*.jsonl"),
    }
    if getattr(args, "json", False):
        print(json.dumps(paths, indent=2))
    else:
        for k, v in paths.items():
            print(f"{k:<12} {v}")
    return 0


def cmd_quote(args) -> int:
    """LTP + OHLC on the daemon's token, so quotes never need the MCP session."""
    broker, _ = _live_broker()
    syms = [s if ":" in s else f"NSE:{s.upper()}" for s in args.symbols]
    q = broker.quotes(syms)
    for s in syms:
        v = q.get(s)
        if not v:
            print(f"  {s:<18} (no data)")
            continue
        o = v.ohlc
        frm_open = (v.last_price - o.open) / o.open * 100 if o.open else 0
        frm_close = (v.last_price - o.close) / o.close * 100 if o.close else 0
        print(f"  {s:<18} ltp {v.last_price:>10.2f}  {frm_open:>+6.2f}%open "
              f"{frm_close:>+6.2f}%prev  O {o.open:>9.2f} H {o.high:>9.2f} "
              f"L {o.low:>9.2f} C {o.close:>9.2f}")
    return 0
