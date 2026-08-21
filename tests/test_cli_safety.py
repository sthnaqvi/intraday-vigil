"""CLI-level safety gates that don't need a real broker connection.

cmd_monitor's notifier refusal is checked FIRST, before _live_broker() is called — so it
can be tested directly without mocking Kite. Assert it stays that way: these tests would
hang or fail on a real auth attempt if the check ever moved after _live_broker().
"""
import types

import pytest

from vigil import notify
from vigil.commands import _shared, daemon


class _Args(types.SimpleNamespace):
    """Minimal stand-in for argparse.Namespace with cmd_monitor's expected fields."""

    def __init__(self, **kwargs):
        kwargs.setdefault("paper", False)
        super().__init__(**kwargs)


@pytest.fixture(autouse=True)
def _no_real_broker(monkeypatch):
    """If the refusal check is ever accidentally bypassed, fail fast and loud instead of
    trying to hit the real Kite API / opening a browser for login."""
    def _boom(*a, **k):
        raise AssertionError("_live_broker was called — the notifier refusal check "
                             "must run BEFORE any broker/network access")
    monkeypatch.setattr(daemon, "_live_broker", _boom)


def test_live_monitor_refuses_without_a_notifier(monkeypatch, capsys):
    monkeypatch.setattr(notify, "can_notify", lambda: False)
    code = daemon.cmd_monitor(_Args(dry_run=False, allow_silent=False, force=False))
    assert code == 3
    assert "REFUSED" in capsys.readouterr().err


def test_allow_silent_overrides_the_refusal(monkeypatch):
    """Should get PAST the refusal and into _live_broker (which we've made explode, so
    reaching it — not the refusal — is what proves --allow-silent worked)."""
    monkeypatch.setattr(notify, "can_notify", lambda: False)
    with pytest.raises(AssertionError, match="_live_broker was called"):
        daemon.cmd_monitor(_Args(dry_run=False, allow_silent=True, force=False))


def test_dry_run_is_exempt_from_the_notifier_check(monkeypatch):
    """Dry-run places no orders, so a missing notifier is not a safety issue for it."""
    monkeypatch.setattr(notify, "can_notify", lambda: False)
    with pytest.raises(AssertionError, match="_live_broker was called"):
        daemon.cmd_monitor(_Args(dry_run=True, allow_silent=False, force=False))


def test_paper_mode_is_exempt_from_the_notifier_check(monkeypatch):
    """Paper mode places no REAL orders, same reasoning as dry-run."""
    monkeypatch.setattr(notify, "can_notify", lambda: False)
    with pytest.raises(AssertionError, match="_live_broker was called"):
        daemon.cmd_monitor(_Args(dry_run=False, allow_silent=False, force=False, paper=True))


def test_live_monitor_proceeds_when_a_notifier_is_available(monkeypatch):
    monkeypatch.setattr(notify, "can_notify", lambda: True)
    with pytest.raises(AssertionError, match="_live_broker was called"):
        daemon.cmd_monitor(_Args(dry_run=False, allow_silent=False, force=False))


def test_start_forwards_allow_silent_to_the_spawned_daemon(monkeypatch, tmp_path):
    """`vigil start` spawns `vigil monitor` as a subprocess — the flag must cross that
    boundary or --allow-silent on `start` would be a lie."""
    from vigil import config
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PID_FILE", tmp_path / "data" / "daemon.pid")
    monkeypatch.setattr(daemon, "_daemon_pid", lambda: None)
    monkeypatch.setattr(daemon.auth, "login", lambda **k: None)

    captured = {}

    class FakeProc:
        pid = 4242
        returncode = 0
        def poll(self):
            return None  # still running after the grace check

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)

    daemon.cmd_start(_Args(dry_run=False, force=False, paste=False, allow_silent=True))
    assert "--allow-silent" in captured["cmd"]


