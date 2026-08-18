# MODE: REASSESS

Mid-session re-ranking. **User-triggered only** — run it when the user asks, or when they
accept the prompt MONITOR offers later in the session. There is no self-scheduling: the
daemon, not this skill, owns the in-session clock, and a scheduled wakeup cannot reliably
span multi-hour gaps. Run it regardless of how many positions are open — even 1 position
requires sector validation.

1. Re-fetch live quotes for every sector (same as Step 4 in `mode-start.md`).
2. Re-rank every sector — has momentum shifted since open?
3. For each open position (from a fresh `vigil status --json`): check if its sector is
   still in the top/bottom 3.
4. If a position's sector dropped out of the rank → flag for exit consideration.
5. Show new opportunities in better-ranked sectors.
6. Ask the user: "Exit {symbol} (sector rank dropped to 7)? New opportunity: {stock} in a
   sector now ranked 2?"

Any new entry from reassess goes through the **entry gate** (status flags + the hard
cutoff) and the full Step 5 counter-trend short check, Step 6 guard-then-size procedure,
and Step 7 risk-seed write from `mode-start.md`.

Exiting a position from reassess: cancel its SL order first, then market-exit (same as
EXIT mode, single symbol — or just `vigil exit SYMBOL`, which does both atomically). The
daemon detects the closure next cycle.
