"""Session state: tracked positions, persistence, and broker reconciliation.

The broker is the source of truth for positions/orders/triggers. The session
file persists only what the broker cannot tell us: sl_pct, phase,
breakeven_done, the realised-R ledger, and which one-shot alerts already fired.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import clock, config, rules
from .rules import Direction

SL_ORDER_TYPES = ("SL-M", "SL")
PENDING_STATUSES = ("TRIGGER PENDING", "OPEN", "AMO REQ RECEIVED")


@dataclass
class TrackedPosition:
    symbol: str
    direction: str  # Direction value
    entry: float
    qty: int
    sl_order_id: str
    sl_price: float
    sl_pct: float
    phase: int = 1
    breakeven_done: bool = False
    trail_started: bool = False
    sl_pct_derived: bool = False
    pdh: float | None = None
    pdl: float | None = None

    @property
    def dir(self) -> Direction:
        return Direction(self.direction)

    @property
    def r(self) -> float:
        return self.entry * self.sl_pct

    @property
    def trail_pct(self) -> float:
        return config.TRAIL_MULT * self.sl_pct


@dataclass
class SessionState:
    date: str
    positions: dict[str, TrackedPosition] = field(default_factory=dict)
    closed: list[dict] = field(default_factory=list)
    fired: list[str] = field(default_factory=list)
    kill_switch: bool = False
    squareoff_done: bool = False

    @property
    def realized_r_today(self) -> float:
        return round(sum(c["realized_r"] for c in self.closed), 3)

    @property
    def realized_pnl_today(self) -> float:
        return round(sum(c["realized_pnl"] for c in self.closed), 2)

    # ---------- persistence ----------

    @staticmethod
    def _path(date_str: str) -> Path:
        return config.DATA_DIR / f"session-{date_str}.json"

    def save(self) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        data = asdict(self)
        self._path(self.date).write_text(json.dumps(data, indent=2))

    @classmethod
    def load_or_create(cls) -> "SessionState":
        today = clock.now_ist().date().isoformat()
        path = cls._path(today)
        if path.exists():
            raw = json.loads(path.read_text())
            positions = {s: TrackedPosition(**p) for s, p in raw.pop("positions", {}).items()}
            return cls(positions=positions, **raw)
        return cls(date=today)


def load_risk_seeds() -> dict[str, dict]:
    """data/risk.json — written by `algo enter` / `algo arm` / `algo add`, or by the
    Claude skill for MCP-placed entries:
    {"INDIGO": {"sl_pct": 0.01, "pdh": 4205.0, "pdl": 4080.0}, ...}"""
    if config.RISK_FILE.exists():
        try:
            return json.loads(config.RISK_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_risk_seeds(seeds: dict[str, dict]) -> None:
    """Atomic write. Callers must merge — never clobber another symbol's seed."""
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = config.RISK_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(seeds, indent=2))
    os.replace(tmp, config.RISK_FILE)


# ---------- broker views ----------

def open_mis_positions(positions_day: list[dict]) -> dict[str, dict]:
    return {
        p["tradingsymbol"]: p
        for p in positions_day
        if p.get("product") == "MIS" and p.get("quantity", 0) != 0
    }


def position_direction(pos_row: dict) -> Direction:
    return Direction.LONG if pos_row["quantity"] > 0 else Direction.SHORT


def position_entry_price(pos_row: dict) -> float:
    """VWAP of the entry side (not `average_price`, which nets both sides)."""
    if pos_row["quantity"] > 0:
        return pos_row["buy_price"]
    return pos_row["sell_price"]


def find_sl_order(orders: list[dict], symbol: str, direction: Direction) -> dict | None:
    """The pending SL protecting this position: opposite side, SL/SL-M, MIS."""
    want_txn = "SELL" if direction == Direction.LONG else "BUY"
    for o in orders:
        if (
            o.get("tradingsymbol") == symbol
            and o.get("product") == "MIS"
            and o.get("order_type") in SL_ORDER_TYPES
            and o.get("transaction_type") == want_txn
            and o.get("status") in PENDING_STATUSES
        ):
            return o
    return None


def order_by_id(orders: list[dict], order_id: str) -> dict | None:
    for o in orders:
        if o.get("order_id") == order_id:
            return o
    return None


# ---------- reconciliation ----------

@dataclass
class ReconcileReport:
    new_tracked: list[str] = field(default_factory=list)
    exited: list[dict] = field(default_factory=list)
    orphan_sl_cancelled: list[str] = field(default_factory=list)
    unprotected: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    refreshed: list[dict] = field(default_factory=list)
    # Tracked positions whose SL order is no longer resting at the exchange. Distinct from
    # `unprotected`, which only ever covered positions seen for the FIRST time.
    lost_sl: list[dict] = field(default_factory=list)


