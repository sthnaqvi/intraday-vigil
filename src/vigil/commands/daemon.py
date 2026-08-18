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
from ._shared import _daemon_pid, _live_broker, _pidfile_is_mine


def cmd_start(args) -> int:
    """One command for the morning: login if needed, then run the daemon detached."""
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

    mode = "DRY RUN" if args.dry_run else "LIVE"
    print(f"Daemon started ({mode}, pid {proc.pid}). It waits for the "
          f"{config.MARKET_OPEN.strftime('%H:%M')} bell if early,\n"
          f"squares off at {config.SQUAREOFF_AT.strftime('%H:%M')}, and exits on its own. "
          f"`vigil status` any time; `vigil stop` to halt.")
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


def cmd_login(args) -> int:
    auth.login(paste=args.paste, force=args.force)
    return 0


def cmd_monitor(args) -> int:
    # A daemon that manages real money must be able to reach the user. dry-run mode
    # places no orders, so it's exempt — but live mode with no working notifier means
    # SL hits, unprotected positions, and token expiry all happen invisibly.
    if not args.dry_run and not args.allow_silent and not notify.can_notify():
        print("REFUSED — no desktop notifier available (checked: macOS osascript, "
              "Linux notify-send). A live daemon with no notifier gives you zero alerts "
              "for SL hits, unprotected positions, or token expiry.\n"
              "Fix: install a notifier, or run with --allow-silent to accept running "
              "silent (you'll need to watch `vigil status` / the dashboard yourself).",
              file=sys.stderr)
        return 3
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
