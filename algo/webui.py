"""Local dashboard + full control surface for the intraday daemon. Stdlib only.

Binds to 127.0.0.1 by design: it exposes live positions AND can place orders, so it must
never be reachable off the machine.

Every command runs through the real CLI (`python -m algo ...`) as an argv list — never a
shell string — so all the existing guards apply unchanged: the 1.5% SL cap, the entry gate,
the kill switch, the 14:30 cutoff. The UI is a front-end for those rules, not a way round
them.

Anything that moves money requires a typed confirmation, checked on the server. A dashboard
that can fire an order with one stray click is a dashboard that eventually does.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import sys
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

from . import audit, claudelink, clock, config
from . import triggers as triggers_mod

SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,24}$")

# Bumped whenever the page changes. Shown in the header and compared by the running page,
# so a browser holding a stale copy says so instead of failing silently — which is exactly
# how the first control build appeared "broken" (buttons rendered, handlers undefined).
UI_VERSION = "2026-08-18.6"

# Raw technical logs, newest lines last. Kept explicit — no path traversal from the client.
LOG_SOURCES = {
    "algo.log":    lambda: config.LOGS_DIR / "algo.log",
    "actions.jsonl": lambda: config.LOGS_DIR / "actions.jsonl",
    "api.jsonl":   lambda: config.LOGS_DIR / "api.jsonl",
    "web.jsonl":   lambda: config.LOGS_DIR / "web.jsonl",
    "daemon.out":  lambda: config.LOGS_DIR / "daemon.out",
    "events":      lambda: config.DATA_DIR / f"events-{clock.now_ist().date().isoformat()}.jsonl",
    "status.json": lambda: config.STATUS_FILE,
    "risk.json":   lambda: config.RISK_FILE,
    "triggers.json": lambda: config.DATA_DIR / "triggers.json",
}


# Profile never changes within a session; margins do, but not every 3 seconds. Without
# these caches the dashboard's own polling would make ~1,200 Kite calls an hour.
ACCOUNT_TTL_S = 20
_profile_cache: dict | None = None
_account_cache: dict = {"ts": 0.0, "data": None}


def account_snapshot(force: bool = False) -> dict:
    """Client id, name and live margins. Never raises — a dead token is a display state."""
    global _profile_cache
    now = time.monotonic()
    if not force and _account_cache["data"] and now - _account_cache["ts"] < ACCOUNT_TTL_S:
        return _account_cache["data"]
    try:
        from .auth import get_kite
        kite = get_kite()
        if _profile_cache is None:
            _profile_cache = kite.profile()
        prof = _profile_cache or {}
        eq = (kite.margins() or {}).get("equity", {}) or {}
        avail, util = eq.get("available", {}) or {}, eq.get("utilised", {}) or {}
        # Kite often reports available.cash as 0 with the real figure in live_balance.
        live = avail.get("live_balance")
        if not live:
            live = avail.get("cash") or eq.get("net")
        data = {
            "ok": True,
            "client_id": prof.get("user_id"),
            "name": prof.get("user_name"),
            "broker": prof.get("broker"),
            "email": prof.get("email"),
            "exchanges": prof.get("exchanges") or [],
            "products": prof.get("products") or [],
            "net": eq.get("net"),
            "available": live,
            "opening": avail.get("opening_balance"),
            "used": util.get("debits"),
            "m2m_unrealised": util.get("m2m_unrealised"),
            "m2m_realised": util.get("m2m_realised"),
        }
    except Exception as e:
        data = {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _account_cache.update(ts=now, data=data)
    return data


def read_log(src: str, lines: int = 300) -> dict:
    if src not in LOG_SOURCES:
        return {"ok": False, "text": f"unknown log source: {src}"}
    path = LOG_SOURCES[src]()
    if not path.exists():
        return {"ok": True, "text": f"({path} does not exist yet)", "path": str(path)}
    try:
        content = path.read_text(errors="replace").splitlines()
    except OSError as e:
        return {"ok": False, "text": f"cannot read {path}: {e}"}
    tail = content[-max(1, min(lines, 5000)):]
    return {"ok": True, "path": str(path), "lines": len(content),
            "text": "\n".join(tail) or "(empty)"}

# name -> (argv builder, needs_typed_confirmation, human label)
# Confirmation text must equal the symbol (or the literal below) or the server refuses.
COMMANDS: dict[str, dict] = {
    # --- read-only ---
    "status":       {"confirm": None, "args": []},
    "positions":    {"confirm": None, "args": []},
    "triggers":     {"confirm": None, "args": []},
    "quote":        {"confirm": None, "args": ["symbols"]},
    # --- daemon lifecycle (no money moves) ---
    "start":        {"confirm": None, "args": []},
    "stop":         {"confirm": None, "args": []},
    # --- seeds / triggers (no immediate order) ---
    "add-position": {"confirm": None, "args": ["symbol", "sl_pct", "pdh", "pdl"]},
    "disarm":       {"confirm": None, "args": ["symbol"]},
    "arm":          {"confirm": "symbol", "args": ["symbol", "side", "above", "below",
                                                   "qty", "sl_pct", "pdh", "pdl", "auto"]},
    # --- these place or cancel real orders ---
    "enter":        {"confirm": "symbol", "args": ["symbol", "side", "qty", "sl_pct",
                                                   "pdh", "pdl"]},
    "add":          {"confirm": "symbol", "args": ["symbol", "qty"]},
    "exit":         {"confirm": "symbol", "args": ["symbol"]},
    "protect":      {"confirm": "symbol", "args": ["symbol", "sl_pct", "trigger"]},
    "squareoff":    {"confirm": "SQUAREOFF", "args": []},
}

# Skill modes live on the Claude side, so the UI enqueues them as questions.
SKILL_MODES = {
    "monitor":  "Run /intraday-trader monitor — render the daemon snapshot, check the "
                "protected flag on every position, and run the thesis-decay check.",
    "reassess": "Run /intraday-trader reassess — re-rank all 11 sectors, check whether each "
                "open position's sector is still top/bottom 3, and surface new setups.",
    "exit":     "Run /intraday-trader exit — square off all open MIS manually and report the "
                "exit summary.",
    "rca":      "Run /intraday-trader rca — post-session analysis from today's event log, "
                "scored against the rubric, with the top 3 mistakes to avoid tomorrow.",
    "start":    "Run /intraday-trader start — opening bias, macro theme, sector ranking and "
                "stock scoring for a new session.",
}


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _build_argv(cmd: str, params: dict) -> list[str]:
    """Whitelisted argv. Positional symbol first, everything else as validated flags."""
    spec = COMMANDS[cmd]
    argv = [sys.executable, "-m", "algo", cmd]

    for key in spec["args"]:
        val = params.get(key)
        if val in (None, "", False):
            continue

        if key == "symbol":
            sym = str(val).strip().upper()
            if not SYMBOL_RE.match(sym):
                raise ValueError(f"bad symbol: {val!r}")
            argv.append(sym)
        elif key == "symbols":
            for s in (val if isinstance(val, list) else str(val).split()):
                sym = str(s).strip().upper()
                if not SYMBOL_RE.match(sym.replace("NSE:", "")):
                    raise ValueError(f"bad symbol: {s!r}")
                argv.append(sym)
        elif key == "side":
            side = str(val).lower()
            if side not in ("long", "short"):
                raise ValueError(f"bad side: {val!r}")
            argv += ["--side", side]
        elif key == "auto":
            if val:
                argv.append("--auto")
        elif key == "qty":
            argv += ["--qty", str(int(val))]
        else:  # numeric flags: sl_pct, pdh, pdl, above, below, trigger
            argv += [_flag(key), str(float(val))]

    # Non-interactive: the UI's typed confirmation replaces the terminal prompt.
    if cmd in ("enter", "add", "exit", "protect", "squareoff"):
        argv.append("--yes")
    return argv


def run_command(cmd: str, params: dict, confirm: str | None) -> dict:
    if cmd not in COMMANDS:
        return {"ok": False, "output": f"unknown command: {cmd}"}
    spec = COMMANDS[cmd]

    need = spec["confirm"]
    if need:
        expected = (str(params.get("symbol", "")).strip().upper() if need == "symbol"
                    else need)
        if not expected:
            return {"ok": False, "output": "a symbol is required"}
        if (confirm or "").strip().upper() != expected:
            return {"ok": False,
                    "output": f"Confirmation failed. Type {expected} to authorise `{cmd}`."}

    try:
        argv = _build_argv(cmd, params)
    except (ValueError, TypeError) as e:
        return {"ok": False, "output": f"invalid input: {e}"}

    # One trace for the whole action: this log line, the subprocess's own logging, every
    # Kite call it makes, and every event it emits.
    trace = audit.new_trace()
    audit.action("command.requested", trace=trace, cmd=cmd, params=params,
                 confirmed=bool(need), argv=argv[1:])
    started = time.monotonic()
    try:
        proc = subprocess.run(argv, cwd=config.PROJECT_ROOT, capture_output=True,
                              text=True, timeout=120,
                              env=audit.child_env(trace, "web"))
    except subprocess.TimeoutExpired:
        audit.action("command.timeout", trace=trace, cmd=cmd, seconds=120)
        return {"ok": False, "output": f"`{cmd}` timed out after 120s", "trace": trace}
    out = (proc.stdout or "") + (proc.stderr or "")
    audit.action("command.finished", trace=trace, cmd=cmd, code=proc.returncode,
                 ms=round((time.monotonic() - started) * 1000, 1),
                 output=out.strip()[:4000])
    return {"ok": proc.returncode == 0, "code": proc.returncode, "trace": trace,
            "output": out.strip() or f"(no output, exit {proc.returncode})",
            "argv": " ".join(argv[2:])}


PAGE = r"""<!doctype html>
<meta charset="utf-8">
<title>intraday-algo</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--panel:#171a21;--side:#13161c;--line:#262b36;--fg:#e6e9ef;--dim:#8b93a7;
      --up:#31c48d;--down:#f05252;--warn:#f0b429;--accent:#6c8cff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);height:100vh;display:flex;flex-direction:column;
     font:14px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;overflow:hidden}
