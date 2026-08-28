# Incidents: verification gaps

The recurring failure mode across all three of these: something *reported* success without
re-checking that the broker actually agreed. Each one produced a change to how `vigil`
verifies its own actions — see `src/vigil/monitor.py` and `src/vigil/state.py` for where
these checks now live in code.

## An omitted quantity silently became 1

A stop-loss modify call omitted the quantity parameter. Kite's API doesn't reject that —
it defaults the quantity to 1. So the modify "succeeded," but only one share of the
position was actually covered by the new stop; the rest of the position sat at the old
trigger with no error, no rejection, nothing in the response to suggest anything was
wrong. It was caught by a human noticing the mismatch, well after it had been sitting
silently.

**Fix:** `GuardedBroker.modify_stop_order` (`src/vigil/guard.py`) asserts `quantity >= 1`
and every caller always passes the full position quantity explicitly — there is no
"modify just the price" code path that can omit it.

## A recorded fix that the exchange had actually rejected

The daemon emitted a `SL_QTY_FIX` event recording a quantity correction as successful.
What had actually happened: Kite's API accepted the modify request (HTTP 200), but the
*exchange* rejected it downstream with an error about the limit/trigger price gap being
outside the permitted band. The resting order was completely unchanged, carrying a
rejection message the code never read. A majority of the position sat unprotected while
both the event log and the status snapshot showed it as fixed.

**Fix:** every SL modify is now followed by re-reading the order from the broker and
comparing its actual trigger price and quantity against what was requested. A mismatch
emits `SL_MODIFY_REJECTED` — a distinct, honest event — instead of assuming the request
succeeded because the API call didn't raise.

## A deliberately cancelled stop went undetected for a full session

A user cancelled a resting stop manually, outside the daemon entirely — a deliberate
decision, not an accident. The reconciliation logic only checked for a missing stop the
*first time* it discovered a position; a stop that vanished from a position it was already
tracking fell through every existing check. The position sat fully exposed with no stop
while the status display kept printing the last price the daemon remembered the stop being
at.

**Fix:** every tracked position's stop status is now checked every cycle, regardless of
phase or whether it's a first discovery, and a missing stop alarms loudly — repeating every
cycle until acted on — rather than assuming the last-known state is still true. The daemon
deliberately does *not* auto-replace a stop that vanishes (`AUTO_REPROTECT` defaults to
`False`, `src/vigil/config.py`): a cancellation might be deliberate, and silently
re-placing it would override a decision the user just made. Detection and alarm are
unconditional; re-placement is a decision the daemon leaves to the human, or to an explicit
`vigil protect SYMBOL`.

## Fast alarms didn't mean fast cycles

The alarm above fires every cycle, but "every cycle" still meant the slow ~150s cadence for
a position whose price sat nowhere near its (already-vanished) stop — the cycle-speed check
only ever looked at price proximity to the last-known trigger, with no notion that the stop
itself might be entirely missing. A lost stop that got flagged repeatedly still sat naked
for several cycles' worth of real time before anyone acted, purely because detection itself
was running at normal speed rather than the fast tier reserved for a position "near" its
stop.

**Fix:** missing protection now forces the fast cycle on its own, independent of price
(`src/vigil/monitor.py`) — a position with no resting stop is at least as urgent as one that
is merely close to its stop, so it gets checked (and re-alarmed) at the same faster cadence,
cutting the detection-to-alarm latency roughly in half for exactly this scenario.

## An order-placement parameter was present on two call sites and missing on a third

The broker adapter has three near-identical order-construction methods. Two of them passed
a required protection parameter the exchange demands for this order type; the third —
placing a fresh stop — simply didn't, no functional reason, just an omission that nothing
caught because the three methods were never compared against each other or exercised
side-by-side in a test. An entry filled correctly, then its stop was rejected outright by
the exchange, leaving a live position with no protection until it was noticed and a stop
placed by hand.

**Fix:** the missing parameter was added to the third method, and the fix was verified
against the full test suite rather than trusted on inspection alone.

