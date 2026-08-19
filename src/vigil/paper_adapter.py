"""PaperAdapter: a real, self-contained simulated broker — not a stub. It keeps its own
order book and positions, fills market orders instantly at whatever price you've told it
the symbol is trading at, rests stop orders until that price crosses their trigger, and
fills them then. Lets someone try the whole daemon — SL lifecycle, phases, trailing,
squareoff — with no Kite account and no real money.

In-memory per instance, but optionally self-persisting: construct with `persist_path` (or
via `load_or_create()`) and every mutation is written to that file immediately, so separate
CLI invocations (`vigil enter`, then `vigil status` in a new process) see the same book.
Without a persist_path, state resets when the process exits — that's the right default for
tests and for the library-level usage in docs/quickstart.md, where a fresh book each run is
the point, not a limitation.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

from .models import OHLC, Order, Position, Quote

PENDING = "TRIGGER PENDING"
COMPLETE = "COMPLETE"
CANCELLED = "CANCELLED"


class PaperAdapter:
    def __init__(self, starting_funds: float = 1_000_000.0,
                persist_path: Path | None = None):
        self._prices: dict[str, float] = {}
        self._positions: dict[str, Position] = {}
        self._orders: dict[str, Order] = {}
        self._oid = 0
        self.funds = starting_funds
        self._persist_path = persist_path

    # ---------- persistence ----------

    def _to_dict(self) -> dict:
        return {
            "funds": self.funds,
            "prices": self._prices,
            "positions": {k: asdict(v) for k, v in self._positions.items()},
            "orders": {k: asdict(v) for k, v in self._orders.items()},
            "oid": self._oid,
        }

    def _save(self) -> None:
        if self._persist_path is None:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._persist_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._to_dict(), indent=2))
        os.replace(tmp, self._persist_path)

    def _reload(self) -> None:
        """Pull the latest shared state from disk before serving a read or applying a
        mutation. Without this, a long-running process (the daemon) would keep serving
        whatever it saw at construction time forever — oblivious to a separate CLI
        process's `vigil enter` landing in the file seconds later. A real broker's API
        gives you this for free: every call reaches a live, shared source of truth, not a
        snapshot taken once at startup. A no-op when there's no persist_path, or nothing
        persisted yet."""
        if self._persist_path is None or not self._persist_path.exists():
            return
        try:
            data = json.loads(self._persist_path.read_text())
        except (json.JSONDecodeError, OSError):
            return  # keep serving in-memory state rather than crash on a torn write
        self.funds = data.get("funds", self.funds)
        self._prices = data.get("prices", {})
        self._positions = {k: Position(**v) for k, v in data.get("positions", {}).items()}
        self._orders = {k: Order(**v) for k, v in data.get("orders", {}).items()}
        self._oid = data.get("oid", self._oid)

    @classmethod
    def load_or_create(cls, persist_path: Path) -> PaperAdapter:
        """The book at persist_path, or a fresh one if it doesn't exist yet / is corrupt."""
        adapter = cls(persist_path=persist_path)
        adapter._reload()
        return adapter

    # ---------- test/dev price control ----------

    def set_price(self, symbol: str, price: float) -> None:
        """Move a symbol's simulated LTP, then fill any resting stop it just crossed —
        this is what makes it a broker simulation instead of inert bookkeeping."""
        self._reload()
        self._prices[symbol] = price
        for order in list(self._orders.values()):
            if order.symbol != symbol or order.status != PENDING:
                continue
            crossed = (
                price <= order.trigger_price if order.transaction_type == "SELL"
                else price >= order.trigger_price
            )
            if crossed:
                self._fill(order, order.trigger_price)
        self._save()

    # ---------- internal order book ----------

    def _next_id(self) -> str:
        self._oid += 1
        return f"PAPER-{self._oid}"

    def _fill(self, order: Order, price: float) -> None:
        pos = self._positions.get(
            order.symbol,
            Position(symbol=order.symbol, exchange="NSE", product="MIS",
                    quantity=0, buy_price=0.0, sell_price=0.0, average_price=0.0),
        )
        delta = order.quantity if order.transaction_type == "BUY" else -order.quantity
        prior_qty = pos.quantity
        new_qty = prior_qty + delta
        # VWAP-blend the entry side, same convention Kite reports: buy_price/sell_price
        # are the average of that side only, not a net of both.
        same_side = prior_qty != 0 and (prior_qty > 0) == (delta > 0)
        done = abs(prior_qty) if same_side else 0
        if order.transaction_type == "BUY":
            base = pos.buy_price
            buy_price = ((base * done) + price * order.quantity) / (done + order.quantity) \
                if base or done else price
            sell_price = pos.sell_price
        else:
            base = pos.sell_price
            sell_price = ((base * done) + price * order.quantity) / (done + order.quantity) \
                if base or done else price
            buy_price = pos.buy_price
        self._positions[order.symbol] = Position(
            symbol=order.symbol, exchange="NSE", product="MIS", quantity=new_qty,
            buy_price=buy_price, sell_price=sell_price,
            average_price=buy_price or sell_price,
        )
        self._orders[order.order_id] = Order(
            order_id=order.order_id, symbol=order.symbol, product=order.product,
            order_type=order.order_type, transaction_type=order.transaction_type,
            status=COMPLETE, trigger_price=order.trigger_price, quantity=order.quantity,
            average_price=price,
        )

    # ---------- reads ----------

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        self._reload()
        out = {}
        for s in symbols:
            sym = s.split(":", 1)[1] if ":" in s else s
            if sym in self._prices:
                p = self._prices[sym]
                out[s] = Quote(last_price=p, ohlc=OHLC(open=p, high=p, low=p, close=p))
        return out

    def positions_day(self) -> list[Position]:
        # Kite includes a symbol here even after it's flat (quantity 0) — day P&L is
        # tracked per symbol regardless of current holding. Filtering to "open" positions
        # is state.open_mis_positions's job, not the port's; matching that here is what
        # makes the two adapters actually swappable instead of just similar.
        self._reload()
        return list(self._positions.values())

    def orders(self) -> list[Order]:
        self._reload()
        return list(self._orders.values())

    def margins(self) -> dict:
        self._reload()
        return {"equity": {"available": {"live_balance": self.funds},
                           "utilised": {}, "net": self.funds}}

    def instruments(self, exchange: str = "NSE") -> list[dict]:
        return []  # no real instrument master — paper mode has no WebSocket ticker to feed

    def historical_daily(self, instrument_token, from_date, to_date) -> list[dict]:
        return []  # no PDH/PDL history; the daemon falls back to today's day-H/L

    # ---------- mutations ----------

    def place_market_order(self, symbol: str, transaction_type: str, quantity: int,
                           exchange: str = "NSE") -> str:
        self._reload()
        order_id = self._next_id()
        price = self._prices.get(symbol, 0.0)
        order = Order(order_id=order_id, symbol=symbol, product="MIS",
                      order_type="MARKET", transaction_type=transaction_type,
                      status=PENDING, trigger_price=0.0, quantity=quantity)
        self._orders[order_id] = order
        self._fill(order, price)  # market orders fill instantly, same as at a real venue
        self._save()
        return order_id

    def place_stop_order(self, symbol: str, transaction_type: str, trigger_price: float,
                         quantity: int, exchange: str = "NSE") -> str:
        self._reload()
        order_id = self._next_id()
        self._orders[order_id] = Order(
            order_id=order_id, symbol=symbol, product="MIS", order_type="SL-M",
            transaction_type=transaction_type, status=PENDING,
            trigger_price=trigger_price, quantity=quantity,
        )
        self._save()
        return order_id

    def modify_stop_order(self, order_id: str, trigger_price: float, quantity: int) -> str:
        self._reload()
        o = self._orders[order_id]
        if o.status != PENDING:
            raise ValueError(f"order {order_id} is {o.status}, not modifiable")
        self._orders[order_id] = Order(
            order_id=o.order_id, symbol=o.symbol, product=o.product,
            order_type=o.order_type, transaction_type=o.transaction_type,
            status=o.status, trigger_price=trigger_price, quantity=quantity,
            average_price=o.average_price,
        )
        self._save()
        return order_id

    def cancel_order(self, order_id: str) -> str:
        self._reload()
        o = self._orders[order_id]
        self._orders[order_id] = Order(
            order_id=o.order_id, symbol=o.symbol, product=o.product,
            order_type=o.order_type, transaction_type=o.transaction_type,
            status=CANCELLED, trigger_price=o.trigger_price, quantity=o.quantity,
            average_price=o.average_price,
        )
        self._save()
        return order_id
