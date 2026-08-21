# Squareoff timing: why the default moved from 15:05 to 15:00

One-line summary: preponing the daemon's own scripted square-off (`SQUAREOFF_AT`) from
15:05 to 15:00 is supported by historical data — moving it *later* is not.

## Context

Two sessions (2026-08-20 and 2026-08-21 — see `docs/incidents/discipline-and-process.md`)
hit the same mistake: the daemon was stopped a few minutes before its own scripted
square-off could fire, handing the exit to the exchange's own forced closure
(`venue_squareoff_at`, 15:10) instead of a controlled one. One session lost real money on
it (₹1,046); the other was saved only by which way price happened to drift in the gap —
the same decision, opposite luck.

That raised a question worth answering with data instead of opinion: is 15:05 even the
right time for the daemon's own exit to fire, or should it move? Two sub-questions follow
directly from the incidents themselves — should it move *later* (to buy more time before
deciding), or *earlier* (to shrink the window where a stopped daemon leaves a position
exposed to the forced close)?

## Methodology

25 liquid NSE stocks (KOTAKBANK, OBEROIRLTY, DRREDDY, HDFCBANK, ICICIBANK, TCS, INFY,
RELIANCE, MARUTI, HINDUNILVR, TATASTEEL, SUNPHARMA, ADANIGREEN, SBIN, WIPRO, HCLTECH,
BAJAJ-AUTO, CIPLA, ITC, JSWSTEEL, HINDALCO, BPCL, DLF, INDIGO, TATAPOWER), 1-minute candles,
44 trading days (2026-06-22 to 2026-08-21), window 14:57–15:12 IST — bracketing both the
current 15:00 default and the incidents' actual 15:02–15:04 stop-time and the 15:10 forced
close. 1,100 stock-days total. Metric throughout: % price move relative to a chosen
reference minute, reported as mean (expected value) and population stdev (risk/variance).

## Methodology correction — a bug caught mid-research

The first pass at classifying "pre-holiday" trading days excluded weekends from the "next
day is closed" check, so it only caught genuine mid-week exchange holidays — every ordinary
Friday (followed by a 2-day Sat/Sun closure, exactly the kind of session a pre-holiday
classification should include) was silently skipped. That undercounted the sample from
8 real pre-holiday trading days down to 1, which is not a usable sample size for any
conclusion. Caught after being pointed out directly, fixed by classifying a trading day as
pre-holiday whenever the market is not open the very next calendar day, for any reason —
correctly picking up 7 ordinary Fridays plus 1 mid-week holiday eve. Every pre-holiday
figure below uses the corrected classifier. This is documented here rather than silently
folded in because it's exactly the kind of thing a future reader needs to know was checked,
not assumed.

## Finding 1 — moving `SQUAREOFF_AT` later has no benefit

Anchored at 15:04 (13 stocks first, then the full 25-stock set gave the same shape),
looking at candidate exit times out to 15:12:

| Exit minute | Mean % vs 15:04 | Stdev % |
|---|---|---|
| 15:05 (old default) | −0.0017 | 0.0702 |
| 15:07 | +0.0004 | 0.1042 |
| 15:10 (broker forced close) | +0.0024 | 0.1354 |
| 15:12 | +0.0053 | 0.1513 |

Mean stays within noise of zero at every candidate; stdev more than doubles from 15:05 to
15:12. This is the signature of a random walk with no drift — variance grows with elapsed
time, expected value doesn't move. **There is no minute in this window where waiting longer
produces a reliably better exit price**, so nothing here supports moving `SQUAREOFF_AT`
later toward the broker's 15:10 deadline.

## Finding 2 — moving it earlier does help, and it's why 15:00 was chosen

Widening the window to start at 14:57 (25-stock set, 1,100 stock-days) and looking at the
move *into* 15:04 rather than out of it:

| Minute (anchor 14:57) | Mean % | Stdev % |
|---|---|---|
| 14:58 | −0.0095 | 0.0568 |
| 15:00 | −0.0198 | 0.1445 |
| **15:04** (incident stop-time) | **−0.0339** | **0.1897** |
| 15:05 (old default) | −0.0358 | 0.1963 |

