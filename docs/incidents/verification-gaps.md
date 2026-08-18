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
