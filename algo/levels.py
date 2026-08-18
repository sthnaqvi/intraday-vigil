"""PDH/PDL levels for the stop-hunt guard.

Priority: risk.json seeds (skill fetches these via MCP in the morning)
-> Kite historical API (paid add-on; may be unavailable on a personal key)
-> today's running day-H/L only (from the quote response), with a warning.
"""
from __future__ import annotations

import csv
from datetime import timedelta

from . import clock, config
from .broker import Broker
from .events import EventLog


def _instruments_cache_path():
    return config.DATA_DIR / f"instruments-{clock.now_ist().date().isoformat()}.csv"


def instrument_tokens(broker: Broker, symbols: list[str]) -> dict[str, int]:
    """NSE tradingsymbol -> instrument_token, cached to a daily CSV."""
    path = _instruments_cache_path()
    mapping: dict[str, int] = {}
    if path.exists():
        with path.open() as f:
            for row in csv.reader(f):
                mapping[row[0]] = int(row[1])
    else:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        rows = broker.instruments("NSE")
        with path.open("w", newline="") as f:
            writer = csv.writer(f)
            for r in rows:
                writer.writerow([r["tradingsymbol"], r["instrument_token"]])
                mapping[r["tradingsymbol"]] = int(r["instrument_token"])
    return {s: mapping[s] for s in symbols if s in mapping}


def fetch_pdh_pdl(broker: Broker, symbol: str, token: int, events: EventLog) -> tuple[float, float] | None:
    """Previous trading day's high/low from daily candles. None if unavailable."""
    today = clock.now_ist().date()
    try:
        candles = broker.historical_daily(token, today - timedelta(days=7), today - timedelta(days=1))
        if candles:
            last = candles[-1]
            return float(last["high"]), float(last["low"])
    except Exception as e:
        events.emit(
            "WARNING", symbol,
            message=f"historical_data unavailable ({e}) — stop-hunt guard will use "
                    "risk.json seeds or today's day-H/L only",
        )
    return None
