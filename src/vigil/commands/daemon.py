"""Daemon lifecycle: start, stop, login, and the monitor loop itself."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

from .. import auth, clock, config, notify
from ..monitor import MonitorLoop
from ..state import SessionState
from ._shared import _daemon_pid, _live_broker, _pidfile_is_mine, is_paper_mode, set_paper_mode


def _startup_message(mode: str, pid: int, now: datetime, force: bool) -> str:
    """What the daemon will actually do next, given the real time right now — not a
    canned line that only happens to be true if you start it before the market opens.
    A fixed "waits for the 09:15 bell" message printed at noon is exactly backwards: the
    daemon isn't waiting for anything at that point, it's already managing positions.
    """
    header = f"Daemon started ({mode}, pid {pid})."
    tail = "`vigil status` any time; `vigil stop` to halt."
    open_s = config.MARKET_OPEN.strftime("%H:%M")
    squareoff_s = config.SQUAREOFF_AT.strftime("%H:%M")
    holidays = clock.load_holidays()

    if not clock.is_market_day(now.date(), holidays):
        forced = " (--force: running on a non-trading day)" if force else ""
        return f"{header}{forced} Today isn't a trading day. {tail}"
    if now.time() < config.MARKET_OPEN:
        open_dt = datetime.combine(now.date(), config.MARKET_OPEN, tzinfo=now.tzinfo)
        mins = max(0, int((open_dt - now).total_seconds() // 60))
        return (f"{header} Waiting for the {open_s} bell (about {mins}m). Squares off at "
                f"{squareoff_s} and exits on its own. {tail}")
    if now.time() < config.SQUAREOFF_AT:
        return (f"{header} Market is open — already managing positions live. Squares off "
                f"at {squareoff_s} and exits on its own. {tail}")
    if now.time() <= config.MARKET_CLOSE:
        return (f"{header} Past today's {squareoff_s} squareoff time — any open MIS "
                f"position gets squared off almost immediately, then it exits. {tail}")
    forced = " (--force: running after today's close)" if force else ""
    return f"{header}{forced} Outside today's session hours. {tail}"


def cmd_start(args) -> int:
    """One command for the morning: login if needed (skipped in paper mode — there's no
    broker to log into), then run the daemon detached."""
    paper = args.paper
    set_paper_mode(paper)
    if not paper:
        auth.login(paste=args.paste)  # fast-path returns instantly on a valid token
    if (pid := _daemon_pid()) is not None:
        print(f"Daemon already running (pid {pid}). `vigil status` to inspect.")
        return 0
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, "-m", "vigil", "monitor"]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.force:
        cmd.append("--force")
    if args.allow_silent:
        cmd.append("--allow-silent")
    if paper:
        cmd.append("--paper")
    daemon_out = config.LOGS_DIR / "daemon.out"
    with daemon_out.open("a") as out:
        # No cwd pin: with the package installed (editable or otherwise), `-m vigil`
        # resolves from any working directory — every path this process touches comes
        # from config.* (absolute, resolved via VIGIL_HOME/XDG), never a relative one.
        proc = subprocess.Popen(cmd, stdout=out, stderr=out, start_new_session=True)
    config.PID_FILE.write_text(str(proc.pid))

    # The notifier check (and the market-hours check) both happen before any network
    # I/O, so a refusal exits almost instantly. Without this grace check the user sees
    # "Daemon started" and only discovers the refusal by tailing daemon.out.
    time.sleep(0.5)
    if proc.poll() is not None:
        tail = "\n".join(daemon_out.read_text().splitlines()[-6:]) if daemon_out.exists() else ""
        config.PID_FILE.unlink(missing_ok=True)
        if proc.returncode != 0:
            print(f"Daemon exited immediately (code {proc.returncode}):\n{tail}",
                  file=sys.stderr)
            return proc.returncode
        # A CLEAN exit this fast, with no crash, is almost always "market is closed and
        # --force wasn't passed" (see monitor.py's _run_loop). Printing the generic
        # "Daemon started... squares off at 15:00" success message here would be false —
        # there is no daemon anymore by the time that line hits the screen.
        print(f"Daemon exited immediately (market closed, no --force):\n{tail}",
              file=sys.stderr)
        return 0

    mode = "DRY RUN" if args.dry_run else ("PAPER" if paper else "LIVE")
    print(_startup_message(mode, proc.pid, clock.now_ist(), args.force))
    if paper:
        print("\nPaper mode — no real broker, no real money. Next: place a simulated "
              "trade with `vigil enter`, or open the dashboard with `vigil web` to watch "
              "it. See docs/quickstart.md.")
    else:
        print("\nNext: place a trade with `vigil enter` (or the Claude skill), or open "
              "the dashboard with `vigil web` to watch the session. See docs/usage.md.")
    return 0


def _squareoff_window_block(now: datetime) -> str | None:
    """None if `vigil stop` is fine to run right now; otherwise the refusal message.
    Only blocks when there are open tracked positions — an idle daemon has nothing to
    hand off to the broker's forced closure in the first place."""
    window_start = (datetime.combine(now.date(), config.SQUAREOFF_AT)
                    - timedelta(minutes=config.STOP_REFUSAL_LEAD_MIN)).time()
    if not (window_start <= now.time() < config.BROKER_SQUAREOFF_AT):
        return None
    session = SessionState.load_or_create()
    if not session.positions:
        return None
    syms = ", ".join(session.positions)
    return (
        f"{now.strftime('%H:%M:%S')} is inside the pre-squareoff window "
        f"({window_start.strftime('%H:%M')}-{config.BROKER_SQUAREOFF_AT.strftime('%H:%M')}) "
        f"with open positions ({syms}). Stopping now hands the exit to the broker's own "
        f"forced closure instead of the daemon's controlled one — this has cost real money "
        f"before (docs/incidents/discipline-and-process.md). Either let it run to "
        f"{config.SQUAREOFF_AT.strftime('%H:%M')}, place a manual exit first "
        f"(`vigil exit SYMBOL` or `vigil squareoff`), or pass --i-know to stop anyway."
    )


