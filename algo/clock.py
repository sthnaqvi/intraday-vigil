"""IST time helpers and market-hours guard."""
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config

IST = ZoneInfo("Asia/Kolkata")


def now_ist() -> datetime:
    return datetime.now(IST)


def load_holidays(path: Path = config.HOLIDAYS_FILE) -> set[date]:
    holidays: set[date] = set()
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.split("#")[0].strip()
            if line:
                holidays.add(date.fromisoformat(line))
    return holidays


def is_market_day(d: date, holidays: set[date] | None = None) -> bool:
    if d.weekday() >= 5:
        return False
    return d not in (holidays if holidays is not None else load_holidays())


def is_market_open(dt: datetime, holidays: set[date] | None = None) -> bool:
    return (
        is_market_day(dt.date(), holidays)
        and config.MARKET_OPEN <= dt.time() <= config.MARKET_CLOSE
    )
