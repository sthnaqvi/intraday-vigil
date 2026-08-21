# Stop-loss lifecycle rules

This is the single source of truth for the SL lifecycle. `vigil`'s daemon
(`src/vigil/monitor.py`, `src/vigil/rules.py`) executes every rule on this page
automatically for every open position — phase transitions and SL modifications react to
price the instant a tick arrives, not on a poll (see "Cadence" below). The Claude skill
applies only the entry-time rules (initial SL width, stop-hunt guard before sizing) and
never modifies an SL order once the daemon owns it — see `skill/intraday-vigil/SKILL.md`'s
hard rules.

All the defaults below are configured in `src/vigil/config.py` and `src/vigil/rules.py`.
They're this project's defaults, not laws of physics — change them for your own risk
tolerance, but change them there, not per trade.

## The three phases

Every MIS position moves through three phases, tracked by unrealised profit in multiples
of R. `1R = entry_price × sl_pct` — the ₹ (or whatever currency) amount at risk per share.

### Phase 1 — Protection (`profit_R < 1.0`)

**The SL is not touched. No exceptions.** Premature tightening is the most common way to
get stopped out of a trade that would otherwise have worked. Hold Phase 1 until
`profit_R ≥ 1.0`, even through a stall or a small pullback.

### Phase 2 — Breakeven (`1.0 ≤ profit_R < 1.5`)

The instant `profit_R` first crosses 1.0, the SL moves to entry price, once, and stays
there until Phase 3. The daemon always passes the full position quantity on this modify —
omitting it is a documented Kite quirk that silently protects only 1 share (see
`docs/incidents/verification-gaps.md`) — and re-reads the order afterward to confirm the
modify actually landed rather than trusting the API response alone.

### Phase 3 — Mechanical trail at `2 × sl_pct` (`profit_R ≥ 1.5`)

The stop trails at `trail_pct = TRAIL_MULT × sl_pct` (`TRAIL_MULT = 2.0` by default) behind
the live price, recalculated on every price tick:

```
# long
trail_sl = LTP × (1 − trail_pct)
# short
trail_sl = LTP × (1 + trail_pct)
```

Only ratchets — a new level is applied only if it's better than the current SL — and only
if it moves the stop by more than `TRAIL_MIN_MOVE` (0.5% by default), to avoid churning the
order on small oscillations.

**Why proportional to `sl_pct`, never a fixed percentage:** a trail sized for a wide-stop
stock needs a much bigger move to clear breakeven than a tight-stop stock's own SL width
would produce — see `docs/incidents/trail-and-sl-lifecycle.md` for what that looked like in
practice on a real fixed-percentage trail.

## Stop-hunt guard (all phases, every SL placement or move)

Never place or trail a stop within `STOP_HUNT_BUFFER` (0.3% by default) of the previous
day's high, the previous day's low, or a clear intraday swing high/low. If a computed level
lands inside that band, push it `STOP_HUNT_BUFFER` further in the trade's favour — below
the level for a long, above it for a short (`rules.apply_stop_hunt_guard`).

## SL width cap

**`sl_pct` above 1.5% is refused, everywhere an SL is set** (`enter`, `arm`, `add`,
`protect` — see the daemon CLI reference). This isn't advisory: a wide enough stop makes
`profit_R` reach 1.0 only after a very large move, which can make Phase 2 and Phase 3
unreachable within a normal session — see `docs/incidents/trail-and-sl-lifecycle.md`.

## Position sizing

Two defensible approaches exist and this project has to pick one as the default, since the
lifecycle above is quantity-independent (R and the phase thresholds are price-based, so
they behave identically at any size — only the rupee magnitude scales):

**Default: size from available margin.** `qty = floor(margin_allocated_to_this_slot /
(entry / leverage))`. Report the resulting risk in currency plainly
(`qty × abs(entry − final_sl)`) so the size is never a surprise, but the sizing input is
available margin, not a risk budget.

**Alternative: size from a fixed percentage of capital at risk.**
`risk_amount = available_capital × risk_pct` (a common convention is 1%), then
`qty = floor(risk_amount / abs(entry − final_sl))`.