The mean drifts consistently negative from 14:57 through ~15:05 — a real, if small,
directional effect, not noise. Splitting it further: essentially all of that drift happens
**before** 15:04 (0 → −0.034%); from 15:04 to 15:12 the mean is flat again (matches Finding
1). In other words: **real close-in risk starts building from ~15:00, not from 15:04** where
the actual incidents happened to occur. That's the direct reason 15:00 was picked as the new
default — it exits before most of this window's risk accrues, rather than after.

Measured directly as "prepone vs. the actual 15:04 incident stop-time":

| Candidate squareoff | Mean % vs 15:04 | Stdev % vs 15:04 |
|---|---|---|
| 14:57 | +0.0343 | 0.1900 |
| **15:00** | **+0.0143** | **0.1252** |
| 15:02 | +0.0067 | 0.0812 |
| 15:03 | +0.0043 | 0.0587 |

15:00 cuts variance by roughly a third relative to 15:04/15:05, while the sign of the mean
move stays in the position's favor (positive = a long captured a *better* price exiting
earlier, not a worse one) — moving earlier isn't a trade-off here, it's a win on both axes
inside this sample.

## Finding 3 — VIX predicts variance even inside the "Low" band (follow-up, not implemented)

Splitting the 44 days into relative VIX terciles (the whole sample sat between 9.3 and
15.2 — entirely within this project's own "Low" (<15) band; there is no genuinely elevated
VIX day in this dataset):

| VIX tercile | Stdev % @ 15:10 (vs 14:57 anchor) |
|---|---|
| Lower (10.76–11.93) | 0.1954 |
| Higher (13.15–14.68) | 0.2837 |

~45% wider spread in the higher-relative-VIX tercile, measured entirely within what the
skill's current sizing logic (`skill/intraday-vigil/references/mode-start.md`'s static
Low/Moderate/Elevated/High bands) treats as one undifferentiated "Low → normal sizing"
bucket. The prepone-vs-15:04 benefit from Finding 2 also held up across every VIX tercile,
and was *stronger* on the higher-relative-VIX days (+0.036% mean benefit at 15:00 vs.
+0.004% in the lower tercile) — closing early matters more, not less, exactly when
volatility is already a bit elevated.

**This is a real, separate finding, not addressed by the `SQUAREOFF_AT` change in this doc
and not implemented anywhere in this repo yet.** Acting on it would mean adding
relative/continuous VIX-awareness to the skill's sizing logic — a distinct design task from
moving one config constant. Recorded here explicitly as a follow-up recommendation so it
isn't lost.

## Pre-holiday caveat

Using the corrected classifier (7 Fridays + 1 holiday eve = 8 days, 200 stock-observations):

| | Mean % vs 15:04 @ 15:00 | Stdev % @ 15:00 |
|---|---|---|
| Pre-holiday | −0.0008 | 0.1131 |
| Normal day | −0.0177 | 0.1274 |

Both the drift and the variance-reduction benefit are smaller and noisier on pre-holiday
Fridays than on normal days — "% better than 15:04" sits close to a coin flip there rather
than the clear lean seen on normal days. Stated plainly: the case for preponing is stronger
on ordinary sessions than on Fridays before a weekend, and 8 days is still a modest sample
for a day-type effect. Not a reason to hold off on the change (normal days are the large
majority of the sample and show the effect clearly), but a real, named caveat rather than a
hidden one.

## Recommendation

**`SQUAREOFF_AT` = 15:00** (was 15:05). A round number was picked over the empirical
15:00–15:01 optimum for simplicity and because the difference between the two is well
within the sample's own noise. This leaves ~10 minutes of head start before the broker's
15:10 forced close, up from ~5 — comfortably clear of the `min_squareoff_lead_s` (240s)
invariant `MarketProfile.__post_init__` enforces.

## What would change this conclusion

- **A genuinely elevated-VIX day** (>20, or even >25) in the sample — this dataset has none,
  so Finding 3's "stronger benefit at higher VIX" claim is only validated within the calm
  regime, not under real volatility stress.
- **A larger pre-holiday sample** — 8 days is enough to notice the pattern, not enough to
  trust it the way the 900+ normal-day sample can be trusted.
- **A different asset mix** — all 25 names here are liquid, large/mid-cap NSE equities;
  thinner or more volatile names weren't tested and may not show the same shape.

Whoever revisits this with a bigger dataset should start here rather than re-deriving it
from scratch.
