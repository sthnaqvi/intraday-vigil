"""Scripted fake KiteConnect for loop tests. Mutates its own order book on modify."""
from __future__ import annotations


class MockKite:
    def __init__(self):
        self._quotes: dict[str, dict] = {}
        self._positions: dict[str, list[dict]] = {"day": [], "net": []}
        self._orders: list[dict] = []
        self._oid = 100
        self.modify_calls: list[dict] = []
        self.place_calls: list[dict] = []
        self.cancel_calls: list[str] = []
        self._tick_sizes: dict[str, float] = {}
        self.fail_next_modify: Exception | None = None
        self.reject_order_on_failed_modify = True
        # Kite accepts the modify (HTTP 200) but the exchange rejects it, so the resting
        # order is unchanged and carries a status_message. This actually happens (error
        # 16448) and it is invisible unless the order is re-read.
        self.silent_modify_rejections = 0
        self.silent_reject_message = (
            "16448 : Difference between limit price and trigger price is beyond "
            "permissible range"
        )
        self._available_margin = 1_000_000.0
        self._margin_fraction = 0.2  # 5x leverage by default; set_margin_leverage() to change
        self.reject_orders_over_available = False  # opt in for margin-rejection tests

    # ---------- scripting helpers ----------

    def set_quote(self, symbol: str, ltp: float, open_=None, high=None, low=None, close=None):
        self._quotes[f"NSE:{symbol}"] = {
            "last_price": ltp,
            "ohlc": {"open": open_ or ltp, "high": high or ltp,
                     "low": low or ltp, "close": close or ltp},
        }

    def set_available_margin(self, amount: float):
        self._available_margin = amount

    def set_margin_leverage(self, leverage: float):
        """5x leverage -> 20% of order value required as margin, etc."""
        self._margin_fraction = 1.0 / leverage

    def set_tick_size(self, symbol: str, tick: float):
        """Script a non-default exchange tick for a symbol (most NSE equities are 0.05;
        some — DRREDDY included — trade in 0.10). Unscripted symbols are simply absent
        from instruments(), so levels.tick_sizes() falls back to config.NSE_TICK, same
        as every test written before per-symbol ticks existed."""
        self._tick_sizes[symbol] = tick

    def set_position(self, symbol: str, quantity: int, buy_price: float = 0.0,
                     sell_price: float = 0.0):
        self._positions["day"] = [
            p for p in self._positions["day"] if p["tradingsymbol"] != symbol
        ]
        self._positions["day"].append({
            "tradingsymbol": symbol, "exchange": "NSE", "product": "MIS",
            "quantity": quantity, "buy_price": buy_price, "sell_price": sell_price,
            "average_price": buy_price or sell_price,
        })

    def add_sl_order(self, symbol: str, transaction_type: str, trigger_price: float,
                     quantity: int, status: str = "TRIGGER PENDING",
                     order_type: str = "SL-M") -> str:
        self._oid += 1
        oid = str(self._oid)
        self._orders.append({
            "order_id": oid, "tradingsymbol": symbol, "product": "MIS",
            "order_type": order_type, "transaction_type": transaction_type,
            "status": status, "trigger_price": trigger_price, "quantity": quantity,
            "average_price": 0.0,
        })
        return oid

    def trigger_sl(self, order_id: str, fill_price: float):
        """Simulate the exchange firing an SL: order COMPLETE, position flat."""
        o = self._order(order_id)
        o["status"] = "COMPLETE"
        o["average_price"] = fill_price
        for p in self._positions["day"]:
            if p["tradingsymbol"] == o["tradingsymbol"]:
                if p["quantity"] > 0:
                    p["sell_price"] = fill_price
                else:
                    p["buy_price"] = fill_price
                p["quantity"] = 0

    def _order(self, order_id: str) -> dict:
        return next(o for o in self._orders if o["order_id"] == order_id)

    # ---------- KiteConnect surface ----------

    def profile(self):
        return {"user_id": "TEST"}

    def margins(self):
        return {"equity": {"available": {"live_balance": self._available_margin}}}

    def order_margins(self, params):
        out = []
        for p in params:
            price = self._quotes.get(f'{p["exchange"]}:{p["tradingsymbol"]}', {}) \
                .get("last_price", 0.0)
            total = round(price * p["quantity"] * self._margin_fraction, 2)
            out.append({"total": total})
        return out

    def quote(self, symbols):
        return {s: dict(self._quotes[s]) for s in symbols if s in self._quotes}

    def positions(self):
        return {"day": [dict(p) for p in self._positions["day"]], "net": []}

    def orders(self):
        return [dict(o) for o in self._orders]

    def instruments(self, exchange="NSE"):
        return [
            {"tradingsymbol": sym, "instrument_token": 900000 + i, "tick_size": tick}
            for i, (sym, tick) in enumerate(self._tick_sizes.items())
        ]

    def modify_order(self, variety, order_id, trigger_price=None, quantity=None, **kw):
        self.modify_calls.append(
            {"order_id": order_id, "trigger_price": trigger_price, "quantity": quantity}
        )
        if self.fail_next_modify is not None:
            err, self.fail_next_modify = self.fail_next_modify, None
            if self.reject_order_on_failed_modify:
                self._order(order_id)["status"] = "REJECTED"
            raise err
        if self.silent_modify_rejections > 0:
            self.silent_modify_rejections -= 1
            # API says OK, order stays exactly as it was, exchange leaves a message.
            self._order(order_id)["status_message"] = self.silent_reject_message
            return order_id
        o = self._order(order_id)
        if trigger_price is not None:
            o["trigger_price"] = trigger_price
        # deliberately mimics Kite: omitting quantity resets it to 1
        o["quantity"] = quantity if quantity is not None else 1
        return order_id

    def place_order(self, variety, exchange, tradingsymbol, transaction_type,
                    quantity, product, order_type, trigger_price=None, **kw):
        self.place_calls.append({
            "tradingsymbol": tradingsymbol, "transaction_type": transaction_type,
            "quantity": quantity, "order_type": order_type, "trigger_price": trigger_price,
        })
        if self.reject_orders_over_available and order_type == "MARKET":
            price = self._quotes.get(f"{exchange}:{tradingsymbol}", {}).get("last_price", 0.0)
            required = round(price * quantity * self._margin_fraction, 2)
            if required > self._available_margin:
                from kiteconnect.exceptions import GeneralException
                short = required - self._available_margin
                raise GeneralException(
                    f"Insufficient funds. Margin required: {required}. "
                    f"Margin available: {self._available_margin}. Add {short:.2f} to "
                    "place this order.")
        if order_type == "SL-M":
            return self.add_sl_order(tradingsymbol, transaction_type, trigger_price, quantity)
        # MARKET fills instantly and moves quantity by the signed amount, so the same
        # path models an ENTRY (0 -> +/-N), a SCALE-IN, and an EXIT (+/-N -> 0).
        self._oid += 1
        fill = self._quotes.get(f"NSE:{tradingsymbol}", {}).get("last_price", 0.0)
        delta = quantity if transaction_type == "BUY" else -quantity
        row = next((p for p in self._positions["day"]
                    if p["tradingsymbol"] == tradingsymbol), None)
        if row is None:
            self.set_position(tradingsymbol, 0)
            row = self._positions["day"][-1]
        prior = row["quantity"]
        row["quantity"] = prior + delta
        # Kite reports buy_price/sell_price as the VWAP of that side, so blend it.
        key = "buy_price" if transaction_type == "BUY" else "sell_price"
        done = abs(prior) if (prior != 0 and (prior > 0) == (delta > 0)) else 0
        row[key] = ((row[key] * done) + fill * quantity) / (done + quantity) if row[key] \
            else fill
        return str(self._oid)

    def cancel_order(self, variety, order_id):
        self.cancel_calls.append(order_id)
        self._order(order_id)["status"] = "CANCELLED"
        return order_id
