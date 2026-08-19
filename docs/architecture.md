# Architecture

How the pieces fit together, and why a few things are shaped the way they are.

## Layers

```
┌─────────────────────────────────────────────────────────────────────┐
│  cli.py            argparse wiring only — dispatches to commands/    │
│  commands/*.py     one function per subcommand, thin: parse args,    │
│                     call execution/state/monitor, print              │
│  webui.py           dashboard — shells out to the SAME CLI (see       │
│                     "why web shells out to CLI" below), never         │
│                     touches a broker directly                         │
└─────────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────────┐
│  monitor.py         the daemon loop: reconcile → qty check → SL       │
│                     lifecycle → time rules → status snapshot          │
│  triggers.py         Trigger (data model), TriggerEngine (transport-  │
│                     free: what to do when a price update arrives)     │
│  feed.py             PriceFeed: how a price update arrives             │
│                     (KiteTickerFeed push / PollingFeed pull)          │
│  execution.py        intent → port primitive (Direction → BUY/SELL)   │
│  state.py            SessionState, TrackedPosition, reconcile()        │
│  rules.py             pure decision functions — phases, trail, guard,  │
│                     sizing math. No I/O.                              │
│  market_profile.py   session hours, squareoff timing + its invariant  │
└─────────────────────────────────────────────────────────────────────┘
                              │
┌─────────────────────────────────────────────────────────────────────┐
│  ports.py            BrokerClient Protocol — 6 reads, 4 mutations      │
│  guard.py             GuardedBroker — dry-run, spacing, retry, audit   │
│                     for ANY adapter, by composition                    │
│  models.py            Position / Order / Quote — zero broker field     │
│                     names past this line                              │
├─────────────────────────────────────────────────────────────────────┤
│  kite_adapter.py +    Kite's mutation shape, wire-format mapping        │
│  mapping.py                                                            │
│  paper_adapter.py     in-memory simulated broker                       │
└─────────────────────────────────────────────────────────────────────┘
```

Each layer only calls downward. `monitor.py` never imports `kite_adapter.py` — an
import-linter contract in `pyproject.toml` enforces this in CI, not just by convention.

## Why the dashboard shells out to the CLI instead of calling the daemon's code directly

`webui.py`'s `run_command` builds an argv list and runs it as a subprocess
(`sys.executable -m vigil <cmd> ...`), the same way a terminal would. Three reasons this
beats an in-process call:

1. **One enforcement point.** The entry gate, the SL width cap, the kill switch, and audit
   logging all live in the CLI path. An in-process shortcut would need to duplicate all of
   it or risk a divergent code path with weaker guarantees than typing the command by hand.
2. **Process isolation.** A dashboard bug can't corrupt the running daemon's in-memory
   state or crash it — the dashboard and the daemon are always separate processes, and a
   subprocess that hangs or crashes doesn't take the server down with it.
3. **Auditability for free.** Every dashboard action produces the exact same
   `actions.jsonl` / `api.jsonl` trail a terminal invocation would, tagged with the same
   trace id that ties the click to the subprocess to every broker call it made — see
   `audit.py`'s module docstring.

The cost is latency (spawning a process per click) and that the dashboard can only do what
the CLI can. Both are accepted trade-offs for a tool that manages real money.

## Why prices stay `float`

`Position`, `Order`, and `Quote` (`models.py`) use `float` for every price field, not
`Decimal`. NSE trades in 0.05 rupee ticks with modest absolute price ranges — the
representable-precision risk `Decimal` guards against in, say, currency-pair or
high-value-asset trading doesn't materialize here, and every downstream consumer (Kite's
own API, JSON serialization for the audit log and status snapshot, the dashboard) is
already float-shaped. `rules.round_to_tick_favor` rounds explicitly to `NSE_TICK` (0.05)
at every price computation, which is the actual correctness boundary that matters — not
the float/Decimal choice itself. If a future adapter targets a market where sub-tick float
drift is a genuine risk (many small accumulating fills, or a market with very fine tick
sizes relative to price), that adapter's mapping layer is the right place to introduce
`Decimal`, not a blanket change to `models.py`.

## The event log and the audit trail are two different things

`events.py`'s `EventLog` records *domain* events — `PHASE_CHANGE`, `SL_HIT`,
`TRIGGER_FIRED` — one JSON line per event, read by the skill's MONITOR and RCA modes and
by `test_replay_golden.py`'s golden characterization test. `audit.py` records *causality* —
which command was invoked, with what arguments, by what trace id, and every broker call
that trace id's process made (`actions.jsonl`, `api.jsonl`, `web.jsonl`). An event says
what happened to a position; the audit trail says what caused it. Debugging "why is this
position in this state" starts with `audit.py`'s trace reconstruction, not the event log
alone — see the audit trail example in `docs/usage.md`.

## Domain models vs. mapping vs. adapters

`models.py` has zero broker-specific field names — no `tradingsymbol`, no nested `ohlc`
dict, no Kite transaction-type strings leaking through. `mapping.py` is the only module
that knows how to build a `Position`/`Order`/`Quote` from Kite's actual wire format,
and `kite_adapter.py` is the only module that knows Kite's *mutation* shape (`variety`,
`product`, `market_protection`). This split is what makes `paper_adapter.py` possible as a
~150-line class instead of a fork of the Kite adapter: it only has to produce `models.py`
objects and honor `ports.py`'s contract, never touch Kite's wire format at all. See
`docs/adding-a-broker.md` for the practical mechanics of writing a third one.
