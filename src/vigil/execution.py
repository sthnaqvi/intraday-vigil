"""Trading intent above the broker port: "enter long", "exit a short", "protect a
position" all resolve to the same four port primitives, just with a different
transaction side. The port itself has no notion of entry vs exit — only BUY vs SELL.
"""
from __future__ import annotations

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
