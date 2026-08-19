"""Commands that reach outside the daemon: the local dashboard, the Claude bridge."""
from __future__ import annotations

import json
import sys

from .. import config


def cmd_web(args) -> int:
    from .. import webui
    webui.serve(port=args.port)
    return 0


def cmd_ask(args) -> int:
    """Bridge to Claude: enqueue a question, list pending ones, or write an answer back."""
    from .. import claudelink

    if args.pending:
        rows = claudelink.pending()
        if not rows:
            print("No pending questions.")
            return 0
        for r in rows:
            print(f"--- {r['id']}  {r['ts'][11:19]}")
            print(f"Q: {r['question']}")
            if r.get("context", {}).get("positions"):
                print(f"   context: {json.dumps(r['context']['positions'])[:200]}")
        print("\nAnswer with: vigil ask --answer <id> --text \"...\"")
        return 0

    if args.answer:
        if not args.text:
            print("--answer needs --text", file=sys.stderr)
            return 2
        ok = claudelink.answer(args.answer, args.text)
        print("Answer saved." if ok else f"No request with id {args.answer}", )
        return 0 if ok else 3

    if not args.question:
        print("Give a question, or use --pending / --answer.", file=sys.stderr)
        return 2

    ctx = {}
    if config.STATUS_FILE.exists():
        try:
            snap = json.loads(config.STATUS_FILE.read_text())
            ctx = {"positions": snap.get("positions", []),
                   "realized_pnl_today": snap.get("realized_pnl_today")}
        except Exception:
            pass
    req = claudelink.enqueue(" ".join(args.question), context=ctx)
    if req["status"] == claudelink.ANSWERED:
        print(req["answer"])
    else:
        print(f"Queued as {req['id']} — no claude CLI on this machine, so a Claude session "
              f"must pick it up with `vigil ask --pending`.")
    return 0
