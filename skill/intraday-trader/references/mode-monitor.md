# MODE: MONITOR

**No manual polling loop to re-implement SL lifecycle logic. No SL modifications.** The
daemon runs its own cycle, moves SLs, fixes qty mismatches, fires time alerts, and squares
off near the close on its own — a competing manual loop watching for the same conditions
would race it, not help it. MONITOR is an on-demand *read* of the daemon's snapshot whenever
the user asks. (Polling for other reasons — e.g. confirming an action landed — is fine; see
`SKILL.md` for how to do it correctly, since a broken poll condition fails silently.)

## Procedure

1. Run `vigil status --json`.
2. **Freshness check**: stale if `now − as_of > 2 × daemon.cycle_seconds` (`as_of` is IST).
   If missing or stale → do NOT render stale numbers as current. Say the snapshot is stale
   (with its HH:MM:SS), then start the daemon yourself via Bash as a background task:
   `vigil start` (if it lingers, it's waiting on broker login — the user must finish it in
   the opened browser tab). Offer to show the stale snapshot explicitly labelled as stale.
3. **Protection check — do this before rendering anything else.** For every position, read
   `protected`. If it's `false`, that position has **no stop at the exchange** and the
   `sl_price` shown is a memory, not a live order. Lead with it:

   > 🚨 {SYMBOL} is UNPROTECTED — {qty} {direction}, SL order is {sl_order_status}.
   > Re-place: `vigil protect {SYMBOL}` · Exit: `vigil exit {SYMBOL}`

   Ask before re-placing — the user may have cancelled it deliberately (see
   `docs/incidents/verification-gaps.md` for why this is never assumed to be a bug). But
   never let it pass unmentioned either: state the naked exposure and that it runs until
   square-off.

4. **Thesis-decay check** (open positions only). The counter-trend rule that gates *entry*
   has no equivalent for a position already on. Apply it in reverse: pull today's
   15-minute candles and ask whether the setup that justified the trade still exists.

   - Short: has it made a **new low in the last 3–4 candles**? Long: a new high?
   - Is the range compressing and volume drying up?
   - Is the position's sector still ranked top/bottom 3?

   If the answer is no, say so plainly — *"I would not enter this now"* is the honest
   test. A stalled thesis is not an automatic exit, but it changes the hold/exit
   conversation and must be surfaced (see `docs/incidents/discipline-and-process.md`).

5. If fresh, render the snapshot:

```
⏱ 10:47 AM — SL Daemon Snapshot (as_of 10:46:32 · cycle 47 · live · 150s cadence)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
INDIGO      ▲ ₹4,210  +1.8R  🔄 Phase 3 — trailing, SL ₹4,126 (2.0% trail)
DEEPAKFERT  ▲ ₹692    +0.6R  🔒 Phase 1 — SL untouched at ₹685
ADANIGREEN  ▲ ₹1,043  +1.2R  ✅ Phase 2 — SL at breakeven ₹1,030
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Closed today: TATAPOWER −₹700 (−1.0R, SL hit)
Day realised: −₹700 (−1.0R) · entries allowed · kill-switch OFF
```
(Figures above are illustrative, not a real session.)

Render rules:
- Phase tags: 1 → "🔒 Phase 1 — SL untouched", 2 → "✅ Phase 2 — SL at breakeven",
  3 → "🔄 Phase 3 — trailing" with `trail_pct` shown. Flag `near_sl: true` rows with
  "⚠️ NEAR SL".
- Show `closed_today` with realised P&L/R.
- Footer: `realized_pnl_today`/`realized_r_today`, entry-gate state (`no_new_entries` +
  reason if set), kill-switch state. If `daemon.mode == "dry_run"` show a prominent
  "⚠️ DRY RUN — daemon is logging intents only, SLs are NOT being managed" banner.
- If `kill_switch` is true, lead with: "🛑 KILL SWITCH — no new entries."

6. **Reassess prompt**: if it's meaningfully into the session (late morning or early
   afternoon by your market's session length) and reassess hasn't been run since that mark
   this session, append: "Want me to run `/intraday-trader reassess` to re-rank sectors?"
   (Reassess is user-triggered; never self-schedule it.)

## What MONITOR must never do

- Never call `modify_order` or `cancel_order` on an SL order — even if a qty mismatch or a
  "better" trail level seems obvious. The daemon verifies and fixes its own orders; a
  second writer causes races. If something looks wrong at the broker, tell the user and
  point at the daemon's logs and today's events file (`vigil paths --json` for their
  location).
- Never re-implement phase logic from quotes. The snapshot is the truth; render it.

## Ambiguous message rule — before any exit/cancel

When the user sends a message that could apply to more than one open position (e.g. "I
don't think it will go higher", "exit it", "get out"), **STOP.** Do not cancel or sell
anything. Ask one specific question: "Which position are you referring to — [list all open
symbols]?" Only act after the user names a specific symbol. See
`docs/incidents/discipline-and-process.md` for what an ambiguous read cost once.

## Time alerts

The daemon fires its configured time alerts, sets `no_new_entries` after the cutoff, and
force-squares everything near the close on its own. The skill does not schedule its own
alerts — but when rendering a snapshot after the cutoff, remind the user the entry gate is
closed.

**If the user wants to stop the daemon's own squareoff to let a position run longer, that
request is only half-answered by stopping the daemon.** `SQUAREOFF_AT` exists specifically
to beat the broker's own force-square by a few minutes so a controlled exit wins that race
instead of the exchange's forced one — disabling it without arming a replacement (a specific
exit-trigger price, or a firm manual-exit time, decided *before* the daemon comes back down)
just hands the exit to the broker's forced closure a few minutes later, at whatever price it
happens to be. That's not "letting it run" in any meaningful sense — it's the same uncontrolled
outcome, just delayed. Cost a real ₹1,046 on 2026-08-20 doing exactly this — see
`docs/incidents/2026-08-20-session.md`. Always pair "stop the scripted exit" with an explicit
answer to "then what closes it, and when."
