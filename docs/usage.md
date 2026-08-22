# Usage

Every `vigil` subcommand. `vigil --help` and `vigil <command> --help` are the live source
of truth — `tests/test_usage_docs.py` asserts every subcommand name appears somewhere on
this page, so the two can't drift apart the way an earlier version of this project's docs
once did (documenting under half its actual commands).

## Daily flow

```bash
vigil start           # login if needed + background daemon; idempotent, safe to repeat
vigil status           # session dashboard
vigil stop             # halt the daemon (broker SLs stay active)
```

Place trades however you like — through the Claude skill, manually in the broker's own
app, or with the commands below. The daemon auto-discovers any open position within one
cycle and starts managing its stop. First sessions: run with `--dry-run` and diff the
logged `DRY_RUN_INTENT` events against what you'd expect (see `docs/safety.md`).

## Daemon lifecycle

| Command | What it does |
|---|---|
| `vigil start [--dry-run] [--force] [--paste] [--paper] [--allow-silent]` | Morning one-shot: login if needed + background daemon |
| `vigil stop` | Stop the daemon (broker SLs remain active) |
| `vigil restart [--dry-run] [--force] [--paste] [--paper] [--allow-silent]` | `stop` (if running) + `start`, in the order that matters — same flags as `start`. Resting SLs live at the broker and today's tracked state is on disk either way, so this loses nothing. |
| `vigil login [--force] [--paste]` | Just the login step; `--paste` if the browser redirect can't work. Also ends any paper session. |
| `vigil monitor [--dry-run] [--force] [--paper] [--allow-silent]` | The loop in the foreground (what `start` backgrounds) |
| `vigil paths [--json]` | Where this install keeps its state (VIGIL_HOME/XDG resolution) |

