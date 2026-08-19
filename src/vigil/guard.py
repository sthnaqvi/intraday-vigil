"""GuardedBroker: wraps any BrokerClient adapter by composition and owns every
cross-cutting safety concern — dry-run gate, call spacing, retry with backoff, and audit
logging. An adapter author cannot bypass any of this by overriding a method, because
none of it lives in a class the adapter inherits from; GuardedBroker calls the adapter's
methods, never the other way around.
"""
from __future__ import annotations

import time as _time
from datetime import date

from . import audit, config
from .events import EventLog
from .models import Order, Position, Quote
from .ports import BrokerClient

try:  # allow tests to run without the package's network deps mattering
    from kiteconnect.exceptions import NetworkException, TokenException
except Exception:  # pragma: no cover
    class NetworkException(Exception):
        pass

    class TokenException(Exception):
        pass


class GuardedBroker:
    def __init__(self, adapter: BrokerClient, events: EventLog, dry_run: bool = False):
        self.adapter = adapter
        self.events = events
        self.dry_run = dry_run
        self._last_call = 0.0
        self._dry_seq = 0

    @property
    def adapter_kind(self) -> str:
        """A short, display-friendly label for whatever adapter this wraps — derived from
        its class name, not hardcoded per broker, so a new adapter gets a sensible label
        for free. Used anywhere a user needs to be told, unambiguously, whether a session
        is against a real account (e.g. "kite") or a simulated one (e.g. "paper") — a
        confusion here is exactly the kind of thing that must never happen quietly."""
        name = type(self.adapter).__name__
        return name.removesuffix("Adapter").lower() or name.lower()

    # ---------- plumbing ----------

    def _space(self) -> None:
        wait = config.API_MIN_SPACING_S - (_time.monotonic() - self._last_call)
        if wait > 0:
            _time.sleep(wait)
        self._last_call = _time.monotonic()

    # Mutations are logged with their full request and response. Reads are logged too —
    # you cannot debug "why did the daemon think that" without seeing what it read — but
    # summarised, because full quote payloads every 150s would bury the signal.
    _MUTATIONS = {"place_market_order", "place_stop_order", "modify_stop_order", "cancel_order"}

    def _api_log(self, fn_name: str, args, kwargs, result=None, error=None,
                 ms: float | None = None, attempt: int = 0) -> None:
        """One broker call -> logs/api.jsonl. Best-effort; never breaks the caller."""
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
        audit.api("broker.call", **rec)

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
                # Non-network broker errors (rejections, validation) are not retryable
                self._api_log(fn_name, args, kwargs, error=e,
                              ms=(_time.monotonic() - started) * 1000, attempt=i)
                raise
        raise last_err  # type: ignore[misc]

    # ---------- reads ----------

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return self._call(self.adapter.quotes, symbols)

    def positions_day(self) -> list[Position]:
        return self._call(self.adapter.positions_day)

    def orders(self) -> list[Order]:
        return self._call(self.adapter.orders)

    def margins(self) -> dict:
        return self._call(self.adapter.margins)

    def instruments(self, exchange: str = "NSE") -> list[dict]:
        return self._call(self.adapter.instruments, exchange)

    def historical_daily(self, instrument_token: int, from_date: date, to_date: date) -> list[dict]:
        return self._call(self.adapter.historical_daily, instrument_token, from_date, to_date)

    # ---------- mutations (dry-run gated) ----------

    def _dry(self, action: str, **params) -> str:
        self._dry_seq += 1
        self.events.emit("DRY_RUN_INTENT", params.get("symbol"), action=action, **params)
        return f"DRY-{self._dry_seq}"

    def place_market_order(self, symbol: str, transaction_type: str, quantity: int,
                           exchange: str = "NSE") -> str:
        assert quantity >= 1
        if self.dry_run:
            return self._dry("place_market_order", symbol=symbol,
                             transaction_type=transaction_type, quantity=quantity)
        return self._call(self.adapter.place_market_order, symbol, transaction_type,
                          quantity, exchange)

    def place_stop_order(self, symbol: str, transaction_type: str, trigger_price: float,
                         quantity: int, exchange: str = "NSE") -> str:
        if self.dry_run:
            return self._dry("place_stop_order", symbol=symbol,
                             transaction_type=transaction_type, trigger_price=trigger_price,
                             quantity=quantity)
        return self._call(self.adapter.place_stop_order, symbol, transaction_type,
                          trigger_price, quantity, exchange)

    def modify_stop_order(self, order_id: str, trigger_price: float, quantity: int) -> str:
        """Modify an SL order. ALWAYS pass quantity — Kite defaults to 1 if omitted."""
        assert quantity >= 1
        if self.dry_run:
            return self._dry("modify_stop_order", order_id=order_id,
                             trigger_price=trigger_price, quantity=quantity)
        return self._call(self.adapter.modify_stop_order, order_id, trigger_price, quantity)

    def cancel_order(self, order_id: str) -> str:
        if self.dry_run:
            return self._dry("cancel_order", order_id=order_id)
        return self._call(self.adapter.cancel_order, order_id)
