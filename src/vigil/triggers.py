"""Armed entry triggers, watched over Kite's tick WebSocket.

Why this exists: entries used to be placed by Claude over the MCP session, on a ~180s
poll. Two problems — the MCP token dies roughly hourly, and a 180s poll can miss a level
break by minutes. Both are solved by moving the watch and the execution into the daemon,
which holds a token valid for the whole trading day and receives ticks in real time.

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
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from . import clock, config, levels, rules
from .broker import Broker
from .events import EventLog, logger
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

def execute(trigger: Trigger, broker: Broker, events: EventLog, session: Any,
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
        entry_id = broker.place_entry(trigger.symbol, trigger.dir, trigger.qty)
        trigger.entry_order_id = entry_id
    except Exception as e:
        trigger.status = FAILED
        trigger.detail = f"entry order failed: {e!r}"
        events.emit("TRIGGER_ENTRY_FAILED", trigger.symbol, error=repr(e))
        alert_dialog(f"{trigger.symbol}: ENTRY ORDER FAILED\n\n{e}")
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
    sl_price = rules.initial_sl_price(fill, trigger.sl_pct, trigger.dir, guard_levels)
    try:
        sl_id = broker.place_sl(trigger.symbol, trigger.dir, sl_price, trigger.qty)
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
    notify(f"{trigger.symbol} ENTERED {trigger.direction} {trigger.qty} @ {fill}, SL {sl_price}",
           sound=True)
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


# ---------- websocket watcher ----------

class TriggerWatcher:
    """Subscribes to ticks for every armed trigger and acts the moment a level breaks.

    Runs on KiteTicker's own background thread, so the monitor loop is untouched. All
    mutation of shared trigger state happens under a lock.
    """

    def __init__(self, broker: Broker, events: EventLog, session: Any,
                 on_fire: Callable[[Trigger], None] | None = None):
        self.broker = broker
        self.events = events
        self.session = session
        self.on_fire = on_fire
        self.ticker = None
        self.lock = threading.Lock()
        self.token_to_symbol: dict[int, str] = {}
        self._running = False

    def _resolve_tokens(self, symbols: list[str]) -> dict[str, int]:
        return levels.instrument_tokens(self.broker, symbols)

    def start(self) -> bool:
        """Connect and subscribe. Returns False if there is nothing armed or no ticker."""
        triggers = armed(load())
        if not triggers:
            return False
        try:
            from kiteconnect import KiteTicker
        except Exception as e:  # pragma: no cover
            self.events.emit("WARNING", message=f"KiteTicker unavailable ({e}) — "
                                                "triggers fall back to the monitor cycle")
            return False

        symbols = sorted({t.symbol for t in triggers})
        mapping = self._resolve_tokens(symbols)
        missing = [s for s in symbols if s not in mapping]
        if missing:
            self.events.emit("WARNING", message=f"no instrument token for {missing}")
        if not mapping:
            return False
        self.token_to_symbol = {v: k for k, v in mapping.items()}

        token_file = json.loads(config.TOKEN_FILE.read_text())
        self.ticker = KiteTicker(token_file["api_key"], token_file["access_token"])
        tokens = list(self.token_to_symbol)

        def on_connect(ws, _response):
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_LTP, tokens)
            self.events.emit("TICKER_CONNECTED", symbols=symbols, tokens=tokens)

        def on_ticks(_ws, ticks):
            try:
                self._on_ticks(ticks)
            except Exception as e:  # never let a bad tick kill the socket
                self.events.emit("ERROR", message=f"tick handler failed: {e!r}")

        def on_close(_ws, code, reason):
            self.events.emit("TICKER_CLOSED", code=str(code), reason=str(reason))

        def on_error(_ws, code, reason):
            self.events.emit("TICKER_ERROR", code=str(code), reason=str(reason))

        self.ticker.on_connect = on_connect
        self.ticker.on_ticks = on_ticks
        self.ticker.on_close = on_close
        self.ticker.on_error = on_error
        # threaded=True keeps the reactor off the monitor loop's thread; reconnects are
        # handled by KiteTicker itself.
        self.ticker.connect(threaded=True, disable_ssl_verification=False)
        self._running = True
        logger.info("[TICKER] watching %s", ", ".join(symbols))
        return True

    def _on_ticks(self, ticks: list[dict]) -> None:
        with self.lock:
            triggers = load()
            live = armed(triggers)
            if not live:
                return
            changed = False
            for tick in ticks:
                symbol = self.token_to_symbol.get(tick.get("instrument_token"))
                ltp = tick.get("last_price")
                if not symbol or ltp is None:
                    continue
                for t in live:
                    if t.symbol != symbol or t.status != ARMED or not t.crossed(float(ltp)):
                        continue
                    changed = True
                    self.events.emit("TRIGGER_HIT", symbol, level=t.level, ltp=float(ltp),
                                     side=t.side, auto=t.auto)
                    if t.auto:
                        execute(t, self.broker, self.events, self.session, float(ltp))
                    else:
                        t.status = CANCELLED
                        t.detail = f"level hit at {ltp} — auto disabled, not placed"
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
                save(triggers)

    def stop(self) -> None:
        if self.ticker is not None and self._running:
            try:
                self.ticker.close()
            except Exception:
                pass
            self._running = False