header{display:flex;justify-content:space-between;align-items:center;gap:12px;
       padding:10px 16px;border-bottom:1px solid var(--line);flex:0 0 auto;flex-wrap:wrap}
h1{font-size:13px;margin:0;letter-spacing:.06em;text-transform:uppercase;color:var(--dim)}
#shell{display:flex;flex:1;min-height:0}
nav{width:190px;flex:0 0 190px;background:var(--side);border-right:1px solid var(--line);
    padding:10px 0;overflow-y:auto}
nav a{display:block;padding:9px 16px;color:var(--dim);cursor:pointer;font-size:13px;
      border-left:3px solid transparent;user-select:none}
nav a:hover{color:var(--fg);background:#1a1e26}
nav a.on{color:var(--fg);background:#1c212b;border-left-color:var(--accent)}
nav .grp{padding:12px 16px 5px;font-size:10px;letter-spacing:.1em;color:#5c6478;text-transform:uppercase}
nav .badge{float:right;font-size:10px;padding:1px 6px;border-radius:999px;background:#3a1416;color:var(--down)}
#content{flex:1;min-width:0;overflow-y:auto;padding:16px}
.pane{display:none} .pane.on{display:block}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;
       overflow:hidden;margin-bottom:14px}
.panel h2{font-size:11px;margin:0;padding:9px 14px;color:var(--dim);letter-spacing:.09em;
          text-transform:uppercase;border-bottom:1px solid var(--line)}
.body{padding:12px 14px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px;white-space:nowrap}
th{text-align:right;color:var(--dim);font-weight:500;padding:5px 9px;
   border-bottom:1px solid var(--line);font-size:11px;text-transform:uppercase}
th:first-child,td:first-child{text-align:left}
td{text-align:right;padding:6px 9px;border-bottom:1px solid #1e222b}
tr:last-child td{border-bottom:0}
.up{color:var(--up)} .down{color:var(--down)} .dim{color:var(--dim)}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px}
.ok{background:#123524;color:var(--up)} .bad{background:#3a1416;color:var(--down)}
.warnp{background:#3a2c10;color:var(--warn)}
.banner{background:#3a1416;border:1px solid var(--down);color:#ffd7d7;
        padding:10px 14px;border-radius:8px;font-weight:600;margin-bottom:12px}
.stale{background:#3a2c10;border:1px solid var(--warn);color:#ffe9bf;
       padding:10px 14px;border-radius:8px;margin-bottom:12px}
input,select,textarea{background:#0c0e12;color:var(--fg);border:1px solid var(--line);
      border-radius:6px;padding:7px 9px;font:13px ui-monospace,Menlo,monospace}
input{width:110px} input.sym{width:125px;text-transform:uppercase}
textarea{width:100%;resize:vertical}
button{background:#2a3040;color:var(--fg);border:1px solid var(--line);border-radius:6px;
       padding:7px 13px;font:600 12px ui-monospace,Menlo,monospace;cursor:pointer}
button:hover{border-color:var(--accent)}
button.go{background:var(--accent);color:#fff;border-color:var(--accent)}
button.danger{background:#5b1a1d;border-color:var(--down);color:#ffd7d7}
button:disabled{opacity:.45;cursor:default}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:7px 0}
label{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.05em}
fieldset{border:1px solid var(--line);border-radius:6px;padding:10px 12px;margin:0 0 10px}
legend{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.07em;padding:0 5px}
.confirm{border-color:var(--down)!important;width:135px}
.ev{font-size:12px;padding:3px 0;border-bottom:1px solid #1e222b}
.ev:last-child{border-bottom:0}
.qa{border:1px solid var(--line);border-radius:6px;padding:10px;margin-bottom:8px}
.qa .q{color:var(--accent);font-weight:600}
.qa .a{white-space:pre-wrap;margin-top:5px}
code{background:#0c0e12;padding:1px 5px;border-radius:4px;color:var(--warn)}
pre.raw{white-space:pre;background:#0a0c10;border:1px solid var(--line);border-radius:6px;
        padding:11px;font-size:12px;line-height:1.45;max-height:calc(100vh - 260px);
        overflow:auto;margin:0}
#out{white-space:pre-wrap;background:#0c0e12;border:1px solid var(--line);border-radius:6px;
     padding:10px;font-size:12px;max-height:300px;overflow:auto;margin-top:8px}
.hint{color:var(--dim);font-size:11px}
#jserr{display:none;background:#5b1a1d;color:#ffd7d7;padding:8px 16px;font-size:12px}

/* ---- live log dock ---- */
#live{flex:0 0 auto;display:flex;flex-direction:column;background:#0a0c10;
      border-left:1px solid var(--line);height:100%;min-height:0}
#live.collapsed{width:44px}
#live.open{width:min(460px,38vw);min-width:280px}
#live.left{order:-1;border-left:0;border-right:1px solid var(--line)}
#railbtn{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;
         gap:8px;background:none;border:0;color:var(--warn);cursor:pointer;padding:0;
         writing-mode:vertical-rl;letter-spacing:.14em;font-size:11px;text-transform:uppercase}
#railbtn:hover{background:#12161d;color:#ffd479}
#railbtn .dot{writing-mode:horizontal-tb;width:7px;height:7px;border-radius:50%;
              background:var(--up);animation:pulse 1.4s infinite}
@keyframes pulse{0%,100%{opacity:.25}50%{opacity:1}}
#livehead{display:flex;align-items:center;gap:6px;padding:7px 9px;background:#12161d;
          border-bottom:1px solid var(--line);flex:0 0 auto;flex-wrap:wrap}
#livehead .t{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--warn);
             font-weight:600}
#livehead button{padding:4px 8px;font-size:11px}
#livehead select,#livehead input{padding:4px 7px;font-size:11px}
#livesearch{width:120px}
#livebody{flex:1;min-height:0;overflow:auto;padding:8px 10px;font-size:11.5px;
          line-height:1.5;white-space:pre-wrap;word-break:break-word;color:#c8cedb}
#livebody mark{background:#5a4a12;color:#ffe9a8;border-radius:2px}
#livebody mark.cur{background:var(--warn);color:#221a04}
#jump{position:absolute;bottom:14px;right:18px;z-index:3;font-size:11px;padding:5px 10px;
      background:var(--accent);color:#fff;border:0;border-radius:999px;cursor:pointer;
      box-shadow:0 2px 10px #0008}
#livewrap{position:relative;flex:1;min-height:0;display:flex}
.livefoot{padding:4px 9px;font-size:10px;color:var(--dim);border-top:1px solid var(--line);
          flex:0 0 auto;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
</style>

<div id="jserr"></div>
<header>
  <h1>intraday-algo <span class="dim" id="ver"></span></h1>
  <div id="acct" class="dim">…</div>
  <div id="daemon" class="dim">loading…</div>
</header>

<div id="shell">
  <nav>
    <div class="grp">Session</div>
    <a data-pane="overview" class="on">Overview</a>
    <a data-pane="positions">Positions <span id="nb_pos" class="badge" style="display:none"></span></a>
    <a data-pane="events">Events</a>
    <div class="grp">Act</div>
    <a data-pane="daemon">Daemon</a>
    <a data-pane="trade">Trade</a>
    <a data-pane="triggers">Triggers</a>
    <div class="grp">Inspect</div>
    <a data-pane="account">Account</a>
    <a data-pane="logs">Raw logs</a>
    <a data-pane="claude">Claude</a>
  </nav>

  <div id="content">
    <div id="alerts"></div>

    <div class="pane on" id="p_overview">
      <div class="panel"><h2>Positions</h2><div class="body" id="pos">—</div></div>
      <div class="panel"><h2>Armed triggers</h2><div class="body" id="trig">—</div></div>
      <div class="panel"><h2>Recent events</h2><div class="body" id="events_s">—</div></div>
    </div>

    <div class="pane" id="p_positions">
      <div class="panel"><h2>Open &amp; closed</h2><div class="body" id="pos2">—</div></div>
      <div class="panel"><h2>Manage</h2><div class="body">
        <fieldset><legend>Scale, protect or exit one symbol</legend>
          <div class="row">
            <input class="sym" id="m_sym" placeholder="SYMBOL">
            <label>add qty</label><input id="m_qty" type="number" placeholder="600">
            <label>sl%</label><input id="m_sl" type="number" step="0.01">
            <label>trigger</label><input id="m_trig" type="number" step="0.05">
          </div>
          <div class="row">
            <input class="confirm" id="m_ok" placeholder="type SYMBOL">
            <button onclick="run('add',{symbol:v('m_sym'),qty:v('m_qty')},v('m_ok'))">add (scale in)</button>
            <button onclick="run('protect',{symbol:v('m_sym'),sl_pct:v('m_sl'),trigger:v('m_trig')},v('m_ok'))">protect</button>
            <button class="danger" onclick="run('exit',{symbol:v('m_sym')},v('m_ok'))">exit symbol</button>
          </div>
        </fieldset>
        <fieldset><legend>Square off everything</legend><div class="row">
          <input class="confirm" id="so_ok" placeholder="type SQUAREOFF">
          <button class="danger" onclick="run('squareoff',{},v('so_ok'))">square off all</button>
        </div></fieldset>
      </div></div>
    </div>

    <div class="pane" id="p_events">
      <div class="panel"><h2>Today's events</h2><div class="body" id="events_full">—</div></div>
    </div>

    <div class="pane" id="p_daemon">
      <div class="panel"><h2>Daemon</h2><div class="body">
        <div class="row">
          <button onclick="run('start')">start</button>
          <button onclick="run('stop')">stop</button>
          <button onclick="run('status')">status</button>
          <button onclick="run('positions')">positions</button>
          <button onclick="run('triggers')">triggers</button>
        </div>
        <fieldset><legend>Quote</legend><div class="row">
          <input class="sym" id="q_syms" placeholder="HCLTECH INFY" style="width:220px">
          <button onclick="run('quote',{symbols:v('q_syms')})">quote</button>
          <span class="hint">space-separated; works without the MCP session</span>
        </div></fieldset>
        <fieldset><legend>Seed sl_pct / PDH / PDL (no order)</legend><div class="row">
          <input class="sym" id="s_sym" placeholder="SYMBOL">
          <label>sl%</label><input id="s_sl" type="number" step="0.01">
          <label>pdh</label><input id="s_pdh" type="number" step="0.05">
          <label>pdl</label><input id="s_pdl" type="number" step="0.05">
          <button onclick="run('add-position',{symbol:v('s_sym'),sl_pct:v('s_sl'),pdh:v('s_pdh'),pdl:v('s_pdl')})">save seed</button>
        </div></fieldset>
      </div></div>
    </div>

    <div class="pane" id="p_trade">
      <div class="panel"><h2>Enter a position</h2><div class="body">
        <div class="row">
          <input class="sym" id="e_sym" placeholder="SYMBOL">
          <select id="e_side"><option value="long">long</option><option value="short">short</option></select>
          <label>qty</label><input id="e_qty" type="number" placeholder="590">
          <label>sl%</label><input id="e_sl" type="number" step="0.01" placeholder="0.91">
          <label>pdh</label><input id="e_pdh" type="number" step="0.05">
          <label>pdl</label><input id="e_pdl" type="number" step="0.05">
        </div>
        <div class="row">
          <input class="confirm" id="e_ok" placeholder="type SYMBOL">
          <button class="go" onclick="run('enter',{symbol:v('e_sym'),side:v('e_side'),qty:v('e_qty'),sl_pct:v('e_sl'),pdh:v('e_pdh'),pdl:v('e_pdl')},v('e_ok'))">enter</button>
          <span class="hint">market order + guarded SL · refuses sl% &gt; 1.5 · obeys the 14:30 gate</span>
        </div>
      </div></div>
    </div>

    <div class="pane" id="p_triggers">
      <div class="panel"><h2>Armed triggers</h2><div class="body" id="trig2">—</div></div>
      <div class="panel"><h2>Arm a level</h2><div class="body">
        <div class="row">
          <input class="sym" id="a_sym" placeholder="SYMBOL">
          <select id="a_side"><option value="long">long</option><option value="short">short</option></select>
          <label>above</label><input id="a_above" type="number" step="0.05">
          <label>below</label><input id="a_below" type="number" step="0.05">
          <label>qty</label><input id="a_qty" type="number">
          <label>sl%</label><input id="a_sl" type="number" step="0.01">
        </div>
        <div class="row">
          <label><input type="checkbox" id="a_auto" style="width:auto"> auto-execute</label>
          <input class="confirm" id="a_ok" placeholder="type SYMBOL">
          <button onclick="run('arm',{symbol:v('a_sym'),side:v('a_side'),above:v('a_above'),below:v('a_below'),qty:v('a_qty'),sl_pct:v('a_sl'),auto:c('a_auto')},v('a_ok'))">arm</button>
          <button onclick="run('disarm',{symbol:v('a_sym')})">disarm</button>
          <span class="hint">without auto-execute it only alerts</span>
        </div>
      </div></div>
    </div>

    <div class="pane" id="p_account">
      <div class="panel"><h2>Kite account</h2><div class="body" id="acct_full">—</div></div>
      <div class="panel"><h2>Funds</h2><div class="body" id="acct_funds">—</div></div>
    </div>

    <div class="pane" id="p_logs">
      <div class="panel"><h2>Raw technical log</h2><div class="body">
        <div class="row">
          <select id="log_src"></select>
          <label>lines</label><input id="log_n" type="number" value="300" style="width:80px">
          <button onclick="loadLog()">reload</button>
          <label><input type="checkbox" id="log_auto" style="width:auto" checked> auto</label>
          <span class="hint" id="log_meta"></span>
        </div>
        <pre class="raw" id="log_text">select a source</pre>
      </div></div>
    </div>

    <div class="pane" id="p_claude">
      <div class="panel"><h2>Skill modes</h2><div class="body">
        <div class="row" id="modes"></div>
        <span class="hint">these queue for a Claude session — read with <code>algo ask --pending</code></span>
      </div></div>
      <div class="panel"><h2>Ask</h2><div class="body">
        <textarea id="q" rows="3" placeholder="e.g. HCLTECH stalled for 2 hours — hold or exit?"></textarea>
        <div class="row"><button class="go" id="ask">Ask</button><span id="mode" class="hint"></span></div>
        <div id="qa" style="margin-top:10px"></div>
      </div></div>
    </div>

    <div class="panel"><h2>Command output</h2><div class="body">
      <div id="out" class="dim">Command output appears here.</div>
    </div></div>
  </div>

  <aside id="live" class="collapsed">
    <button id="railbtn" onclick="liveToggle(true)" title="Show live log">
      <span class="dot" id="raildot"></span> live log
    </button>
    <div id="livehead" style="display:none">
      <button onclick="liveToggle(false)" title="Collapse">›</button>
      <span class="t">Live</span>
      <select id="live_src" onchange="liveSwitch()"></select>
      <input id="livesearch" placeholder="find…" oninput="liveFind()">
      <span id="livematch" class="hint"></span>
      <button onclick="liveStep(-1)" title="Previous match">↑</button>
      <button onclick="liveStep(1)" title="Next match">↓</button>
      <button onclick="liveSide()" title="Move to the other side">⇄</button>
    </div>
    <div id="livewrap" style="display:none">
      <div id="livebody"></div>
      <button id="jump" style="display:none" onclick="livePin()">↓ live</button>
    </div>
    <div class="livefoot" id="livefoot" style="display:none"></div>
  </aside>
</div>

<script>
// Stamped by the server at request time. If this page is older than the running server,
// render() says so instead of leaving buttons that quietly do nothing.
const BUILT="__UI_VERSION__";
// Surface JS failures instead of letting a button silently do nothing.
window.onerror=(m,src,l,c,e)=>{
  const b=document.getElementById('jserr');
  b.style.display='block';
  b.textContent='UI error: '+m+' ('+(src||'').split('/').pop()+':'+l+') — hard-reload the page (Cmd-Shift-R).';
};

const $=id=>document.getElementById(id);
const v=id=>{const el=$(id); return el?el.value.trim():'';};
const c=id=>{const el=$(id); return el?el.checked:false;};
const n=(x,d=2)=>x==null?'—':Number(x).toFixed(d);
const sign=x=>x>0?'up':x<0?'down':'dim';
const esc=s=>String(s).replace(/[&<>]/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[m]));

document.querySelectorAll('nav a').forEach(a=>a.onclick=()=>{
  document.querySelectorAll('nav a').forEach(x=>x.classList.remove('on'));
  a.classList.add('on');
  document.querySelectorAll('.pane').forEach(p=>p.classList.remove('on'));
  $('p_'+a.dataset.pane).classList.add('on');
  if(a.dataset.pane==='logs') loadLog();
});

async function run(cmd,params={},confirm=null){
  $('out').textContent='running '+cmd+'…'; $('out').className='dim';
  try{
    const r=await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({cmd,params,confirm})});
    const j=await r.json();
    $('out').textContent=(j.ok?'✓ ':'✗ ')+(j.argv||cmd)+'\n\n'+j.output;
    $('out').className=j.ok?'':'down';
    // The output panel used to sit below the fold, so a command that ran fine but
    // reported a refusal looked like a dead button. Always bring it into view.
    $('out').scrollIntoView({behavior:'smooth', block:'nearest'});
    if(cmd==='start'){
      // `start` returns as soon as the child is spawned; the child may exit immediately
      // (market closed). Re-check after a beat and say so plainly.
      setTimeout(async()=>{
        const s=await (await fetch('/api/state')).json();
        if(!s.daemon.running){
          $('out').textContent += '\n\n⚠ The daemon exited straight after starting'
            + (s.market_open ? '.' : ' — the market is CLOSED (now '+s.now+').')
            + '\nUse `algo monitor --force` in a terminal to run outside market hours.';
          $('out').className='down';
        }
      }, 1500);
    }
  }catch(e){ $('out').textContent='request failed: '+e; $('out').className='down'; }
  tick();
}
async function askMode(m){
  await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({mode:m})}); tick();
}
async function loadLog(){
  const src=v('log_src')||$('log_src').value; if(!src) return;
  try{
    const j=await (await fetch(`/api/logs?src=${encodeURIComponent(src)}&lines=${v('log_n')||300}`)).json();
    $('log_text').textContent=j.text;
    $('log_meta').textContent=(j.path||'')+(j.lines?`  ·  ${j.lines} lines total`:'');
    const el=$('log_text'); el.scrollTop=el.scrollHeight;
  }catch(e){ $('log_text').textContent='failed to load: '+e; }
}

