"""CLI: start | stop | login | monitor | status | positions | add-position | squareoff"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import traceback
from datetime import datetime

from . import audit, auth, clock, config, levels, rules, state as state_mod
from . import triggers as triggers_mod
from .broker import Broker
from .events import EventLog, setup_logging
from .monitor import MonitorLoop
from .rules import Direction
from .state import SessionState


def _live_broker(dry_run: bool = False) -> tuple[Broker, EventLog]:
    events = EventLog()
    kite = auth.get_kite()
    return Broker(kite, events, dry_run=dry_run), events


# ---------- daemon process management ----------

def _daemon_pid() -> int | None:
    """PID of a live daemon, or None. Cleans up a stale pidfile."""
    if not config.PID_FILE.exists():
        return None
    try:
        pid = int(config.PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        config.PID_FILE.unlink(missing_ok=True)
        return None


def cmd_start(args) -> int:
    """One command for the morning: login if needed, then run the daemon detached."""
    auth.login(paste=args.paste)  # fast-path returns instantly on a valid token
    if (pid := _daemon_pid()) is not None:
        print(f"Daemon already running (pid {pid}). `algo status` to inspect.")
        return 0
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "algo", "monitor"]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.force:
        cmd.append("--force")
    with (config.LOGS_DIR / "daemon.out").open("a") as out:
        proc = subprocess.Popen(cmd, cwd=config.PROJECT_ROOT, stdout=out, stderr=out,
                                start_new_session=True)
    config.PID_FILE.write_text(str(proc.pid))
    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"Daemon started ({mode}, pid {proc.pid}). It waits for the 09:15 bell if early,\n"
          f"squares off at 15:05, and exits on its own. `algo status` any time; "
          f"`algo stop` to halt.")
    return 0


def cmd_stop(args) -> int:
    pid = _daemon_pid()
    if pid is None:
        print("No daemon running.")
        return 0
    os.kill(pid, signal.SIGINT)
    for _ in range(20):
        time.sleep(0.25)
        if _daemon_pid() is None:
            break
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
    else:
        os.kill(pid, signal.SIGTERM)
    config.PID_FILE.unlink(missing_ok=True)
    print(f"Daemon (pid {pid}) stopped. Broker SL orders remain active at the exchange.")
    return 0


# ---------- commands ----------

def cmd_login(args) -> int:
    auth.login(paste=args.paste, force=args.force)
    return 0


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
            f"SL {sl['trigger_price']} x{sl['quantity']} ({sl['order_id']})"
            if sl else "NO SL ORDER!"
        )
        print(f"{symbol:<14}{direction.value:<6}qty {abs(row['quantity']):<6}"
              f"entry {entry:<10}{sl_desc}")
    return 0


def cmd_status(args) -> int:
    if not config.STATUS_FILE.exists():
        print("No status.json yet — start the daemon with: algo start")
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
                  f"{p.get('sl_order_status')}. `algo protect {p['symbol']}` or "
                  f"`algo exit {p['symbol']}` ***")

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


def cmd_monitor(args) -> int:
    broker, events = _live_broker(dry_run=args.dry_run)
    session = SessionState.load_or_create()
    loop = MonitorLoop(broker, events, session)
    try:
        config.PID_FILE.write_text(str(os.getpid()))
        loop.run(force=args.force)
    finally:
        if _pidfile_is_mine():
            config.PID_FILE.unlink(missing_ok=True)
    return 0


def _pidfile_is_mine() -> bool:
    try:
        return int(config.PID_FILE.read_text().strip()) == os.getpid()
    except (OSError, ValueError):
        return False


# ---------- entries & armed triggers (no MCP dependency) ----------

def _as_fraction(sl_pct: float) -> float:
    """Accept 1.0 as 1% and 0.01 as 1% — same convention as add-position."""
    return sl_pct if sl_pct <= 0.2 else sl_pct / 100.0


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
        note="manual algo enter",
    )
    ltp = 0.0
    try:
        q = broker.quotes([f"NSE:{t.symbol}"])
        ltp = float(q[f"NSE:{t.symbol}"]["last_price"])
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
        print("Restart the daemon (`algo stop && algo start`) to subscribe to this symbol.")
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


def _open_position(broker: Broker, symbol: str) -> dict | None:
    return state_mod.open_mis_positions(broker.positions_day()).get(symbol)


def cmd_add(args) -> int:
    """Scale into an OPEN position and keep the risk seed honest.

    Adding shares moves the entry VWAP, which changes the effective sl_pct against the
    resting SL. Doing this by hand on 2026-08-18 left the seed stale and the daemon
    reported a profitable position as a loss. This command recomputes and rewrites it.
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
        print(f"No open MIS position in {symbol}. Use `algo enter` to open one.",
              file=sys.stderr)
        return 3

    direction = state_mod.position_direction(pos)
    old_qty = abs(pos["quantity"])
    old_entry = state_mod.position_entry_price(pos)

    sl_order = state_mod.find_sl_order(broker.orders(), symbol, direction)
    if sl_order is None:
        print(f"{symbol} has NO resting SL — refusing to add to an unprotected position.",
              file=sys.stderr)
        return 3
    sl_price = sl_order.get("trigger_price") or 0.0

    if not args.yes:
        print(f"{symbol} {direction.value}: {old_qty} @ {old_entry:.2f}, SL {sl_price} "
              f"-> adding {args.qty}")
        if input("Proceed? [y/N] ").strip().lower() != "y":
            print("Aborted.")
            return 1

    broker.place_entry(symbol, direction, args.qty)

    time.sleep(2)
    pos = _open_position(broker, symbol) or pos
    new_qty = abs(pos["quantity"])
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
    qty = abs(pos["quantity"])

    if not args.yes:
        print(f"Exit {symbol} {direction.value} {qty} at market (cancels its SL first)?")
        if input("[y/N] ").strip().lower() != "y":
            print("Aborted.")
            return 1

    sl_order = state_mod.find_sl_order(broker.orders(), symbol, direction)
    if sl_order is not None:
        try:
            broker.cancel(sl_order["order_id"])
        except Exception as e:
            events.emit("WARNING", symbol, message=f"SL cancel failed before exit: {e}")
            print(f"WARNING: could not cancel the SL ({e}) — it may fill alongside the exit.",
                  file=sys.stderr)
    broker.place_market_exit(symbol, direction, qty)
    events.emit("MANUAL_EXIT", symbol, qty=qty, direction=direction.value)
    time.sleep(2)
    still = _open_position(broker, symbol)
    if still is None:
        print(f"{symbol}: exit filled — position FLAT.")
    else:
        print(f"{symbol}: exit sent, still showing {abs(still['quantity'])} open. "
              "Re-check with `algo positions`.")
    return 0


