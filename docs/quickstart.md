# Quickstart

First session, no broker account, no money at risk. Everything below runs against
`PaperAdapter`, an in-process simulated broker with its own order book — a genuine
implementation of the same port Kite uses, not a stub.

## Install

```bash
pip install "vigil[paper]"
```

Confirm it landed:

```bash
vigil --help
```

## The shape of a session

`vigil`'s daemon watches positions and manages their stop-loss lifecycle. In paper mode
there's no live price feed driving it — you're the market. A short interactive walk:

```python
from vigil.paper_adapter import PaperAdapter
from vigil.guard import GuardedBroker
from vigil.events import EventLog
from vigil.state import SessionState
from vigil.monitor import MonitorLoop
from vigil import execution
from vigil.rules import Direction

events = EventLog()
adapter = PaperAdapter()
broker = GuardedBroker(adapter, events, dry_run=False)

# Seed a price, then open a long the same way `vigil enter` would.
adapter.set_price("DEMO", 100.0)
execution.place_entry(broker, "DEMO", Direction.LONG, 10)
execution.place_sl(broker, "DEMO", Direction.LONG, 99.0, 10)   # 1% stop

session = SessionState(date="2026-01-01")
loop = MonitorLoop(broker, events, session, fetch_levels=False)
loop.cycle()
print(session.positions["DEMO"])   # phase 1, entry 100.0, sl 99.0

# Move price up 1R (= entry * sl_pct) — the daemon moves the stop to breakeven (100.0).
adapter.set_price("DEMO", 101.0)
loop.cycle()
print(session.positions["DEMO"].phase, session.positions["DEMO"].sl_price)  # 2 100.0

# Move price back down. It's now below the *breakeven* stop (100.0, not the original
# 99.0) — PaperAdapter fills the resting order the moment price crosses ITS trigger.
adapter.set_price("DEMO", 99.0)
loop.cycle()
print(session.positions)   # {} — the position closed at breakeven, not a loss
print(session.closed)      # exit_reason "BE_STOP", realized_r 0.0, realized_pnl 0.0
```

That's the lifecycle's whole point: once a position reaches +1R, its downside is gone —
the worst case from here is scratch, never the original stop.

That's the entire lifecycle end to end — discovery, breakeven, exit — with nothing but
`PaperAdapter` and a hand-driven price. The real daemon (`vigil start`) runs this same
`MonitorLoop.cycle()` on a timer against whichever broker adapter you've configured,
reading live prices instead of `set_price()` calls.

## Wiring it into the CLI (Kite)

Paper mode today is a library-level story, not yet a `vigil start --paper` CLI flag — see
`src/vigil/paper_adapter.py`'s module docstring for the adapter itself, which is fully
implemented and covered by the conformance suite; the CLI/persistence wiring on top of it
is open work. For a live Kite session:

```bash
pip install "vigil[kite]"
```

1. Create a Kite Connect app at [developers.kite.trade](https://developers.kite.trade) and
   set its redirect URL to `http://127.0.0.1:3100/kite-token-exchange`.
2. Put `KITE_API_KEY` and `KITE_API_SECRET` in the daemon's env file (`vigil paths --json`
   → `state_dir` → `.env`, `chmod 600` it).
3. `vigil start --dry-run` — logs in, runs the full loop, but every SL modification is only
   *logged* (as `DRY_RUN_INTENT` events), nothing touches a real order. Run at least one
   full session this way and diff the log against what you'd expect before going live.
4. `vigil status` any time; `vigil stop` to halt (resting stops stay live at the exchange).

Read [`docs/safety.md`](safety.md) before dropping `--dry-run`.

## Next

- [`docs/usage.md`](usage.md) — every command
- [`docs/sl-rules.md`](sl-rules.md) — exactly what the daemon does and why
- `skill/intraday-trader/` — the Claude skill for everything upstream of the daemon
  (sector selection, entry timing, post-session review)
