"""Central audit trail: who asked for what, what we sent, what came back.

The event log says *what happened to a position*. It never said *what caused it* — there
was no record that a dashboard click ran `vigil start`, no record of the Kite reads a cycle
made, and no way to tie a button press to the order that resulted.

Everything here is append-only JSONL under `logs/`, and every record carries a **trace id**
so one action can be followed end to end:

    actions.jsonl   a command was requested (web click / CLI invocation) and its result
    api.jsonl       every Kite call — mutations in full, reads summarised
    web.jsonl       every HTTP request the dashboard served
    events-*.jsonl  domain events (already existed), now stamped with the same trace

A trace propagates into subprocesses through VIGIL_TRACE_ID, so a click in the browser and
the order it places share one id.
"""
from __future__ import annotations

import json
import os
import threading
import uuid
from typing import Any

from . import clock, config

TRACE_ENV = "VIGIL_TRACE_ID"
SOURCE_ENV = "VIGIL_TRACE_SOURCE"

_local = threading.local()
_write_lock = threading.Lock()


def new_trace() -> str:
    return uuid.uuid4().hex[:12]


def current_trace() -> str:
    """Trace for this process/thread: inherited from the parent, else stable per process."""
    explicit = getattr(_local, "trace", None)
    if explicit:
        return explicit
    inherited = os.environ.get(TRACE_ENV)
    if inherited:
        return inherited
    if not hasattr(_local, "generated"):
        _local.generated = new_trace()
    return _local.generated


def set_trace(trace: str | None) -> None:
    _local.trace = trace


def source() -> str:
    return os.environ.get(SOURCE_ENV, "cli")


def child_env(trace: str, src: str) -> dict[str, str]:
    """Environment for a subprocess so it keeps the same trace."""
    env = dict(os.environ)
    env[TRACE_ENV] = trace
    env[SOURCE_ENV] = src
    return env


def _write(filename: str, record: dict[str, Any]) -> None:
    """Append one JSONL record. Never raises — logging must not break trading."""
    try:
        config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str) + "\n"
        with _write_lock:
            with (config.LOGS_DIR / filename).open("a") as f:
                f.write(line)
    except Exception:
        pass


def _base(kind: str, trace: str | None = None) -> dict[str, Any]:
    return {
        "ts": clock.now_ist().isoformat(),
        "trace": trace or current_trace(),
        "src": source(),
        "kind": kind,
        "pid": os.getpid(),
    }


def action(kind: str, trace: str | None = None, **fields: Any) -> None:
    """A command was requested or completed. Goes to logs/actions.jsonl."""
    rec = _base(kind, trace)
    rec.update(fields)
    _write("actions.jsonl", rec)


def api(kind: str, **fields: Any) -> None:
    """A Kite call. Goes to logs/api.jsonl."""
    rec = _base(kind)
    rec.update(fields)
    _write("api.jsonl", rec)


def web(**fields: Any) -> None:
    """An HTTP request the dashboard served. Goes to logs/web.jsonl."""
    rec = _base("web.request")
    rec.update(fields)
    _write("web.jsonl", rec)


def summarise(value: Any, limit: int = 400) -> Any:
    """Compact stand-in for a large payload: shape, not contents.

    Reads happen every cycle for every symbol; storing full quote payloads would bury the
    signal and grow without bound. Counts and keys are what actually help when debugging.
    """
    try:
        if isinstance(value, list):
            return {"type": "list", "len": len(value)}
        if isinstance(value, dict):
            keys = list(value)[:12]
            return {"type": "dict", "len": len(value), "keys": keys}
        text = str(value)
        return text if len(text) <= limit else text[:limit] + f"…(+{len(text)-limit})"
    except Exception:
        return "<unsummarisable>"