Pick one per your own risk tolerance and apply it consistently — the important part is that
whichever you choose, it's applied *after* the stop-hunt guard has finalized the SL price
(the guard can widen the SL, which changes risk per share, which changes quantity — sizing
before the guard runs sizes off a price that's about to move). Order matters: finalize the
SL, then size.

VIX-based (or your market's equivalent volatility-index) size multipliers, if you use them,
apply after the base quantity is computed: commonly half-size in an elevated-volatility
band, quarter-size above that, with an additional halving on a scheduled macro event day
(central bank announcements etc).

**Reserve a transaction-cost buffer — never size against 100% of available capital.**
Brokerage, STT, and exchange charges are deducted from free cash, not from the margin
blocked for the position, so sizing right up to the exact available-margin figure leaves
nothing to cover the exit that will eventually have to fire (the SL, the square-off, or a
manual close) — a real insufficient-funds notification from this exact gap forced this rule
in (see `docs/incidents/discipline-and-process.md`). Hold back a fixed reserve — a flat rupee
amount comfortably above a few round-trips' worth of charges, or a small percentage (1% is a
reasonable default) — before computing `qty`, on every slot, every time:
```
sizing_capital = margin_allocated_to_this_slot - transaction_cost_buffer
```

## Order-quantity verification

The daemon re-reads every tracked position's SL order every `QTY_VERIFY_INTERVAL_S` (20s by
default) and compares its actual quantity against the position's actual quantity,
independent of any phase transition or recent modify. A mismatch is fixed immediately, and
the fix is verified by re-reading the order afterward — never assumed from the API call not
raising an exception. See `docs/incidents/verification-gaps.md` for the two separate real
incidents (an omitted quantity, and an exchange-side rejection the code didn't check for)
that made this unconditional.

## Cadence: what's tick-driven vs. periodic

Phase transitions and SL modifications (breakeven, trail) react to price the instant a
WebSocket tick arrives — not on a fixed poll — via `MonitorLoop._on_price` /
`_apply_position_decision` (`src/vigil/monitor.py`). If a symbol's ticks go stale (dropped
socket) or no push feed is running at all (paper mode has no real WebSocket), a periodic
poll fallback drives the same decision path instead — never a separate, weaker code path.

Everything else that genuinely has no push alternative runs on its own short, independent
cadence (`src/vigil/config.py`): broker-truth reconciliation (`RECONCILE_INTERVAL_S`, ~20s
— Kite's WebSocket carries price ticks only, not order/position state), qty verification
(`QTY_VERIFY_INTERVAL_S`, ~20s), and the run loop's own wake-up (`LOOP_TICK_S`, ~5s) that
paces time actions (squareoff, alerts, kill switch) and decides when each slower concern
above is next due. None of these gate SL decisions anymore — that coupling was a holdover
from this project's very first version, when the *skill itself* polled to save Claude
tokens; the current daemon never calls Claude, so that constraint doesn't apply to it.

## Known trade-off: market protection converts SL-M into SL (limit)

Kite rejects an API **modify** of an SL order unless `market_protection` is supplied
(`InputException: Market orders without market protection are not allowed via API`).
Supplying it converts the resting SL-M into an SL with a limit price at
`trigger × (1 ± MARKET_PROTECTION_PCT)`.

Both consequences are real: a flash move can't fill the stop at an arbitrary price (good),
but a gap straight through the protection band can leave the stop resting unfilled (bad).
`MARKET_PROTECTION_PCT` (`src/vigil/config.py`) must stay small — 5 was rejected outright by
the exchange for being too wide a band; 1 is accepted and is the current default.

Note the asymmetry: a fresh `place_order` accepts a plain SL-M with no market protection —
only `modify_order` demands it. Cancel-and-replace would avoid the conversion entirely, but
it isn't the default: it trades a small, certain risk (a wider fill band on modify) for an
uncertain, larger one (a window with no stop at all, or two live stops both trying to exit
the same position).
