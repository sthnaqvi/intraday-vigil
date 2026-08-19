"""PriceFeed: how the daemon learns a symbol's latest traded price, decoupled from what
it does with that price (that's TriggerEngine, in triggers.py — it doesn't know or care
whether a price came from a WebSocket tick or a polled quote).

Two implementations: KiteTickerFeed (push, via Kite's tick WebSocket — low latency, needs
instrument tokens and a WebSocket) and PollingFeed (pull, built on Broker.quotes() — every
broker has that, so every broker gets trigger-watching for free, just on cycle cadence
instead of real time).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Protocol

from . import config, levels
from .events import EventLog, logger


class PriceFeed(Protocol):
    """A push feed: once started, calls on_price(symbol, ltp, source) from its own
    thread — asynchronously, with no cadence the caller controls — until stop()."""

    def start(self, symbols: list[str],
             on_price: Callable[[str, float, str], None]) -> bool: ...

    def stop(self) -> None: ...


class KiteTickerFeed:
    """Wraps KiteTicker. Never fatal to construct or fail to start — start() returning
    False (no ticker library, no instrument tokens, nothing armed) just means the caller
    should fall back to polling instead."""

    def __init__(self, kite_read: Any, events: EventLog):
        self.kite_read = kite_read  # anything with .instrument_tokens-compatible reads
        self.events = events
        self.ticker = None
        self.token_to_symbol: dict[int, str] = {}
        self._running = False
        self._on_price: Callable[[str, float, str], None] | None = None

    def start(self, symbols: list[str],
             on_price: Callable[[str, float, str], None]) -> bool:
        if not symbols:
            return False
        try:
            from kiteconnect import KiteTicker
        except Exception as e:  # pragma: no cover
            self.events.emit("WARNING", message=f"KiteTicker unavailable ({e}) — "
                                                "falls back to polling")
            return False

        mapping = levels.instrument_tokens(self.kite_read, symbols)
        missing = [s for s in symbols if s not in mapping]
        if missing:
            self.events.emit("WARNING", message=f"no instrument token for {missing}")
        if not mapping:
            return False
        self.token_to_symbol = {v: k for k, v in mapping.items()}
        self._on_price = on_price

        token_file = json.loads(config.TOKEN_FILE.read_text())
        self.ticker = KiteTicker(token_file["api_key"], token_file["access_token"])
        tokens = list(self.token_to_symbol)

        def on_connect(ws, _response):
            ws.subscribe(tokens)
            ws.set_mode(ws.MODE_LTP, tokens)
            self.events.emit("TICKER_CONNECTED", symbols=symbols, tokens=tokens)

        def on_ticks(_ws, ticks):
            try:
                self._on_ticks(ticks)
            except Exception as e:  # never let a bad tick kill the socket
                self.events.emit("ERROR", message=f"tick handler failed: {e!r}")

        def on_close(_ws, code, reason):
            self.events.emit("TICKER_CLOSED", code=str(code), reason=str(reason))

        def on_error(_ws, code, reason):
            self.events.emit("TICKER_ERROR", code=str(code), reason=str(reason))

        self.ticker.on_connect = on_connect
        self.ticker.on_ticks = on_ticks
        self.ticker.on_close = on_close
        self.ticker.on_error = on_error
        # threaded=True keeps the reactor off the monitor loop's thread; reconnects are
        # handled by KiteTicker itself.
        self.ticker.connect(threaded=True, disable_ssl_verification=False)
        self._running = True
        logger.info("[TICKER] watching %s", ", ".join(symbols))
        return True

    def _on_ticks(self, ticks: list[dict]) -> None:
        if self._on_price is None:
            return
        for tick in ticks:
            symbol = self.token_to_symbol.get(tick.get("instrument_token"))
            ltp = tick.get("last_price")
            if symbol and ltp is not None:
                self._on_price(symbol, float(ltp), "ws")

    def stop(self) -> None:
        if self.ticker is not None and self._running:
            try:
                self.ticker.close()
            except Exception:
                pass
            self._running = False


class PollingFeed:
    """Pull feed built on Broker.quotes() — works for any broker, no WebSocket needed.
    Not a PriceFeed: it has no background thread of its own, so it's driven by the
    caller's own cadence via poll() rather than start()/stop()."""

    def __init__(self, broker: Any):
        self.broker = broker

    def poll(self, symbols: list[str],
            on_price: Callable[[str, float, str], None]) -> None:
        if not symbols:
            return
        quotes = self.broker.quotes([f"NSE:{s}" for s in symbols])
        for s in symbols:
            q = quotes.get(f"NSE:{s}")
            if q:
                on_price(s, float(q.last_price), "poll")
