# Markets

Session hours, squareoff timing, and holidays — one object (`MarketProfile`,
`src/vigil/market_profile.py`) instead of scattered constants that nothing checked against
each other.

## MarketProfile

```python
@dataclass(frozen=True)
class MarketProfile:
    tz: ZoneInfo
    tick: float
    market_open: time
    market_close: time
    no_new_entries_after: time
    squareoff_at: time                 # when THIS daemon exits everything
    venue_squareoff_at: time           # when the exchange/broker force-squares the product
    min_squareoff_lead_s: int = 240    # squareoff_at must beat venue_squareoff_at by this much
```

`config.py` builds one default instance, `NSE`, and derives `MARKET_OPEN`,
`MARKET_CLOSE`, `NO_NEW_ENTRIES_AFTER`, `SQUAREOFF_AT`, `BROKER_SQUAREOFF_AT`, and
`TIME_ALERTS` from it — every existing call site (`config.SQUAREOFF_AT` etc.) reads the
same as before; the difference is that the relationships between these values are now
enforced, not just documented in a comment next to them.

## The squareoff timing invariant

`__post_init__` raises `ValueError` unless `squareoff_at` leaves at least
`min_squareoff_lead_s` before `venue_squareoff_at`. This exists because the daemon's own
square-off *must* finish before the exchange's own force-square rule — otherwise the
broker wins the race, you get its uncontrolled market fill instead of the daemon's
controlled exit, and any bookkeeping keyed on the daemon's own square-off event races a
position that's already flat. Before this was enforced, nothing stopped someone editing
`SQUAREOFF_AT` from accidentally erasing that head start.

```python
>>> MarketProfile(..., squareoff_at=time(15, 9), venue_squareoff_at=time(15, 10))
ValueError: squareoff_at=15:09:00 leaves only 60s before venue_squareoff_at=15:10:00 —
need at least 240s of head start on the broker's own force-square...
```

## Derived alerts, not hardcoded strings

`MarketProfile.time_alerts()` computes each alert's fire time and message text *backward*
from `squareoff_at` — "1h 5m to auto-squareoff" is computed from the actual configured gap,
not typed once and left to drift if `squareoff_at` ever changes. Move `squareoff_at`
earlier or later and every alert time and every minute count in its text moves with it,
automatically.

## The run-time half of the invariant

A construction-time check only catches a misconfiguration; it says nothing about the daemon
actually *waking up* in time to act on it. `rules.seconds_until_next_action(now, fired)`
returns the number of seconds until the next not-yet-fired alert or the square-off itself,
and `monitor.py`'s `cycle()` clamps its sleep interval to whichever is sooner:

```python
interval = min(cycle_interval, max(seconds_until_next_action, 1))
```

Without this, a cycle finishing just before a scheduled action could sleep a flat interval
straight past it — the same failure mode that motivated moving `squareoff_at` earlier in
the first place (see `docs/incidents/trail-and-sl-lifecycle.md`), just at the run-time
layer instead of the configuration layer. Both layers are needed: construction-time
catches a bad *configuration*; run-time catches the clock being ignored *while running*.

## Holidays

Weekends are automatic (`clock.is_market_day` checks `weekday() >= 5`). Exchange holidays
are not derived from anything — add them to `<state_dir>/data/holidays.txt`, one
`YYYY-MM-DD` per line (`#` starts a comment). A fresh install with no holidays file will
happily treat a holiday as a trading day; `--force` on `vigil start`/`vigil monitor`
overrides the market-hours guard entirely, for testing outside real session hours.

## Tick size

`MarketProfile.tick` (0.05 for NSE) feeds `rules.round_to_tick_favor`, which rounds every
computed SL price to the nearest valid tick, always in the trade's favour (a long's stop
rounds down, a short's rounds up — never toward a worse fill than the raw calculation
produced).
