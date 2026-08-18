# intraday-algo

Deterministic SL-lifecycle daemon for Zerodha Kite intraday (MIS) positions.
Replaces the AI-driven monitor loop of the `intraday-trader` Claude skill: the
morning workflow (macro theme, sectors, entries) stays human+Claude; once
positions and SL orders exist at the broker, this daemon owns the SL lifecycle.

Spec source of truth: `~/.claude/skills/intraday-trader/references/sl-rules.md`.

## What it does, every cycle (150s / 90s near SL)

1. Reconciles broker truth (positions + orders) against tracked state — new
   positions are auto-discovered, exits are detected via SL order status
   `COMPLETE` (works for profitable trail exits too).
2. Verifies every SL order's quantity == position quantity (Kite silently
   defaults to qty=1 on modify); fixes mismatches immediately.
3. Runs the phase lifecycle:
   - **Phase 1** (< +1R): SL untouched.
   - **Phase 2** (≥ +1R): one-time move to breakeven.
   - **Phase 3** (≥ +1.5R): mechanical trail at 2×sl_pct from LTP, ratchet-only,
     0.5% min-move between successive trails (first trail after breakeven exempt).
   - Stop-hunt guard on every placement: never within 0.3% of PDH/PDL/day-H/L.
4. Time rules (IST): alerts 14:00 / 14:30 / 14:45, `no_new_entries` after 14:30,
   full square-off at 15:10 (before Zerodha's 15:15 force square-off).
5. Kill switch: day realised R ≤ −2.0 → `kill_switch` flag (no new entries).
6. Writes `data/status.json` (snapshot) and `data/events-<date>.jsonl` (audit
   log for post-session RCA), sends macOS notifications on every transition.

Safety: modify results are verified before state advances; a rejected/dead SL
order with the position still open is re-placed immediately; a cycle crash
never kills the process; killing the daemon is safe (broker SLs keep resting).

## Setup (once)

```
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`.env` needs `KITE_API_KEY` and `KITE_API_SECRET` (chmod 600).

At https://developers.kite.trade set the app's **redirect URL** to
`http://localhost:3100/kite-token-exchange` (or always use `login --paste`).

## Daily use

**Full guide: [docs/USAGE.md](docs/USAGE.md)** — login to advanced, one place.

```
.venv/bin/python -m algo start          # the whole morning in one command:
                                        #   login (skipped if token valid) + background daemon
.venv/bin/python -m algo status         # session dashboard
.venv/bin/python -m algo stop           # halt daemon (broker SLs stay active)
.venv/bin/python -m algo add-position INDIGO --sl-pct 1.0 --pdh 4205 --pdl 4080
.venv/bin/python -m algo squareoff      # manual emergency flatten
```

`add-position` writes `data/risk.json` — the handoff file the Claude skill
writes after placing entries (sl_pct per symbol, optional pdh/pdl). Without a
seed, sl_pct is derived from the virgin SL order (logged as a warning); a
position with *no* SL order and no seed triggers a modal alert.

## Tests

```
.venv/bin/python -m pytest tests/ -q
```

Includes regression tests for the two canonical incidents (DRREDDY fixed-5%
trail flaw, INDIGO stop-hunt SL) from sl-rules.md.

## Acceptance path before trusting it with money

1. `algo positions` after a morning login — discovery matches reality.
2. One full market session with `--dry-run` — diff the `DRY_RUN_INTENT` events
   against what the Claude loop actually did.
3. First live session with a single small position.

## Not in v1 (deliberate)

Entry placement, sector ranking, position sizing, KiteTicker websocket,
launchd service, NSE holiday calendar (add dates to `data/holidays.txt`,
one YYYY-MM-DD per line), true intraday swing detection (day-H/L used).
