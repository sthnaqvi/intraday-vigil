"""Broker-agnostic domain models for the read path: positions, orders, quotes.

Frozen dataclasses, zero Kite-specific field names or types. `mapping.py` is the only
place that knows how to build these from a Kite response; nothing else may reach into a
raw broker dict again once a value has crossed into one of these.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class OHLC:
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0


@dataclass(frozen=True)
class Quote:
    last_price: float
    ohlc: OHLC


@dataclass(frozen=True)
class Position:
    symbol: str
    exchange: str
    product: str
    quantity: int          # signed: positive long, negative short
    buy_price: float
    sell_price: float
    average_price: float


@dataclass(frozen=True)
class Order:
    order_id: str
    symbol: str
    product: str
    order_type: str
    transaction_type: str
    status: str
    trigger_price: float
    quantity: int
    average_price: float = 0.0
    status_message: str | None = None
