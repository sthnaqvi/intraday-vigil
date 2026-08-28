# Changelog

## 0.2.0 — 2026-08-29

### Added
- `.github/workflows/release.yml` — on a `v*` tag push, verifies the tag matches
  `pyproject.toml`'s version, builds the package, publishes a GitHub Release (with the
  matching CHANGELOG section as its body and the wheel/sdist attached), and uploads to
  PyPI via trusted publishing (OIDC — no stored token). `workflow_dispatch` lets an
  already-pushed tag be released retroactively.
- Dashboard: a "Closed today" section on the Overview pane, just below Armed triggers, so
  today's exits are visible without switching to the Events pane — with Qty and Entry
  columns added to that table on both its Overview and Events copies.

### Changed
- Dashboard hero card now leads with Total P&L (realized + unrealized) instead of
  unrealized alone, with Unrealized and Realized broken out as their own sign-colored
  lines below it. Also fixes a latent CSS specificity bug where those lines never
  actually rendered red/green regardless of sign.
- Dashboard account header now shows both M2M unrealized and M2M realized (was
  unrealized-only), matching what the Account pane's detail table already had.
- `TRIGGER_FIRED` and `vigil enter`'s detail line now report the *effective* stop width
  and rupee risk from the fill — not the caller's requested `sl_pct` — alongside the
  requested value as `input_sl_pct`. A widened stop-hunt guard or a slipped fill used to
  leave both of these carrying a number the trade had already invalidated.
