# Incidents: trail and SL lifecycle

Real sessions that shaped the three-phase SL lifecycle (`docs/sl-rules.md`) and the
stop-hunt guard. Figures below are R-multiples and ratios, not ₹ amounts or dates — see
`docs/sl-rules.md` for why.

## Manual override into the stop-hunt zone

A Phase 3 position was trailing mechanically at `2 × sl_pct` from the current price. With
the position comfortably in profit, the resting stop was manually moved tighter — "looks
safer, closer to current price" — instead of trusting the mechanical trail.

The manually-chosen level landed within the stop-hunt guard's 0.3% buffer of the previous
day's high, a zone the mechanical trail would have avoided automatically. Price dipped
just far enough to fill the manual stop, then reversed and ran well past where the
mechanical trail would have sat. The position exited flat-ish instead of capturing a move
of roughly 2R beyond the manual exit point.

**Lesson:** never override a Phase 3 mechanical trail with a discretionary "safer-looking"
level. The stop-hunt guard exists precisely because obvious round-number and prior-extreme
levels are where a manual eye instinctively wants to place a stop — and where the market is
statistically most likely to wick through and reverse.

## A fixed trail percentage stalled a big winner

A long position reached over +2R unrealized, comfortably past the Phase 3 threshold. A
fixed trail percentage (rather than one scaled to the position's own `sl_pct`) was applied
to compute the new stop — and the resulting level came out *below* the existing breakeven
stop. Because the lifecycle only ever ratchets a stop tighter, never looser, the trail
never fired. The position round-tripped from its peak back down to breakeven and exited
for a small fraction of the unrealized gain it had shown at its best point.

The math: a trail percentage only beats a breakeven stop once price has moved roughly
`trail_pct / (1 + trail_pct)` beyond entry. A trail sized for a 5%-stop stock needs a much
bigger move to clear breakeven than this position's actual stop width could produce.

**Lesson:** `trail_pct` must be derived from each position's own `sl_pct`
(`trail_pct = TRAIL_MULT × sl_pct`, `TRAIL_MULT = 2.0` today) — never a single fixed
percentage shared across every stock. This is enforced in `TrackedPosition.trail_pct`
(`src/vigil/state.py`), computed fresh, never hand-set per trade.

## A wide stop made the whole lifecycle unreachable

Several positions in one session used a stop width around 10× the recommended cap. One of
them reached a genuinely good unrealized gain — the session's best trade — but at that
width, the profit needed to reach Phase 2 (breakeven, `profit_R ≥ 1.0`) was itself a very
large percentage move. None of that session's positions ever crossed it. Every open gain
sat completely unprotected until the session ended.

**Lesson:** SL width and the phase lifecycle are coupled — a wide stop doesn't just risk
more per share, it can make the entire breakeven/trail mechanism structurally unreachable
within a normal session. This is why `vigil` refuses `sl_pct > 1.5%` everywhere an SL is
set (`enter`, `arm`, `add`, `protect`), rather than treating it as an advisory cap.

## The cap check ran before the guard that could break it

An entry's input `sl_pct` was safely under the cap and passed the refusal check cleanly.
The stop-hunt guard then ran afterward — correctly, on its own terms — and widened the SL
because the raw pre-guard price landed within the guard's buffer of a level the position
had just broken out through. Nothing re-validated the *result* against the cap. The
position ended up with an SL a fraction over 1.5%, silently, because the check and the
step that could invalidate it ran in the wrong order.

**Lesson:** a cap check is only as good as what it checks last. If a downstream step can
still move the value after validation, the cap isn't actually enforced — it's enforced on
an intermediate value that isn't the one that ends up live. Validate the final state, not
the input.

## An aggregate profit swing had zero protection because R is per-position

Three simultaneously open positions produced an aggregate unrealized profit of several
thousand rupees at its peak — a genuinely large number. Not one of the three positions had
individually crossed its own 1.0R breakeven threshold at that moment; the phase lifecycle
is computed strictly per-position, from that position's own entry and SL width. The entire
aggregate gain therefore carried no mechanical protection whatsoever, and round-tripped
back through flat into a loss within the same hour — nothing had moved, because nothing
was individually due to move.

Nobody watching caught it either. Every status check reported each position's own number
correctly; none of them framed "the total is unusually large relative to what's actually
protected" as a decision point, because nothing in the render compares the aggregate to
any individual position's protection state.

**Lesson:** a large green *total* is not the same claim as "some position has earned
protection." R-multiples are computed per-position by design — that's correct for sizing
and for the phase mechanics themselves — but it means a portfolio can look excellent in
aggregate while every individual leg remains fully exposed. A system (or a habit) that only
ever asks "what's my total P&L" and never "what's my most-exposed position's own R" will
miss this every time it happens.
