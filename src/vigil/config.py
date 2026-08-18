"""All rule constants in one place. Spec: ~/.claude/skills/intraday-trader/references/sl-rules.md"""
import os
from pathlib import Path

from .market_profile import NSE

# PROJECT_ROOT is the source checkout directory (three levels up from this file: config.py
# -> vigil/ -> src/ -> repo root). Kept for introspection/debugging only — it must never be
# the base for anything this process WRITES. A pip-installed package's __file__ lives
# inside site-packages; deriving state paths from it would write a live broker access
# token, the daily P&L log, and armed order triggers into the install directory. That used
# to be exactly what DATA_DIR/LOGS_DIR/ENV_FILE/TOKEN_FILE did below, which is why this
# file was unpippable before that was fixed. It was ALSO used as a subprocess cwd when
# spawning `python -m vigil ...` (cli.py, webui.py) until the package became pip-
# installable (editable or otherwise): `-m vigil` now resolves correctly from any cwd, so
# neither call site pins one anymore.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _state_dir() -> Path:
    """Where this process's data/logs/config live. Resolution order:

    1. `VIGIL_HOME` — explicit override, e.g. to run two isolated sessions side by side.
    2. `XDG_STATE_HOME/vigil` — respected on any XDG-compliant system (most Linux).
    3. `~/.local/state/vigil` — same convention, used as the default everywhere including
       macOS. (`~/Library/Application Support/vigil` would be more idiomatic Mac, but a
       single cross-platform default is worth more than platform purity for a CLI tool
       whose users move between machines.)
    """
    if home := os.environ.get("VIGIL_HOME"):
        return Path(home).expanduser().resolve()
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "state"
    return (base / "vigil").resolve()


STATE_DIR = _state_dir()
DATA_DIR = STATE_DIR / "data"
LOGS_DIR = STATE_DIR / "logs"
ENV_FILE = STATE_DIR / ".env"
TOKEN_FILE = DATA_DIR / "token.json"
RISK_FILE = DATA_DIR / "risk.json"
STATUS_FILE = DATA_DIR / "status.json"
PID_FILE = DATA_DIR / "daemon.pid"
HOLIDAYS_FILE = DATA_DIR / "holidays.txt"

# SL lifecycle
PHASE2_R = 1.0            # breakeven threshold (profit in R)
PHASE3_R = 1.5            # trail threshold
TRAIL_MULT = 2.0          # trail_pct = TRAIL_MULT * sl_pct  (never a fixed 5%)
TRAIL_MIN_MOVE = 0.005    # only modify if SL moves > 0.5%
STOP_HUNT_BUFFER = 0.003  # keep SL 0.3% clear of PDH/PDL/day-H/L
NSE_TICK = NSE.tick

# Cadence
CYCLE_NORMAL_S = 150
CYCLE_NEAR_SL_S = 90
NEAR_SL_PCT = 0.005       # position "near SL" when LTP within 0.5% of trigger

# Risk
KILL_SWITCH_R = -2.0      # daily realised R at/below which no new entries

# When a tracked position's SL disappears (cancelled in the Kite app, pulled by the broker),
# should the daemon re-place it automatically? Default False: a user once cancelled a
# stop deliberately, and silently re-placing it would have overridden a decision they had
# just made. Detection and a loud, repeating alert are unconditional either way;
# `vigil protect SYMBOL` re-places on demand.
AUTO_REPROTECT = False

# Session times (IST) — all derived from the NSE MarketProfile (market_profile.py), whose
# __post_init__ enforces that SQUAREOFF_AT leaves a real head start before
# BROKER_SQUAREOFF_AT, and whose time_alerts() computes alert text from the actual
# configured times instead of hand-typed minute counts that could drift out of sync.
MARKET_OPEN = NSE.market_open
MARKET_CLOSE = NSE.market_close
NO_NEW_ENTRIES_AFTER = NSE.no_new_entries_after
# Zerodha force-squares MIS at 15:10 (auction/closing rule). The daemon MUST finish before
# that or the broker wins the race: we take its market fill instead of a controlled exit,
# and SQUAREOFF_FILL bookkeeping races a position that is already flat.
BROKER_SQUAREOFF_AT = NSE.venue_squareoff_at
SQUAREOFF_AT = NSE.squareoff_at
TIME_ALERTS = NSE.time_alerts()

# API behaviour
# Zerodha rejects API market orders (MARKET, and SL-M modifies) without market
# protection. Percentage cap on fill price away from the trigger. Keep this SMALL:
# 5 was rejected by the exchange with '16448: Difference between limit price and
# trigger price is beyond permissible range'. 1 is accepted.
# Note: market protection converts an SL-M into an SL (limit) at trigger*(1+pct),
# so a gap beyond that band can leave the stop unfilled.
MARKET_PROTECTION_PCT = 1
API_MIN_SPACING_S = 0.35
RETRY_BACKOFF_S = [2, 4, 8]
MAX_FAILED_CYCLES_BEFORE_ALERT = 3

# Auth — must match the redirect URL registered at developers.kite.trade:
# http://localhost:3100/kite-token-exchange
LOGIN_LISTEN_PORT = 3100
LOGIN_REDIRECT_PATH = "/kite-token-exchange"
TOKEN_VALID_FROM_HOUR_IST = 6   # tokens generated before 06:00 IST today are stale
