"""Structured event log (JSONL, RCA input) + atomic status.json snapshot + rotating human log."""
from __future__ import annotations

import json
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from . import clock, config

logger = logging.getLogger("intraday-algo")


def setup_logging() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    fh = RotatingFileHandler(config.LOGS_DIR / "algo.log", maxBytes=2_000_000, backupCount=3)
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(fh)
    logger.addHandler(sh)


class EventLog:
    def __init__(self, data_dir: Path = config.DATA_DIR):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _events_path(self) -> Path:
        return self.data_dir / f"events-{clock.now_ist().date().isoformat()}.jsonl"

    def emit(self, type_: str, symbol: str | None = None, **data: Any) -> None:
        # `trace` ties this event to the action that caused it — a dashboard click, a CLI
        # invocation, a daemon cycle — and to the Kite calls in api.jsonl.
        from . import audit  # local import: audit imports config, events imports clock
        record = {"ts": clock.now_ist().isoformat(), "type": type_, "symbol": symbol,
                  "trace": audit.current_trace(), "src": audit.source(), "data": data}
        with self._events_path().open("a") as f:
            f.write(json.dumps(record) + "\n")
        extra = f" {symbol}" if symbol else ""
        logger.info("[%s]%s %s", type_, extra, json.dumps(data) if data else "")

    def write_status(self, snapshot: dict) -> None:
        tmp = config.STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(snapshot, indent=2))
        os.replace(tmp, config.STATUS_FILE)
