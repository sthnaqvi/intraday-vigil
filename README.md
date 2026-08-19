# vigil

**bell-to-bell intraday trading, watched.**

A deterministic stop-loss-lifecycle daemon for intraday (MIS-style) equity positions,
paired with a Claude Code skill for the parts that need judgment (sector selection, entry
timing, post-session review). The daemon owns everything mechanical once a position and
its stop exist at the broker: breakeven moves, mechanical trailing, quantity verification,
time-based alerts, and a scheduled square-off — every cycle, deterministically, with every
action verified against the broker before the daemon believes it happened.

> ⚠️ **This places real orders with real money when run against a live broker.** Read
> [`docs/safety.md`](docs/safety.md) before you point it at a funded account. Nothing here
> is investment advice — it's an execution tool, not a strategy. Start in
> [paper mode](docs/quickstart.md), with no broker account and no money at risk, and read
> the [known incidents](docs/incidents/) this project's own hard rules came from.

## What it does, every cycle

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
6. Writes a session snapshot and an append-only audit log every cycle, and sends a desktop
   notification on every transition that matters (a live daemon **refuses to start** with
   no working notifier, unless you explicitly accept running silent).

Safety properties: every SL modify is verified against a broker re-read before state
advances, never assumed from the API call not raising; a rejected or dead SL order with the
position still open is re-placed immediately; a cycle crash never kills the process; killing
the daemon is always safe — resting stops keep protecting you at the exchange.

## Install

```bash
pip install "vigil[kite]"     # Zerodha Kite — needs a Kite Connect API key
# or
pip install "vigil[paper]"    # paper trading — no broker account needed
```

Both give you the `vigil` command. See [`docs/quickstart.md`](docs/quickstart.md) for a
first session end to end, starting in paper mode.

## Daily use

```bash
vigil start           # the whole session in one command: login if needed + background daemon
vigil status           # session dashboard
vigil stop             # halt the daemon (broker SLs stay active)
```

Full command reference, all 20 subcommands: [`docs/usage.md`](docs/usage.md).

## Multi-broker

The daemon talks to a broker through a small port (`src/vigil/ports.py`) — six reads, four
mutations, with no notion of "entry" vs "exit" at that layer, just BUY vs SELL. Two adapters
ship today: Kite (`src/vigil/kite_adapter.py`) and an in-process paper broker
(`src/vigil/paper_adapter.py`) that keeps its own simulated order book. Writing a new one is
a single class plus running `tests/conformance/test_broker_contract.py` against it — see
[`docs/adding-a-broker.md`](docs/adding-a-broker.md).

## The Claude skill

`skill/intraday-trader/` is the companion Claude Code skill for everything the daemon
doesn't decide for you — morning bias, macro theme, sector ranking, entry timing, and
post-session review. Install it with:

```bash
ln -s "$(pwd)/skill/intraday-trader" ~/.claude/skills/intraday-trader
```

(or copy it, if you'd rather not symlink). It talks to the daemon entirely through the
`vigil` CLI — see the skill's own `SKILL.md` for its hard rules, chief among them: **the
skill never modifies an SL order once the daemon owns it.**

## Tests

```bash
pip install -e ".[kite,dev]"
pytest tests/ -q
```

Includes a golden characterization test that replays a full scripted session and asserts
the exact ordered event stream, a conformance suite run against every broker adapter, and
regression tests for the incidents in [`docs/incidents/`](docs/incidents/).

## Docs

- [`docs/quickstart.md`](docs/quickstart.md) — first session, paper mode
- [`docs/usage.md`](docs/usage.md) — every command
- [`docs/safety.md`](docs/safety.md) — blast radius: what can place an order, what dry-run
  doesn't cover, why the dashboard is loopback-only
- [`docs/sl-rules.md`](docs/sl-rules.md) — the SL lifecycle spec
- [`docs/architecture.md`](docs/architecture.md) — how the pieces fit together
- [`docs/adding-a-broker.md`](docs/adding-a-broker.md) — the port contract, for a new adapter
- [`docs/markets.md`](docs/markets.md) — session hours, squareoff timing, holidays
- [`docs/incidents/`](docs/incidents/) — real sessions that shaped the hard rules above

## License

MIT — see [`LICENSE`](LICENSE).
