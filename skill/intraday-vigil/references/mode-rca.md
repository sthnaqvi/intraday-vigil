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

Output a markdown report flagging the mistakes worth fixing before the next session — most
sessions surface 2-3, but report however many the evidence actually supports, not a fixed
count padded out or trimmed to fit "3."

## Where this lives — same private/public split as the incident log

**Save the rendered report to `private/rca/<YYYY-MM-DD>.md` in the repo, not just as a chat
attachment.** A report only delivered as a file in a coding-session send is easy to lose
track of and isn't visible to `grep`/`find` in the repo the way everything else this skill
touches is. `private/` is gitignored (see `.gitignore`) — RCAs carry the same real
dates/₹-figures as `private/incidents/verbatim-log.md`, so they get the same treatment:
real numbers live in `private/`, nothing with a specific ₹ amount or date gets published.

**If the session surfaced anything worth remembering beyond "today's numbers" — a bug, a
process gap, a decision that cost money — that's not what the RCA file is for on its own.**
Add it as a new numbered entry to `private/incidents/verbatim-log.md` (continue the
existing numbering, keep the real figures), then write a scrubbed version (no exact dates,
no exact ₹ amounts, no ticker names if the existing files' style omits them) as a new `##`
section appended to whichever *existing* `docs/incidents/*.md` file matches its theme.
**Don't trust a theme list copied here going stale — run `ls docs/incidents/` and read each
file's own opening paragraph (every one states its theme in the first few lines) to decide
the match, since a file can get added after this list was last written.** As of this
writing the themes are:
- `verification-gaps.md` — something *looked* fine without being checked against ground
  truth (an order accepted, a token that returned data, an unverified assumption)
- `discipline-and-process.md` — a trading/process decision, not a code bug
- `trail-and-sl-lifecycle.md` — SL/phase mechanics specifically
- `reference-data-drift.md` — external reference data (broker identifiers, symbols) gone
  stale since it was last written, as opposed to a code path that never checked itself

Only create a new thematic file if the finding genuinely doesn't fit any existing one —
check the actual files first, not just this list. If you do create one, add it to the list
above in the same edit, so the list and the directory never drift apart again. **Never
create a new dated file under `docs/incidents/` per session** — that breaks the by-theme
structure and stops findings from accumulating anywhere consistent.