## A single hardcoded rounding constant was applied to every instrument

Stop prices were rounded to one hardcoded tick size, applied uniformly. The exchange's real
tick size varies per instrument — most trade at one common value, but a meaningful minority
trade at a different one — and rounding to the wrong tick produces a price the exchange
rejects outright. An entry filled, then its stop was rejected for exactly this reason,
leaving the same class of gap as above: a live position with no protection until manually
placed and the underlying constant fixed.

**Fix:** tick size is now looked up per symbol from the broker's own instrument master
(cached once per day) rather than assumed from a single project-wide default, threaded
through every call site that rounds a stop price. Regression test covers a stop computed
for a non-default-tick instrument and asserts the result is actually a valid multiple of
that instrument's real tick — the earlier hardcoded version would have silently produced an
invalid price for exactly this case without the test ever failing on the common-tick
instruments it was originally written against.

## An unexpected broker rejection was theorized about instead of measured

An order was rejected for a margin amount well outside what a normal calculation predicted.
Instead of calling the broker's own margin-calculator endpoint for that exact order — which
was available the entire time — a theory was formed about *why* (a leverage policy change)
and a position was resized down based on the guess, not a measurement. The guess was wrong;
correcting it required being shown independent evidence twice before the arithmetic was
actually reconciled against the calculator's real output. The concrete cost: a position ran
undersized by several multiples of what the (unused) calculator would have supported, for
the rest of the session.

**Fix (process, not code):** when a broker rejects an order for an unexpected reason, call
its own margin/order-validation calculator for that exact order *before* forming any theory
about the cause — measure first, theorize never. No exceptions for time pressure; the
calculator call costs seconds, a wrong theory costs position size for the rest of the
session.

## A quantity decrease and a quantity increase were treated as the same case

Reconciliation had one branch for "a tracked position's quantity changed," written for the
case of adding to a position — which needs its entry price and R recomputed. A quantity
*decrease* (a partial exit, placed outside the normal order-placement path) fell into the
identical branch and was silently absorbed: the smaller quantity was recorded, and nothing
else happened. No profit-and-loss was ever recorded for the shares that left. The daemon's
own realized-P&L ledger only ever reflected each symbol's *final* close, computed from a
blended average price applied against the wrong (already-reduced) quantity — undercounting
the true session P&L by more than half.

**Fix:** a quantity decrease that doesn't go all the way to zero is now its own case —
realized P&L is computed for exactly the quantity that left, using the broker's own current
blended sell/buy price, and recorded as its own ledger entry, additive with whatever the
eventual full close records later. The remaining position's phase and breakeven history is
left untouched, since a partial exit isn't a new trade. Three regression tests cover the
P&L being recorded correctly, phase state surviving the partial, and a later full close not
double-counting quantity already recorded by the partial.

## A margin rejection was misread by dividing the wrong numbers

A broker rejection reported the total margin required for an order and how much more was
needed to cover it — a small shortfall relative to the whole order. The diagnosis divided
the *entire* reported requirement by the order's quantity, producing a per-share figure far
higher than expected, and concluded from that the instrument's leverage must have dropped
sharply. It hadn't. The rejection's own shortfall figure, divided by the real leverage,
gave the actual gap — a small fraction of one share's worth of value, not evidence the
leverage assumption was wrong at all. The resulting order was cut by roughly two-thirds of
what the evidence actually supported, on a trade that already existed and needed no fresh
justification to size correctly.

This is the same failure mode as the calculator-avoidance entry above — a plausible-looking
number produced without checking it against what the rejection actually implied — just with
the arithmetic error one level deeper: not skipping the measurement, but misreading it once
obtained.

The trade this happened on was already working in the position's favor before the
mistake — the scale-in itself was the right call, moving the average entry toward where
price ultimately closed. Run against the day's actual closing price, the undersized order
was the entire difference between that session ending at a small loss and ending at a
small profit — not a rounding error against an otherwise-fine outcome, but the one number
that decided which side of zero the day landed on. Worth stating plainly, since a first
pass at reviewing this same day scored it as a repeat-pattern footnote and had to be
corrected after the fact for missing exactly this.

