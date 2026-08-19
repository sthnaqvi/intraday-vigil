# Adding a broker

Scope: single-leg cash-equity intraday positions with a venue-resting stop order. That's
what the daemon manages today, and it's the honest boundary of what this guide covers — a
broker whose model doesn't fit that shape (options, futures with margin calls, multi-leg
strategies) needs more than a new adapter.

## The contract

`src/vigil/ports.py`'s `BrokerClient` is a `Protocol`, not a base class — write a plain
class with the right methods, no inheritance required. Six reads:

```python
quotes(symbols: list[str]) -> dict[str, Quote]
positions_day() -> list[Position]
orders() -> list[Order]
margins() -> dict
instruments(exchange: str = "NSE") -> list[dict]
historical_daily(instrument_token: int, from_date: date, to_date: date) -> list[dict]
```

Four mutations — read the docstrings on `BrokerClient` in `ports.py` before implementing
these; they're not decoration:

```python
place_market_order(symbol, transaction_type, quantity, exchange="NSE") -> str
place_stop_order(symbol, transaction_type, trigger_price, quantity, exchange="NSE") -> str
modify_stop_order(order_id, trigger_price, quantity) -> str
cancel_order(order_id) -> str
```

Two contracts matter more than the rest:

- **`place_stop_order` must rest at the venue and survive this process dying.** If your
  broker can't guarantee that — no server-side stop order type, only a client-side
  watch-and-fire — do not fake it by polling in the background and calling it a stop. The
  daemon's whole safety model depends on "the stop protects the position even if the
  daemon is dead"; an adapter that can't provide that must say so (there's no capability
  flag wired up yet to auto-refuse live mode on this — see "What's not built yet" below —
  so today that means: don't ship the adapter for live trading until there is one).
- **`modify_stop_order` returning normally means accepted, not applied.** A venue can 200
  the request and still reject it server-side (wrong tick, price band, whatever) leaving
  the old stop resting unchanged. Callers always re-read the order and compare before
  trusting a modify — see `monitor.py`'s `_execute_intent` for exactly how. Your adapter
  must never synthesize a success it didn't actually confirm.

There's no `entry` or `exit` primitive — both are just `place_market_order` with the
transaction side chosen by the caller. That intent lives in `execution.py`, one layer up
from the port; don't add entry/exit-flavored methods to your adapter.

## Two examples already in the tree

- `src/vigil/kite_adapter.py` — the real thing, talks to Zerodha Kite. `mapping.py` next
  to it is the only place Kite's wire format (`tradingsymbol`, the `buy_price`/`sell_price`
  VWAP split, nested `ohlc`) gets translated into `models.py`'s broker-agnostic shapes.
- `src/vigil/paper_adapter.py` — a genuine simulated broker with its own in-memory order
  book, not a stub. Read this one first if you're writing a new adapter — it's short,
  self-contained, and shows the VWAP-blending and order-lifecycle mechanics without any
  broker-specific wire format to wade through.

## What GuardedBroker gives you for free

Wrap your adapter in `GuardedBroker` (`src/vigil/guard.py`) and you get dry-run gating,
call spacing, retry with backoff, and audit logging for nothing — none of it belongs in
the adapter itself. `Broker` (`src/vigil/broker.py`) shows the pattern for a convenience
wrapper: `class Broker(GuardedBroker): def __init__(self, kite, events, dry_run=False):
super().__init__(KiteAdapter(kite), events, dry_run=dry_run)`. Do the equivalent for your
adapter if it has its own natural constructor arguments, or just call
`GuardedBroker(YourAdapter(...), events, dry_run=...)` directly — there's no requirement
to wrap it.

## Prove it: the conformance suite

`tests/conformance/test_broker_contract.py` is parametrized over every adapter and tests
the *behavior* the daemon actually depends on — a market order opens/scales/closes a
position with the right VWAP, a stop order rests and can be modified and cancelled, quotes
reports what was set. Add your adapter to the `CASES` list (a factory function returning
`(adapter_instance, set_price_fn)`) and run:

```bash
pytest tests/conformance/ -v
```

Every test passing is the bar for "the port is honored." It deliberately does **not**
test price-driven auto-fill of a resting stop — that's simulator-specific behavior (see
`test_paper_adapter.py` for how `PaperAdapter` tests its own), not part of the port's
contract.

## What's not built yet

- **Capability flags.** The plan for this project calls for something like
  `caps.resting_stop: bool` on an adapter, checked at daemon startup to refuse live mode
  if the guarantee above can't be met. Not implemented — today that check is a human
  reading this doc before shipping an adapter for live trading, not code.
- **Generic exception classification.** `GuardedBroker._call` still catches
  `kiteconnect.exceptions.TokenException`/`NetworkException` directly rather than through
  a broker-agnostic error hierarchy, because Kite is still the only adapter with a
  token/network failure mode to classify. If your broker has meaningfully different
  failure modes (rate limits, a different auth-expiry story), you'll want to generalize
  this rather than bolt on more `except SomeVendorException` clauses.
- **Broker discovery via entry points.** `pyproject.toml` doesn't yet register adapters as
  pluggable `importlib.metadata` entry points — a third-party `vigil-dhan` package can't
  register itself with the daemon without a code change here first.

None of these block writing and testing an adapter; they block wiring a *new* adapter into
`vigil start`/`vigil monitor` as a `--broker` choice, which — like `--paper` on the CLI
(`docs/quickstart.md`) — is real, scoped work still open on top of a working port.