function posTable(s){
  return (s.positions.length ? `<table><tr>
    <th>Symbol</th><th>Dir</th><th>Qty</th><th>Entry</th><th>LTP</th><th>R</th><th>P&L</th><th>Phase</th><th>Stop</th></tr>`+
    s.positions.map(p=>`<tr><td>${p.symbol}</td><td class="dim">${p.direction}</td><td>${p.qty}</td>
      <td>${n(p.entry)}</td><td>${n(p.ltp)}</td><td class="${sign(p.profit_r)}">${n(p.profit_r)}</td>
      <td class="${sign(p.unrealized_pnl)}">${n(p.unrealized_pnl)}</td><td>P${p.phase}</td>
      <td class="${p.protected===false?'down':''}">${p.protected===false?'NO STOP':n(p.sl_price)}</td></tr>`).join('')
    +`</table>` : '<span class="dim">No open positions.</span>')
    + (s.closed_today.length?`<table style="margin-top:10px"><tr><th>Closed</th><th>Reason</th><th>Exit</th><th>R</th><th>P&L</th></tr>`+
       s.closed_today.map(x=>`<tr><td>${x.symbol}</td><td class="dim">${x.exit_reason}</td>
       <td>${n(x.exit_price)}</td><td class="${sign(x.realized_r)}">${n(x.realized_r)}</td>
       <td class="${sign(x.realized_pnl)}">${n(x.realized_pnl)}</td></tr>`).join('')+`</table>`:'');
}
function trigTable(s){
  return s.triggers.length ? `<table><tr>
    <th>Symbol</th><th>Dir</th><th>Side</th><th>Level</th><th>Qty</th><th>SL%</th><th>Auto</th><th>Status</th></tr>`+
    s.triggers.map(t=>`<tr><td>${t.symbol}</td><td class="dim">${t.direction}</td><td>${t.side}</td>
      <td>${n(t.level)}</td><td>${t.qty}</td><td>${n(t.sl_pct*100)}%</td>
      <td>${t.auto?'<span class="pill warnp">AUTO</span>':'alert'}</td><td class="dim">${t.status}</td></tr>`).join('')
    +`</table>` : '<span class="dim">Nothing armed.</span>';
}
function evList(s,limit){
  const e=s.events.slice(0,limit);
  return e.length ? e.map(x=>`<div class="ev"><span class="dim">${x.ts}</span> <b>${x.type}</b> ${x.symbol||''}
       <span class="dim">${esc(x.data)}</span></div>`).join('') : '<span class="dim">No events yet.</span>';
}

