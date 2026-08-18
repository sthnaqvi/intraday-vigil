"""Bridge from the daemon / web UI to Claude.

Two delivery paths, deliberately:

1. **CLI (automatic).** If a `claude` binary is reachable, a request is executed straight
   away and the answer stored. Nothing is installed on this machine today, so this path is
   dormant — but it lights up the moment one appears, with no code change.

2. **Queue (always).** Every request is appended to `data/claude-requests.jsonl` regardless.
   A Claude session reads pending items with `vigil ask --pending` and writes answers back
   with `vigil ask --answer <id> --text ...`.

The queue is the contract. The CLI is an optimisation on top of it.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from . import clock, config

REQUESTS_FILE = config.DATA_DIR / "claude-requests.jsonl"

PENDING = "pending"
ANSWERED = "answered"
FAILED = "failed"

# Where a `claude` binary might live. First hit wins; None means queue-only mode.
CLI_CANDIDATES = [
    "claude",
    str(Path.home() / ".claude" / "local" / "claude"),
    str(Path.home() / ".bun" / "bin" / "claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
]


def resolve_cli() -> str | None:
    for c in CLI_CANDIDATES:
        found = shutil.which(c) if os.sep not in c else (c if os.access(c, os.X_OK) else None)
        if found:
            return found
    return None


def _path() -> Path:
    # Read config at call time so tests can redirect DATA_DIR.
    return config.DATA_DIR / "claude-requests.jsonl"


def _read() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _rewrite(rows: list[dict]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
    os.replace(tmp, p)


def enqueue(question: str, context: dict | None = None, run_cli: bool = True) -> dict:
    """Record a question. Answer it inline if a CLI exists, else leave it pending."""
    req = {
        "id": uuid.uuid4().hex[:8],
        "ts": clock.now_ist().isoformat(),
        "question": question,
        "context": context or {},
        "status": PENDING,
        "answer": None,
    }
    rows = _read()
    rows.append(req)
    _rewrite(rows)

    cli = resolve_cli() if run_cli else None
    if cli:
        try:
            prompt = question
            if context:
                prompt = f"{question}\n\nLive context:\n{json.dumps(context, indent=2)}"
            proc = subprocess.run([cli, "-p", prompt], capture_output=True, text=True,
                                  timeout=180)
            if proc.returncode == 0:
                answer(req["id"], proc.stdout.strip() or "(empty response)")
            else:
                answer(req["id"], f"claude exited {proc.returncode}: {proc.stderr[:500]}",
                       status=FAILED)
        except Exception as e:
            answer(req["id"], f"claude invocation failed: {e}", status=FAILED)
        return next((r for r in _read() if r["id"] == req["id"]), req)
    return req


def answer(request_id: str, text: str, status: str = ANSWERED) -> bool:
    rows = _read()
    hit = False
    for r in rows:
        if r["id"] == request_id:
            r["answer"] = text
            r["status"] = status
            r["answered_at"] = clock.now_ist().isoformat()
            hit = True
    if hit:
        _rewrite(rows)
    return hit


def pending() -> list[dict]:
    return [r for r in _read() if r["status"] == PENDING]


def recent(limit: int = 20) -> list[dict]:
    return _read()[-limit:][::-1]
