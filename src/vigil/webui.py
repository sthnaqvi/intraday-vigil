"""Local dashboard + full control surface for the intraday daemon. Stdlib only.

Binds to 127.0.0.1 by design: it exposes live positions AND can place orders, so it must
never be reachable off the machine.

Every command runs through the real CLI (`python -m vigil ...`) as an argv list — never a
shell string — so all the existing guards apply unchanged: the 1.5% SL cap, the entry gate,
the kill switch, the 14:30 cutoff. The UI is a front-end for those rules, not a way round
them.

Anything that moves money requires a typed confirmation, checked on the server. A dashboard
that can fire an order with one stray click is a dashboard that eventually does.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import audit, claudelink, clock, config, paper_mode
from . import triggers as triggers_mod

SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,24}$")

# The dashboard's HTML/CSS/JS used to be a single Python string literal (PAGE) baked into
# this module; it's now real files here, served byte-for-byte. __file__-relative rather
# than importlib.resources: works identically for a source checkout and an installed
# package with zero extra packaging config, since the directory always sits next to this
# module wherever it ends up.
STATIC_DIR = Path(__file__).parent / "web_static"

# Content types for the three static files. Explicit rather than mimetypes.guess_type —
# this dashboard serves exactly three files and a wrong guess (e.g. text/plain for .js)
# would silently break the page in some browsers.
_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.css": "text/css; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
}


def _ui_version() -> str:
    """A version marker derived from the actual static files, not a number a human has
    to remember to bump. The old scheme (a manually-edited UI_VERSION string) was itself
    a source of the exact bug it existed to catch: forget to bump it after an edit and a
    stale-page warning silently stops firing. This can't drift — it IS the content."""
    h = hashlib.sha256()
    for name in sorted(_CONTENT_TYPES):
        h.update((STATIC_DIR / name).read_bytes())
    return h.hexdigest()[:10]

# Raw technical logs, newest lines last. Kept explicit — no path traversal from the client.
LOG_SOURCES = {
    "algo.log":    lambda: config.LOGS_DIR / "algo.log",
    "actions.jsonl": lambda: config.LOGS_DIR / "actions.jsonl",
    "api.jsonl":   lambda: config.LOGS_DIR / "api.jsonl",
    "web.jsonl":   lambda: config.LOGS_DIR / "web.jsonl",
    "daemon.out":  lambda: config.LOGS_DIR / "daemon.out",
    "events":      lambda: (config.DATA_DIR
                            / f"events-{clock.now_ist().date().isoformat()}.jsonl"),
    "status.json": lambda: config.STATUS_FILE,
    "risk.json":   lambda: config.RISK_FILE,
    "triggers.json": lambda: config.DATA_DIR / "triggers.json",
}


# Profile never changes within a session; margins do, but not every few seconds. Without
# these caches the dashboard's own polling would make ~1,200+ Kite calls an hour.
ACCOUNT_TTL_S = 20
_profile_cache: dict | None = None
_account_cache: dict = {"ts": 0.0, "data": None}
# Only needed once requests can run concurrently (ThreadingHTTPServer, for /api/stream) —
# a plain HTTPServer handled one request at a time and this cache's read-check-then-write
# was never actually racy. Cheap to hold; the alternative is two threads both seeing the
# cache as stale at once and both making a redundant Kite call, not corruption, but no
# reason to leave even that on the table now that it's a real possibility.
_account_lock = threading.Lock()


def account_snapshot(force: bool = False) -> dict:
    """Client id, name and live margins. Never raises — a dead token is a display state.

    In paper mode, there is no real Kite account to fetch, and no reason to make the
    header show a scary auth error for credentials a paper session never needed."""
    global _profile_cache
    if paper_mode.is_paper_mode():
        return {"ok": True, "paper": True, "client_id": "PAPER",
                "name": "simulated account, no real money", "broker": "paper",
                "available": None}
    with _account_lock:
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
    "monitor":  "Run /intraday-vigil monitor — render the daemon snapshot, check the "
                "protected flag on every position, and run the thesis-decay check.",
    "reassess": "Run /intraday-vigil reassess — re-rank all 11 sectors, check whether each "
                "open position's sector is still top/bottom 3, and surface new setups.",
    "exit":     "Run /intraday-vigil exit — square off all open MIS manually and report the "
                "exit summary.",
    "rca":      "Run /intraday-vigil rca — post-session analysis from today's event log, "
                "scored against the rubric, with the top 3 mistakes to avoid tomorrow.",
    "start":    "Run /intraday-vigil start — opening bias, macro theme, sector ranking and "
                "stock scoring for a new session.",
}


def _flag(name: str) -> str:
    return "--" + name.replace("_", "-")


