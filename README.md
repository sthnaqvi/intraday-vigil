# vigil

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
   full square-off at **15:05** (deliberately ahead of Zerodha's own **15:10**
   MIS force-square-off — the daemon must finish before the broker does, or it
   takes the broker's market fill instead of a controlled exit).
5. Kill switch: day realised R ≤ −2.0 → `kill_switch` flag (no new entries).
6. Writes `data/status.json` (snapshot) and `data/events-<date>.jsonl` (audit
   log for post-session RCA), and sends a desktop notification on every
   transition (macOS via osascript, Linux via notify-send, otherwise a
   terminal fallback — see `vigil start --allow-silent` if neither is
   available and you accept running without alerts).

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
.venv/bin/python -m vigil start          # the whole morning in one command:
                                        #   login (skipped if token valid) + background daemon
.venv/bin/python -m vigil status         # session dashboard
.venv/bin/python -m vigil stop           # halt daemon (broker SLs stay active)
```

All 18 subcommands (`vigil --help` is the live source of truth; this table is
generated from it, not hand-maintained):

| Command | What it does |
|---|---|
| `start` | Morning one-shot: login if needed + run daemon in background |
| `stop` | Stop the background daemon (broker SLs stay active) |
| `login` | Daily Kite login (skips browser if token still valid) |
| `positions` | Live MIS positions + their SL orders |
| `status` | Session dashboard (`--json` for the raw snapshot) |
| `add-position` | Seed `sl_pct` (and pdh/pdl) for a symbol |
| `monitor` | Run the SL-lifecycle loop in the foreground (what `start` backgrounds) |
| `squareoff` | Cancel SLs and market-exit all MIS now |
| `enter` | Open a MIS position + SL now (no MCP needed) |
| `arm` | Arm a price trigger watched over the tick WebSocket |
| `add` | Scale into an open position (rewrites the risk seed) |
| `exit` | Exit ONE symbol (cancel its SL, then market-exit) |
| `web` | Local dashboard — **can place orders** behind typed confirmation; binds to localhost only, always |
| `ask` | Ask Claude (runs the CLI if present, else queues) |
| `protect` | Re-place a missing SL on an open position |
| `quote` | LTP + OHLC without the MCP session |
| `triggers` | List armed / fired triggers |
| `disarm` | Cancel armed triggers (all, or one symbol) |

`add-position` (and `enter`/`arm`/`add`) write `data/risk.json` — the handoff
file the Claude skill also writes after placing entries (sl_pct per symbol,
optional pdh/pdl). Without a seed, sl_pct is derived from the virgin SL order
(logged as a warning); a position with *no* SL order and no seed triggers a
modal alert.

## Tests

```
.venv/bin/python -m pytest tests/ -q
```

Includes regression tests for the two canonical incidents (DRREDDY fixed-5%
trail flaw, INDIGO stop-hunt SL) from sl-rules.md.

## Acceptance path before trusting it with money

1. `vigil positions` after a morning login — discovery matches reality.
2. One full market session with `--dry-run` — diff the `DRY_RUN_INTENT` events
   against what the Claude loop actually did.
3. First live session with a single small position.

## Not in v1 (deliberate)

Sector ranking and position sizing (the Claude skill's job, not this daemon's
— `enter`/`add`/`arm` take an explicit qty from the caller and place it, they
don't decide it), a launchd/systemd service definition (currently
`vigil start` + your own terminal session or `screen`/`tmux`), true intraday
swing-high/low detection for the stop-hunt guard (today's running day-H/L is
used as one of three PDH/PDL sources — see docs/USAGE.md).

Entry placement, the KiteTicker websocket (armed triggers), and the NSE
holiday calendar (`data/holidays.txt`, one `YYYY-MM-DD` per line) are all
implemented — an earlier version of this list called them out as missing
after they'd already shipped. If you're reading this and something else here
looks stale, `vigil --help` and the test suite are the ground truth.
