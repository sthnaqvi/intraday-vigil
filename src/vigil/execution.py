"""Trading intent above the broker port: "enter long", "exit a short", "protect a
position" all resolve to the same four port primitives, just with a different
transaction side. The port itself has no notion of entry vs exit — only BUY vs SELL.
"""
from __future__ import annotations

from . import rules as rules_mod
from . import state as state_mod
from .events import EventLog
from .ports import BrokerClient
from .rules import Direction


def place_entry(broker: BrokerClient, symbol: str, direction: Direction, quantity: int,
                exchange: str = "NSE") -> str:
    """Open a position at market. LONG -> BUY, SHORT -> SELL."""
    txn = "BUY" if direction == Direction.LONG else "SELL"
    return broker.place_market_order(symbol, txn, quantity, exchange)


def place_market_exit(broker: BrokerClient, symbol: str, direction: Direction, quantity: int,
                      exchange: str = "NSE") -> str:
    """Close a position at market — the opposite side of the position it closes."""
    txn = "SELL" if direction == Direction.LONG else "BUY"
    return broker.place_market_order(symbol, txn, quantity, exchange)


def place_sl(broker: BrokerClient, symbol: str, direction: Direction, trigger_price: float,
            quantity: int, exchange: str = "NSE") -> str:
    """Fresh SL-M protecting an open position (side opposite to the position)."""
    txn = "SELL" if direction == Direction.LONG else "BUY"
    return broker.place_stop_order(symbol, txn, trigger_price, quantity, exchange)


_MARGIN_REJECTION_MARKERS = ("insufficient", "margin required")


def margin_rejection_hint(broker: BrokerClient, symbol: str, direction: Direction,
                          error: Exception, reference_qty: int = 100) -> str | None:
    """On an insufficient-funds rejection, ask the broker's own order-margin calculator
    what quantity would actually fit — instead of leaving the caller (human or Claude) to
    infer a leverage multiplier from the rejection's numbers. Returns a ready-to-print
    hint string, or None if this doesn't look like a margin rejection, or the calculator
    call itself fails (a diagnostic aid must never crash the original error path).

    Direct fix for a documented, twice-repeated mistake: dividing the rejection's *total*
    required margin by the attempted quantity (implying leverage had collapsed) instead of
    dividing the *shortfall* by the real leverage. Both times, the correction only arrived
    after the user supplied the right arithmetic by hand. See
    docs/incidents/verification-gaps.md."""
    if not any(m in str(error).lower() for m in _MARGIN_REJECTION_MARKERS):
        return None
    try:
        available = (broker.margins().get("equity", {}).get("available", {})
                    .get("live_balance") or 0.0)
        if available <= 0:
            return None
        txn = "BUY" if direction == Direction.LONG else "SELL"
        info = broker.order_margins(symbol, txn, reference_qty)
        total = info.get("total") or 0.0
        if total <= 0:
            return None
        per_share = total / reference_qty
        qty = rules_mod.qty_from_margin(available, per_share)
        return (f"Broker's own margin calculator: ~Rs {per_share:.2f}/share for {symbol} "
                f"MIS, Rs {available:,.2f} available -> affordable qty ~{qty} "
                f"(not derived from an assumed leverage multiplier).")
    except Exception:
        return None


def close_position(broker: BrokerClient, events: EventLog, symbol: str) -> bool:
    """Cancel the resting SL (if any) and market-exit whatever position currently exists
    for symbol — reads the position fresh from the broker rather than trusting any
    caller's idea of direction/quantity, so a manual `vigil exit` and a fired exit
    trigger both close exactly what's actually open. Returns False (does nothing) if
    there's no open position — the caller decides what that means for it."""
    positions = state_mod.open_mis_positions(broker.positions_day())
    pos = positions.get(symbol)
    if pos is None:
        return False
    direction = state_mod.position_direction(pos)
    quantity = abs(pos.quantity)
    sl_order = state_mod.find_sl_order(broker.orders(), symbol, direction)
    if sl_order is not None:
        try:
            broker.cancel_order(sl_order.order_id)
        except Exception as e:
            events.emit("WARNING", symbol, message=f"SL cancel failed before exit: {e}")
    place_market_exit(broker, symbol, direction, quantity)
    return True
