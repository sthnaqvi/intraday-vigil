"""CLI: argparse wiring only. Command implementations live in commands/*.py.

start | stop | restart | login | positions | status | add-position | monitor | squareoff |
enter | arm | add | exit | web | ask | protect | quote | triggers | disarm |
arm-exit | exit-triggers | disarm-exit | paper-price | paths | skill-install
"""
from __future__ import annotations

import argparse
import sys
import time
import traceback

from . import audit, auth
from .commands.armed import (
    cmd_arm,
    cmd_arm_exit,
    cmd_disarm,
    cmd_disarm_exit,
    cmd_exit_triggers,
    cmd_triggers,
)
from .commands.daemon import cmd_login, cmd_monitor, cmd_restart, cmd_start, cmd_stop
from .commands.info import cmd_paths, cmd_positions, cmd_quote, cmd_status
from .commands.integrations import cmd_ask, cmd_web
from .commands.orders import (
    cmd_add,
    cmd_add_position,
    cmd_enter,
    cmd_exit,
    cmd_paper_price,
    cmd_protect,
    cmd_squareoff,
)
from .commands.skill import cmd_skill_install
from .events import setup_logging


def main(argv: list[str] | None = None) -> int:
    setup_logging()
    p = argparse.ArgumentParser(prog="vigil",
                                description="Intraday SL-lifecycle daemon")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("start",
                        help="morning one-shot: login if needed + run daemon in background")
    sp.add_argument("--dry-run", action="store_true", help="log intents, mutate nothing")
    sp.add_argument("--force", action="store_true", help="run outside market hours")
    sp.add_argument("--paste", action="store_true", help="paste-URL login fallback")
    sp.add_argument("--paper", action="store_true",
                    help="paper trading — a simulated broker, no real account, no real "
                         "money. Skips login entirely. Every command run afterward "
                         "(enter, status, web, ...) stays in paper mode until you run "
                         "`vigil start` again without --paper, or `vigil login`.")
    sp.add_argument("--allow-silent", action="store_true",
                    help="start live even if no desktop notifier is available "
                         "(macOS osascript / Linux notify-send). You will get NO alerts "
                         "for SL hits, unprotected positions, or token expiry unless "
                         "you are actively watching the log.")
    sp.set_defaults(fn=cmd_start)

    sp = sub.add_parser("stop", help="stop the background daemon (broker SLs stay active)")
    sp.add_argument("--i-know", action="store_true",
                    help="stop anyway even inside the pre-squareoff window with open "
                         "positions — see the refusal message for why this is asked for "
                         "explicitly")
    sp.set_defaults(fn=cmd_stop)

    sp = sub.add_parser("restart",
                        help="stop (if running) + start — same flags as `start`; "
                             "resting SLs and today's tracked state are never at risk")
    sp.add_argument("--dry-run", action="store_true", help="log intents, mutate nothing")
    sp.add_argument("--force", action="store_true", help="run outside market hours")
    sp.add_argument("--paste", action="store_true", help="paste-URL login fallback")
    sp.add_argument("--paper", action="store_true", help="paper trading — see `vigil start --help`")
    sp.add_argument("--allow-silent", action="store_true",
                    help="start live even if no desktop notifier is available")
    sp.add_argument("--i-know", action="store_true",
                    help="restart anyway even inside the pre-squareoff window with open "
                         "positions — see `vigil stop --help`")
    sp.set_defaults(fn=cmd_restart)

    sp = sub.add_parser("login", help="daily Kite login (skips browser if token still valid)")
    sp.add_argument("--paste", action="store_true",
                    help="paste redirected URL instead of local listener")
    sp.add_argument("--force", action="store_true", help="re-login even if token looks valid")
    sp.set_defaults(fn=cmd_login)

    sp = sub.add_parser("positions", help="live MIS positions + their SL orders")
    sp.set_defaults(fn=cmd_positions)

    sp = sub.add_parser("status", help="session dashboard (--json for the raw snapshot)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("add-position", help="seed sl_pct (and pdh/pdl) for a symbol")
    sp.add_argument("symbol")
    sp.add_argument("--sl-pct", type=float, required=True,
                    help="SL width, e.g. 1.0 for 1%% (values <= 0.2 read as fractions)")
    sp.add_argument("--pdh", type=float, default=None)
    sp.add_argument("--pdl", type=float, default=None)
    sp.set_defaults(fn=cmd_add_position)

    sp = sub.add_parser("monitor", help="run the SL-lifecycle loop in the foreground")
    sp.add_argument("--dry-run", action="store_true", help="log intents, mutate nothing")
    sp.add_argument("--force", action="store_true", help="run outside market hours")
    sp.add_argument("--paper", action="store_true", help="paper trading — see `vigil start --help`")
    sp.add_argument("--allow-silent", action="store_true",
                    help="run live even if no desktop notifier is available")
    sp.set_defaults(fn=cmd_monitor)

    sp = sub.add_parser("squareoff", help="cancel SLs and market-exit all MIS now")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--yes", action="store_true", help="skip confirmation")
    sp.set_defaults(fn=cmd_squareoff)

    sp = sub.add_parser("enter", help="open a MIS position + SL now (no MCP needed)")
    sp.add_argument("symbol")
    sp.add_argument("--side", required=True, choices=["long", "short", "LONG", "SHORT"])
    sp.add_argument("--qty", type=int, required=True)
    sp.add_argument("--sl-pct", type=float, required=True,
                    help="SL width, e.g. 1.0 for 1%% (values <= 0.2 read as fractions)")
    sp.add_argument("--pdh", type=float, default=None)
    sp.add_argument("--pdl", type=float, default=None)
    sp.add_argument("--yes", action="store_true", help="skip confirmation")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--override-gate", action="store_true",
                    help="bypass the kill-switch / 14:30 entry gate (you must mean it)")
    sp.set_defaults(fn=cmd_enter)

    sp = sub.add_parser("arm", help="arm a price trigger watched over the tick WebSocket")
    sp.add_argument("symbol")
    sp.add_argument("--side", required=True, choices=["long", "short", "LONG", "SHORT"])
    sp.add_argument("--above", type=float, default=None, help="fire when price breaks above")
    sp.add_argument("--below", type=float, default=None, help="fire when price breaks below")
    sp.add_argument("--qty", type=int, required=True)
    sp.add_argument("--sl-pct", type=float, required=True)
    sp.add_argument("--pdh", type=float, default=None)
    sp.add_argument("--pdl", type=float, default=None)
    sp.add_argument("--auto", action="store_true",
                    help="place the order automatically on the break (default: alert only)")
    sp.add_argument("--note", default="")
    sp.set_defaults(fn=cmd_arm)

    sp = sub.add_parser("add", help="scale into an open position (rewrites the risk seed)")
    sp.add_argument("symbol")
    sp.add_argument("--qty", type=int, required=True)
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--override-gate", action="store_true")
    sp.set_defaults(fn=cmd_add)

    sp = sub.add_parser("exit", help="exit ONE symbol (cancel its SL, then market-exit)")
    sp.add_argument("symbol")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_exit)

    sp = sub.add_parser("web", help="local dashboard — CAN place orders behind typed "
                                     "confirmation; binds to localhost only, always")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(fn=cmd_web)

    sp = sub.add_parser("ask", help="ask Claude (runs the CLI if present, else queues)")
    sp.add_argument("question", nargs="*")
    sp.add_argument("--pending", action="store_true", help="list unanswered questions")
    sp.add_argument("--answer", metavar="ID", help="write an answer back to a question")
    sp.add_argument("--text", help="the answer text (with --answer)")
    sp.set_defaults(fn=cmd_ask)

    sp = sub.add_parser("protect", help="re-place a missing SL on an open position")
    sp.add_argument("symbol")
    sp.add_argument("--sl-pct", type=float, default=None,
                    help="override; defaults to the risk.json seed / tracked value")
    sp.add_argument("--trigger", type=float, default=None, help="explicit trigger price")
    sp.add_argument("--force", action="store_true", help="place even if an SL already rests")
    sp.add_argument("--yes", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.set_defaults(fn=cmd_protect)

    sp = sub.add_parser("paths", help="where this install keeps its state (VIGIL_HOME/XDG)")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(fn=cmd_paths)

    sp = sub.add_parser("skill-install",
                        help="symlink the Claude skill into ~/.claude/skills/ "
                             "(source checkout only — see the README for a plain-PyPI install)")
    sp.add_argument("--force", action="store_true",
                    help="repoint an existing symlink that points elsewhere")
    sp.set_defaults(fn=cmd_skill_install)

    sp = sub.add_parser("paper-price",
                        help="paper mode only: move a symbol's simulated price, "
                             "filling any resting stop it crosses")
    sp.add_argument("symbol")
    sp.add_argument("price", type=float)
    sp.set_defaults(fn=cmd_paper_price)

    sp = sub.add_parser("quote", help="LTP + OHLC without the MCP session")
    sp.add_argument("symbols", nargs="+")
    sp.set_defaults(fn=cmd_quote)

    sp = sub.add_parser("triggers", help="list armed / fired triggers")
    sp.set_defaults(fn=cmd_triggers)

    sp = sub.add_parser("disarm", help="cancel armed triggers (all, or one symbol)")
    sp.add_argument("symbol", nargs="?", default=None)
    sp.set_defaults(fn=cmd_disarm)

    sp = sub.add_parser("arm-exit", help="arm an automatic exit, independent of the "
                                          "resting SL — fires with no confirmation")
    sp.add_argument("symbol")
    sp.add_argument("--above", type=float, default=None, help="fire when price breaks above")
    sp.add_argument("--below", type=float, default=None, help="fire when price breaks below")
    sp.add_argument("--note", default="")
    sp.set_defaults(fn=cmd_arm_exit)

    sp = sub.add_parser("exit-triggers", help="list armed / fired exit triggers")
    sp.set_defaults(fn=cmd_exit_triggers)

    sp = sub.add_parser("disarm-exit", help="cancel armed exit triggers (all, or one symbol)")
    sp.add_argument("symbol", nargs="?", default=None)
    sp.set_defaults(fn=cmd_disarm_exit)

    args = p.parse_args(argv)
    # Every invocation is recorded, whoever started it. When the dashboard spawned this
    # process it passed VIGIL_TRACE_ID, so the click and this run share one trace.
    raw = argv if argv is not None else sys.argv[1:]
    audit.action("cli.invoke", cmd=args.cmd, argv=list(raw))
    started = time.monotonic()
    code = 2
    try:
        code = args.fn(args)
        return code
    except auth.AuthError as e:
        print(f"auth: {e}", file=sys.stderr)
        code = 2
        return code
    except Exception as e:
        audit.action("cli.crashed", cmd=args.cmd, error=repr(e),
                     tb=traceback.format_exc()[-2000:])
        raise
    finally:
        audit.action("cli.finished", cmd=args.cmd, code=code,
                     ms=round((time.monotonic() - started) * 1000, 1))


if __name__ == "__main__":
    raise SystemExit(main())