`--allow-silent` lets a live daemon run without a detected desktop notifier. Without it, a
live daemon with no working notifier **refuses to start** — you'd otherwise get zero
alerts for SL hits, unprotected positions, or token expiry. (Paper mode and `--dry-run`
are exempt — neither places a real order, so a missing notifier isn't a safety issue.)

`--paper` runs against a simulated broker instead of Kite — no account, no credentials,
no real money, and `vigil start --paper` skips login entirely. It's sticky: once you start
in paper mode, every other command (`enter`, `status`, `web`, ...) stays in paper mode
without needing the flag again, until you run `vigil start` without `--paper` or
`vigil login`. See `docs/quickstart.md` for a full walkthrough.

## Reading state

| Command | What it does |
|---|---|
| `vigil positions` | Raw broker view: open MIS positions + the SL order guarding each |
| `vigil status [--json]` | Session dashboard; `--json` prints the raw snapshot |
| `vigil quote SYM [SYM...]` | LTP + OHLC without the MCP session |
| `vigil triggers` | List armed / fired triggers |
| `vigil paper-price SYM PRICE` | Paper mode only: move a symbol's simulated price, filling any resting stop it crosses |

## Placing and managing orders

| Command | What it does |
|---|---|
| `vigil enter SYM --side long\|short --qty N --sl-pct X [--pdh X --pdl Y] [--yes]` | Open a position + SL now — no MCP session needed |
| `vigil arm SYM --side ... --above/--below PRICE --qty N --sl-pct X [--auto]` | Arm a price trigger watched over the price feed |
| `vigil add SYM --qty N` | Scale into an open position (rewrites the risk seed) |
| `vigil add-position SYM --sl-pct 1.0 [--pdh X --pdl Y]` | Seed risk info for a symbol (writes `risk.json`), no order placed |
| `vigil protect SYM [--trigger P] [--force]` | Re-place a missing SL on an open position, preserving phase history |
| `vigil exit SYM [--yes]` | Exit ONE symbol: cancel its SL, then market-exit |
| `vigil squareoff [--yes]` | Cancel all SLs + market-exit everything now |
| `vigil disarm [SYM]` | Cancel armed triggers (all, or one symbol) |

`enter`, `add`, `arm`, and `protect` all refuse `sl_pct > 1.5%` and enforce the entry gate
(kill switch, `no_new_entries`, the hard cutoff) — override the gate only with
`--override-gate`, and only when you mean it.

## Automatic exits (independent of the resting SL)

| Command | What it does |
|---|---|
| `vigil arm-exit SYM --above/--below PRICE [--note ...]` | Arm an automatic exit on SYM — fires with **no confirmation** |
| `vigil exit-triggers` | List armed / fired exit triggers |
| `vigil disarm-exit [SYM]` | Cancel armed exit triggers (all, or one symbol) |

An exit trigger is a second, independent watch on top of the resting SL — "close this the
moment price crosses X," for taking profit early or cutting a loss faster than the
mechanical trail would. Unlike `vigil arm` (which defaults to alert-only), an exit trigger
always fires automatically: arming one only makes sense if breaking the level also closes
the position. On fire, the daemon cancels the resting SL and market-exits at whatever
quantity is actually open — read fresh from the broker, not assumed — so it fires correctly
even if the position size changed since the trigger was armed. Works on any symbol with an
open (or soon-to-be-open) position, whether or not that symbol also has an entry trigger.

## Dashboard and the Claude bridge

| Command | What it does |
|---|---|
| `vigil web [--port 8765]` | Local dashboard — **can place orders** behind typed confirmation; binds to `127.0.0.1` only, always |
| `vigil ask [question] [--pending] [--answer ID --text ...]` | Ask Claude (runs the CLI if present, else queues) |
| `vigil skill-install [--force]` | Symlink the Claude skill into `~/.claude/skills/` — bundled with the package itself, so this works after a plain `pip install`, no repo clone needed. Verifies the result, refuses to clobber an unrelated symlink or a real directory without `--force` (never touches a real directory even then). |

See `docs/safety.md` for exactly what the dashboard's confirmation flow guarantees.

**Skill automation** (`AUTO_ENQUEUE_ENABLED`, `AUTO_MONITOR_INTERVAL_S` in `config.py`): the
daemon can queue a Claude request itself, through the same `claudelink.enqueue()` path
`vigil ask` uses, without you asking. `AUTO_ENQUEUE_ENABLED` (default `True`) is the master
switch for all of the below; `AUTO_MONITOR_INTERVAL_S` (default `0`, off) additionally
gates a periodic MONITOR check on a timer, independent of the events below.

| Trigger | Mode queued | Why |
|---|---|---|
| A tracked position's SL vanishes and stays naked (`AUTO_REPROTECT` off, or the re-protect attempt itself fails) | `monitor` | Immediate protection question, not a bigger-picture one |
| An approaching time-alert cutoff (`alert_1400`/`alert_1430`/`alert_1445`) | `monitor` | Same — a procedural check, not a move |
| A position reaches phase 3 (trailing) | `reassess` | A real move — worth re-checking sector rank and thesis, not just protection status |
| The kill switch trips | `reassess` | The day itself is the "bigger move" — re-check every open position |
| Timer, if `AUTO_MONITOR_INTERVAL_S` > 0 | `monitor` | The one trigger with an ongoing token cost even when nothing happened — off by default |

Every queued request runs on its own background thread (`enqueue()`'s subprocess can take
up to 180s — never on the daemon's own loop) and lands in the same queue `vigil ask
--pending` reads. REASSESS can only ever propose a new entry; it can't place or modify
anything unattended either way.

## The state contract

| File (under `vigil paths --json` → `data_dir`) | Writer | Reader | Content |
|---|---|---|---|
| `risk.json` | skill or `vigil add-position`/`enter`/`arm` | daemon | `{"SYMBOL": {"sl_pct": 0.01, "pdh": 4205, "pdl": 4080}}` |
| `status.json` | daemon, every cycle | skill / `vigil status` | Full session snapshot: positions, phases, P&L, flags |
| `events-<date>.jsonl` | daemon | skill's RCA mode, `vigil ask` context | Append-only audit log of every decision |
| `triggers.json` | `vigil arm`/`disarm` | daemon | Armed entry-trigger state |
| `exit_triggers.json` | `vigil arm-exit`/`disarm-exit` | daemon | Armed exit-trigger state |

Without a `risk.json` seed the daemon still works: it derives `sl_pct` from the untouched
SL order (logged as a warning). A position with **no SL at all** and no seed triggers a
modal alert — seed it with `vigil add-position` and the daemon places the SL itself on the
next cycle.

## Advanced

**Restart recovery.** Kill the daemon, restart it, crash mid-modify — all safe. Broker
state (positions, orders, triggers) is re-read as the source of truth every cycle;
`session-<date>.json` restores `sl_pct`/phase/the realised-R ledger; a breakeven SL is even
recognized from `trigger == entry` if the session file is lost.

**Token expiry mid-session** (Kite). The daemon never crashes on it: you get a modal alert,
it retries every 60s, and the resting SLs keep protecting you. Run `vigil login` in
another terminal; the daemon picks up the new token on its own process restart — or just
`vigil stop && vigil start`.

**Event log anatomy.** Each line of `events-<date>.jsonl`: `{"ts", "type", "symbol",
"data"}`. Key types: `POSITION_DISCOVERED`, `PHASE_CHANGE`, `SL_MODIFY` (with
`from_trigger`/`to_trigger`/`reason`/`guard_applied`), `SL_MODIFY_VERIFIED`,
`SL_MODIFY_REJECTED`, `SL_REPLACED` (dead order re-placed), `SL_QTY_FIX`, `SL_LOST`,
`SL_HIT`, `ORPHAN_SL_CANCELLED`, `TIME_ALERT`, `SQUAREOFF_*`, `KILL_SWITCH`,
`TRIGGER_ARMED`/`HIT`/`FIRED`/`BLOCKED`, `EXIT_TRIGGER_HIT`/`FIRED`/`FAILED`,
`TICKER_CONNECTED`/`CLOSED`/`ERROR`,
`DRY_RUN_INTENT`, `WARNING`, `ERROR`. Grep example:

```bash
grep SL_MODIFY "$(vigil paths --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["data_dir"])')"/events-$(date +%F).jsonl \
  | python3 -m json.tool --json-lines
```

**The audit trail.** `logs/actions.jsonl` (every CLI invocation or dashboard click, with
argv, exit code, duration) and `logs/api.jsonl` (every broker call — mutations verbatim,
reads summarised) both carry a `trace` id shared with the events file, so one action can be
followed end to end. See `docs/architecture.md` for how this differs from the event log.

**PDH/PDL sources**, in priority order: a `risk.json` seed → the broker's historical-data
endpoint (may be a paid add-on on some Kite plans; a failure just logs a warning) → today's
running high/low only.

**Logs.** Human-readable: `logs/algo.log` (rotating; the filename is an operational choice
kept as-is through the rename, not a leftover) and `logs/daemon.out` (the backgrounded
daemon's console). Structured: the events JSONL and the audit trail above.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `auth: No valid token for today` | `vigil login` (Kite tokens die ~6 AM IST daily) |
| Login browser tab errors after credentials | Redirect URL at developers.kite.trade must exactly match `LOGIN_LISTEN_PORT`/`LOGIN_REDIRECT_PATH` in `config.py`; or use `vigil login --paste` |
| Login timeout (15 min) | Rerun; check nothing else owns the listen port (`lsof -iTCP:3100`) |
| `vigil status` says STALE | Daemon died or was never started — `vigil start` (check `logs/daemon.out`) |
| "SL qty mismatch fixed" notifications | Working as intended — that's a documented broker quirk being caught, see `docs/incidents/verification-gaps.md` |
| `historical_data unavailable` warning | Some API keys lack the historical-data add-on; seed `pdh`/`pdl` via `add-position` instead |
| Daemon refuses to run | Market closed — `--force` to override (e.g. testing outside session hours) |
| `kiteconnect is not installed` | You're on the base install or `intraday-vigil[paper]` — `pip install "intraday-vigil[kite]"` for live Kite trading |
