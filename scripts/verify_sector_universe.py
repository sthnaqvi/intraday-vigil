#!/usr/bin/env python3
"""Cross-check every instrument token in skill/intraday-vigil/references/sector-universe.md
against Kite's live NSE instrument master, and optionally rewrite the file with corrections.

Why this exists: on 2026-08-20, a live session hit repeated failures traced back to this file
carrying wrong tokens for DLF, GODREJPROP, OBEROIRLTY, CIPLA, and BAJAJ-AUTO — five of the
~34 symbols in the doc. NSE instrument tokens are not permanently fixed; they can and do drift
(corporate actions, relisting, periodic instrument-master reissues). The file was last written
wholesale during a skill-migration rewrite (commit 65f435f) without validating tokens against
a live source, which is how five wrong values got in at once instead of one stray typo. The
BAJAJ-AUTO case was the most dangerous of the five: the wrong token didn't error, it silently
returned real-looking daily-candle data for a *different* ~2000-rupee-range instrument.

Usage:
    .venv/bin/python scripts/verify_sector_universe.py            # report only
    .venv/bin/python scripts/verify_sector_universe.py --fix       # rewrite mismatches in place

Requires a valid daemon token (run `vigil login` first if needed) — this hits the same
instruments("NSE") endpoint the daemon itself uses, no separate credentials.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kiteconnect import KiteConnect  # noqa: E402

from vigil import config  # noqa: E402

DOC_PATH = (
    Path(__file__).resolve().parent.parent
    / "skill" / "intraday-vigil" / "references" / "sector-universe.md"
)
LINE_RE = re.compile(r"^(-\s+NSE:(\S+)\s+\(token\s+)(\d+)(\).*)$")


def load_live_tokens() -> dict[str, int]:
    if not config.TOKEN_FILE.exists():
        print(f"No saved token at {config.TOKEN_FILE} — run `vigil login` first.",
              file=sys.stderr)
        sys.exit(2)
    data = json.loads(config.TOKEN_FILE.read_text())
    kite = KiteConnect(api_key=data["api_key"])
    kite.set_access_token(data["access_token"])
    rows = kite.instruments("NSE")
    # Prefer the plain-equity row when a symbol has more than one (e.g. a BE/BZ series
    # duplicate) — series "EQ" is what this project trades.
    by_symbol: dict[str, int] = {}
    for r in rows:
        sym = r["tradingsymbol"]
        if sym not in by_symbol or r.get("series") == "EQ":
            by_symbol[sym] = int(r["instrument_token"])
    return by_symbol


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fix", action="store_true", help="rewrite mismatched tokens in place")
    args = ap.parse_args()

    if not DOC_PATH.exists():
        print(f"Doc not found: {DOC_PATH}", file=sys.stderr)
        return 2

    live = load_live_tokens()
    lines = DOC_PATH.read_text().splitlines()

    mismatches = []
    missing = []
    out_lines = []
    for line in lines:
        m = LINE_RE.match(line)
        if not m:
            out_lines.append(line)
            continue
        prefix, symbol, doc_token, suffix = m.groups()
        live_token = live.get(symbol)
        if live_token is None:
            missing.append(symbol)
            out_lines.append(line)
            continue
        if str(live_token) != doc_token:
            mismatches.append((symbol, int(doc_token), live_token))
            out_lines.append(f"{prefix}{live_token}{suffix}" if args.fix else line)
        else:
            out_lines.append(line)

    if not mismatches and not missing:
        print("All tokens match the live instrument master. Nothing to do.")
        return 0

    if mismatches:
        print(f"{len(mismatches)} mismatched token(s):")
        for sym, doc_tok, live_tok in mismatches:
            print(f"  {sym:12s} doc={doc_tok:<10d} live={live_tok}")
    if missing:
        print(f"{len(missing)} symbol(s) not found in the live NSE instrument master "
              f"(delisted, renamed, or wrong exchange?): {', '.join(missing)}")

    if args.fix and mismatches:
        DOC_PATH.write_text("\n".join(out_lines) + "\n")
        print(f"\nFixed {len(mismatches)} token(s) in {DOC_PATH}")
    elif mismatches:
        print("\nRun again with --fix to rewrite these in place.")

    return 1 if (mismatches or missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())
