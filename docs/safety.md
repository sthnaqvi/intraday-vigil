# Safety

What can place an order, what dry-run does and doesn't cover, and why the dashboard is
loopback-only. Read this before running against a funded account.

## Blast radius — what can actually place, modify, or cancel an order

Every mutation goes through exactly one place: `GuardedBroker` (`src/vigil/guard.py`),
wrapping whatever adapter you've configured. Four operations exist at that layer —
`place_market_order`, `place_stop_order`, `modify_stop_order`, `cancel_order` — and
everything in this codebase that touches a live order goes through one of them:

- The daemon's monitor loop (breakeven moves, trailing, quantity fixes, square-off).
- `vigil enter` / `vigil add` / `vigil arm` (with `--auto`) / `vigil protect` /
  `vigil exit` / `vigil squareoff`.
- The dashboard (`vigil web`) — but only by shelling out to the same CLI commands above,
  as an argv list, never a raw request passed through. See "The dashboard" below.
- Armed triggers, whether delivered by the WebSocket feed or the polling fallback — both
  call the same `TriggerEngine.on_price` (`src/vigil/triggers.py`), under one lock.

Nothing else in the codebase constructs an order. `vigil status`, `vigil positions`,
`vigil quote`, and the skill's MONITOR/RCA modes are read-only by construction — they call
only the six read methods on the port.

## What `--dry-run` does and does not cover

`--dry-run` (on `vigil start` / `vigil monitor`) makes every mutation short-circuit into a
logged `DRY_RUN_INTENT` event instead of reaching the broker — see `GuardedBroker._dry` in
`src/vigil/guard.py`. It **does** cover:

- Every SL placement, modify, and cancel the lifecycle would have made.
- Entries and exits through the daemon's own commands.
- The full decision logic — phase transitions, the stop-hunt guard, quantity
  verification — since all of that runs identically in dry-run; only the final broker call
  is intercepted.

It does **not** cover:

- Anything placed directly through the broker's own MCP tools or app, bypassing the
  daemon entirely — the skill's hard rule against this exists partly for this reason.
- The dashboard's own typed-confirmation flow, which still requires the correct
  confirmation text even in dry-run (it's testing the confirmation UI, not the order).
- Fill behavior. Dry-run never asks "would this order have actually filled at this
  price" — it only proves what the daemon *intended* to send.

Run at least one full session with `--dry-run` and diff the `DRY_RUN_INTENT` stream
against what you'd expect by eye before ever dropping the flag.

## Why the dashboard is loopback-only

`vigil web` binds to `127.0.0.1` — unconditionally, with no flag to change it. It shows
live positions and can place real orders; there is no authentication layer, so exposing it
beyond localhost would mean anyone who can reach that port can trade your account. If you
need remote access, put an SSH tunnel or a reverse proxy with real authentication in front
of it — don't widen the bind address.

Every mutating action on the dashboard requires a **typed confirmation checked
server-side**: the symbol name for a single-symbol action, or the literal word for a full
square-off. This is enforced in `webui.py`'s `run_command`, not in the browser — a raw
`fetch` that skips the confirmation UI is refused the same way a browser click without
typing the confirmation is. Every command the dashboard runs goes through the real CLI as
an **argv list**, never a shell string, so the SL width cap, the entry gate, the kill
switch, and the hard cutoff all apply exactly as they do from a terminal.

If you extend the dashboard, keep those three properties: loopback-only bind, server-side
typed confirmation on anything that mutates, and argv-list execution with no shell
interpolation.

## Killing the daemon is always safe

Stopping the daemon (`vigil stop`, a crash, `Ctrl-C`, the machine losing power) never
removes protection: every resting stop lives at the broker, not in the daemon's memory. A
dead daemon means the SL lifecycle stops *advancing* — no more breakeven moves, no more
trailing — but whatever stop was last resting stays resting. Restarting the daemon
re-reads broker state as ground truth (`state.reconcile`) and picks up exactly where the
account actually is, not where the daemon last remembered it being.

The one thing a dead daemon cannot do is enforce the scheduled square-off
(`docs/markets.md`) — if you're relying on that and the daemon isn't running near the
close, exit manually (`vigil squareoff`) or expect the broker's own force-square rule to
take over, later and less controlled.

## Token / credential handling

`vigil login` writes the access token to `<state_dir>/data/token.json` with `chmod 600`.
`.env` (API key/secret) is never committed — see `.env.example` for the expected shape and
the top-level `.gitignore` for what's excluded. Treat both files like any other credential:
don't paste their contents anywhere, don't commit `VIGIL_HOME` if you point it somewhere
version-controlled.

## Reading further

- `docs/sl-rules.md` — exactly what the daemon decides and when.
- `docs/incidents/` — real failures, what let them happen, what changed as a result.
- `src/vigil/ports.py` — the port's own docstrings carry the contract every adapter must
  honor (a stop must rest at the venue and survive the daemon dying; a modify's return
  means accepted, not applied).
