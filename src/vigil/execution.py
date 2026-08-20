"""Trading intent above the broker port: "enter long", "exit a short", "protect a
position" all resolve to the same four port primitives, just with a different
transaction side. The port itself has no notion of entry vs exit — only BUY vs SELL.
"""
from __future__ import annotations

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
