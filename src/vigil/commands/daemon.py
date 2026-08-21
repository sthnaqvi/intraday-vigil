"""Daemon lifecycle: start, stop, login, and the monitor loop itself."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time

from .. import auth, config, notify
from ..monitor import MonitorLoop
from ..state import SessionState
from ._shared import _daemon_pid, _live_broker, _pidfile_is_mine, is_paper_mode, set_paper_mode


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
    if proc.poll() is not None and proc.returncode != 0:
        tail = "\n".join(daemon_out.read_text().splitlines()[-6:]) if daemon_out.exists() else ""
        config.PID_FILE.unlink(missing_ok=True)
        print(f"Daemon exited immediately (code {proc.returncode}):\n{tail}", file=sys.stderr)
        return proc.returncode

    mode = "DRY RUN" if args.dry_run else ("PAPER" if paper else "LIVE")
    print(f"Daemon started ({mode}, pid {proc.pid}). It waits for the "
          f"{config.MARKET_OPEN.strftime('%H:%M')} bell if early,\n"
          f"squares off at {config.SQUAREOFF_AT.strftime('%H:%M')}, and exits on its own. "
          f"`vigil status` any time; `vigil stop` to halt.")
    if paper:
        print("\nPaper mode — no real broker, no real money. Next: place a simulated "
              "trade with `vigil enter`, or open the dashboard with `vigil web` to watch "
              "it. See docs/quickstart.md.")
    else:
        print("\nNext: place a trade with `vigil enter` (or the Claude skill), or open "
              "the dashboard with `vigil web` to watch the session. See docs/usage.md.")
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