const money=x=>x==null?'—':'₹'+Number(x).toLocaleString('en-IN',{maximumFractionDigits:0});

function renderAccount(a){
  if(!a || !a.ok){
    $('acct').innerHTML=`<span class="pill bad">KITE TOKEN</span> <span class="dim">${a?esc(a.error||'unavailable'):'…'}</span>`;
    $('acct_full').innerHTML=`<span class="down">Cannot read the account: ${a?esc(a.error||''):'…'}</span>
      <div class="hint" style="margin-top:6px">The daemon token dies around 06:00 daily.
      Re-auth with <code>algo login</code>.</div>`;
    $('acct_funds').innerHTML='<span class="dim">—</span>';
    return;
  }
  // Header: the two numbers that decide whether you can trade at all.
  $('acct').innerHTML=
    `<b>${esc(a.client_id||'?')}</b> <span class="dim">${esc(a.name||'')}</span>`
    + ` · avail <b class="up">${money(a.available)}</b>`
    + ` · used <b>${money(a.used)}</b>`
    + (a.m2m_unrealised!=null
        ? ` · M2M <b class="${sign(a.m2m_unrealised)}">${money(a.m2m_unrealised)}</b>` : '');

  $('acct_full').innerHTML=`<table>
    <tr><td>Client ID</td><td><b>${esc(a.client_id||'—')}</b></td></tr>
    <tr><td>Name</td><td>${esc(a.name||'—')}</td></tr>
    <tr><td>Broker</td><td>${esc(a.broker||'—')}</td></tr>
    <tr><td>Email</td><td>${esc(a.email||'—')}</td></tr>
    <tr><td>Exchanges</td><td>${esc((a.exchanges||[]).join(', ')||'—')}</td></tr>
    <tr><td>Products</td><td>${esc((a.products||[]).join(', ')||'—')}</td></tr></table>`;

  const used=Number(a.used||0), net=Number(a.net||0), total=used+Number(a.available||0);
  const pct=total?Math.round(used/total*100):0;
  $('acct_funds').innerHTML=`<table>
    <tr><td>Opening balance</td><td>${money(a.opening)}</td></tr>
    <tr><td>Net</td><td>${money(a.net)}</td></tr>
    <tr><td>Available to trade</td><td class="up"><b>${money(a.available)}</b></td></tr>
    <tr><td>Used (margin blocked)</td><td>${money(a.used)} <span class="dim">${pct}% deployed</span></td></tr>
    <tr><td>M2M unrealised</td><td class="${sign(a.m2m_unrealised)}">${money(a.m2m_unrealised)}</td></tr>
    <tr><td>M2M realised</td><td class="${sign(a.m2m_realised)}">${money(a.m2m_realised)}</td></tr>
    </table>
    <div class="hint" style="margin-top:8px">Used is margin blocked, not money at risk —
    MIS is ~5x, so notional exposure is roughly 5x this figure. Refreshes every 20s.</div>`;
}