**Fix (process, not code):** when a broker rejects an order for an unexpected amount,
compute `shortfall ÷ leverage` first — that's the additional order value needed. Only
conclude the leverage assumption itself was wrong if that number is wildly inconsistent
with the rejection's own figures.

## An unrelated exit was recorded as if it had been placed by the user

A position closed while the tracking process wasn't running to observe how. On restart, the
reconciliation path found the position already flat at the broker and recorded it with a
generic "manual exit" label — the default for any closure it can't otherwise explain. The
actual mechanism (a broker-side forced closure, unrelated to anything the user or the
tracked SL order did) was never distinguished from a real manual sell, because the
reconcile path only has one label for "closed, and I don't know why."

The realized P&L figure itself was still correct, independently verified against the
broker's own position record — this was a label problem, not a money problem. But a label
that's wrong by default corrupts every later read of the session's own history, silently.

**Fix:** when reconciliation finds a tracked position closed with no local order or trigger
record explaining it, label it as unexplained rather than defaulting to the same label used
for a real, observed manual exit.

## A daemon reported "live and managing positions" while its order path was dead

The daemon started cleanly: broker login succeeded, the token was saved, the instrument
master verified every reference token, and it printed that it was live and already managing
positions. Every read path then worked, and kept working for the entire session — quotes
across the full sector universe, account margins, daily and intraday candles for a dozen
symbols. Nothing in any output suggested a problem.

The first order attempt failed outright. The broker applies its static-IP whitelist to
**order** endpoints only; read endpoints ignore it entirely. The host was dual-stack, with
IPv6 winning the default route, so every API call egressed from an address that was not the
whitelisted one. The whitelist itself was configured correctly — the whitelisted address
simply wasn't the one in use. Read traffic never cared, which is precisely why the failure
stayed invisible until an order was attempted.

No money was at risk on the session this happened, because no position was open. The real
exposure is the other case. Had a position been open, the daemon would have reported live,
logged clean cycles and rendered a healthy status — while being unable to execute its
breakeven move, its trail, or its scheduled square-off, since placing, modifying and
cancelling orders are all order-API calls. The position would have fallen through to the
exchange's own forced square-off with the daemon logging failures the whole way.

A per-process workaround existed and was deliberately declined. The entry command places the
entry and its stop within a single process, so forcing just that process onto the working
route would have opened the position with a stop attached — while the daemon, a separate
process still on the broken route, could not have managed it afterwards. Opening a position
under that condition is strictly worse than not trading at all.

Two things worth recording about the fix itself. First, the obvious mechanism for a
venv-wide network override — dropping a `sitecustomize.py` into site-packages — silently did
nothing, because some Python distributions ship their own `sitecustomize.py` in the standard
library directory, which wins the import and shadows the venv copy. No error, no warning; the
first attempt simply had no effect. A `.pth` file that imports a differently-named module
works, because `site` executes those unconditionally. Second, the fix was verified by
inspecting the *daemon process's own* established connections, not by re-running a one-off
command — a one-off proves only that one process was fixed, which is exactly the failure
mode being guarded against.

**Fix still owed:** startup verifies the token, the instrument master and the read APIs,
then declares itself live — it never checks that it can place an order. The proposal is a
preflight at daemon start, when the market is open: place a single-share limit order far
enough from market that it cannot fill, confirm the broker accepts it, cancel it
immediately, and refuse to report "live" if it is rejected. The cost is one order and a
cancel; the benefit is converting a silent capability gap into a loud startup failure. An
egress-address check against a configured expected value is cheaper but strictly weaker — it
proves which address is being used, not that the broker will accept orders from it.

**Rule:** a process that has proven it can *read* has proven nothing about whether it can
*act*. Anything trusted to protect a position must verify the write path before claiming to
be protecting anything.