def reconcile(
    state: SessionState,
    positions_day: list[dict],
    orders: list[dict],
    seeds: dict[str, dict],
) -> ReconcileReport:
    """Diff broker truth against tracked state. Pure bookkeeping — the caller
    (monitor) executes any resulting broker actions (orphan cancels, fresh SLs)."""
    report = ReconcileReport()
    open_now = open_mis_positions(positions_day)

    # 1. Exits: tracked symbols no longer open at the broker
    for symbol in list(state.positions):
        if symbol in open_now:
            continue
        tp = state.positions.pop(symbol)
        sl_order = order_by_id(orders, tp.sl_order_id)
        pos_row = next((p for p in positions_day if p["tradingsymbol"] == symbol), None)

        if sl_order and sl_order.get("status") == "COMPLETE":
            exit_price = sl_order.get("average_price") or tp.sl_price
            reason = {1: "SL_HIT", 2: "BE_STOP", 3: "TRAIL_EXIT"}[tp.phase]
        else:
            reason = "SQUAREOFF" if state.squareoff_done else "MANUAL_EXIT"
            if pos_row is not None:
                exit_price = (
                    pos_row["sell_price"] if tp.dir == Direction.LONG else pos_row["buy_price"]
                )
            else:
                exit_price = tp.sl_price
            if sl_order and sl_order.get("status") in PENDING_STATUSES:
                report.orphan_sl_cancelled.append(tp.sl_order_id)

        r_mult = round(rules.realized_r(tp.entry, exit_price, tp.r, tp.dir), 2)
        pnl = round(
            (exit_price - tp.entry) * tp.qty
            if tp.dir == Direction.LONG
            else (tp.entry - exit_price) * tp.qty,
            2,
        )
        record = {
            "symbol": symbol,
            "direction": tp.direction,
            "entry": tp.entry,
            "exit_price": exit_price,
            "qty": tp.qty,
            "realized_r": r_mult,
            "realized_pnl": pnl,
            "exit_reason": reason,
            "phase_at_exit": tp.phase,
            "exit_time": clock.now_ist().isoformat(),
        }
        state.closed.append(record)
        report.exited.append(record)

    # 2. New positions: open at the broker but not tracked
    for symbol, pos_row in open_now.items():
        if symbol in state.positions:
            tp = state.positions[symbol]
            new_qty = abs(pos_row["quantity"])
            # Adding to a position moves the entry VWAP, which changes R and therefore
            # every phase threshold. Re-read entry and the risk.json seed whenever qty
            # changes, not just on first discovery. (A partial exit leaves the entry-side
            # VWAP untouched, so this is a no-op there.) Left stale on 2026-08-18, this
            # reported a profitable HCLTECH short as a loss.
            if new_qty != tp.qty:
                old_entry, old_pct = tp.entry, tp.sl_pct
                broker_entry = position_entry_price(pos_row)
                if broker_entry > 0:
                    tp.entry = broker_entry
                seed = seeds.get(symbol, {})
                if "sl_pct" in seed:
                    tp.sl_pct = round(float(seed["sl_pct"]), 5)
                    tp.sl_pct_derived = False
                elif tp.entry > 0 and tp.sl_price > 0:
                    tp.sl_pct = round(abs(tp.entry - tp.sl_price) / tp.entry, 5)
                    tp.sl_pct_derived = True
                if tp.entry != old_entry or tp.sl_pct != old_pct:
                    report.refreshed.append({
                        "symbol": symbol,
                        "qty": [tp.qty, new_qty],
                        "entry": [old_entry, tp.entry],
                        "sl_pct": [old_pct, tp.sl_pct],
                    })
            tp.qty = new_qty

            # An SL can vanish under a position we are already tracking — cancelled in the
            # Kite app, pulled by the broker, or lost to a rejected modify. Nothing used to
            # notice: `unprotected` was only populated for first-seen positions, the qty
            # check skips non-pending orders, and _replace_if_dead only runs when a Phase
            # 2/3 modify is attempted. A Phase 1 position could sit naked all session while
            # status.json still displayed its old SL price. (Observed 2026-08-18.)
            sl = order_by_id(orders, tp.sl_order_id)
            if sl is None or sl.get("status") not in PENDING_STATUSES:
                if sl is None or sl.get("status") != "COMPLETE":
                    report.lost_sl.append({
                        "symbol": symbol,
                        "sl_order_id": tp.sl_order_id,
                        "status": (sl or {}).get("status", "MISSING"),
                        "qty": new_qty,
                        "direction": tp.direction,
                        "last_known_sl": tp.sl_price,
                    })
            continue
        direction = position_direction(pos_row)
        entry = position_entry_price(pos_row)
        qty = abs(pos_row["quantity"])
        sl_order = find_sl_order(orders, symbol, direction)
        seed = seeds.get(symbol, {})

        if sl_order is None:
            report.unprotected.append(symbol)
            continue  # monitor decides whether it can place a fresh SL (needs a seed)

        trigger = sl_order.get("trigger_price") or 0.0
        if "sl_pct" in seed:
            sl_pct, derived = float(seed["sl_pct"]), False
        elif entry > 0 and trigger > 0:
            sl_pct, derived = abs(entry - trigger) / entry, True
            report.warnings.append(
                f"{symbol}: sl_pct derived from broker SL ({sl_pct:.2%}) — "
                "only valid if the SL has not been moved yet"
            )
        else:
            report.warnings.append(f"{symbol}: cannot resolve sl_pct — skipping tracking")
            continue

        state.positions[symbol] = TrackedPosition(
            symbol=symbol,
            direction=direction.value,
            entry=entry,
            qty=qty,
            sl_order_id=sl_order["order_id"],
            sl_price=trigger,
            sl_pct=round(sl_pct, 5),
            sl_pct_derived=derived,
            pdh=seed.get("pdh"),
            pdl=seed.get("pdl"),
        )
        report.new_tracked.append(symbol)

    # 3. Kill switch
    if not state.kill_switch and state.realized_r_today <= config.KILL_SWITCH_R:
        state.kill_switch = True

    return report