function render(s){
  $('ver').textContent='v'+s.ui_version;
  renderAccount(s.account);
  if(s.ui_version!==BUILT){
    const b=$('jserr'); b.style.display='block';
    b.textContent=`This page is v${BUILT} but the server is v${s.ui_version} — hard-reload (Cmd-Shift-R).`;
  }
  const d=s.daemon;
  const mkt = s.market_open
    ? `<span class="pill ok">MARKET OPEN</span>`
    : `<span class="pill warnp">MARKET CLOSED</span>`;
  $('daemon').innerHTML = (d.running
    ? `<span class="pill ${d.fresh?'ok':'warnp'}">${d.fresh?'LIVE':'STALE'}</span> pid ${d.pid} · ${d.mode} · ${d.age_s}s ago`
    : `<span class="pill bad">DAEMON NOT RUNNING</span>`)
    + ` · ${mkt} ${s.now} · day <b class="${sign(s.realized_pnl_today)}">₹${n(s.realized_pnl_today)}</b>`;

  let al='';
  if(!d.running && !s.market_open)
    al+=`<div class="stale">Daemon is not running, and the market is CLOSED (${s.now}).
         <b>start</b> will launch it and it will exit immediately — that is expected, not a broken button.
         To run anyway: <code>algo monitor --force</code>.</div>`;
  else if(!d.running) al+=`<div class="banner">Daemon is not running — no SL management and no auto square-off.</div>`;
  else if(!d.fresh) al+=`<div class="stale">Snapshot is ${d.age_s}s old — treat these numbers as stale.</div>`;
  const naked=s.positions.filter(p=>p.protected===false);
  for(const p of naked)
    al+=`<div class="banner">${p.symbol} UNPROTECTED — ${p.qty} ${p.direction}, SL order ${p.sl_order_status}.</div>`;
  if(s.kill_switch) al+=`<div class="banner">KILL SWITCH — day at ${n(s.realized_r_today)}R. No new entries.</div>`;
  $('alerts').innerHTML=al;
  const nb=$('nb_pos');
  if(naked.length){ nb.style.display='inline'; nb.textContent='!' } else nb.style.display='none';

  $('pos').innerHTML=posTable(s); $('pos2').innerHTML=posTable(s);
  $('trig').innerHTML=trigTable(s); $('trig2').innerHTML=trigTable(s);
  $('events_s').innerHTML=evList(s,12); $('events_full').innerHTML=evList(s,200);

  if(!$('log_src').options.length){
    const opts=s.log_sources.map(x=>`<option>${x}</option>`).join('');
    $('log_src').innerHTML=opts;
    $('live_src').innerHTML=opts;
    $('live_src').value=localStorage.getItem('liveSrc')||'algo.log';
    if(localStorage.getItem('liveLeft')==='1') $('live').classList.add('left');
    if(localStorage.getItem('liveOpen')==='1') liveToggle(true);
  }
  // Pulse the rail dot only while the daemon is actually cycling.
  $('raildot').style.background = d.running && d.fresh ? 'var(--up)' : 'var(--dim)';
  $('modes').innerHTML = s.skill_modes.map(m=>`<button onclick="askMode('${m}')">/${m}</button>`).join('');
  $('mode').textContent = s.claude.cli ? `CLI: ${s.claude.cli}`
    : 'No claude CLI — questions queue for a Claude session';
  $('qa').innerHTML = s.claude.recent.map(r=>`<div class="qa">
     <div class="q">${esc(r.question)}</div>
     <div class="a">${r.status==='pending'?'<span class="dim">queued — waiting for a Claude session</span>':esc(r.answer||'')}</div>
     </div>`).join('');
}

