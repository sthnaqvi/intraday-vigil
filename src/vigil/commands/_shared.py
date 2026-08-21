"""Helpers used across more than one command module.

Kept separate from any single command file rather than duplicated, and separate from
cli.py itself so the argparse-wiring module doesn't accumulate broker/pidfile logic.
"""
from __future__ import annotations

import os

from .. import auth, config
from .. import state as state_mod
from ..broker import Broker
from ..events import EventLog
from ..execution import margin_rejection_hint  # noqa: F401 (re-exported for callers)
from ..guard import GuardedBroker
from ..models import Position
from ..paper_mode import is_paper_mode, set_paper_mode  # noqa: F401 (re-exported for callers)


def _live_broker(dry_run: bool = False,
                 paper: bool | None = None) -> tuple[GuardedBroker, EventLog]:
    """The broker every command actually talks to. `paper` defaults to whatever mode the
    session is already in (see is_paper_mode()) — only vigil start/monitor ever pass it
    explicitly, to enter or leave paper mode; everything else just follows along."""
    events = EventLog()
    use_paper = is_paper_mode() if paper is None else paper
    if use_paper:
        from .. import paper_adapter as paper_mod
        adapter = paper_mod.PaperAdapter.load_or_create(config.DATA_DIR / "paper_book.json")
        return GuardedBroker(adapter, events, dry_run=dry_run), events
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


def _open_position(broker: GuardedBroker, symbol: str) -> Position | None:
    return state_mod.open_mis_positions(broker.positions_day()).get(symbol)
