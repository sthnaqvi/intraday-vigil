"""KiteAdapter: the only module that knows Kite's mutation call shape (variety="regular",
product="MIS", order_type strings, market_protection). Implements BrokerClient's raw
primitives against a KiteConnect instance, nothing else — no dry-run gate, no retry, no
audit logging. GuardedBroker wraps this (or any other BrokerClient) to add those.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from . import config, mapping
from .models import Order, Position, Quote


class KiteAdapter:
    def __init__(self, kite: Any):
        self.kite = kite

    # ---------- reads ----------

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        raw = self.kite.quote(symbols)
        return {k: mapping.quote_from_kite(v) for k, v in raw.items()}

    def positions_day(self) -> list[Position]:
        raw = self.kite.positions()["day"]
        return [mapping.position_from_kite(p) for p in raw]

    def orders(self) -> list[Order]:
        raw = self.kite.orders()
        return [mapping.order_from_kite(o) for o in raw]

    def margins(self) -> dict:
        return self.kite.margins()

    def instruments(self, exchange: str = "NSE") -> list[dict]:
        return self.kite.instruments(exchange)

    def historical_daily(self, instrument_token: int, from_date: date, to_date: date) -> list[dict]:
        return self.kite.historical_data(instrument_token, from_date, to_date, "day")

    # ---------- mutations ----------

    def place_market_order(self, symbol: str, transaction_type: str, quantity: int,
                           exchange: str = "NSE") -> str:
        return self.kite.place_order(
            variety="regular", exchange=exchange, tradingsymbol=symbol,
            transaction_type=transaction_type, quantity=quantity, product="MIS",
            order_type="MARKET", market_protection=config.MARKET_PROTECTION_PCT,
        )

    def place_stop_order(self, symbol: str, transaction_type: str, trigger_price: float,
                         quantity: int, exchange: str = "NSE") -> str:
        return self.kite.place_order(
            variety="regular", exchange=exchange, tradingsymbol=symbol,
            transaction_type=transaction_type, quantity=quantity, product="MIS",
            order_type="SL-M", trigger_price=trigger_price,
        )

    def modify_stop_order(self, order_id: str, trigger_price: float, quantity: int) -> str:
        return self.kite.modify_order(
            variety="regular", order_id=order_id, order_type="SL-M",
            trigger_price=trigger_price, quantity=quantity,
            market_protection=config.MARKET_PROTECTION_PCT,
        )

    def cancel_order(self, order_id: str) -> str:
        return self.kite.cancel_order(variety="regular", order_id=order_id)
