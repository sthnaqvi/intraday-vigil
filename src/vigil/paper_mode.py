"""Whether this session is trading against a simulated broker or a real one.

A marker file, not a config value: `vigil start --paper` writes it, `vigil login` (a
live-Kite action) clears it. Every command's `_live_broker()` call and the dashboard's
account panel both check it, so paper mode — once entered — applies everywhere in the
session without needing `--paper` repeated on every single command, and without the
dashboard trying to fetch a real Kite account that was never configured.
"""
from __future__ import annotations

from pathlib import Path

from . import config

_MARKER_NAME = "paper_mode"


def _marker_path() -> Path:
    return config.DATA_DIR / _MARKER_NAME


def is_paper_mode() -> bool:
    return _marker_path().exists()


def set_paper_mode(enabled: bool) -> None:
    marker = _marker_path()
    if enabled:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        marker.write_text("paper trading — no real broker, no real money\n")
    else:
        marker.unlink(missing_ok=True)