/* ---------- live log dock ----------
   A narrow rail when collapsed, and when open it only auto-scrolls if you are already
   parked at the bottom. Scroll up to read something and the feed stops yanking you away;
   a "↓ live" button re-pins. */
const PIN_PX = 48;
let livePinned = true, liveText = '', liveMatches = [], liveIdx = 0;

function liveToggle(open){
  const el=$('live');
  el.classList.toggle('open', open); el.classList.toggle('collapsed', !open);
  $('railbtn').style.display = open ? 'none' : 'flex';
  for(const id of ['livehead','livewrap','livefoot'])
    $(id).style.display = open ? (id==='livewrap'?'flex':'block') : 'none';
  localStorage.setItem('liveOpen', open?'1':'0');
  if(open){ livePinned=true; liveLoad(); }
}
function liveSide(){
  const el=$('live'), left=el.classList.toggle('left');
  localStorage.setItem('liveLeft', left?'1':'0');
}
function liveSwitch(){
  localStorage.setItem('liveSrc', $('live_src').value);
  livePinned=true; liveLoad();
}
function livePin(){
  const b=$('livebody'); b.scrollTop=b.scrollHeight;
  livePinned=true; $('jump').style.display='none';
}
$('livebody')?.addEventListener('scroll',()=>{
  const b=$('livebody');
  const dist=b.scrollHeight-b.scrollTop-b.clientHeight;
  livePinned = dist < PIN_PX;
  $('jump').style.display = livePinned ? 'none' : 'block';
});

