# Incidents: discipline and process

Not code bugs — process failures in how entries, exits, and setup selection got decided.
Each produced a hard rule that the skill (and in two cases, the daemon itself) now enforces
rather than relying on remembering to apply it.

## An ambiguous exit instruction was read as specific

A message like "I don't think it will go higher" or "exit it" arrived while more than one
position was open. It was interpreted as referring to a particular symbol without asking
which one — and the guess was wrong. The result was a premature exit of a position that was
still comfortably in profit, and a second position closed alongside it that wasn't the one
meant.

**Rule:** any exit or cancel instruction that could plausibly apply to more than one open
position stops the workflow entirely. The only next action is asking which symbol, by name,
before anything is cancelled or sold. This is documented as a hard rule in the skill's
MONITOR reference — see `skill/intraday-vigil/references/mode-monitor.md`.

## Entries placed past the cutoff with no setup basis

Several positions were opened noticeably later in the session than the intended entry
cutoff, none of them backed by the setup criteria (an opening-range breakout, in this
case) the rest of the day's entries required. All were closed at a net loss by session end.

**Rule:** the 14:30 IST no-new-entries cutoff is hard and non-negotiable — no recovery
trades, no "high conviction" exceptions, no manual override without an explicit
`--override-gate` the human has to type themselves. It's enforced in two independent
places: the skill's entry gate (checked before any entry is proposed) and the daemon's own
gate (`triggers.gate_block_reason`, `src/vigil/triggers.py`) for every entry it places,
including armed triggers firing unattended.

## A short placed against the day's dominant bullish momentum

A short position was taken in a sector ranked well down the momentum table on a day the
broad market had gapped up strongly. The sizing and sector-ranking rules already specified
that a strongly bullish gap day should produce all-long candidates, with a short permitted
only from the very bottom of the ranking and only on a flat-to-mildly-bearish gap — but
that rule existed as guidance rather than a checked gate at the point the pick was made.

**Rule:** direction discipline against the day's gap bias is now a hard filter applied
*before* a candidate is shown, not a guideline checked after the fact. See the
direction-discipline rule in `skill/intraday-vigil/references/mode-start.md`.

## A stalled short thesis wasn't flagged for seven candles

A short position's original justification — the stock was making new intraday lows — had
quietly stopped being true. Several candles passed with no new low and thinning volume,
which is exactly the signature of a move that's run out of sellers, but nothing in the
process surfaced that until it was noticed by eye, well after the thesis had stalled.

**Rule:** MONITOR now includes a mandatory thesis-decay check on every open position, not
just at entry — re-reading the same setup criteria that justified the trade and saying so
plainly if they no longer hold. A stalled thesis isn't an automatic exit, but it has to
enter the hold/exit conversation instead of going unmentioned. See
`skill/intraday-vigil/references/mode-monitor.md`.

## A scheduled exit was disabled without deciding what replaces it

The daemon's own scheduled square-off exists specifically to beat the exchange's own forced
closure by a few minutes, so a controlled exit wins that race instead of an uncontrolled
one. It was disabled mid-session, with the stated intent of letting profitable positions
run longer toward the exchange's own later deadline — but no exit-trigger or manual-exit
plan was put in place for the window that opened up. The result was operationally
identical to doing nothing: the exchange's own forced closure ended up deciding the exit
price a few minutes later, at whatever level the market happened to be at, while one
position bled steadily and specifically during the unmanaged window with real money lost
to it.

**Rule:** disabling a scheduled exit is only half a decision. It must be paired with an
explicit answer to "then what closes this, and when" — a specific trigger price or a firm
manual-exit time, decided *before* the safety net comes down — never just the removal of
the earlier exit with an assumption that things will work out. See
`skill/intraday-vigil/references/mode-monitor.md`.

## Position sizing never reserved anything for its own transaction costs

Sizing has always computed quantity from the full amount of available margin, with nothing
held back for the brokerage, statutory, and exchange charges that get deducted from free
cash rather than from the margin blocked for the position itself. This produced a real
insufficient-funds notification from the broker when an exit fired against an account with
no cash cushion left to cover its own charges.

**Rule:** a fixed reserve — held back from the sizing calculation on every slot, every
time — must exist before quantity is computed, specifically to cover the transaction costs
of the exit that will eventually have to fire. See `docs/sl-rules.md`'s position-sizing
section.

## A scheduled exit was disabled without a replacement plan — again, on a later session

The exact same decision as the entry above happened again on a different day: the
scripted square-off exists specifically to beat the exchange's own forced closure by a few
minutes, and it was stopped a few minutes before it could fire, with nothing armed to
replace it for the window that opened up — no manual exit, no exit-trigger, no firm
manual-exit time decided in advance. The documented rule from the first occurrence already
existed and was broken again anyway.

The exchange's own forced closure filled the gap both times, as it always will when nothing
else is watching. The outcome differed only by chance: the first time, the position bled
steadily through the entire unmanaged window and the forced closure landed at the worst
price of that stretch; the second time, price happened to drift back in the position's
favor during the gap, so the forced closure landed better than the price at the moment the
exit was stopped. Same decision, same process gap, opposite luck — which is exactly the
problem with relying on a documented rule that only survives by being remembered in the
moment.

**Rule (restated, now twice-violated):** disabling a scheduled exit is only half a decision
without an explicit answer to "then what closes it, and when" — decided *before* the safety
net comes down, not assumed afterward. A rule that depends purely on memory has now failed
on two separate occasions; the fix under consideration is making the disabling action itself
harder to take inside the pre-squareoff window, not restating the rule a third time.
