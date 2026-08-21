# Quickstart

First session, start to finish, no broker account and no money at risk. Every command
below runs against `PaperAdapter`, a real simulated broker with its own order book — the
same code path Kite uses, not a stub.

## 1. Install

```bash
pip install "intraday-vigil[paper]"
```

Confirm it landed:

```bash
vigil --help
```

## 2. Start a paper session

```bash
vigil start --paper --force
```

`--force` just means "run even if the market is closed right now" — useful for trying
this out outside trading hours. You'll see something like:

```
Daemon started (PAPER, pid 12345). It waits for the 09:15 bell if early,
squares off at 15:05, and exits on its own. `vigil status` any time; `vigil stop` to halt.

Paper mode — no real broker, no real money. Next: place a simulated trade with
`vigil enter`, or open the dashboard with `vigil web` to watch it.
```

That daemon is now running in the background, doing nothing yet — it has no position to
manage. Check on it any time with `vigil status`:

```
Daemon:  running (pid 12345) | mode live | broker paper | snapshot 3s ago
*** PAPER MODE — simulated broker, no real money at risk ***

No open positions.

Day realised: Rs +0.00 (+0.00R)
```

## 3. Open the dashboard

```bash
vigil web
```

This starts a local web server and prints the address it's listening on
(`http://127.0.0.1:8765` by default). **Open that URL in your own browser** — the command
doesn't open a browser tab for you, you have to navigate there yourself. Leave this
command running in its own terminal (or background it); it serves the dashboard for as
long as it's alive.

The header shows a blue **PAPER — no real money** badge the whole time you're in paper
mode, so it's never ambiguous whether real money is involved.

## 4. Set a starting price and place a trade

A paper session has no real market feeding it prices — you move the price yourself with
`vigil paper-price`. Open a **second terminal** (leave `vigil web` running in the first)
and set a starting price before entering, or the trade would fill at zero:

```bash
vigil paper-price DEMO 100.00
vigil enter DEMO --side long --qty 10 --sl-pct 1.0 --yes
```

`--sl-pct 1.0` means a 1% stop — `vigil enter` computes the stop price from the current
paper price and places both the entry and the stop atomically, same as it would against
Kite. **This trade exists at the broker immediately** (`vigil positions` shows it right
away) — but the background daemon only *discovers* it on its own next reconcile pass
(every ~20s by default, `config.RECONCILE_INTERVAL_S`), so `vigil status` and the
dashboard can take up to that long to show it for the first time. That's not a bug or a
hang; broker-truth reconciliation is a periodic poll (Kite has no push alternative for
order/position state itself) — SL decisions, once a position is tracked, react to price
the instant a tick arrives, not on a poll.

## 5. Watch the SL lifecycle run

Move the price up 1% (one "R") and watch the daemon move the stop to breakeven — the
background daemon from step 2 reacts to the price change directly, no cycle to wait on:

```bash
vigil paper-price DEMO 101.00
```

Give it a cycle or two (watch the dashboard's **Recent Events** panel, or re-run
`vigil status`) — you'll see a `PHASE_CHANGE` event and the stop move to entry price. Move
it further to trigger the mechanical trail (Phase 3 at +1.5R):

```bash
vigil paper-price DEMO 103.00
```

Move it back down through the resting stop to close the position:

```bash
vigil paper-price DEMO 100.50
```

`vigil status` now shows it in **Closed today**, with realised R and P&L.

## 6. Square off and review

```bash
vigil squareoff --yes    # cancels any resting stops, market-exits anything still open
vigil stop                # halts the daemon; harmless to run even if it already exited
```

For the post-session review, read today's event log — every decision the daemon made,
in order:

```bash
vigil paths --json    # find data_dir, then:
cat "$(vigil paths --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["data_dir"])')"/events-$(date +%F).jsonl
```

The Claude skill's RCA mode (`/intraday-vigil rca`) does this same walk automatically and
scores the session — see `skill/intraday-vigil/references/mode-rca.md`.

## Next: a live Kite session

```bash
pip install "intraday-vigil[kite]"
```

1. Create a Kite Connect app at [developers.kite.trade](https://developers.kite.trade) and
   set its redirect URL to `http://127.0.0.1:3100/kite-token-exchange`.
2. Put `KITE_API_KEY` and `KITE_API_SECRET` in the daemon's env file (`vigil paths --json`
   → `state_dir` → `.env`, `chmod 600` it).
3. `vigil start --dry-run` — logs in for real, runs the full loop, but every SL
   modification is only *logged* (as `DRY_RUN_INTENT` events), nothing touches a real
   order. Run at least one full session this way and diff the log against what you'd
   expect before going live.
4. `vigil login` any time ends a paper session and switches back to your real account.

**Read [`docs/safety.md`](safety.md) before dropping `--dry-run`.**

## Reference

This page is `vigil` on its own — placing trades yourself, by hand. For how this fits
together with the Claude skill that actually picks stocks and sizes trades for you, see
**[`docs/user-guide.md`](user-guide.md)** — read it if `vigil start` didn't do what you
expected it to.

- [`docs/usage.md`](usage.md) — every command
- [`docs/sl-rules.md`](sl-rules.md) — exactly what the daemon does and why
- `skill/intraday-vigil/` — the Claude skill for everything upstream of the daemon
  (sector selection, entry timing, post-session review)
