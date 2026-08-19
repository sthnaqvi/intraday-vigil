# Changelog

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
- The Claude Code skill, migrated into this repo (`skill/intraday-trader/`), split into a
  router plus per-mode reference files, and de-personalized.

### Changed
- Renamed `algo` → `vigil` throughout; moved to a `src/` layout; became pip-installable
  with a `vigil` console-script entry point.
- Position/order reads go through typed models instead of raw broker dicts.
- `cli.py` split into `commands/*.py`, one module per command group.

### Fixed
- The base package (no `[kite]` extra) crashed on import because `auth.py` imported
  `kiteconnect` at module level — found by the packaging verification's own clean-venv
  smoke test, not by inspection.
- Several money-path verification gaps from real production incidents — see
  `docs/incidents/verification-gaps.md` for the full write-ups: an SL quantity fix that
  was recorded as successful without re-reading the order to confirm it, and a cancelled
  stop that went undetected because reconciliation only checked newly-discovered
  positions.