function liveRender(){
  const b=$('livebody');
  const q=v('livesearch');
  if(!q){
    b.textContent=liveText; liveMatches=[]; $('livematch').textContent='';
  }else{
    const rx=new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g,'\\$&'),'gi');
    let i=0;
    b.innerHTML=esc(liveText).replace(new RegExp(rx.source,'gi'),
      m=>`<mark data-i="${i++}">${m}</mark>`);
    liveMatches=[...b.querySelectorAll('mark')];
    if(liveIdx>=liveMatches.length) liveIdx=0;
    $('livematch').textContent=liveMatches.length?`${liveIdx+1}/${liveMatches.length}`:'0';
    liveMatches.forEach((m,n)=>m.classList.toggle('cur',n===liveIdx));
  }
  if(livePinned){ b.scrollTop=b.scrollHeight; $('jump').style.display='none'; }
}
function liveFind(){ liveIdx=0; liveRender();
  if(liveMatches.length){ livePinned=false; liveMatches[0].scrollIntoView({block:'center'}); } }
function liveStep(d){
  if(!liveMatches.length) return;
  liveIdx=(liveIdx+d+liveMatches.length)%liveMatches.length;
  livePinned=false; liveRender();
  liveMatches[liveIdx]?.scrollIntoView({block:'center',behavior:'smooth'});
}
async function liveLoad(){
  if(!$('live').classList.contains('open')) return;
  const src=$('live_src').value; if(!src) return;
  try{
    const j=await (await fetch(`/api/logs?src=${encodeURIComponent(src)}&lines=400`)).json();
    if(j.text!==liveText){ liveText=j.text; liveRender(); }
    $('livefoot').textContent=(j.path||'').split('/').slice(-2).join('/')+(j.lines?` · ${j.lines} lines`:'');
  }catch(e){ $('livefoot').textContent='live log unavailable: '+e; }
}

