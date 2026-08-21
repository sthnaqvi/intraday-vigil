# Sector Macro Map — Theme → Rank Adjustments

After computing raw sector ranks from live momentum, apply these rank shifts based on the selected macro theme. Positive = rank improves (more bullish → more likely selected for longs). Negative = rank drops.

## crude-down (Oil price falling globally)
- Aviation:          +2  (fuel bill drops → IndiGo/SpiceJet margins expand)
- FMCG:             +1  (lower logistics/input energy costs)
- Auto:             +1  (lower manufacturing energy costs)
- Energy/Oil & Gas: -2  (crude revenue falls)

## crude-up (Oil price rising globally)
- Energy/Oil & Gas: +2  (upstream producers gain — crude revenue rises)
  - **Within-sector split (critical):** rising crude helps *upstream* (ONGC — sells crude
    at higher prices) but SQUEEZES *OMCs* (BPCL/HPCL/IOC — buy crude dear, retail prices
    sticky → marketing margins crushed). On crude-up: long ONGC-type names, never long
    BPCL-type names (BPCL is a short candidate). RELIANCE is mixed (refining + upstream).
- Metal:            +1  (commodity inflation lifts metals)
- Aviation:         -2  (fuel costs spike → airline margins crushed)
- FMCG:            -1  (higher logistics costs)
- Auto:            -1  (higher energy costs in production)

## fii-buying (Foreign funds buying India)
- Banking:  +2  (FIIs prefer large-cap financials: HDFC, ICICI, SBI)
- IT:        +2  (FIIs prefer large-cap IT: Infy, TCS)
- Realty:   +1  (rate-sensitive, benefits from risk-on sentiment)

## fii-selling (Foreign funds selling India)
- Banking:  -2  (FIIs sell financials first on outflow)
- IT:        -2  (FIIs exit IT holdings)
- All:      -0.5 (broad market pressure — tilt toward shorts across board)

## rate-event (RBI / US Fed announcement today)
- Banking:  ±2  (direction unclear until announcement — wait, then reassess)
- Realty:   ±1  (rate sensitive in both directions)
- **Critical action**: Halve ALL position sizes regardless of direction. SL decisions are
  already tick-driven, not something to speed up — instead, check `vigil status --json`
  for `daemon.broker` and recent `TICKER_CONNECTED`/`TICKER_RESUBSCRIBED` events to confirm
  the tick feed is actually live and not silently degraded to the slower poll fallback, and
  check in via MONITOR more often yourself through the volatile window.

## sector-news (Big news in one specific sector)
- Affected sector: +3 (or -3 if news is negative/bearish)
- All others: 0

## no-clear-theme
- No adjustments. Use raw momentum ranking.

---

## Application Logic

```python
# After ranking sectors 1–11 by momentum:
for sector in all_sectors:
    adjustment = macro_map[theme].get(sector, 0)
    adjusted_rank[sector] = raw_rank[sector] - adjustment  # lower number = better rank

# Re-sort by adjusted_rank
# Clamp to 1–11 range
# Break ties by raw momentum score
# Pick top 3 for longs (or bottom 3 for shorts based on bias)
```
