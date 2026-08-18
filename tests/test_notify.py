"""Notifier backend detection and the guaranteed terminal fallback.

Before this, every notification was a bare macOS osascript call wrapped in
`except Exception: pass`. Off macOS, or if osascript itself failed, the daemon went
completely silent with no indication anything was wrong. These tests pin two things:
can_notify() correctly reflects whether a REAL (non-terminal) backend is available, and
notify()/alert_dialog() never raise and never fail to reach at least the terminal.
"""
import importlib

import pytest

from vigil import notify


@pytest.fixture(autouse=True)
def _reload_after():
    """Every test here monkeypatches platform/shutil detection; reload back to the real
    machine's backend afterward so later test files see genuine detection again."""
    yield
    importlib.reload(notify)


def test_macos_backend_detected_when_osascript_present(monkeypatch):
    monkeypatch.setattr(notify.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda tool: "/usr/bin/osascript"
                        if tool == "osascript" else None)
    mod = importlib.reload(notify)
    assert mod.can_notify() is True
    assert "macOS" in mod.backend_name()


def test_linux_backend_detected_when_notify_send_present(monkeypatch):
    monkeypatch.setattr(notify.platform, "system", lambda: "Linux")
    monkeypatch.setattr(notify.shutil, "which", lambda tool: "/usr/bin/notify-send"
                        if tool == "notify-send" else None)
    mod = importlib.reload(notify)
    assert mod.can_notify() is True
    assert "Linux" in mod.backend_name()


def test_no_backend_when_nothing_available(monkeypatch):
    monkeypatch.setattr(notify.platform, "system", lambda: "Linux")
    monkeypatch.setattr(notify.shutil, "which", lambda tool: None)
    mod = importlib.reload(notify)
    assert mod.can_notify() is False
    assert "none" in mod.backend_name().lower()


def test_terminal_fallback_never_raises_and_always_delivers(monkeypatch, capsys):
    monkeypatch.setattr(notify.platform, "system", lambda: "Linux")
    monkeypatch.setattr(notify.shutil, "which", lambda tool: None)
    mod = importlib.reload(notify)

    mod.notify("SL hit on TESTSYM", sound=True)
    mod.alert_dialog("TESTSYM is UNPROTECTED")

    err = capsys.readouterr().err
    assert "SL hit on TESTSYM" in err
    assert "TESTSYM is UNPROTECTED" in err


def test_backend_failure_still_reaches_terminal(monkeypatch, capsys):
    """Even when a real backend IS detected, if the actual call fails, the message must
    still reach the user somehow — not vanish into a bare except/pass like before."""
    monkeypatch.setattr(notify.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(notify.shutil, "which", lambda tool: "/usr/bin/osascript")
    mod = importlib.reload(notify)

    class AlwaysFails:
        name = "broken"
        def notify(self, *a, **k): return False
        def alert(self, *a, **k): return False

    mod._backend = AlwaysFails()
    mod.notify("must not vanish")
    assert "must not vanish" in capsys.readouterr().err
