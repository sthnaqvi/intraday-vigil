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

## An expectancy figure built on the wrong sample nearly killed a positive-EV setup

The user proposed a small counter-trend scalp: buy a stock sitting at its day low late in
the session, exit on a fixed rupee-per-share bounce. Asked to evaluate it, I computed an
expectancy, reported it as clearly negative — and reported it again as still negative even
under what I described as a charitable win rate — and recommended against the trade. The
stated basis was a measured zero-for-seventeen hit rate for that bounce size during the
afternoon of that session.

When later asked for genuine in-depth research, the correct study reversed the sign. Taking
every session over roughly six months in which the same stock sat within a quarter-percent
of its running day low at the same time of day on a down day gave eighteen matching setups.
Resolving each one path-dependently on five-minute bars — walking the bars forward to see
whether the target or the stop came first, and counting an ambiguous bar as a loss — gave a
50% win rate and a **positive** expectancy at every stop width tested.

Three flaws, in increasing order of importance:

1. **Wrong conditioning.** The original test measured buying at *any* candle low and looking
   for the target within a short window. Most of those candles were mid-trend, so it
   measured random dip-buying inside a downtrend — a different population entirely from the
   at-the-day-low, late-session mean-reversion setup actually being proposed.
2. **A single session's sample, in one regime**, presented as though it characterised the
   setup in general.
3. **A binary outcome space.** Every non-win was treated as a full stop-out. In reality
   between a third and a half of these setups neither hit the target nor the stop — they
   drift and get squared off near breakeven. Omitting that flat bucket is what drove the
   expectancy so far negative, and it was the single largest error of the three.

The cost on the day was nil, since the order path was down and no trade was possible
regardless. The cost in the general case is a user talked out of a positive-expectancy setup
by a precise-looking number, which carries far more authority than "I don't like this
setup" — and is far harder to argue with.

**Fix (process):** before quoting an expectancy that drives a go/no-go decision, (a)
condition the sample on the setup actually being traded, not a superficially similar one;
(b) match the measurement window to the real exit constraint, which for an intraday position
is the scheduled square-off, not the closing bell; and (c) include the timeout/flat bucket in
the outcome space alongside win and stop. State the sample size and its confidence interval
inline — at eighteen observations a 50% win rate carries a 95% interval of roughly 26–74%,
which is a materially more honest message than a bare point estimate quoted to the rupee.

## A morning momentum metric was applied unchanged in the afternoon

The session's opening workflow was invoked around four hours after the open. Its sector
step ranks every sector by percentage move from the day's open, and this was applied
unmodified.

Early in the session that metric describes current momentum. Four hours in it describes a
nearly-complete session's move, which is a different quantity. The distortion was visible in
the output: the top-ranked sector's lead came almost entirely from a single constituent
whose entire move had finished more than an hour earlier, leaving it flat in a narrow band
ever since; strip that one name and the sector's average fell by more than half. The
bottom-ranked sector was in the same position in reverse — ranked last on a move complete by
midday, on well-below-average volume. Neither rank described what those sectors were doing
at the moment the decision was being made.

What makes this worth recording is that the identical problem *was* caught one step earlier
and then not carried forward. The opening gap was explicitly flagged as a hours-old fact,
and the live index reading was deliberately weighted over it when choosing direction. The
same reasoning simply wasn't applied to the sector ranking that followed — even though the
router file instructs reading the mode file and both sector reference files together, before
starting, precisely so that gap, theme and sector selection get reasoned about as one
interdependent set rather than as sequential steps.

**Fix:** when the opening workflow runs materially late, rank on a trailing window — the
last hour or ninety minutes of intraday candles — *alongside* percent-from-open, show both
columns, and state which one the selection rests on. And say plainly that a morning metric is
being run at midday, rather than running it silently and letting the ranking look more
current than it is.
## A breakout was entered long after its high had printed

A stock broke a multi-hour range on the two largest volume bars of its day, each larger than
the last, and pushed to a new session high. The read was correct — it was a genuine
expanding-volume breakout.

The order was placed roughly twenty minutes after that high. The stock never traded above the
entry again. It faded back into the broken range within half an hour, lost the breakout level
twice, and was closed by the daemon's own scheduled square-off for the largest single loss of
the session — on its own, more than the day's other trades combined.

The delay was not hesitation. It was spent on legitimate work: re-ranking sectors, checking
VWAP relationships, running the counter-trend test on the alternative candidate, building the
sizing table. All of it correct, and all of it applied to a move that was completing while it
was being checked. By the time the order went in, the trade that had been analysed no longer
existed. What was actually bought was a retest of a level that had already been rejected
once, several points above the breakout bar, sized and stopped for the original idea.

A second, compounding error sat alongside it. The alternative candidate had scored identically
on the same rubric, and the choice put to the user was a false binary — one or the other,
never both at reduced size, which the available margin would comfortably have supported. The
split would have left the pair close to flat where the single position was deep in the red.

**Fix:** on a breakout entry, the age of the extreme is part of the setup. If the high is more
than about two bars old, what is on offer is a retest — different levels, different stop,
different odds. Re-derive it as that trade or skip it, but do not place the breakout order
against it. And when two candidates score equally, splitting is a real option: put it on the
list rather than forcing a choice between them.

## A position was doubled after the cutoff, and its R improved while its risk grew

Late in a session — well past the hard no-new-entries cutoff, with the kill switch already
active, and a handful of minutes before the daemon's own scheduled square-off — a market
order nearly doubled an existing position. It was placed directly at the broker rather than
through the daemon, which only learned of it by reconciliation; an attempt to stop the daemon
followed within half a minute and was refused by the guard.

Two things are worth separating here. The first is the obvious one: the added risk had
essentially no managed session left in which to work, and the position was force-closed
minutes later at the venue's own square-off.

The second is subtler and is the reason this is recorded at all. Averaging into the position
moved the average entry closer to the market while the resting stop stayed where it was.
That *increased* the position's 1R currency value — more shares, each risking slightly less
to the same stop price — so the same unrealised loss suddenly read as a much smaller R
multiple. Roughly: a position sitting near −0.4R before the add showed under −0.2R
immediately afterwards, having moved not at all. Meanwhile the actual currency at risk to
the stop had risen by about two-thirds.

Anyone watching R alone — which is the metric the whole lifecycle is built on — would have
read the position as having become materially safer at the precise moment its real exposure
grew. R is a ratio, and adding to a position changes its denominator.

**Fix:** no code path can block an order placed directly at the broker, so the guard here is
procedural. What is transferable is the metric discipline: on any quantity change, report the
currency risk delta alongside the R, and treat an R that improves because of an add as an
artefact of the denominator rather than a signal about the trade. A related defect surfaced
in the same event — the stored risk seed kept its original stop percentage after the average
entry moved, leaving the seed and the resting order disagreeing about the stop's width, which
would have mis-set the trail multiple had the position ever reached the trailing phase.

