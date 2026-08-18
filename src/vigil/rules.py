"""Pure decision functions. No I/O, no API objects — everything here is unit-testable.

All SL money-math lives in this module; monitor.py orchestrates, broker.py executes.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from . import config


class Direction(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"


@dataclass(frozen=True)
class ModifyIntent:
    order_id: str
    trigger_price: float
    quantity: int
    reason: str
    guard_applied: bool = False


def round_to_tick_favor(price: float, direction: Direction, tick: float = config.NSE_TICK) -> float:
    """Round to exchange tick, always in the trade's favour (away from price):
    long SL rounds down, short SL rounds up."""
    ticks = price / tick
    n = math.floor(ticks + 1e-9) if direction == Direction.LONG else math.ceil(ticks - 1e-9)
    return round(n * tick, 2)


def profit_r(ltp: float, entry: float, r: float, direction: Direction) -> float:
    if direction == Direction.LONG:
        return (ltp - entry) / r
    return (entry - ltp) / r


def target_phase(pr: float) -> int:
    if pr < config.PHASE2_R:
        return 1
    if pr < config.PHASE3_R:
        return 2
    return 3


def apply_stop_hunt_guard(
    sl: float,
    levels: list[float],
    direction: Direction,
    buffer: float = config.STOP_HUNT_BUFFER,
    max_iter: int = 10,
) -> tuple[float, bool]:
    """If sl lands within `buffer` of any key level (PDH/PDL/day-H/L), push it
    `buffer` beyond that level in the trade's favour. Iterates in case the push
    lands on another level. Returns (adjusted_sl, guard_applied)."""
    applied = False
    for _ in range(max_iter):
        # tiny epsilon so a push of exactly `buffer` isn't re-flagged by fp error
        offending = [lv for lv in levels if lv > 0 and abs(sl - lv) / lv < buffer - 1e-9]
        if not offending:
            break
        applied = True
        if direction == Direction.LONG:
            # push below the lowest offending level
            sl = min(offending) * (1 - buffer)
        else:
            sl = max(offending) * (1 + buffer)
    return sl, applied


def _is_better(new_sl: float, current_sl: float, direction: Direction) -> bool:
    return new_sl > current_sl if direction == Direction.LONG else new_sl < current_sl


def breakeven_decision(
    entry: float,
    current_sl: float,
    direction: Direction,
    levels: list[float],
    order_id: str,
    quantity: int,
) -> ModifyIntent | None:
    """Phase 2 one-shot: SL to entry (stop-hunt guarded, tick-rounded).
    Caller gates this on `not breakeven_done`."""
    guarded, applied = apply_stop_hunt_guard(entry, levels, direction)
    new_sl = round_to_tick_favor(guarded, direction)
    if not _is_better(new_sl, current_sl, direction):
        return None
    return ModifyIntent(order_id, new_sl, quantity, "breakeven_+1R", applied)


def trail_decision(
    ltp: float,
    trail_pct: float,
    current_sl: float,
    direction: Direction,
    levels: list[float],
    order_id: str,
    quantity: int,
    min_move: float = config.TRAIL_MIN_MOVE,
    require_min_move: bool = True,
) -> ModifyIntent | None:
    """Phase 3 mechanical trail at trail_pct (= 2 x sl_pct) from LTP.
    Ratchet only: modify iff better than current SL AND moved > min_move.

    require_min_move=False for the FIRST trail after breakeven: the anti-spam
    threshold exists to stop churn between successive trails, and with canonical
    DRREDDY numbers (BE 1271 -> trail 1277.20, a 0.49% move) it would otherwise
    block the very activation the rule was written to guarantee."""
    if direction == Direction.LONG:
        raw = ltp * (1 - trail_pct)
    else:
        raw = ltp * (1 + trail_pct)
    guarded, applied = apply_stop_hunt_guard(raw, levels, direction)
    new_sl = round_to_tick_favor(guarded, direction)
    if not _is_better(new_sl, current_sl, direction):
        return None
    if require_min_move and abs(new_sl - current_sl) / current_sl <= min_move:
        return None
    return ModifyIntent(order_id, new_sl, quantity, "trail_2x_slpct", applied)


def initial_sl_price(entry: float, sl_pct: float, direction: Direction, levels: list[float]) -> float:
    """Fresh SL placement (discovery of an unprotected position, or reject->replace)."""
    raw = entry * (1 - sl_pct) if direction == Direction.LONG else entry * (1 + sl_pct)
    guarded, _ = apply_stop_hunt_guard(raw, levels, direction)
    return round_to_tick_favor(guarded, direction)


def near_sl(ltp: float, trigger: float, threshold: float = config.NEAR_SL_PCT) -> bool:
    return trigger > 0 and abs(ltp - trigger) / trigger < threshold


def cycle_interval(any_near_sl: bool) -> int:
    return config.CYCLE_NEAR_SL_S if any_near_sl else config.CYCLE_NORMAL_S


def realized_r(entry: float, exit_price: float, r: float, direction: Direction) -> float:
    return profit_r(exit_price, entry, r, direction)


def due_time_actions(now: datetime, fired: set[str]) -> list[str]:
    """Returns keys of one-shot time actions now due: the three alerts + 'squareoff'."""
    due = [
        key
        for key, (t, _msg) in config.TIME_ALERTS.items()
        if now.time() >= t and key not in fired
    ]
    if now.time() >= config.SQUAREOFF_AT and "squareoff" not in fired:
        due.append("squareoff")
    return due


def seconds_until_next_action(now: datetime, fired: set[str]) -> int | None:
    """Seconds until the next not-yet-fired time alert or squareoff, or None if nothing
    remains today. The run loop clamps its sleep to this: a flat 150s cycle interval that
    ignores the clock can sleep straight past squareoff if a cycle happens to finish just
    before it, eating into the head start SQUAREOFF_AT is supposed to have on the broker's
    own force-square."""
    candidates = [t for key, (t, _msg) in config.TIME_ALERTS.items() if key not in fired]
    if "squareoff" not in fired:
        candidates.append(config.SQUAREOFF_AT)
    if not candidates:
        return None
    today = now.date()
    future = [
        (datetime.combine(today, t, tzinfo=now.tzinfo) - now).total_seconds()
        for t in candidates
    ]
    future = [d for d in future if d > 0]
    return int(min(future)) if future else None


def no_new_entries(now: datetime, kill_switch: bool) -> tuple[bool, str | None]:
    if kill_switch:
        return True, f"kill_switch: daily realised R <= {config.KILL_SWITCH_R}"
    if now.time() >= config.NO_NEW_ENTRIES_AFTER:
        return True, f"past {config.NO_NEW_ENTRIES_AFTER.strftime('%H:%M')} IST hard cutoff"
    return False, None
