"""MarketProfile: NSE session hours, squareoff timing, and tick size as one validated
object instead of scattered module constants that nothing checked against each other.

Before this, `SQUAREOFF_AT` (when the daemon exits everything) and `BROKER_SQUAREOFF_AT`
(when the exchange force-squares MIS) were two unrelated time constants — nothing stopped
someone from moving them so close together that the daemon lost the race and the broker's
uncontrolled market fill replaced a managed exit. That relationship is now enforced at
construction time instead of trusted by convention.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def _fmt_minutes(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m" if h else f"{m} min"


@dataclass(frozen=True)
class MarketProfile:
    tz: ZoneInfo
    tick: float
    market_open: time
    market_close: time
    no_new_entries_after: time
    squareoff_at: time                 # when THIS daemon exits everything
    venue_squareoff_at: time           # when the exchange/broker force-squares MIS
    min_squareoff_lead_s: int = 240    # squareoff_at must beat venue_squareoff_at by this much

    def __post_init__(self) -> None:
        lead = (
            datetime.combine(date.today(), self.venue_squareoff_at)
            - datetime.combine(date.today(), self.squareoff_at)
        ).total_seconds()
        if lead < self.min_squareoff_lead_s:
            raise ValueError(
                f"squareoff_at={self.squareoff_at} leaves only {lead:.0f}s before "
                f"venue_squareoff_at={self.venue_squareoff_at} — need at least "
                f"{self.min_squareoff_lead_s}s of head start on the broker's own "
                "force-square, or the daemon can lose the race and an uncontrolled "
                "market fill replaces a managed exit."
            )

    def time_alerts(self) -> dict[str, tuple[time, str]]:
        """One-shot warning alerts timed backward from squareoff_at, so the minute counts
        in their own text can never drift out of sync with however squareoff_at is set."""
        def before(minutes: int) -> time:
            return (datetime.combine(date.today(), self.squareoff_at)
                    - timedelta(minutes=minutes)).time()

        sq = self.squareoff_at.strftime("%H:%M")
        venue = self.venue_squareoff_at.strftime("%H:%M")
        return {
            "alert_1400": (before(65), f"{_fmt_minutes(65)} to auto-squareoff ({sq}). "
                                       "Review open positions."),
            "alert_1430": (before(35), f"{_fmt_minutes(35)} left. No new entries from now. "
                                       "Consider exiting losers."),
            "alert_1445": (before(20), f"Exit manually now or hold till the {sq} auto "
                                       f"square-off (broker force-squares at {venue})."),
        }


NSE = MarketProfile(
    tz=ZoneInfo("Asia/Kolkata"),
    tick=0.05,
    market_open=time(9, 15),
    market_close=time(15, 30),
    no_new_entries_after=time(14, 30),
    squareoff_at=time(15, 5),
    venue_squareoff_at=time(15, 10),
)
