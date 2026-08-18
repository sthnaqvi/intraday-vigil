# vigil — Complete Usage Guide

From zero to a fully-automated trading day. The system has two halves:

| Half | What it does | Where |
|---|---|---|
| **Python daemon** (this repo) | Everything mechanical after entry: SL phases, breakeven, trailing, qty verification, time alerts, 15:05 square-off, kill switch | `~/Others/vigil` |
| **Claude skill** (`/intraday-trader`) | Everything needing judgment: morning bias, macro theme, sector ranking, entries, post-session RCA | `~/.claude/skills/intraday-trader` |

They talk through three small files in `data/` (see [The contract](#the-contract-how-the-skill-and-daemon-connect)).

---

## 1. One-time setup

Already done on this machine, listed for reinstalls:

```bash
cd ~/Others/vigil
/opt/homebrew/bin/python3.13 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

1. `.env` in the project root with `KITE_API_KEY` and `KITE_API_SECRET` (`chmod 600 .env`).
2. At https://developers.kite.trade set your app's **redirect URL** to
   `http://localhost:3100/kite-token-exchange`.
3. Optional: `alias vigil='~/Others/vigil/.venv/bin/python -m vigil'` in `~/.zshrc` —
   the rest of this doc assumes it.

---

## 2. The basic day (one command)

```bash
vigil start
```

That's it. `start` chains everything:

1. **Login** — if today's token is still valid it's silent; otherwise a browser tab
   opens for Kite login (token captured automatically on redirect, saved to
   `data/token.json`, valid until ~6 AM tomorrow).
2. **Daemon launch** — the monitor loop starts in the background (survives closing
   the terminal). Started before 9:15? It waits for the bell on its own.
3. From then on it runs the whole session hands-off and **exits by itself after the
   15:05 square-off** (deliberately ahead of Zerodha's own 15:10 MIS force-square).

Place your trades however you like — through the Claude skill or manually in the
Kite app. The daemon auto-discovers any MIS position within one cycle (≤150 s) and
starts managing its SL. You'll get a desktop notification for every event that
matters: position discovered, phase change, SL moved, SL hit, time alerts. (macOS
via osascript, Linux via notify-send, or a terminal fallback — a live daemon
refuses to start with neither available unless you pass `--allow-silent`.)

Check in any time:

```bash
vigil status
```

```
Daemon:  running (pid 43210) | mode live | snapshot 42s ago
SYMBOL      DIR     QTY     ENTRY       LTP      R        P&L  PH        SL
INDIGO      LONG    100   4150.00   4210.00  +1.45   +6000.00   2   4150.00
Day realised: Rs +0.00 (+0.00R)
```

Stop early if you ever need to (`vigil stop`) — resting SL orders stay live at the
exchange, so stopping the daemon never leaves you unprotected.

First few sessions: run `vigil start --dry-run` — identical behaviour, but every
SL modification is only *logged* (`DRY_RUN_INTENT` events), nothing touches real
orders. Diff the log against what you'd have done manually before going live.

---

## 3. Command reference

`vigil --help` and `vigil <command> --help` are the live source of truth — this
table is a copy of it, not hand-maintained, so if the two ever disagree the
`--help` output wins.

| Command | What it does |
|---|---|
| `vigil start [--dry-run] [--force] [--paste] [--allow-silent]` | Morning one-shot: login if needed + background daemon |
| `vigil stop` | Stop the daemon (broker SLs remain active) |
| `vigil login [--force] [--paste]` | Just the login step; `--paste` if the browser redirect can't work |
| `vigil positions` | Raw broker view: open MIS positions + the SL order guarding each |
| `vigil status [--json]` | Session dashboard; `--json` prints the raw snapshot |
| `vigil add-position SYM --sl-pct 1.0 [--pdh X --pdl Y]` | Seed risk info for a symbol (writes `data/risk.json`) |
| `vigil monitor [--dry-run] [--force] [--allow-silent]` | The loop in the foreground (what `start` runs for you) |
| `vigil squareoff [--yes]` | Emergency: cancel all SLs + market-exit everything now |
| `vigil enter SYM --side long\|short --qty N --sl-pct X [--pdh X --pdl Y]` | Open a MIS position + SL now — no MCP session needed |
| `vigil arm SYM --side ... --above/--below PRICE --qty N --sl-pct X [--auto]` | Arm a price trigger watched over the tick WebSocket |
| `vigil add SYM --qty N` | Scale into an open position (rewrites the risk seed) |
| `vigil exit SYM` | Exit ONE symbol: cancel its SL, then market-exit |
| `vigil protect SYM` | Re-place a missing SL on an open position |
| `vigil web [--port 8765]` | Local dashboard — **can place orders** behind typed confirmation; binds to `127.0.0.1` only, always |
| `vigil ask [question] [--pending] [--answer ID --text ...]` | Ask Claude (runs the CLI if present, else queues) |
| `vigil quote SYM [SYM...]` | LTP + OHLC without the MCP session |
| `vigil triggers` | List armed / fired triggers |
| `vigil disarm [SYM]` | Cancel armed triggers (all, or one symbol) |

`--allow-silent` on `start`/`monitor` lets a live daemon run without a
detected desktop notifier (macOS osascript / Linux notify-send). Without it,
a live daemon with no working notifier refuses to start — you'd otherwise get
zero alerts for SL hits, unprotected positions, or token expiry.

---

## 4. The lifecycle rules the daemon enforces

Source of truth: `~/.claude/skills/intraday-trader/references/sl-rules.md`.

- **Phase 1** (profit < +1R): SL is untouchable.
- **Phase 2** (≥ +1R): SL moved to breakeven, once. `quantity` always passed
  (Kite silently resets to 1 otherwise) and every modify is re-verified.
- **Phase 3** (≥ +1.5R): mechanical trail at **2 × sl_pct** from LTP. Ratchet-only,
  minimum 0.5% between successive trails (the first trail after breakeven is
  exempt — with DRREDDY's canonical numbers the threshold would block activation).
- **Stop-hunt guard** on every SL placement: never within 0.3% of PDH, PDL, or
  today's high/low — pushed 0.3% beyond, in your favour.
- **Timeline (IST)**: alerts at 14:00 / 14:30 / 14:45 · `no_new_entries` flag from
  14:30 · full square-off at 15:05 (ahead of Zerodha's 15:10 force square-off —
  the daemon must finish before the broker does, or it takes the broker's
  market fill instead of a controlled exit).
- **Kill switch**: day's realised R ≤ −2.0 → `kill_switch: true` in status.json.
  The daemon manages existing positions but the skill must refuse new entries.
- **Exit detection** is by SL order status `COMPLETE` — a profitable trail exit is
  recognized just like a losing stop.

## 5. The contract (how the skill and daemon connect)

| File | Writer | Reader | Content |
|---|---|---|---|
| `data/risk.json` | skill (or `vigil add-position`) | daemon | `{"INDIGO": {"sl_pct": 0.01, "pdh": 4205, "pdl": 4080}}` |
| `data/status.json` | daemon, every cycle | skill / you | Full session snapshot (positions, phases, P&L, flags) |
| `data/events-<date>.jsonl` | daemon | skill's RCA mode | Append-only audit log of every decision |

Flow: skill places entries + initial SLs via MCP → writes `risk.json` seeds →
daemon discovers positions and takes over → skill's MONITOR mode just renders
`status.json` (stale snapshot = daemon not running → it tells you to `vigil start`)
→ skill's RCA mode replays the events file after 15:30.

Without a seed the daemon still works: it derives sl_pct from the untouched SL
order (logged as a warning). A position with **no SL at all** and no seed triggers
a modal alert — seed it with `vigil add-position` and the daemon places the SL-M
itself on the next cycle.

## 6. Advanced

**Restart recovery.** Kill the daemon, restart it, crash mid-modify — all safe.
Broker state (positions, orders, triggers) is re-read as the source of truth;
`data/session-<date>.json` restores sl_pct/phase/ledger; a breakeven SL is even
recognized from `trigger == entry` if the session file is lost.

**Token expiry mid-session.** The daemon never crashes on it: you get a modal
alert, it retries every 60 s, and the resting SLs keep protecting you. Run
`vigil login` in another terminal; the daemon picks the new token up on its own
process restart — or just `vigil stop && vigil start`.

**Event log anatomy.** Each line of `events-<date>.jsonl`:
`{"ts", "type", "symbol", "data"}`. Key types: `POSITION_DISCOVERED`,
`PHASE_CHANGE`, `SL_MODIFY` (with `from_trigger`/`to_trigger`/`reason`/`guard_applied`),
`SL_MODIFY_VERIFIED`, `SL_MODIFY_REJECTED`, `SL_REPLACED` (dead order re-placed),
`SL_QTY_FIX`, `SL_HIT`, `ORPHAN_SL_CANCELLED`, `TIME_ALERT`, `SQUAREOFF_*`,
`KILL_SWITCH`, `DRY_RUN_INTENT`, `WARNING`, `ERROR`. Grep examples:

```bash
grep SL_MODIFY data/events-$(date +%F).jsonl | python3 -m json.tool --json-lines
```

**Holidays.** Weekends are automatic; add NSE holidays to `data/holidays.txt`
(one `YYYY-MM-DD` per line). `--force` overrides all market-hours guards.

**PDH/PDL sources**, in priority order: `risk.json` seed → Kite historical API
(paid add-on; a failure just logs a warning) → today's running high/low only.

**Logs.** Human-readable: `logs/algo.log` (rotating) and `logs/daemon.out`
(background daemon's console). Structured: the events JSONL.

## 7. Troubleshooting

| Symptom | Fix |
|---|---|
| `auth: No valid token for today` | `vigil login` (tokens die ~6 AM daily) |
| Login browser tab errors after credentials | Redirect URL at developers.kite.trade must be exactly `http://localhost:3100/kite-token-exchange`; or use `vigil login --paste` |
| Login timeout (15 min) | Rerun; check nothing else owns port 3100 (`lsof -iTCP:3100`) |
| `vigil status` says STALE | Daemon died or was never started — `vigil start` (check `logs/daemon.out`) |
| "SL qty mismatch fixed" notifications | Working as intended — that's the Kite qty=1 default being caught |
| `historical_data unavailable` warning | Personal API keys lack the paid historical add-on; seed pdh/pdl via `add-position` or let the skill write them |
| Daemon refuses to run | Market closed — `--force` to override (e.g. testing on a weekend) |
