"""BrokerClient: the interface every broker adapter implements. A Protocol, not an ABC —
structural typing means an adapter author cannot slip in a "helpful" override of a method
that belongs to GuardedBroker (dry-run, spacing, retry, audit) by inheriting from it.

Six reads, four mutations. Entry and exit are NOT separate primitives — both are just a
market order in one direction or the other. That intent (which direction does "entering
long" vs "exiting a short" mean) lives one layer up, in execution.py; the port only knows
how to place a market order and a stop order, full stop.
"""
from __future__ import annotations

from datetime import date
from typing import Protocol

from .models import Order, Position, Quote


class BrokerClient(Protocol):
    # ---------- reads ----------

    def quotes(self, symbols: list[str]) -> dict[str, Quote]: ...

    def positions_day(self) -> list[Position]: ...

    def orders(self) -> list[Order]: ...

    def margins(self) -> dict: ...

    def instruments(self, exchange: str = "NSE") -> list[dict]: ...

    def historical_daily(self, instrument_token: int, from_date: date, to_date: date) -> list[dict]: ...

    # ---------- mutations ----------

    def place_market_order(self, symbol: str, transaction_type: str, quantity: int,
                           exchange: str = "NSE") -> str:
        """Fill-at-market, immediately. Used for both entries and exits — the caller
        decides BUY vs SELL; this primitive has no notion of "opening" or "closing"."""
        ...

    def place_stop_order(self, symbol: str, transaction_type: str, trigger_price: float,
                         quantity: int, exchange: str = "NSE") -> str:
        """Place a fresh stop that RESTS AT THE VENUE — it must keep protecting the
        position if this process dies the instant after the call returns. An adapter
        that cannot guarantee a resting stop must not implement this contract by polling
        and emulating one client-side; it must instead advertise no resting-stop capability
        so the daemon refuses to run live rather than silently offering fake protection."""
        ...

    def modify_stop_order(self, order_id: str, trigger_price: float, quantity: int) -> str:
        """Move an existing stop. Returning normally means the request was ACCEPTED, not
        that it was applied — a venue can 200 the request and then reject it (wrong tick,
        price band, whatever) leaving the old stop resting unchanged. Callers MUST re-read
        the order after calling this and verify the trigger/quantity actually changed
        before treating the position as protected at the new level. An adapter must never
        synthesize success to make this contract easier to satisfy."""
        ...

    def cancel_order(self, order_id: str) -> str: ...
