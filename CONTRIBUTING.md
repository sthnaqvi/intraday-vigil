# Contributing

## Setup

```bash
pip install -e ".[kite,dev,lint]"
```

## Before you send a change

```bash
pytest tests/ -q                                    # 160+ tests, must stay green
ruff check src/ tests/                                # lint
mypy src/vigil/models.py src/vigil/ports.py src/vigil/rules.py \
     src/vigil/market_profile.py src/vigil/state.py    # mypy strict, core modules only
lint-imports                                          # core must never import Kite specifics
```

The test suite runs with `pytest-socket`'s `--disable-socket` (see `pyproject.toml`) — it
is structurally incapable of reaching a real broker. If a new test needs network-shaped
behavior, it needs a fake (`tests/mock_kite.py`, `PaperAdapter`) or `pytest.mark.enable_socket`
with a clear reason, not a real connection.

## Money-path changes

Anything touching `src/vigil/rules.py`, `monitor.py`, `state.py`, `guard.py`, or an
adapter is a money-path change. For these:

- Add or extend a test in `tests/` — a behavior change with no new test coverage is not
  reviewable.
- Run `tests/test_replay_golden.py` and confirm the event stream is still byte-identical
  unless your change is deliberately behavioral, in which case update the expected stream
  and explain why in the PR description.
- If the change affects order placement/modification, run the conformance suite
  (`pytest tests/conformance/ -v`) — it must pass for every adapter, not just the one you
  were testing against.

## Adding a broker adapter

See `docs/adding-a-broker.md` — the port contract, the two contracts that matter most
(a resting stop must survive the daemon dying; a modify's return means accepted, not
applied), and what the conformance suite checks.

## Docs

If you add or rename a CLI subcommand, `tests/test_usage_docs.py` will fail until
`docs/usage.md` mentions it — that's deliberate, not a bug to work around.

## Incidents and the dual-track policy

If you're documenting a real production incident (yours or one you're fixing), keep the
mechanism and the lesson but scrub anything that discloses account activity: no calendar
dates, no absolute currency amounts, no PDH/PDL price levels, no funds/margin figures, no
order or client ids. Use R-multiples and ratios instead — see `docs/incidents/` for the
pattern. Symbols are fine to keep. If you need to keep the verbatim version for your own
records, `private/` is gitignored for exactly that.

## Commit style

Explain *why*, not just *what* — the diff already shows what changed. A commit message
that only restates the diff in prose isn't pulling its weight.
