# MODE: START

Full pre-market to first-trade workflow.

## Step 1 — Bring the daemon up (this is the login that matters)

**Do this first.** The daemon's broker session is the one trading depends on: `vigil
start`. Run it via Bash **as a background task** (idempotent — safe to repeat; it refuses
duplicates). It's backgrounded because if the broker token is dead (Kite tokens die daily,
roughly 6am IST) the command opens a browser tab and waits for login — tell the user to
complete it, then confirm with `vigil status`. With a valid token it returns in seconds.

Then, optionally, bring up the MCP session for research reads: call the broker MCP's
margins/profile tool. If it says you need to log in, call its login tool and give the user
the link **it** returns — never a hand-written broker URL, which won't authorise the MCP
server. Do **not** block the session on this and do **not** poll for it: the daemon can
already quote, enter, arm, and manage stops without it. Mention it once, carry on, and
re-offer the link if the user later wants MCP-only conveniences.

Expect the MCP token to die roughly hourly. That is normal and is not a trading incident.

## Step 2 — Automatic opening bias (no user input needed)

Run in parallel:
- Broker MCP quotes for the index and volatility index (e.g. `NSE:NIFTY 50`,
  `NSE:INDIA VIX` for Zerodha/NSE).
- Broker MCP historical data for the index, from yesterday to today.

**Index gap computation:**
```
gap_pct = (today_open - prev_close) / prev_close × 100
```

| Gap | Label | Bias |
|---|---|---|
| > +0.5% | Strong bullish open | Long bias |
| +0.2 to +0.5% | Mild bullish open | Slight long |
| ±0.2% | Flat open | Neutral |
| -0.2 to -0.5% | Mild bearish open | Slight short |
| < -0.5% | Strong bearish open | Short bias |

**Volatility-index auto-action (no user input, applied automatically):**
- Low (e.g. VIX < 15) → normal sizing
- Moderate (e.g. VIX 15–20) → normal (note in output)
- Elevated (e.g. VIX 20–25) → **auto half-size** — tell the user sizing was halved
- High (e.g. VIX > 25) → **auto quarter-size** — tell the user sizing was quartered

(Thresholds above are this project's defaults — recalibrate for your market's own
volatility index.)

Display a clean summary:
```
📊 Opening Read (auto)
NIFTY: <level> (+0.3% gap) → Mild bullish open
VIX: <level> → Normal sizing
Bias: Mild long preference today
```

## Step 3 — Macro theme (single prompt with auto-suggestion)

Auto-suggest a theme based on gap + volatility data, then show options with layman
descriptions for confirmation:

```
Pick today's macro theme (or accept auto-suggestion):

