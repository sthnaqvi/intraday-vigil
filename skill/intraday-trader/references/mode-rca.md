# MODE: RCA (post-session)

Run after the session closes, once all positions are squared off. Read
`references/rca-template.md` for the full 10-point rubric.

**Primary data source: the daemon's audit trail.** Run `vigil paths --json` to find
`data_dir`, then read `events-<YYYY-MM-DD>.jsonl` (today's date, in the daemon's local
timezone) from there. Parse it line-by-line; each record is `{"ts", "type", "symbol",
"data"}`. Reconstruct the session:

- **Trade log**: position-discovered events (entries as the daemon saw them), SL-hit and
  squareoff-fill events (exits with realised P&L/R), the final closed-positions list in
  the last status snapshot.
- **Lifecycle execution**: phase-change events — was breakeven applied within one cycle of
  +1R? Did Phase 3 trails fire at 2×`sl_pct`? Any rejected modifies or SL replacements?
- **Hygiene**: quantity-fix events (a mismatch existed — why? was the initial SL placed
  with the wrong qty?), orphan-SL-cancelled events, warnings/errors.
- **Discipline**: time-alert timestamps vs. actual behaviour, kill-switch activation,
  squareoff-start (did the scheduled auto-flatten fire because a manual exit was skipped?).
- If the daemon ran `--dry-run`, its logged intents show what it *would* have done — diff
  them against what actually happened to the SLs.

Supplement with the broker's own order history for anything the daemon didn't see (e.g.
entries/exits before the daemon started — a gap worth flagging in itself).

Score the session across: sector selection, entry timing vs opening range, SL placement,
SL lifecycle execution (from events, not memory), position sizing discipline, macro theme
alignment, reassess quality, exit execution, phase transitions, overall discipline.
Total /50.

Output a markdown report flagging the top 3 mistakes to avoid tomorrow.
