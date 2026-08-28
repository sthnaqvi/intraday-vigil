"""Armed entry triggers: the data model, persistence, entry gate, and TriggerEngine — the
transport-free logic that decides what to do when a price update arrives. What actually
delivers those price updates (KiteTickerFeed's WebSocket, or PollingFeed's cycle-cadence
quotes) lives in feed.py and doesn't appear here at all.

Why this exists: entries used to be placed by Claude over the MCP session, on a ~180s
poll. Two problems — the MCP token dies roughly hourly, and a 180s poll can miss a level
break by minutes. Both are solved by moving the watch and the execution into the daemon,
which holds a token valid for the whole trading day and can receive ticks in real time.

A trigger is a standing intent: "if RELIANCE trades above 1328.60, buy 590 MIS and protect
it at sl_pct". `auto: false` (default) alerts loudly and leaves the decision to the human.
`auto: true` places the order the moment the level breaks.

Every entry — automatic or not — passes the same gate the skill applies: kill switch,
no-new-entries, and the hard 14:30 cutoff.
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from . import clock, config, execution, levels, rules
from .events import EventLog
from .guard import GuardedBroker
from .notify import alert_dialog, notify
from .rules import Direction

TRIGGERS_FILE = config.DATA_DIR / "triggers.json"

ARMED = "armed"
FIRED = "fired"
CANCELLED = "cancelled"
FAILED = "failed"


@dataclass
class Trigger:
    symbol: str
    direction: str            # LONG | SHORT
    level: float              # price to break
    side: str                 # "above" | "below"
    qty: int
    sl_pct: float             # decimal fraction, e.g. 0.0091
    auto: bool = False        # place automatically, or just alert?
    pdh: float | None = None
    pdl: float | None = None
    note: str = ""
    status: str = ARMED
    armed_at: str = field(default_factory=lambda: clock.now_ist().isoformat())
    fired_at: str | None = None
    entry_order_id: str | None = None
    sl_order_id: str | None = None
    detail: str = ""

    @property
    def dir(self) -> Direction:
        return Direction.LONG if self.direction == "LONG" else Direction.SHORT

    def crossed(self, ltp: float) -> bool:
        return ltp > self.level if self.side == "above" else ltp < self.level


@dataclass
class ExitTrigger:
    """A standing intent to close whatever position exists on `symbol`, independent of
    its resting SL: "if RELIANCE trades below 1310, close it — take the profit / cut the
    loss now, don't wait for the mechanical trail to catch up." Unlike an entry Trigger
    this has no auto/alert split — arming an exit only makes sense if firing it also
    closes the position, so it always does.
    """
    symbol: str
    level: float               # price to break
    side: str                  # "above" | "below"
    note: str = ""
    status: str = ARMED
    armed_at: str = field(default_factory=lambda: clock.now_ist().isoformat())
    fired_at: str | None = None
    detail: str = ""

    def crossed(self, ltp: float) -> bool:
        return ltp > self.level if self.side == "above" else ltp < self.level


# ---------- persistence ----------

def load() -> list[Trigger]:
    if not TRIGGERS_FILE.exists():
        return []
    try:
        raw = json.loads(TRIGGERS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for d in raw:
        known = {k: v for k, v in d.items() if k in Trigger.__dataclass_fields__}
        out.append(Trigger(**known))
    return out


def save(triggers: list[Trigger]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = TRIGGERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(t) for t in triggers], indent=2))
    os.replace(tmp, TRIGGERS_FILE)


def armed(triggers: list[Trigger]) -> list[Trigger]:
    return [t for t in triggers if t.status == ARMED]


EXIT_TRIGGERS_FILE = config.DATA_DIR / "exit_triggers.json"


def load_exit_triggers() -> list[ExitTrigger]:
    if not EXIT_TRIGGERS_FILE.exists():
        return []
    try:
        raw = json.loads(EXIT_TRIGGERS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for d in raw:
        known = {k: v for k, v in d.items() if k in ExitTrigger.__dataclass_fields__}
        out.append(ExitTrigger(**known))
    return out


def save_exit_triggers(triggers: list[ExitTrigger]) -> None:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = EXIT_TRIGGERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(t) for t in triggers], indent=2))
    os.replace(tmp, EXIT_TRIGGERS_FILE)


def armed_exits(triggers: list[ExitTrigger]) -> list[ExitTrigger]:
    return [t for t in triggers if t.status == ARMED]


def all_armed_symbols() -> set[str]:
    """Every symbol the price feed needs to watch — union of armed entry-trigger and
    armed exit-trigger symbols. A symbol can carry an armed exit with no entry trigger
    at all (the common case: arming an exit on a position that already exists), so
    callers that only looked at `armed(load())` would silently never watch it."""
    return ({t.symbol for t in armed(load())}
            | {t.symbol for t in armed_exits(load_exit_triggers())})


# ---------- entry gate ----------

def gate_block_reason(session: Any) -> str | None:
    """Same gate the skill enforces. Returns a reason string, or None if entries are allowed."""
    now = clock.now_ist()
    if now.time() >= config.NO_NEW_ENTRIES_AFTER:
        return f"past {config.NO_NEW_ENTRIES_AFTER.strftime('%H:%M')} IST hard cutoff"
    if getattr(session, "kill_switch", False):
        return f"kill switch active (day {getattr(session, 'realized_r_today', 0)}R)"
    return None


# ---------- execution ----------

def execute(trigger: Trigger, broker: GuardedBroker, events: EventLog, session: Any,
            ltp: float) -> bool:
    """Place the entry and its protective SL. Returns True on success.

    Order matters and mirrors the skill: entry first, then re-derive the SL from the
    ACTUAL fill (not the trigger level), applying the stop-hunt guard, then place SL-M.
    """
    blocked = gate_block_reason(session)
    if blocked:
        trigger.status = CANCELLED
        trigger.detail = f"blocked by entry gate: {blocked}"
        events.emit("TRIGGER_BLOCKED", trigger.symbol, level=trigger.level, reason=blocked)
        notify(f"{trigger.symbol} trigger blocked: {blocked}", sound=True)
        return False

    try:
        entry_id = execution.place_entry(broker, trigger.symbol, trigger.dir, trigger.qty)
        trigger.entry_order_id = entry_id
    except Exception as e:
        hint = execution.margin_rejection_hint(broker, trigger.symbol, trigger.dir, e)
        trigger.status = FAILED
        trigger.detail = f"entry order failed: {e!r}" + (f" | {hint}" if hint else "")
        events.emit("TRIGGER_ENTRY_FAILED", trigger.symbol, error=repr(e), margin_hint=hint)
        alert_dialog(f"{trigger.symbol}: ENTRY ORDER FAILED\n\n{e}"
                     + (f"\n\n{hint}" if hint else ""))
        return False

    # Resolve the real fill; fall back to the tick price if the position is not visible yet.
    fill = ltp
    try:
        for p in broker.positions_day():
            if p.symbol == trigger.symbol and p.quantity != 0:
                side_price = p.buy_price if trigger.dir == Direction.LONG else p.sell_price
                if side_price:
                    fill = float(side_price)
                break
    except Exception:
        pass

    guard_levels = [v for v in (trigger.pdh, trigger.pdl) if v]
    tick = levels.tick_sizes(broker, [trigger.symbol])[trigger.symbol]
    sl_price = rules.initial_sl_price(fill, trigger.sl_pct, trigger.dir, guard_levels, tick)

    # The 1.5% cap is checked against the caller's INPUT sl_pct before this point (cmd_enter,
    # TriggerEngine) — but the stop-hunt guard above can still widen the price it just
    # computed past that cap, since the guard pushes clear of PDH/PDL/day-H/L regardless of
    # what width that implies. Clamping back to exactly 1.5% here would put the stop right
    # back inside the level the guard just pushed clear of, so this doesn't refuse or
    # shrink it — it makes the breach loud and visible instead of only discoverable later by
    # hand-computing sl_pct from `vigil status --json`. See docs/incidents/trail-and-sl-lifecycle.md
    # ("The cap check ran before the guard that could break it").
    effective_sl_pct = abs(fill - sl_price) / fill if fill else trigger.sl_pct
    if effective_sl_pct > 0.015:
        events.emit("SL_CAP_EXCEEDED_POST_GUARD", trigger.symbol,
                    input_sl_pct=trigger.sl_pct, effective_sl_pct=round(effective_sl_pct, 4),
                    fill=fill, sl_price=sl_price, guard_levels=guard_levels)
        notify(f"{trigger.symbol}: stop-hunt guard widened the SL to "
              f"{effective_sl_pct:.2%}, over the 1.5% cap (input was "
              f"{trigger.sl_pct:.2%}). Placing anyway — pulling it back to the cap would "
              "put the stop inside the level the guard just avoided. Review manually.",
              sound=True)
    try:
        sl_id = execution.place_sl(broker, trigger.symbol, trigger.dir, sl_price, trigger.qty)
        trigger.sl_order_id = sl_id
    except Exception as e:
        trigger.status = FAILED
        trigger.detail = f"POSITION OPEN WITHOUT SL — {e!r}"
        events.emit("TRIGGER_SL_FAILED", trigger.symbol, error=repr(e), entry=fill)
        alert_dialog(
            f"{trigger.symbol}: position is OPEN but the SL order FAILED.\n\n{e}\n\n"
            "The next monitor cycle will try to place one from the risk.json seed."
        )
        _write_seed(trigger, fill, sl_price)
        return False

    trigger.status = FIRED
    trigger.fired_at = clock.now_ist().isoformat()
    trigger.detail = f"entry {fill} sl {sl_price}"
    _write_seed(trigger, fill, sl_price)
    events.emit("TRIGGER_FIRED", trigger.symbol, level=trigger.level, entry=fill,
                qty=trigger.qty, sl_price=sl_price, sl_pct=trigger.sl_pct,
                entry_order_id=trigger.entry_order_id, sl_order_id=trigger.sl_order_id,
                auto=trigger.auto)
    notify(f"{trigger.symbol} ENTERED {trigger.direction} {trigger.qty} @ {fill}, "
          f"SL {sl_price}", sound=True)
    return True


def _write_seed(trigger: Trigger, entry: float, sl_price: float) -> None:
    """Merge this position's risk seed so the monitor loop derives the right R."""
    seeds = {}
    if config.RISK_FILE.exists():
        try:
            seeds = json.loads(config.RISK_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            seeds = {}
    seeds[trigger.symbol] = {
        "sl_pct": round(abs(entry - sl_price) / entry, 4) if entry else trigger.sl_pct,
        "pdh": trigger.pdh,
        "pdl": trigger.pdl,
    }
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.RISK_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seeds, indent=2))
    os.replace(tmp, config.RISK_FILE)


