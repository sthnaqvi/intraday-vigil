"""CLI-level safety gates that don't need a real broker connection.

cmd_monitor's notifier refusal is checked FIRST, before _live_broker() is called — so it
can be tested directly without mocking Kite. Assert it stays that way: these tests would
hang or fail on a real auth attempt if the check ever moved after _live_broker().
"""
import types

import pytest

from vigil import notify
from vigil.commands import daemon


class _Args(types.SimpleNamespace):
    """Minimal stand-in for argparse.Namespace with cmd_monitor's expected fields."""


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
