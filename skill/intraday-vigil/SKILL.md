---
name: intraday-vigil
description: >
  Full-lifecycle intraday trading assistant for MIS-style equity positions, paired with
  the vigil daemon for SL lifecycle execution. TRIGGER on: /intraday-vigil,
  "start trading session", "check my positions", "trailing stop", "modify SL",
  "exit all positions", "end of day", "post-session review", "what sectors are moving",
  "intraday setup", "trade today". Use for any trading-session workflow from pre-market
  to post-session review.
prerequisites:
  - vigil installed and on PATH (`pip install intraday-vigil[kite]`, or
    `intraday-vigil[paper]` with no broker account) — see the top-level README for install
    and broker setup.
  - A broker adapter configured (Kite credentials in the daemon's env, or paper mode).
allowed-tools: Bash, Read
---

# Intraday Trader

> **Risk disclaimer.** This skill and the daemon it drives place real orders with real
> money when run against a live broker (not paper mode). Nothing here is investment
> advice. Read `docs/safety.md` before running against a live account, and confirm
> `vigil status` shows the mode you expect before trusting any output.

Full-session intraday trading workflow. The morning workflow (macro theme, sector ranking,
entries) is human+Claude. Once positions and SL orders exist at the broker, **the SL
lifecycle is owned by the `vigil` daemon** — a separate, always-installed Python process,
not this skill. One command runs it for the whole day: `vigil start` (logs in if needed,
launches the daemon in the background, squares off near the close and exits by itself).
State lives under `vigil paths` — never hardcode a path to it.

**HARD RULE — this skill never modifies, cancels, or re-places SL orders.** No
`modify_order` on SL orders, no qty fixes, no breakeven moves, no trailing. The daemon does
all of that deterministically every cycle and verifies its own modifies. This skill's job
during the session is to *read* the daemon's snapshot (`vigil status`) and advise.

**HARD RULE — nothing time-critical depends on the Kite MCP session (or any other
broker's MCP).** Its token expires in roughly an hour and needs a user click to renew.
Orders, armed triggers, and the whole SL lifecycle run on the daemon's own token, which
lasts the trading day. MCP is a convenience for research reads only. If MCP is down, say
so and keep working — trading is unaffected.

Polling for a real reason — waiting on a slow external process, confirming a change landed —
is fine; the daemon's own cycle doesn't make watching wrong. Two things that go wrong with a
poll, worth getting right rather than avoiding polling itself: never `grep` `vigil status
--json`'s output for more than one field, since it pretty-prints one key per line and a
same-line multi-field pattern can never match — it fails *silently*, no error, just a
condition that's never true; parse it with `python3 -c "import json,sys; ..."` or `jq`
instead. And give a background poll a bound (a max iteration count or timeout), so a
condition that's wrong for some other reason surfaces as a timeout rather than spinning
unnoticed — one loop with a broken grep condition ran for 4+ hours in a live session before
anyone noticed it was checking nothing.

Quotes without MCP: `vigil quote SYMBOL [SYMBOL...]`

## Division of labour

| Responsibility | Owner |
|---|---|
| Macro read, sector ranking, stock scoring | Skill (START / REASSESS) |
| Entry orders + initial SL placement | **Daemon** (`vigil enter`, skill decides the trade) |
| Armed triggers — watch + fire | **Daemon** (`vigil arm`, price feed) |
| Writing risk seeds after entries | **Daemon** (skill only for MCP-placed or corrected entries) |
| Breakeven move, mechanical trail, qty verification, stop-hunt guard | **Daemon** |
| Time alerts, square-off, kill switch | **Daemon** |
| Session snapshot + audit log | **Daemon** |
| Rendering status, manual exits, post-session review | Skill (MONITOR / EXIT / RCA) |

## Modes

| Command | When to use | Reference |
|---|---|---|
| `/intraday-vigil start` | Morning — begin the session | `references/mode-start.md` |
| `/intraday-vigil monitor` | During session — render the daemon's snapshot | `references/mode-monitor.md` |
| `/intraday-vigil reassess` | Mid-session, user-triggered — re-rank sectors, add/exit | `references/mode-reassess.md` |
| `/intraday-vigil exit` | Late session — square off all MIS manually | `references/mode-exit.md` |
| `/intraday-vigil rca` | After the close — post-session analysis | `references/mode-rca.md` |

Read the **entire** reference file for the mode in use, in full, before starting that
mode — don't fetch it step-by-step as you go. This router stays small so a MONITOR call
doesn't pay for START's steps, but once you know which mode you're in, load everything
that mode needs up front: `mode-start.md` **and** `references/sector-universe.md` **and**
`references/sector-macro-map.md` together before Step 1, not sector-universe.md only when
you reach Step 4. Gap, VIX, macro theme, and sector selection are interdependent — reading
ahead is what lets you flag things like "the theme you're about to pick conflicts with
what Step 4's ranking will show" instead of discovering it two steps later.

Show your work at each step — the tables, the computed numbers, the reasoning behind an
auto-suggestion — the same level of detail the reference file itself models, not a
compressed summary of it. If the user's message could support more than one reasonable
next action, ask which they want rather than picking one silently.

If the user says just `/intraday-vigil` with no sub-command, ask which mode they want.

## Entry gate (every new entry, in any mode)

Before placing any entry order:
1. Read `vigil status --json`. If it exists and is fresh (see freshness rule below):
   - `kill_switch: true` → **refuse the entry.** State the day's realised R and that no new
     entries are allowed; manage existing positions only.
   - `no_new_entries: true` → **refuse the entry** and show `no_new_entries_reason`.
2. Regardless of daemon state (including missing/stale status): enforce the local **hard
   cutoff at `no_new_entries_after`** (14:30 IST by default — check `vigil status --json`
   or the daemon's config for the actual configured value). No recovery trades, no revenge
   trades, no "high conviction" exceptions. See `docs/incidents/discipline-and-process.md`
   for what ignoring this cost in practice.

**Freshness rule** (used here and in MONITOR): a snapshot is fresh if
`now − as_of ≤ 2 × daemon.cycle_seconds`. Otherwise treat it as stale — the daemon may not
be running. Tell the user plainly and offer to run `vigil start` (idempotent — safe to
run again if the daemon is already up).

## Key constants (defaults — see `docs/sl-rules.md` for the full spec and how to change them)

- 1R = `entry_price × sl_pct` (risk per share; `sl_pct` is the effective, post-guard value)
- Phase 1→2 (breakeven): `profit_R ≥ 1.0` — daemon-executed
- Phase 2→3 (trail): `profit_R ≥ 1.5`, trail at `2 × sl_pct`, never a fixed percentage —
  daemon-executed
- SL width cap: `sl_pct` above 1.5% is refused everywhere an SL is set
- Stop-hunt buffer: 0.3% from prior-day high/low or a clear intraday swing — skill applies
  this at entry time, daemon applies it on every trail
- Daemon cadence: ~150s standard, faster when a position is near its SL; a snapshot older
  than 2× cadence is stale
- Entry gate: `no_new_entries` / `kill_switch` in the daemon's status, plus the hard cutoff
- Kill switch: default trigger is day realised R ≤ −2.0 (`src/vigil/config.py`)

## Daemon CLI (all on the daemon's own broker session — no MCP needed)

```
vigil start | stop | status | positions | login | paths
vigil enter SYM --side long|short --qty N --sl-pct 1.0 [--pdh X --pdl Y] [--yes]
vigil arm   SYM --side long|short --above|--below PRICE --qty N --sl-pct 1.0 [--auto]
vigil triggers | disarm [SYM]
vigil add-position SYM --sl-pct 1.0    # seed only, no order
vigil squareoff [--yes]                # cancel SLs + market-exit everything
vigil add   SYM --qty N                # scale into an open position; rewrites the risk seed
vigil exit  SYM                        # exit ONE symbol (cancels its SL first)
vigil protect SYM [--trigger P]        # re-place a missing SL, preserving phase history
vigil quote SYM [SYM...]               # LTP/OHLC without MCP
vigil web   [--port 8765]              # local dashboard — loopback only, can place orders
vigil ask   "question" | --pending | --answer ID --text "..."
```

`enter`, `add`, `arm`, and `protect` all refuse `sl_pct > 1.5%` and enforce the entry gate.

## Dashboard and the Claude queue

`vigil web` serves a dashboard bound to `127.0.0.1` only — never reachable off the machine
it runs on — because it can place real orders. It exposes the same CLI surface described
above, through the real CLI as an argv list (never a shell string), so every safety rule
above still applies. Anything that moves money requires a typed confirmation checked
server-side (the symbol name, or the literal word for a full square-off) — a raw request
that skips the confirmation UI is refused the same way. See `docs/architecture.md` for the
full design and `docs/safety.md` for exactly what dry-run does and doesn't cover.

Check the Claude queue at the start of MONITOR and whenever the user mentions the
dashboard: `vigil ask --pending`. Each pending item carries a live snapshot of positions as
context — including whether each is currently protected — so answer from that, not from
memory. Write the answer back with `vigil ask --answer <id> --text "..."` so it appears in
the dashboard.

## Reference files

Always read the one relevant to the current mode before acting:
- `references/mode-start.md`, `mode-monitor.md`, `mode-reassess.md`, `mode-exit.md`,
  `mode-rca.md` — the five modes above.
- `references/sector-universe.md` — sector/stock universe for dynamic ranking.
- `references/sector-macro-map.md` — macro-theme rank adjustments.
- `docs/sl-rules.md` (repo root) — SL lifecycle spec, source of truth for the daemon;
  this skill applies only the entry-time subset of it.
- `docs/incidents/` (repo root) — real sessions that shaped the hard rules in this skill.
