"""Kite wrapper: the ONLY module that mutates broker state.

Adds: dry-run gate (Kite has no sandbox), call spacing, retry with backoff on
network errors, and reject->replace protection for SL orders.
"""
from __future__ import annotations

import time as _time
from typing import Any

from . import audit, clock, config, mapping
from .events import EventLog
from .models import Order, Position, Quote
from .rules import Direction

try:  # allow tests to run without the package's network deps mattering
    from kiteconnect.exceptions import NetworkException, TokenException
except Exception:  # pragma: no cover
    class NetworkException(Exception):
        pass

    class TokenException(Exception):
        pass


class Broker:
    def __init__(self, kite: Any, events: EventLog, dry_run: bool = False):
        self.kite = kite
        self.events = events
        self.dry_run = dry_run
        self._last_call = 0.0
        self._dry_seq = 0

    # ---------- plumbing ----------

    def _space(self) -> None:
        wait = config.API_MIN_SPACING_S - (_time.monotonic() - self._last_call)
        if wait > 0:
            _time.sleep(wait)
        self._last_call = _time.monotonic()

    # Mutations are logged with their full request and response. Reads are logged too —
    # you cannot debug "why did the daemon think that" without seeing what it read — but
    # summarised, because full quote payloads every 150s would bury the signal.
    _MUTATIONS = {"place_order", "modify_order", "cancel_order"}

    def _api_log(self, fn_name: str, args, kwargs, result=None, error=None,
                 ms: float | None = None, attempt: int = 0) -> None:
        """One Kite call -> logs/api.jsonl. Best-effort; never breaks the caller."""
        mutation = fn_name in self._MUTATIONS
        rec: dict = {
            "fn": fn_name,
            "mutation": mutation,
            "ms": round(ms, 1) if ms is not None else None,
        }
        if attempt:
            rec["attempt"] = attempt
        if args:
            rec["args"] = [str(a) for a in args] if mutation else audit.summarise(list(args))
        if kwargs:
            rec["kwargs"] = ({k: str(v) for k, v in kwargs.items()} if mutation
                             else audit.summarise(kwargs))
        if error is not None:
            rec["ok"] = False
            rec["error"] = f"{type(error).__name__}: {error}"
        else:
            rec["ok"] = True
            rec["result"] = str(result) if mutation else audit.summarise(result)
        audit.api("kite.call", **rec)

    def _call(self, fn, *args, **kwargs):
        fn_name = getattr(fn, "__name__", str(fn))
        last_err: Exception | None = None
        for i, backoff in enumerate([0] + config.RETRY_BACKOFF_S):
            if backoff:
                _time.sleep(backoff)
            self._space()
            started = _time.monotonic()
            try:
                result = fn(*args, **kwargs)
                self._api_log(fn_name, args, kwargs, result=result,
                              ms=(_time.monotonic() - started) * 1000, attempt=i)
                return result
            except TokenException as e:
                self._api_log(fn_name, args, kwargs, error=e,
                              ms=(_time.monotonic() - started) * 1000, attempt=i)
                raise
            except NetworkException as e:
                last_err = e
                self._api_log(fn_name, args, kwargs, error=e,
                              ms=(_time.monotonic() - started) * 1000, attempt=i)
            except Exception as e:
                # Non-network Kite errors (rejections, validation) are not retryable
                self._api_log(fn_name, args, kwargs, error=e,
                              ms=(_time.monotonic() - started) * 1000, attempt=i)
                raise
        raise last_err  # type: ignore[misc]

    # ---------- reads ----------

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        raw = self._call(self.kite.quote, symbols)
        return {k: mapping.quote_from_kite(v) for k, v in raw.items()}

    def positions_day(self) -> list[Position]:
        raw = self._call(self.kite.positions)["day"]
        return [mapping.position_from_kite(p) for p in raw]

    def orders(self) -> list[Order]:
        raw = self._call(self.kite.orders)
        return [mapping.order_from_kite(o) for o in raw]

    def margins(self) -> dict:
        return self._call(self.kite.margins)

    def instruments(self, exchange: str = "NSE") -> list[dict]:
        return self._call(self.kite.instruments, exchange)

    def historical_daily(self, instrument_token: int, from_date, to_date) -> list[dict]:
        return self._call(self.kite.historical_data, instrument_token, from_date, to_date, "day")

    # ---------- mutations (dry-run gated) ----------

    def _dry(self, action: str, **params) -> str:
        self._dry_seq += 1
        self.events.emit("DRY_RUN_INTENT", params.get("tradingsymbol"), action=action, **params)
        return f"DRY-{self._dry_seq}"

    def modify_sl(self, order_id: str, trigger_price: float, quantity: int) -> str:
        """Modify an SL order. ALWAYS passes quantity — Kite defaults to 1 if omitted."""
        assert quantity >= 1
        if self.dry_run:
            return self._dry("modify_order", order_id=order_id,
                             trigger_price=trigger_price, quantity=quantity)
        return self._call(
            self.kite.modify_order,
            variety="regular",
            order_id=order_id,
            order_type="SL-M",
            trigger_price=trigger_price,
            quantity=quantity,
            market_protection=config.MARKET_PROTECTION_PCT,
        )

    def place_sl(self, symbol: str, direction: Direction, trigger_price: float,
                 quantity: int, exchange: str = "NSE") -> str:
        """Fresh SL-M protecting an open position (side opposite to the position)."""
        txn = "SELL" if direction == Direction.LONG else "BUY"
        if self.dry_run:
            return self._dry("place_order", tradingsymbol=symbol, transaction_type=txn,
                             order_type="SL-M", trigger_price=trigger_price, quantity=quantity)
        return self._call(
            self.kite.place_order,
            variety="regular",
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=txn,
            quantity=quantity,
            product="MIS",
            order_type="SL-M",
            trigger_price=trigger_price,
        )

    def place_entry(self, symbol: str, direction: Direction, quantity: int,
                    exchange: str = "NSE") -> str:
        """Open a MIS position at market. LONG -> BUY, SHORT -> SELL.

        Exists so entries never depend on the Claude MCP session, whose token expires
        roughly hourly; the daemon's own token lasts the whole trading day.
        """
        assert quantity >= 1
        txn = "BUY" if direction == Direction.LONG else "SELL"
        if self.dry_run:
            return self._dry("place_order", tradingsymbol=symbol, transaction_type=txn,
                             order_type="MARKET", quantity=quantity)
        return self._call(
            self.kite.place_order,
            variety="regular",
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=txn,
            quantity=quantity,
            product="MIS",
            order_type="MARKET",
            market_protection=config.MARKET_PROTECTION_PCT,
        )

    def place_market_exit(self, symbol: str, direction: Direction, quantity: int,
                          exchange: str = "NSE") -> str:
        txn = "SELL" if direction == Direction.LONG else "BUY"
        if self.dry_run:
            return self._dry("place_order", tradingsymbol=symbol, transaction_type=txn,
                             order_type="MARKET", quantity=quantity)
        return self._call(
            self.kite.place_order,
            variety="regular",
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type=txn,
            quantity=quantity,
            product="MIS",
            order_type="MARKET",
            market_protection=config.MARKET_PROTECTION_PCT,
        )

    def cancel(self, order_id: str) -> str:
        if self.dry_run:
            return self._dry("cancel_order", order_id=order_id)
        return self._call(self.kite.cancel_order, variety="regular", order_id=order_id)