- `vigil enter`'s stop-hunt guard can still widen a placed SL past the 1.5% cap when
  clearing a nearby PDH/PDL/day-H/L level (the position is already open by then, so
  refusing isn't viable). This is now loudly flagged — a distinct `SL_CAP_EXCEEDED_POST_GUARD`
  event plus a notification — instead of only discoverable by hand-checking `vigil status
  --json` afterward.

### Fixed
- A closed position's day-R contribution was summed per exit leg instead of
  quantity-weighted per position, double-counting any position that exited in more than
  one piece and able to trip `KILL_SWITCH_R` on a day barely 1R down.
- On a dual-stack host where IPv6 won the default route, every order call
  (`place_order`/`modify_order`/`cancel_order`) egressed from an address never on Kite's
  IP whitelist and was rejected — invisibly, since read endpoints ignore the whitelist and
  keep working, so the daemon still reported itself healthy. Outbound connections are now
  pinned to IPv4 (`VIGIL_FORCE_IPV4=0` to opt out); `vigil start` logs which family broker
  traffic actually leaves on.
- CI's `test` job failed collection on every Python version — `tests/` was missing
  `__init__.py`, so bare `pytest` (what CI runs, vs. `python -m pytest` used locally)
  couldn't resolve the `from tests.X import Y` absolute imports used across 9 test files.

### Docs
- Recorded two process findings from a zero-trade session RCA in `docs/incidents/`: an
  expectancy figure computed on the wrong sample (wrong conditioning, one session in one
  regime, and non-wins treated as full stop-outs rather than including the
  drift-to-breakeven case) that nearly talked the user out of a setup a proper study later
  showed to be positive-expectancy; and a sector ranking run four hours after the open
  with its percent-from-open metric left stale, so the top-ranked sector's apparent lead
  had actually finished over an hour earlier.

## 0.1.0 — 2026-08-23

First tagged release. Carries the `0.1.0.dev0` work below from a poll-driven prototype to
a real-time, tick-driven daemon with a live dashboard and an opt-in Claude bridge.

### Added
- Real-time, tick-driven SL decision engine: breakeven and mechanical trailing now react
  to price the instant a WebSocket tick arrives, replacing the old shared 150s/90s poll —
  a holdover from this project's very first version, when the *skill itself* polled to
  save Claude tokens, a constraint that stopped applying once SL decisions moved into a
  separate, deterministic daemon that never calls Claude. Broker-truth reconciliation,
  qty-drift verification, and a tick-staleness fallback (for a dropped socket or paper
  mode's simulated feed) each now run on their own independent cadence instead of sharing
  one interval.
- `vigil web`: a real-time cockpit dashboard, pushed over Server-Sent Events
  (`GET /api/stream`) instead of a 3-second poll — positions, armed triggers, and the
  event log update the instant something changes. Full 7-pane redesign (Overview, Trade,
  Events, Daemon, Account, Logs, Claude) with a sticky Day P&L/Available/Protection header
  and a functional live-log dock (search, per-source switching, pin-to-bottom).
- Skill automation: on a naked position, the kill switch tripping, a position reaching
  its trailing phase, or an approaching time-alert cutoff, the daemon can now proactively
  queue a situation-shaped Claude request itself (`AUTO_ENQUEUE_ENABLED`,
  `AUTO_MONITOR_INTERVAL_S` in `config.py`) — a second, non-blocking channel (runs on its
  own background thread) alongside the existing desktop notification, never a replacement
  for it. The two "bigger move" triggers (phase 3, kill switch) queue a REASSESS request;
  the two immediate-procedural ones (a naked position, a time alert) queue MONITOR. Off by
  default; REASSESS can only ever propose, never place or modify an order unattended.
- `vigil restart` — stop (if running) + start, in the correct order, reusing `stop`'s
  existing wait-until-actually-gone polling so it can't race a fresh monitor loop onto a
  position the old one hasn't finished shutting down on.
- `vigil skill-install` — one verified command replacing the manual `ln -s` + `readlink`
  install dance, refusing to silently clobber an unrelated symlink or a real directory.
  The skill now ships bundled inside the wheel itself (`pyproject.toml`'s `force-include`
  maps it to `vigil/_skill/intraday-vigil`), so a plain `pip install intraday-vigil[kite]`
  is enough — no repo clone needed for the common case.
- A broker order-margin calculator (`BrokerClient.order_margins()`, implemented for Kite,
  stubbed for paper mode) — removes a margin-sizing guess that was twice misdiagnosed the
  same wrong way by hand (dividing a rejection's *total* required margin by the attempted
  quantity instead of its own shortfall figure), once being the exact difference between a
  session closing red and closing green.
- `vigil stop` now refuses (absent `--i-know`) inside the pre-squareoff window with
  positions still open — stopping the daemon there has cost real money once (the exit
  handed to the broker's own forced closure) and been saved by luck once. A rule that only
  survives being remembered mid-session isn't a safety net; see
  `docs/incidents/discipline-and-process.md`.

### Changed
- `notify()`/`alert_dialog()` now log via `events.logger` unconditionally, before the OS
  notification backend is even attempted, instead of only falling back to a terminal echo
  if the backend failed. A successful OS toast used to mean the terminal running `vigil
  monitor` — the primary way this tool is actually watched — never showed what fired; miss
  the toast and you got a sound and nothing else. Now every alert is durably visible in
  both the CLI console and `logs/algo.log` (already a Live Log dock source).
- A reconciled position already flat at the broker with no `COMPLETE` fill on its own SL
  order is now labelled by what actually happened — a broker-forced closure gets its own
  distinct reason — instead of defaulting to `MANUAL_EXIT` even when the daemon wasn't
  running to see the real cause. Realized P&L was always correct; only `exit_reason` was
  wrong, and only in this one gap.
- README, `docs/usage.md`, and `docs/user-guide.md` brought current with everything above
  — neither the real-time dashboard nor skill automation was mentioned anywhere before this
  release. README gained a value-proposition section (a DIY-vs-hardened comparison and a
  named comparison against Zerodha GTT/Streak/Tradetron/AlgoTest/Chartink/DIY Python),
  placed after Install/Daily use rather than before them. `CONTRIBUTING.md`'s setup steps
  now cover installing the skill and exercising a change in paper mode, not just running
  the test suite.

### Fixed
- `GuardedBroker`'s dry-run path crashed on the very first `--dry-run` mutation
  (`emit() got multiple values for argument 'symbol'`) — the one path meant to make
  trial-and-error safe.
- The Claude skill wasn't actually reachable from a plain `pip install intraday-vigil` —
  `skill/` sits beside `src/`, not inside the packaged `vigil` module, so the wheel never
  included it and `vigil skill-install` had nothing to link without a full repo clone.
  Fixed by bundling the skill into the wheel itself (see "Added" above); verified against
  a real built wheel installed into a throwaway venv with no repo anywhere nearby.
- `TICKER_RESUBSCRIBED` was firing on every failed reconnect attempt, not just successful
  ones, and would have retried far more often than intended against a persistent failure
  once the run loop's own heartbeat sped up — now backed off and only announced on real
  success.

## 0.1.0.dev0 — unreleased

First open-source release, rewritten from a private, single-account daemon into a
multi-broker-capable package.

### Added
- `BrokerClient` port (`src/vigil/ports.py`) with a `GuardedBroker` safety wrapper
  (dry-run, call spacing, retry, audit) usable with any adapter.
- `PaperAdapter` — a real in-process simulated broker with its own order book, plus a
  conformance suite (`tests/conformance/`) run against every adapter.
- Domain models (`Position`, `Order`, `Quote`) with zero broker-specific field names.
- `MarketProfile` — session hours and squareoff timing as one validated object; the
  daemon now refuses to construct if the squareoff head start over the broker's own
  force-square rule is too thin, and the run loop clamps its sleep so it can't oversleep
  past a scheduled action.
- `PriceFeed` abstraction (`KiteTickerFeed` push, `PollingFeed` pull) and a
  transport-free `TriggerEngine`, replacing duplicated trigger-matching logic that used
  to exist separately (and without a shared lock) in the WebSocket handler and the poll
  fallback.
- `vigil paths` — resolves the state directory for tooling and the skill, replacing
  hardcoded filesystem paths.
- Full docs set: quickstart, usage, safety, architecture, adding-a-broker, markets, and
  dual-track incident write-ups (`docs/incidents/`).
- The Claude Code skill, migrated into this repo (`skill/intraday-vigil/`), split into a
  router plus per-mode reference files, and de-personalized.

### Changed
- Renamed `algo` → `vigil` throughout; moved to a `src/` layout; became pip-installable
  with a `vigil` console-script entry point.
- Renamed the published package and the skill to `intraday-vigil` (from the PyPI name
  `vigil`, already claimed by an unrelated package, and the skill's old generic
  `intraday-trader`). The `vigil` CLI command is unchanged — `pip install
  "intraday-vigil[kite]"` still gives you `vigil start`/`vigil status`/etc. — so this is a
  packaging and discoverability change, not a rewrite of daily usage.
- Position/order reads go through typed models instead of raw broker dicts.
- `cli.py` split into `commands/*.py`, one module per command group.
- Default `squareoff_at` preponed from 15:05 to 15:00 (`MarketProfile`'s `NSE` instance) —
  derived alerts shift 5 minutes earlier with it. Data-driven, not a guess:
  `docs/research/squareoff-timing.md`.

### Fixed
- The base package (no `[kite]` extra) crashed on import because `auth.py` imported
  `kiteconnect` at module level — found by the packaging verification's own clean-venv
  smoke test, not by inspection.
- Several money-path verification gaps from real production incidents — see
  `docs/incidents/verification-gaps.md` for the full write-ups: an SL quantity fix that
  was recorded as successful without re-reading the order to confirm it, and a cancelled
  stop that went undetected because reconciliation only checked newly-discovered
  positions.