async function tick(){
  try{
    const r=await fetch('/api/state');
    render(await r.json());
  }catch(e){
    $('daemon').innerHTML='<span class="pill bad">UI lost the server</span>';
  }
  if($('p_logs').classList.contains('on') && c('log_auto')) loadLog();
  liveLoad();
}
$('ask').onclick=async()=>{
  const q=v('q'); if(!q) return;
  $('ask').disabled=true;
  try{ await fetch('/api/ask',{method:'POST',headers:{'Content-Type':'application/json'},
       body:JSON.stringify({question:q})}); $('q').value=''; await tick(); }
  finally{ $('ask').disabled=false; }
};
tick(); setInterval(tick,3000);
</script>
"""


def _snapshot() -> dict:
    snap, age, fresh = {}, None, False
    if config.STATUS_FILE.exists():
        try:
            snap = json.loads(config.STATUS_FILE.read_text())
            age = (clock.now_ist() - datetime.fromisoformat(snap["as_of"])).total_seconds()
            fresh = age < 2 * snap.get("daemon", {}).get("cycle_seconds", 150)
        except Exception:
            snap = {}

    pid = None
    if config.PID_FILE.exists():
        try:
            import os as _os
            pid = int(config.PID_FILE.read_text().strip())
            _os.kill(pid, 0)
        except Exception:
            pid = None

    events = []
    ev_path = config.DATA_DIR / f"events-{clock.now_ist().date().isoformat()}.jsonl"
    if ev_path.exists():
        for line in ev_path.read_text().splitlines()[-40:][::-1]:
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            events.append({"ts": r["ts"][11:19], "type": r["type"],
                           "symbol": r.get("symbol") or "",
                           "data": json.dumps(r.get("data", {}))[:160]})

    return {
        "daemon": {"running": pid is not None, "pid": pid, "fresh": bool(fresh),
                   "age_s": int(age) if age is not None else None,
                   "mode": snap.get("daemon", {}).get("mode", "?")},
        "positions": snap.get("positions", []),
        "closed_today": snap.get("closed_today", []),
        "kill_switch": snap.get("kill_switch", False),
        "realized_r_today": snap.get("realized_r_today", 0),
        "realized_pnl_today": snap.get("realized_pnl_today", 0),
        "triggers": [t.__dict__ for t in triggers_mod.load()][-15:],
        "events": events,
        "skill_modes": list(SKILL_MODES),
        "log_sources": list(LOG_SOURCES),
        "ui_version": UI_VERSION,
        # Without this the dashboard cannot explain why `start` appears to do nothing:
        # outside market hours the daemon launches, sees a closed market and exits at once.
        "market_open": clock.is_market_open(clock.now_ist(), clock.load_holidays()),
        "now": clock.now_ist().strftime("%H:%M:%S"),
        "account": account_snapshot(),
        "claude": {"cli": claudelink.resolve_cli(), "recent": claudelink.recent(8)},
    }


class Handler(BaseHTTPRequestHandler):
    # /api/state and /api/logs poll every few seconds. Logging them would add ~1,700
    # lines an hour and bury the requests that actually change something.
    POLLING = ("/api/state", "/api/logs")

    def log_message(self, fmt, *a):
        # stdlib writes these to stderr; route them to logs/web.jsonl instead so the
        # dashboard's own traffic is auditable alongside everything else.
        try:
            if str(getattr(self, "path", "")).startswith(self.POLLING):
                return
            audit.web(line=fmt % a, client=self.client_address[0])
        except Exception:
            pass

    def handle_one_request(self):
        started = time.monotonic()
        try:
            super().handle_one_request()
        finally:
            path = getattr(self, "path", "?")
            if not path.startswith(self.POLLING):
                audit.web(path=path, method=getattr(self, "command", "?"),
                          ms=round((time.monotonic() - started) * 1000, 1),
                          client=self.client_address[0])

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Never let a browser hold an old page. A cached build is how the control surface
        # first appeared "broken": buttons rendered, their handlers did not exist.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _json_body(self) -> dict | None:
        n = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return None

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            self._send(200, PAGE.replace("__UI_VERSION__", UI_VERSION).encode(), "text/html; charset=utf-8")
        elif path == "/api/state":
            self._send(200, json.dumps(_snapshot()).encode(), "application/json")
        elif path == "/api/logs":
            qs = parse_qs(urlparse(self.path).query)
            src = (qs.get("src") or [""])[0]
            try:
                lines = int((qs.get("lines") or ["300"])[0])
            except ValueError:
                lines = 300
            self._send(200, json.dumps(read_log(src, lines)).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        payload = self._json_body()
        if payload is None:
            self._send(400, b'{"error":"bad json"}', "application/json")
            return

        if path == "/api/run":
            result = run_command(str(payload.get("cmd", "")),
                                 payload.get("params") or {},
                                 payload.get("confirm"))
            self._send(200, json.dumps(result).encode(), "application/json")
            return

        if path == "/api/ask":
            mode = payload.get("mode")
            q = SKILL_MODES.get(mode) if mode else (payload.get("question") or "").strip()
            if not q:
                self._send(400, b'{"error":"empty question"}', "application/json")
                return
            snap = _snapshot()
            req = claudelink.enqueue(q, context={
                "positions": snap["positions"],
                "closed_today": snap["closed_today"],
                "daemon": snap["daemon"],
                "realized_pnl_today": snap["realized_pnl_today"],
            })
            self._send(200, json.dumps(req).encode(), "application/json")
            return

        self._send(404, b"not found", "text/plain")


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    # Mark this whole process as the dashboard, so its own audit records are labelled
    # "web" too — not just the subprocesses it spawns.
    os.environ[audit.SOURCE_ENV] = "web"
    audit.action("web.serve", host=host, port=port, ui_version=UI_VERSION)
    srv = HTTPServer((host, port), Handler)
    print(f"Dashboard: http://{host}:{port}   (Ctrl-C to stop)")
    print("Order-placing commands require a typed confirmation, checked server-side.")
    if not claudelink.resolve_cli():
        print("No `claude` CLI found — questions queue; read them with `algo ask --pending`.")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        srv.server_close()
