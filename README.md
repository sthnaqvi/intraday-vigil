# intraday-vigil

**bell-to-bell intraday trading, watched.**

![License: MIT](https://img.shields.io/badge/license-MIT-blue)
![Python 3.11+](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![Status: Alpha](https://img.shields.io/badge/status-alpha-orange)

A deterministic stop-loss-lifecycle daemon for intraday (MIS-style) NSE equity positions on
Zerodha Kite, paired with a Claude Code skill for the parts that need judgment — sector
selection, entry timing, post-session review. Once a position and its stop exist at the
broker, the daemon owns everything mechanical: breakeven, mechanical trailing, time-based
alerts, and a scheduled square-off — reacting to price the instant it moves, not on a poll,
with every action verified against the broker before it's trusted. A live web dashboard and
an optional, opt-in Claude bridge sit on top, so you can watch it work or let it flag you in
when something needs a decision.

> ⚠️ **This places real orders with real money when run against a live broker.** Read
> [`docs/safety.md`](docs/safety.md) before you point it at a funded account. Nothing here
> is investment advice — it's an execution tool, not a strategy. Start in
> [paper mode](docs/quickstart.md), with no broker account and no money at risk, and read
> the [known incidents](docs/incidents/) this project's own hard rules came from.

## Install

```bash
pip install "intraday-vigil[kite]"     # Zerodha Kite — needs a Kite Connect API key
# or
pip install "intraday-vigil[paper]"    # paper trading — no broker account needed
```

Both give you the `vigil` command. See [`docs/quickstart.md`](docs/quickstart.md) for a
first session end to end, starting in paper mode.

## Daily use

```bash
vigil start   # the whole session in one command: login if needed + background daemon
vigil status  # terminal session snapshot
vigil web     # live web dashboard at http://127.0.0.1:8765 — SSE-pushed, not polled
vigil stop    # halt the daemon (broker SLs stay active)
```

Full command reference, all 20+ subcommands: [`docs/usage.md`](docs/usage.md).

## What it does

1. Reconciles broker truth (positions + orders) against tracked state — new positions are
   auto-discovered, exits are detected via SL order status, including profitable trail
   exits.
2. Verifies every SL order's quantity matches the position's quantity (a documented broker
   quirk silently defaults an omitted quantity to 1 — see
   [`docs/incidents/verification-gaps.md`](docs/incidents/verification-gaps.md)); fixes
   mismatches immediately, and re-verifies the fix landed.
3. Runs the three-phase SL lifecycle — full spec in [`docs/sl-rules.md`](docs/sl-rules.md):
   - **Phase 1** (< +1R): SL untouched.
   - **Phase 2** (≥ +1R): one-time move to breakeven.
   - **Phase 3** (≥ +1.5R): mechanical trail at 2×`sl_pct` from LTP, ratchet-only.
   - Stop-hunt guard on every placement: never within 0.3% of the prior day's high/low or a
     clear intraday swing.
4. Time rules: scheduled alerts, a no-new-entries cutoff, and a full square-off timed to
   finish *before* the broker's own force-square rule — enforced at construction time, not
   just by convention (`src/vigil/market_profile.py`).
5. Kill switch: a configurable daily-loss threshold sets a flag that blocks new entries.
6. Writes a session snapshot and an append-only audit log on every check, and sends a
   desktop notification on every transition that matters (a live daemon **refuses to
   start** with no working notifier, unless you explicitly accept running silent).
7. `vigil web`: a local, cockpit-style dashboard pushed live over Server-Sent Events —
   positions, armed triggers, and the event log update the instant something changes, not
   on a fixed poll. Can also place and manage orders itself, behind the same typed
   confirmation the CLI requires.
8. Skill automation: on a naked position, the kill switch tripping, a position reaching its
   trailing phase, or an approaching time-alert cutoff, the daemon proactively queues a
   situation-shaped prompt for Claude on its own background thread — a second, non-blocking
   channel alongside the desktop notification above, never a replacement for it. Off by
   default for anything not tied to a real event (`AUTO_ENQUEUE_ENABLED`,
   `AUTO_MONITOR_INTERVAL_S` in `config.py`).

Safety properties: every SL modify is verified against a broker re-read before state
advances, never assumed from the API call not raising; a rejected or dead SL order with the
position still open is re-placed immediately; a failed check never kills the process;
killing the daemon is always safe — resting stops keep protecting you at the exchange.

## Multi-broker

The daemon talks to a broker through a small port (`src/vigil/ports.py`) — six reads, four
mutations, with no notion of "entry" vs "exit" at that layer, just BUY vs SELL. Two adapters
ship today: Kite (`src/vigil/kite_adapter.py`) and an in-process paper broker
(`src/vigil/paper_adapter.py`) that keeps its own simulated order book. Writing a new one is
a single class plus running `tests/conformance/test_broker_contract.py` against it — see
[`docs/adding-a-broker.md`](docs/adding-a-broker.md).

## The Claude skill

It's the companion Claude Code skill for everything the daemon doesn't decide for you —
morning bias, macro theme, sector ranking, entry timing, and post-session review. It ships
bundled with the `vigil` package itself — no separate download, no repo clone. Install it
with:

```bash
vigil skill-install
```

One command — it symlinks the skill into `~/.claude/skills/`, refuses to silently overwrite
an unrelated existing skill or a real directory (`--force` to repoint an existing symlink),
and verifies the link actually resolves before calling it done.

It talks to the daemon entirely through the `vigil` CLI — see the skill's own `SKILL.md`
for its hard rules, chief among them: **the skill never modifies an SL order once the
daemon owns it.** **Running `vigil start` does not invoke the skill or scan any stocks** —
sector ranking, entry timing, and post-session review only happen inside a conversation
with Claude where you ask for them (e.g. `/intraday-vigil start`).

The one exception, and it's opt-in: with skill automation enabled (see "What it does"
above), the daemon itself can queue a `/intraday-vigil monitor` or `/intraday-vigil
reassess` request when something noteworthy happens — a naked position, the kill switch, a
big move, an approaching cutoff. It can only ever *propose*; nothing it queues can place or
modify an order unattended. See [`docs/user-guide.md`](docs/user-guide.md) for the full
day-in-the-life walkthrough of how the two halves work together.

## Why vigil instead of a fresh script or a SaaS platform

Vigil *is* "build your own" — it's just one that already paid the tuition. Every hard rule
in [`docs/sl-rules.md`](docs/sl-rules.md) traces back to a real session that lost real money
before the rule existed — see [`docs/incidents/`](docs/incidents/).

| A fresh DIY script, day one | Already hardened in vigil |
|---|---|
| Omits a quantity on an SL modify — Kite silently defaults it to 1 share protected, no error | `GuardedBroker.modify_stop_order` asserts `quantity ≥ 1`; no code path can omit it |
| Reports a fix as successful because the API call didn't raise, while the exchange rejected it downstream | Every modify is re-read from the broker and compared before state advances — a mismatch emits an honest `SL_MODIFY_REJECTED` instead |
| Never notices a stop that vanished under a position it was already tracking, only checks on first discovery | Every tracked position's stop is checked every cycle, unconditionally |
| Uses one fixed trail %, which can sit *below* an existing breakeven stop and never fire | Trail is always `2 × sl_pct`, derived per-position — never a shared fixed percentage |
| Rounds every stop to one hardcoded tick size — wrong for the minority of instruments on a different tick | Tick size is looked up per instrument from the broker's own master, cached daily |
| Sizes right up to 100% of available margin, leaving nothing for the exit's own brokerage and STT | A fixed transaction-cost reserve is subtracted before quantity is computed, on every slot |

**Against commercial platforms** (Streak, Tradetron, AlgoTest, and similar no-code/low-code
tools): they optimize for breadth of strategies and ease of building, not for this one
narrow, high-stakes surface — SL-lifecycle correctness on an intraday MIS position. None of
them publish anything resembling a re-verify-every-mutation discipline or an incident log
driving their rule set; you're trusting a closed-source execution path you cannot read.
Vigil's entire order-mutation surface is ~5,400 lines you can actually audit, and a new
broker adapter is a documented, testable ~150-line class, not a support ticket.

**The genuinely uncommon part:** pairing a boring, deterministic, always-on daemon for the
dangerous mechanical part (order lifecycle) with an LLM for the judgment part (sector read,
entry timing, thesis decay) — with a hard, code-and-prompt-enforced boundary between the
two. The skill never modifies an SL order once the daemon owns it.

### Vigil against what an Indian retail intraday trader actually reaches for

Pricing and feature sets on commercial platforms change often — treat cost as a rough band,
not a quote.

| Tool | SL sophistication | Verify-after-write | Transparency | Cost | AI-assisted judgment |
|---|---|---|---|---|---|
| **Vigil + intraday-vigil** | Strong | Strong | Strong — open source | Free, self-hosted | Strong |
| Zerodha GTT | Weak — no phases, trail, or stop-hunt guard | Solid | None — closed | Free | None |
| Streak | Adequate | None | Weak | Subscription (tiered) | None |
| Tradetron | Adequate | None | Weak | Subscription + strategy fees | None |
| AlgoTest / Quantsapp | Adequate — options-only, not a fit for equity MIS | None | Weak | Subscription | None |
| Chartink + manual/webhook | None — scanner only, execution is DIY | None | Solid | Free–low | None |
| DIY Python + kiteconnect | Weak | Weak | Strong | Free (your time) | None unless self-built |

**Where vigil actually falls short**, said plainly and not buried above: only Kite has a
production broker adapter today (the port contract supports more — nobody's written them
yet); NSE equities only, by design (tick size, session times, and squareoff timing are all
NSE-specific); no strategy or backtesting engine — it manages risk on a position you already
decided to take, it doesn't find or backtest the trade for you; and it's early-stage (alpha,
single maintainer, no multi-year track record outside its own documented sessions yet).
There's no mobile app and no managed hosting — you run and own the process. Read the code
before trusting it with size.

## Tests

```bash
pip install -e ".[kite,dev]"
pytest tests/ -q
```

Includes a golden characterization test that replays a full scripted session and asserts
the exact ordered event stream, a conformance suite run against every broker adapter, and
regression tests for the incidents in [`docs/incidents/`](docs/incidents/).

## Docs

- [`docs/user-guide.md`](docs/user-guide.md) — **start here** — the whole system, both
  halves, a full day walkthrough of who does what
- [`docs/quickstart.md`](docs/quickstart.md) — first session, paper mode
- [`docs/usage.md`](docs/usage.md) — every command
- [`docs/safety.md`](docs/safety.md) — blast radius: what can place an order, what dry-run
  doesn't cover, why the dashboard is loopback-only
- [`docs/sl-rules.md`](docs/sl-rules.md) — the SL lifecycle spec
- [`docs/architecture.md`](docs/architecture.md) — how the pieces fit together
- [`docs/adding-a-broker.md`](docs/adding-a-broker.md) — the port contract, for a new adapter
- [`docs/markets.md`](docs/markets.md) — session hours, squareoff timing, holidays
- [`docs/research/squareoff-timing.md`](docs/research/squareoff-timing.md) — data-driven
  support for the squareoff-timing default
- [`docs/incidents/`](docs/incidents/) — real sessions that shaped the hard rules above

## License

MIT — see [`LICENSE`](LICENSE).