1. crude-down     — Oil falling globally (airlines' fuel bill drops → aviation stocks rise)
2. crude-up       — Oil rising globally (upstream producers gain on crude revenue;
                    marketing-margin businesses get squeezed; airlines hurt)
3. fii-buying     — Foreign funds buying today (large-cap financials/IT typically lead)
4. fii-selling    — Foreign funds selling (market under pressure, favour shorts)
5. rate-event     — Central bank announcement today (volatile day → all sizes halved auto)
6. sector-news    — Big news in one specific sector (you'll name it)
7. no-clear-theme — Normal day, no special catalyst

💡 AUTO-SUGGESTED: [N] — Why: [one-line reason derived from gap/VIX/global context]
```

If "rate-event" is selected, apply an additional ×0.5 to all position sizes on top of the
volatility adjustment.

## Step 4 — Dynamic sector selection (auto-ranked)

Read `references/sector-universe.md` for the full stock/token list.

Batch-fetch live quotes for all sector representative stocks via the broker MCP.

For each stock:
```
pct_from_open = (LTP - ohlc.open) / ohlc.open × 100
```
Average per sector → rank every sector (1 = strongest positive momentum).

Apply macro-bias adjustments from `references/sector-macro-map.md`.

**Select the top 3 sectors** based on bias:
- Bullish bias → top 3 positive-momentum sectors for longs
- Bearish bias → bottom 3 (most negative) sectors for shorts
- Neutral → top 2 longs + bottom 1 short (or vice versa, based on gap strength)

**HARD RULE — direction discipline (enforced as a gate here, not applied after the pick):**
- Strong bullish gap (>+0.5%): all 3 picks must be longs. A short is only permitted if the
  sector is firmly in the bottom 3 AND the gap is at most neutral (±0.2%). Never short a
  mid-rank sector on a bull-gap day.
- Strong bearish gap (<−0.5%): all 3 picks must be shorts. Same logic reversed.
- **Any bearish gap (< −0.2%): maximum 1 long.** Favour a 2:1 short:long mix. Fighting a
  bearish tape with multiple longs is a proven loser.
- See `docs/incidents/discipline-and-process.md` for what skipping this gate cost once.

Display the sector ranking table. Confirm with the user or let them adjust.

## Step 5 — Stock scoring (per sector, 2–3 candidates)

For each of the 3 chosen sectors, score 2–3 candidates on 6 points:

| Point | Criteria |
|---|---|
| 1 | Trend alignment — LTP above/below 20-day SMA |
| 1 | Volume confirmation — today's volume, **pro-rated to a full session**, > 20-day average |
| 1 | Sector momentum — sector ranked top/bottom 3 |
| 1 | Index alignment — gap direction matches trade direction |
| 1 | Clean chart — no S/R level within 1% of entry |
| 1 | ORB breakout — price broke the opening-range candle's H/L (only check after the range closes) |

Max score 6. Recommend the highest scorer per sector. Show the table.

**Pro-rate the volume check.** Comparing a part-day volume against a full-day average
scores almost everything as "low" for most of the morning:
`projected = today_vol × session_minutes / minutes_elapsed_since_open`. Compare *that* to
the average.

**A positions API can report each position twice** (a broker's "net" and "day" buckets
flattened into one list). Read the quantity from a single row — never sum the rows, or the
position size gets double-counted.

**Counter-trend short check (mandatory for every SHORT candidate):** before recommending
any short, fetch today's 15-minute candles. If the stock has already made its big drop and
is now **basing** — sideways, low-range candles hugging the day low, no new low in the last
3–4 candles — **skip the short**. Shorting into a base after the move is done is chasing;
the risk/reward is gone and a mean-reversion bounce is the likely next move. Pick the next
candidate or reduce to fewer positions.

## Step 5b — Armed triggers (when scoring finds no confirmed setup)

If no candidate has a confirmed setup (longs inside the opening range, shorts basing), do
NOT force entries. Arm the level and **hand it to the daemon** — never watch it yourself.

```
vigil arm RELIANCE --side long --above 1328.60 --qty 590 --sl-pct 0.91 \
  --pdh 1320.8 --pdl 1298.1 [--auto]
```

The daemon subscribes to that symbol on the broker's price feed and reacts the moment the
level breaks — no multi-minute polling latency. It applies the same entry gate (kill
switch, `no_new_entries`, hard cutoff), re-derives the SL from the **actual fill** with the
stop-hunt guard, places the SL, and writes the risk seed. A cycle-cadence poll inside the
daemon is the fallback if the push feed drops. `vigil arm` needs a daemon restart to
subscribe to a newly-armed symbol.

- **Without `--auto`** (default): the daemon fires a notification + alert and places
  nothing. Use this unless the user has explicitly pre-authorised the entry.
- **With `--auto`**: the daemon places entry + SL itself. Only arm this way when the user
  has said yes to that specific trade in advance.

Compute qty/SL/risk per Step 6 before arming so the user sees the numbers up front.
`vigil triggers` lists state; `vigil disarm [SYMBOL]` cancels.

**Do NOT run a manual polling loop for triggers.** It adds latency, breaks silently if
interrupted, and makes execution depend on the MCP session's short-lived token. The daemon
owns the clock. Report status when the user asks (MONITOR), or when the daemon logs a
trigger-hit/trigger-fired event.

Sizing note: a margins API sometimes reports `available.cash` as 0 with the real figure
under a different field (Kite: `available.live_balance`) — use whichever field actually
holds the funds, or `net`, as the capital base.

## Step 6 — SL placement & position sizing (stop-hunt guard FIRST, sizing second)

Order matters: the stop-hunt guard can widen the SL, which changes risk per share, which
changes quantity. Never size before the SL is final. Full spec: `docs/sl-rules.md`.

For each selected stock:

1. **Fetch previous-day levels** (also needed for the risk seed in Step 7): the broker's
   historical-data endpoint for the previous session → record PDH (prev day high) and PDL
   (prev day low).
2. **Choose `sl_pct`**: prompt the user or suggest 0.8–1.2% based on the stock's ATR.
   **Never accept `sl_pct` > 1.5%** (see `docs/sl-rules.md` — wide SLs make the entire
   Phase 2/3 lifecycle unreachable; `docs/incidents/trail-and-sl-lifecycle.md` for what
   that looked like in practice).
3. **Compute raw SL price** from the intended entry (current LTP):
   `sl_price = entry × (1 − sl_pct)` for longs / `entry × (1 + sl_pct)` for shorts.
4. **Apply the stop-hunt guard NOW**: if `sl_price` is within 0.3% of PDH, PDL, or a clear
   intraday swing H/L, push it 0.3% beyond that level, further in your favour (below the
   level for longs, above for shorts). This is the **final** SL price.
5. **Size from available margin** — this project's default (see `docs/sl-rules.md` for the
   sizing formula and the alternative fixed-risk-percentage approach, if you'd rather use
   that instead):
   ```
   capital        = available margin (check for the field that actually holds live funds)
   margin_share   = capital allocated to this slot   # split across open + armed slots
   qty            = floor(margin_share / (entry / leverage))   # e.g. MIS ≈ 5x on NSE
   risk_per_share = abs(entry − final_sl_price)
   ```
   Report the resulting risk in currency plainly (`qty × risk_per_share`) so the size is
   never a surprise — but the sizing input is available margin, not a risk budget.

   **Allocation:** do not dump the full budget into one name while another trigger is
   armed. Weight the confirmed setup and reserve for armed slots; ask the user if the
   split is unclear.

   **What does NOT change with size:** the ≤1.5% SL width cap, the stop-hunt guard, and the
   kill switch. R is price-based and quantity-independent, so phases and the kill switch
   behave identically at any size — only the currency magnitude scales. Never widen the
   stop to justify a bigger position; size up on quantity alone.
6. Apply the volatility multiplier automatically (per Step 2's thresholds). If a scheduled
   macro event day: an additional ×0.5 on top.

Show: symbol, direction, qty, entry price, final SL price (flag if the guard moved it),
effective `sl_pct`, max risk in currency. Ask the user to confirm before placing any order.

## Step 7 — Order placement & daemon handoff

**Entry gate first** (see SKILL.md): check the daemon's status flags + the hard cutoff.

**Place entries through the daemon, not MCP.** The MCP token expires in roughly an hour and
can strand a live session mid-trade; the daemon's token lasts the whole day. One command
does entry + guard-adjusted SL + risk-seed write atomically:

```
vigil enter HCLTECH --side short --qty 914 --sl-pct 1.0 --pdh 1362.8 --pdl 1325.0 --yes
```

It refuses `sl_pct > 1.5%`, enforces the entry gate (override only with `--override-gate`),
re-derives the SL from the **actual fill** rather than the pre-trade LTP, and aborts loudly
if the SL leg fails while the position is open. Drop `--yes` to confirm interactively. Then
read back the fill with `vigil positions` and report it.

Use the broker MCP's order-placement tool only if the daemon is unavailable — and say so
explicitly when you do, because it bypasses the seed write and the atomic SL placement.

**Risk seeds.** `vigil enter` and `vigil arm` write these for you. You only write them by
hand after an MCP-placed entry, or when correcting one. Merge — never clobber other
symbols:
```json
{
  "INDIGO":     {"sl_pct": 0.0100, "pdh": 4205.0, "pdl": 4080.0},
  "DEEPAKFERT": {"sl_pct": 0.0101, "pdh": 701.5,  "pdl": 682.0}
}
```
- `sl_pct` = **effective** SL fraction: `abs(entry − final_sl_price) / entry`, rounded to
  4 decimals. Use the effective value (not the nominal input) so the daemon's R and its
  `trail_pct = 2 × sl_pct` match the SL actually resting at the broker.
- `pdh`/`pdl` from Step 6.1. The daemon uses them for its stop-hunt guard on trails.

**If you add to an existing position, the average entry moves** — the effective `sl_pct`
against the resting SL changes with it. Update the seed. The daemon re-reads entry and
`sl_pct` on any qty change and logs it, but the seed is what it reads.

**Then hand off to the daemon.** Check status freshness:
- Fresh → the daemon already running; it will auto-discover the new position within one
  cycle. Note if `daemon.mode` is `dry_run` — SLs will NOT actually trail; warn the user.
- Stale/missing → start it yourself via Bash as a **background task** (idempotent, always
  safe): `vigil start`. If it lingers, it's waiting on broker login — tell the user to
  finish it in the opened browser tab.

From this point the daemon owns every SL. Switch to MONITOR mode to render its snapshot.