# ---------- trigger engine (transport-free) ----------

class TriggerEngine:
    """Decides what to do when a price update for a symbol arrives — crossed level, fire
    or alert — with no idea whether that price came from a WebSocket tick (feed.py's
    KiteTickerFeed) or a poll cycle (feed.py's PollingFeed). Both transports call the same
    on_price(), through the same lock, so they can never race each other mutating trigger
    state — which is exactly what the old poll fallback and the old WebSocket handler did
    before this: the same match-and-fire logic, duplicated in two places, one of them with
    no lock at all.
    """

    def __init__(self, broker: GuardedBroker, events: EventLog, session: Any,
                on_fire: Callable[[Trigger], None] | None = None,
                lock: threading.Lock | None = None):
        self.broker = broker
        self.events = events
        self.session = session
        self.on_fire = on_fire
        # Shared with MonitorLoop's own position-decision tick handler when one is passed
        # in — a symbol can carry both an open position and an armed exit trigger, both
        # touching the same broker order, so both paths must serialize through one lock,
        # not two independent ones. Defaults to owning its own for any standalone caller
        # (tests, or a TriggerEngine used without a MonitorLoop).
        self.lock = lock if lock is not None else threading.Lock()

    def on_price(self, symbol: str, ltp: float, source: str) -> None:
        with self.lock:
            all_t = load()
            live = [t for t in armed(all_t) if t.symbol == symbol]
            changed = False
            for t in live:
                if not t.crossed(ltp):
                    continue
                changed = True
                self.events.emit("TRIGGER_HIT", symbol, level=t.level, ltp=ltp,
                                 side=t.side, auto=t.auto, source=source)
                if t.auto:
                    execute(t, self.broker, self.events, self.session, ltp)
                else:
                    t.status = CANCELLED
                    t.detail = f"level hit at {ltp} ({source}) — auto disabled, not placed"
                    alert_dialog(
                        f"{symbol} hit {t.level} (ltp {ltp}).\n\n"
                        f"Ready: {t.direction} {t.qty} @ market, SL {t.sl_pct:.2%}.\n"
                        "Auto-execute is OFF, so nothing was placed. "
                        f"Run: vigil enter {symbol} --side {t.direction.lower()} "
                        f"--qty {t.qty} --sl-pct {t.sl_pct * 100:.2f}"
                    )
                    notify(f"{symbol} hit {t.level} — NOT placed (auto off)", sound=True)
                if self.on_fire:
                    self.on_fire(t)
            if changed:
                save(all_t)

            all_exits = load_exit_triggers()
            live_exits = [t for t in armed_exits(all_exits) if t.symbol == symbol]
            exits_changed = False
            for et in live_exits:
                if not et.crossed(ltp):
                    continue
                exits_changed = True
                self.events.emit("EXIT_TRIGGER_HIT", symbol, level=et.level, ltp=ltp,
                                 side=et.side, source=source)
                try:
                    closed = execution.close_position(self.broker, self.events, symbol)
                except Exception as e:
                    et.status = FAILED
                    et.detail = f"close failed: {e!r}"
                    self.events.emit("EXIT_TRIGGER_FAILED", symbol, error=repr(e))
                    alert_dialog(f"{symbol}: EXIT TRIGGER hit {et.level} but the close "
                                f"order FAILED.\n\n{e}\n\nThe resting SL, if any, still "
                                "protects the position.")
                    continue
                if closed:
                    et.status = FIRED
                    et.fired_at = clock.now_ist().isoformat()
                    et.detail = f"closed at ltp {ltp} ({source})"
                    self.events.emit("EXIT_TRIGGER_FIRED", symbol, level=et.level, ltp=ltp)
                    notify(f"{symbol} EXIT TRIGGER fired at {ltp} — position closed",
                          sound=True)
                else:
                    et.status = CANCELLED
                    et.detail = f"level hit at {ltp} ({source}) but no open position"
            if exits_changed:
                save_exit_triggers(all_exits)
