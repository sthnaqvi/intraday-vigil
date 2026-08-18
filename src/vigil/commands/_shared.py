"""Helpers used across more than one command module.

Kept separate from any single command file rather than duplicated, and separate from
cli.py itself so the argparse-wiring module doesn't accumulate broker/pidfile logic.
"""
from __future__ import annotations

import os

from .. import auth, config, state as state_mod
from ..broker import Broker
from ..events import EventLog


def _live_broker(dry_run: bool = False) -> tuple[Broker, EventLog]:
    events = EventLog()
    kite = auth.get_kite()
    return Broker(kite, events, dry_run=dry_run), events


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


def _pidfile_is_mine() -> bool:
    try:
        return int(config.PID_FILE.read_text().strip()) == os.getpid()
    except (OSError, ValueError):
        return False


def _as_fraction(sl_pct: float) -> float:
    """Accept 1.0 as 1% and 0.01 as 1% — same convention as add-position."""
    return sl_pct if sl_pct <= 0.2 else sl_pct / 100.0


def _open_position(broker: Broker, symbol: str) -> dict | None:
    return state_mod.open_mis_positions(broker.positions_day()).get(symbol)