def _build_argv(cmd: str, params: dict) -> list[str]:
    """Whitelisted argv. Positional symbol first, everything else as validated flags."""
    spec = COMMANDS[cmd]
    argv = [sys.executable, "-m", "vigil", cmd]

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
        # No cwd pin: `-m vigil` resolves from any working directory now that the
        # package is installed — see the matching comment in cli.py's cmd_start.
        # argv is built by _build_argv from a whitelisted command + typed params, never
        # from raw request input, and this is a list exec (no shell=True) — S603 is a
        # generic "dynamic subprocess" flag, not a finding specific to this call site.
        proc = subprocess.run(argv, capture_output=True,  # noqa: S603
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
                   "mode": snap.get("daemon", {}).get("mode", "?"),
                   "broker": snap.get("daemon", {}).get("broker", "kite")},
        "positions": snap.get("positions", []),
        "closed_today": snap.get("closed_today", []),
        "kill_switch": snap.get("kill_switch", False),
        "realized_r_today": snap.get("realized_r_today", 0),
        "realized_pnl_today": snap.get("realized_pnl_today", 0),
        # Only currently-armed triggers — triggers.json is one flat file with no date
        # scoping, so an unfiltered load() surfaces cancelled/failed entries from past
        # sessions under a panel titled "Armed triggers". `cmd_status` (commands/info.py)
        # already gets this right; the dashboard didn't.
        "triggers": [t.__dict__ for t in triggers_mod.armed(triggers_mod.load())][-15:],
        "events": events,
        "skill_modes": list(SKILL_MODES),
        "log_sources": list(LOG_SOURCES),
        "ui_version": _ui_version(),
        # Without this the dashboard cannot explain why `start` appears to do nothing:
        # outside market hours the daemon launches, sees a closed market and exits at once.
        "market_open": clock.is_market_open(clock.now_ist(), clock.load_holidays()),
        "now": clock.now_ist().strftime("%H:%M:%S"),
        "account": account_snapshot(),
        "claude": {"cli": claudelink.resolve_cli(), "recent": claudelink.recent(8)},
    }


# How often the SSE stream re-checks and re-sends the snapshot. Sending unconditionally
# on every tick (rather than diffing against the last payload and only pushing on a real
# change) is a deliberate simplification: this is one local user, not a fan-out to many
# clients, so the bandwidth an unconditional push costs is irrelevant — and it keeps
# daemon.age_s/now ticking accurately on screen between real changes for free, without a
# second mechanism to keep those fields live. Still far cheaper than the old 3s poll: one
# held connection, no repeated HTTP handshake/headers per update.
STREAM_TICK_S = 1.0


class Handler(BaseHTTPRequestHandler):
    # /api/state and /api/logs poll every few seconds. Logging them would add ~1,700
    # lines an hour and bury the requests that actually change something. /api/stream is
    # one long-lived connection, not repeated requests, so it doesn't have that problem —
    # left out of this tuple on purpose, its open/close is worth an audit line.
    POLLING = ("/api/state", "/api/logs")

    def log_message(self, fmt, *a):
        # stdlib writes these to stderr; route them to logs/web.jsonl instead so the
        # dashboard's own traffic is auditable alongside everything else.
        try:
            if str(getattr(self, "path", "")).startswith(self.POLLING):
                return
            audit.web(line=fmt % a, client=self.client_address[0])
        except Exception:  # noqa: S110 — logging a request must never break serving it
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

    def _stream_state(self) -> None:
        """Server-Sent Events: push a fresh snapshot every STREAM_TICK_S instead of
        making the browser poll on a fixed interval. Plain HTTP — a held chunked
        response with a text/event-stream content type — not a second protocol: no
        handshake or frame format to hand-roll the way a WebSocket would need, and the
        browser's native EventSource reconnects on its own after a drop, no client-side
        retry logic needed either. Requires ThreadingHTTPServer (see serve()); on a
        plain HTTPServer this one held connection would block every other request —
        every dashboard button — for as long as any tab stayed open.
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        try:
            while True:
                payload = json.dumps(_snapshot())
                self.wfile.write(f"data: {payload}\n\n".encode())
                self.wfile.flush()
                time.sleep(STREAM_TICK_S)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the browser navigated away or closed the tab — not an error

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            html = (STATIC_DIR / "index.html").read_text()
            html = html.replace("__UI_VERSION__", _ui_version())
            self._send(200, html.encode(), _CONTENT_TYPES["index.html"])
        elif path in ("/app.css", "/app.js"):
            name = path.lstrip("/")
            self._send(200, (STATIC_DIR / name).read_bytes(), _CONTENT_TYPES[name])
        elif path == "/api/state":
            self._send(200, json.dumps(_snapshot()).encode(), "application/json")
        elif path == "/api/stream":
            self._stream_state()
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


#: Always loopback. This dashboard can place, modify and cancel real orders (behind
#: typed confirmation — see run_command), so it must never be reachable from the network.
#: There used to be a --host flag here; it was removed rather than guarded, because a
#: flag that CAN expose an order-placing surface to the LAN is a footgun with a label on
#: it, not a safety feature. Use an SSH tunnel if you genuinely need remote access.
_BIND_HOST = "127.0.0.1"


def serve(port: int = 8765) -> None:
    # Mark this whole process as the dashboard, so its own audit records are labelled
    # "web" too — not just the subprocesses it spawns.
    os.environ[audit.SOURCE_ENV] = "web"
    audit.action("web.serve", host=_BIND_HOST, port=port, ui_version=_ui_version())
    # ThreadingHTTPServer, not HTTPServer: /api/stream holds one connection open for as
    # long as a browser tab stays on the page. On a single-threaded server that one
    # connection would block every other request — every dashboard button click — for
    # as long as it's open, not just slow it down.
    srv = ThreadingHTTPServer((_BIND_HOST, port), Handler)
    srv.daemon_threads = True  # a lingering /api/stream connection must not block shutdown
    # flush=True: stdout is fully buffered (not line-buffered) whenever it isn't a TTY —
    # e.g. `vigil web > web.log 2>&1 &`, exactly how a backgrounded dashboard normally
    # runs. Without an explicit flush, this message can sit unwritten in the buffer for
    # as long as serve_forever() blocks, which in practice means "until the process is
    # killed" — so redirected output looks like the server never printed anything at all.
    print(f"Dashboard: http://{_BIND_HOST}:{port}   (Ctrl-C to stop)", flush=True)
    print("Order-placing commands require a typed confirmation, checked server-side.",
          flush=True)
    if not claudelink.resolve_cli():
        print("No `claude` CLI found — questions queue; read them with `vigil ask --pending`.",
              flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard stopped.")
    finally:
        srv.server_close()
