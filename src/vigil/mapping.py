"""Kite wire-format dicts -> domain models. The only module allowed to know Kite's
field names (`tradingsymbol`, the buy_price/sell_price VWAP split, nested `ohlc`, etc).

Broker.positions_day()/.orders()/.quotes() are the sole call sites; everything downstream
of the broker (state.py, monitor.py, triggers.py, the CLI commands) works in models.py
types only.
"""
from __future__ import annotations

from .models import OHLC, Order, Position, Quote


def position_from_kite(row: dict) -> Position:
    return Position(
        symbol=row["tradingsymbol"],
        exchange=row.get("exchange", "NSE"),
        product=row.get("product", ""),
        quantity=row["quantity"],
        buy_price=row.get("buy_price") or 0.0,
        sell_price=row.get("sell_price") or 0.0,
        average_price=row.get("average_price") or 0.0,
    )


def order_from_kite(row: dict) -> Order:
    return Order(
        order_id=row["order_id"],
        symbol=row["tradingsymbol"],
        product=row.get("product", ""),
        order_type=row.get("order_type", ""),
        transaction_type=row.get("transaction_type", ""),
        status=row.get("status", ""),
        trigger_price=row.get("trigger_price") or 0.0,
        quantity=row.get("quantity") or 0,
        average_price=row.get("average_price") or 0.0,
        status_message=row.get("status_message"),
        placed_by=row.get("placed_by") or "",
        order_timestamp=str(row.get("order_timestamp") or ""),
    )


def quote_from_kite(row: dict) -> Quote:
    ohlc = row.get("ohlc") or {}
    return Quote(
        last_price=row.get("last_price") or 0.0,
        ohlc=OHLC(open=ohlc.get("open") or 0.0, high=ohlc.get("high") or 0.0,
                 low=ohlc.get("low") or 0.0, close=ohlc.get("close") or 0.0),
    )