def cmd_stop(args) -> int:
    pid = _daemon_pid()
    if pid is None:
        print("No daemon running.")
        return 0

    if not getattr(args, "i_know", False) and not is_paper_mode():
        blocked = _squareoff_window_block(clock.now_ist())
        if blocked:
            print(f"REFUSED — {blocked}", file=sys.stderr)
            return 3

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
    where = "in the paper book" if is_paper_mode() else "at the exchange"
    print(f"Daemon (pid {pid}) stopped. Resting stop orders remain active {where}.")
    return 0


def cmd_restart(args) -> int:
    """`vigil stop` (if a daemon is running) then `vigil start` — the same two commands,
    in the order that actually matters, done for you instead of left to memory.

    Nothing tracked is at risk either way: every resting SL lives at the broker, not in
    this process, so stopping never removes protection (see docs/safety.md); and today's
    tracked positions, phases, and realised P&L ledger are written to
    data/session-<date>.json every cycle and reloaded by SessionState.load_or_create() on
    the way back up. `cmd_stop` also already blocks until the old process has actually
    exited (or force-kills it after 5s) before returning, so this can't race a fresh
    `monitor` loop starting while the old one is still shutting down.
    """
    if _daemon_pid() is not None:
        cmd_stop(args)
    return cmd_start(args)


def cmd_login(args) -> int:
    set_paper_mode(False)  # explicit live-Kite intent ends any paper session in progress
    auth.login(paste=args.paste, force=args.force)
    return 0


def cmd_monitor(args) -> int:
    paper = args.paper
    if paper:
        set_paper_mode(True)
    # A daemon that manages real money must be able to reach the user. dry-run and paper
    # mode place no REAL orders, so both are exempt — but a live daemon with no working
    # notifier means SL hits, unprotected positions, and token expiry all happen invisibly.
    if not paper and not args.dry_run and not args.allow_silent and not notify.can_notify():
        print("REFUSED — no desktop notifier available (checked: macOS osascript, "
              "Linux notify-send). A live daemon with no notifier gives you zero alerts "
              "for SL hits, unprotected positions, or token expiry.\n"
              "Fix: install a notifier, or run with --allow-silent to accept running "
              "silent (you'll need to watch `vigil status` / the dashboard yourself).",
              file=sys.stderr)
        return 3
    broker, events = _live_broker(dry_run=args.dry_run, paper=paper)
    session = SessionState.load_or_create()
    loop = MonitorLoop(broker, events, session)
    try:
        config.PID_FILE.write_text(str(os.getpid()))
        loop.run(force=args.force)
    finally:
        if _pidfile_is_mine():
            config.PID_FILE.unlink(missing_ok=True)
    return 0