def cmd_web(args) -> int:
    from . import webui
    webui.serve(host=args.host, port=args.port)
    return 0


def cmd_ask(args) -> int:
    """Bridge to Claude: enqueue a question, list pending ones, or write an answer back."""
    from . import claudelink

    if args.pending:
        rows = claudelink.pending()
        if not rows:
            print("No pending questions.")
            return 0
        for r in rows:
            print(f"--- {r['id']}  {r['ts'][11:19]}")
            print(f"Q: {r['question']}")
            if r.get("context", {}).get("positions"):
                print(f"   context: {json.dumps(r['context']['positions'])[:200]}")
        print(f"\nAnswer with: algo ask --answer <id> --text \"...\"")
        return 0

    if args.answer:
        if not args.text:
            print("--answer needs --text", file=sys.stderr)
            return 2
        ok = claudelink.answer(args.answer, args.text)
        print("Answer saved." if ok else f"No request with id {args.answer}", )
        return 0 if ok else 3

    if not args.question:
        print("Give a question, or use --pending / --answer.", file=sys.stderr)
        return 2

    ctx = {}
    if config.STATUS_FILE.exists():
        try:
            snap = json.loads(config.STATUS_FILE.read_text())
            ctx = {"positions": snap.get("positions", []),
                   "realized_pnl_today": snap.get("realized_pnl_today")}
        except Exception:
            pass
    req = claudelink.enqueue(" ".join(args.question), context=ctx)
    if req["status"] == claudelink.ANSWERED:
        print(req["answer"])
    else:
        print(f"Queued as {req['id']} — no claude CLI on this machine, so a Claude session "
              f"must pick it up with `algo ask --pending`.")
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
    qty = abs(pos["quantity"])
    entry = state_mod.position_entry_price(pos)

    existing = state_mod.find_sl_order(broker.orders(), symbol, direction)
    if existing is not None and not args.force:
        print(f"{symbol} already has a resting SL (order {existing['order_id']}, "
              f"trigger {existing['trigger_price']}, qty {existing['quantity']}). "
              "Use --force to place another anyway.", file=sys.stderr)
        return 3

    tp = session.positions.get(symbol)
    seed = state_mod.load_risk_seeds().get(symbol, {})
    sl_pct = args.sl_pct and _as_fraction(args.sl_pct)
    if sl_pct is None:
        sl_pct = float(seed["sl_pct"]) if "sl_pct" in seed else (tp.sl_pct if tp else None)
    if sl_pct is None:
        print(f"No sl_pct for {symbol} — pass --sl-pct or seed it with `algo add-position`.",
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

    new_id = broker.place_sl(symbol, direction, trigger, qty)
    if tp is not None:
        tp.sl_order_id, tp.sl_price = new_id, trigger   # same trade: keep phase/breakeven
        session.save()
    events.emit("SL_REPLACED", symbol, new_order_id=new_id, trigger=trigger, quantity=qty,
                note="manual algo protect")
    print(f"{symbol}: SL-M placed, order {new_id}, trigger {trigger}, qty {qty}")
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
        o = v["ohlc"]
        frm_open = (v["last_price"] - o["open"]) / o["open"] * 100 if o["open"] else 0
        frm_close = (v["last_price"] - o["close"]) / o["close"] * 100 if o["close"] else 0
        print(f"  {s:<18} ltp {v['last_price']:>10.2f}  {frm_open:>+6.2f}%open "
              f"{frm_close:>+6.2f}%prev  O {o['open']:>9.2f} H {o['high']:>9.2f} "
              f"L {o['low']:>9.2f} C {o['close']:>9.2f}")
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


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    p = argparse.ArgumentParser(prog="algo", description="Intraday SL-lifecycle daemon (Zerodha Kite)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start", help="morning one-shot: login if needed + run daemon in background")
    sp.add_argument("--dry-run", action="store_true", help="log intents, mutate nothing")
    sp.add_argument("--force", action="store_true", help="run outside market hours")
    sp.add_argument("--paste", action="store_true", help="paste-URL login fallback")
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("stop", help="stop the background daemon (broker SLs stay active)")
    sp.set_defaults(fn=cmd_stop)

    sp = sub.add_parser("login", help="daily Kite login (skips browser if token still valid)")
    sp.add_argument("--paste", action="store_true", help="paste redirected URL instead of local listener")
    sp.add_argument("--force", action="store_true", help="re-login even if token looks valid")
    sp.set_defaults(fn=cmd_login)

    sp = sub.add_parser("positions", help="live MIS positions + their SL orders")
    sp.set_defaults(fn=cmd_positions)

    sp = sub.add_parser("status", help="session dashboard (--json for the raw snapshot)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("add-position", help="seed sl_pct (and pdh/pdl) for a symbol")
    sp.add_argument("symbol")
    sp.add_argument("--sl-pct", type=float, required=True,
                    help="SL width, e.g. 1.0 for 1%% (values <= 0.2 read as fractions)")
    sp.add_argument("--pdh", type=float, default=None)
    sp.add_argument("--pdl", type=float, default=None)
    sp.set_defaults(fn=cmd_add_position)

    sp = sub.add_parser("monitor", help="run the SL-lifecycle loop in the foreground")
    sp.add_argument("--dry-run", action="store_true", help="log intents, mutate nothing")
    sp.add_argument("--force", action="store_true", help="run outside market hours")
    sp.set_defaults(fn=cmd_monitor)

    sp = sub.add_parser("squareoff", help="cancel SLs and market-exit all MIS now")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--yes", action="store_true", help="skip confirmation")
    sp.set_defaults(fn=cmd_squareoff)

    sp = sub.add_parser("enter", help="open a MIS position + SL now (no MCP needed)")
    sp.add_argument("symbol")
    sp.add_argument("--side", required=True, choices=["long", "short", "LONG", "SHORT"])
    sp.add_argument("--qty", type=int, required=True)
    sp.add_argument("--sl-pct", type=float, required=True,
                    help="SL width, e.g. 1.0 for 1%% (values <= 0.2 read as fractions)")
    sp.add_argument("--pdh", type=float, default=None)
    sp.add_argument("--pdl", type=float, default=None)
    sp.add_argument("--yes", action="store_true", help="skip confirmation")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--override-gate", action="store_true",
                    help="bypass the kill-switch / 14:30 entry gate (you must mean it)")
    sp.set_defaults(fn=cmd_enter)

    sp = sub.add_parser("arm", help="arm a price trigger watched over the tick WebSocket")
    sp.add_argument("symbol")
    sp.add_argument("--side", required=True, choices=["long", "short", "LONG", "SHORT"])
    sp.add_argument("--above", type=float, default=None, help="fire when price breaks above")
    sp.add_argument("--below", type=float, default=None, help="fire when price breaks below")
    sp.add_argument("--qty", type=int, required=True)
    sp.add_argument("--sl-pct", type=float, required=True)
    sp.add_argument("--pdh", type=float, default=None)
    sp.add_argument("--pdl", type=float, default=None)
    sp.add_argument("--auto", action="store_true",
                    help="place the order automatically on the break (default: alert only)")
    sp.add_argument("--note", default="")
    sp.set_defaults(fn=cmd_arm)

    sp = sub.add_parser("add", help="scale into an open position (rewrites the risk seed)")
    sp.add_argument("symbol")
    sp.add_argument("--qty", type=int, required=True)
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--override-gate", action="store_true")
    sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("exit", help="exit ONE symbol (cancel its SL, then market-exit)")
    sp.add_argument("symbol")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_exit)

    sp = sub.add_parser("web", help="local dashboard (read-only; cannot place orders)")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(fn=cmd_web)

    sp = sub.add_parser("ask", help="ask Claude (runs the CLI if present, else queues)")
    sp.add_argument("question", nargs="*")
    sp.add_argument("--pending", action="store_true", help="list unanswered questions")
    sp.add_argument("--answer", metavar="ID", help="write an answer back to a question")
    sp.add_argument("--text", help="the answer text (with --answer)")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("protect", help="re-place a missing SL on an open position")
    sp.add_argument("symbol")
    sp.add_argument("--sl-pct", type=float, default=None,
                    help="override; defaults to the risk.json seed / tracked value")
    sp.add_argument("--trigger", type=float, default=None, help="explicit trigger price")
    sp.add_argument("--force", action="store_true", help="place even if an SL already rests")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_protect)

    sp = sub.add_parser("quote", help="LTP + OHLC without the MCP session")
    sp.add_argument("symbols", nargs="+")
    sp.set_defaults(fn=cmd_quote)

    sp = sub.add_parser("triggers", help="list armed / fired triggers")
    sp.set_defaults(fn=cmd_triggers)

    sp = sub.add_parser("disarm", help="cancel armed triggers (all, or one symbol)")
    sp.add_argument("symbol", nargs="?", default=None)
    sp.set_defaults(fn=cmd_disarm)

    args = p.parse_args(argv)
    # Every invocation is recorded, whoever started it. When the dashboard spawned this
    # process it passed ALGO_TRACE_ID, so the click and this run share one trace.
    raw = argv if argv is not None else sys.argv[1:]
    audit.action("cli.invoke", cmd=args.cmd, argv=list(raw))
    started = time.monotonic()
    code = 2
    try:
        code = args.fn(args)
        return code
    except auth.AuthError as e:
        print(f"auth: {e}", file=sys.stderr)
        code = 2
        return code
    except Exception as e:
        audit.action("cli.crashed", cmd=args.cmd, error=repr(e),
                     tb=traceback.format_exc()[-2000:])
        raise
    finally:
        audit.action("cli.finished", cmd=args.cmd, code=code,
                     ms=round((time.monotonic() - started) * 1000, 1))


if __name__ == "__main__":
    raise SystemExit(main())