def test_start_with_paper_skips_login_and_forwards_the_flag(monkeypatch, tmp_path):
    """--paper must never touch auth.login (there's no broker to log into) and must cross
    the subprocess boundary into the spawned `vigil monitor`, same as --allow-silent."""
    from vigil import config
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PID_FILE", tmp_path / "data" / "daemon.pid")
    monkeypatch.setattr(daemon, "_daemon_pid", lambda: None)

    def _boom_login(**k):
        raise AssertionError("auth.login must not be called in paper mode")
    monkeypatch.setattr(daemon.auth, "login", _boom_login)

    captured = {}

    class FakeProc:
        pid = 4242
        returncode = 0
        def poll(self):
            return None

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeProc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)

    code = daemon.cmd_start(
        _Args(dry_run=False, force=False, paste=False, allow_silent=False, paper=True))
    assert code == 0
    assert "--paper" in captured["cmd"]
    assert _shared.is_paper_mode()


def test_login_clears_paper_mode(monkeypatch, tmp_path):
    """Running a real login is an explicit signal to leave paper mode behind."""
    from vigil import config
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    _shared.set_paper_mode(True)
    assert _shared.is_paper_mode()

    monkeypatch.setattr(daemon.auth, "login", lambda **k: None)
    daemon.cmd_login(_Args(paste=False, force=False))
    assert not _shared.is_paper_mode()


# ---------- restart ----------

class _FakeProc:
    pid = 4242
    returncode = 0

    def poll(self):
        return None  # still running after the grace check


def _wire_common_daemon_mocks(monkeypatch, tmp_path):
    from vigil import config
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "PID_FILE", tmp_path / "data" / "daemon.pid")
    monkeypatch.setattr(daemon.auth, "login", lambda **k: None)
    monkeypatch.setattr(daemon.time, "sleep", lambda s: None)
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda cmd, **kw: _FakeProc())


def test_restart_stops_a_running_daemon_before_starting_a_new_one(monkeypatch, tmp_path):
    """The old process must actually be signalled and confirmed gone before a fresh
    `monitor` loop is spawned — two daemons racing on the same position is exactly the
    kind of double-writer bug this codebase goes out of its way to avoid elsewhere."""
    _wire_common_daemon_mocks(monkeypatch, tmp_path)
    state = {"sigint_sent": False}

    def fake_pid():
        return None if state["sigint_sent"] else 999

    def fake_kill(pid, sig):
        if sig == daemon.signal.SIGINT:
            state["sigint_sent"] = True

    monkeypatch.setattr(daemon, "_daemon_pid", fake_pid)
    monkeypatch.setattr(daemon.os, "kill", fake_kill)

    code = daemon.cmd_restart(
        _Args(dry_run=False, force=False, paste=False, allow_silent=False))
    assert code == 0
    assert state["sigint_sent"], "must signal the old daemon before starting a new one"


def test_restart_skips_stop_when_nothing_is_running(monkeypatch, tmp_path, capsys):
    """No daemon to stop should mean no stop noise — just a plain start."""
    _wire_common_daemon_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr(daemon, "_daemon_pid", lambda: None)
    kill_calls = []
    monkeypatch.setattr(daemon.os, "kill", lambda *a: kill_calls.append(a))

    code = daemon.cmd_restart(
        _Args(dry_run=False, force=False, paste=False, allow_silent=False))
    assert code == 0
    assert kill_calls == [], "nothing to signal when nothing is running"
    assert "No daemon running" not in capsys.readouterr().out


def test_restart_forwards_flags_to_the_new_daemon(monkeypatch, tmp_path):
    """Same forwarding contract as `start` — restart isn't a second, weaker start."""
    _wire_common_daemon_mocks(monkeypatch, tmp_path)
    monkeypatch.setattr(daemon, "_daemon_pid", lambda: None)
    captured = {}

    def fake_popen(cmd, **kw):
        captured["cmd"] = cmd
        return _FakeProc()

    monkeypatch.setattr(daemon.subprocess, "Popen", fake_popen)

    daemon.cmd_restart(
        _Args(dry_run=False, force=False, paste=False, allow_silent=True))
    assert "--allow-silent" in captured["cmd"]
