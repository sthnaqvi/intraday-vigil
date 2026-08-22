import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from vigil import claudelink, config, triggers  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Point every runtime path at a temp dir and silence desktop notifications.

    `config.DATA_DIR` itself is patched here, but three other modules each compute
    their own path as `config.DATA_DIR / "<file>"` ONCE at import time (triggers.py's
    TRIGGERS_FILE/EXIT_TRIGGERS_FILE, claudelink.py's REQUESTS_FILE) — patching
    config.DATA_DIR after that binding does nothing to them, so without patching each
    one directly, any test that touches triggers or the Claude question queue reads
    and can write to this machine's REAL, live data/ directory instead of tmp_path.
    That's how a stale trigger armed in a real trading session once leaked into and
    broke an unrelated, supposedly-hermetic replay test.
    """
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "STATUS_FILE", tmp_path / "data" / "status.json")
    monkeypatch.setattr(config, "RISK_FILE", tmp_path / "data" / "risk.json")
    monkeypatch.setattr(config, "HOLIDAYS_FILE", tmp_path / "data" / "holidays.txt")
    # Same early-binding trap as triggers.py/claudelink.py below, missed when this fixture
    # was first written: config.PID_FILE is also `DATA_DIR / "daemon.pid"` computed once at
    # import time. Left unpatched, a test that reads or signals "the daemon's" PID reads
    # and can act on THIS MACHINE'S real running daemon (`vigil web`/`vigil start`) instead
    # of a hermetic fixture — found live when a real `vigil web` process was running during
    # a test session and a full-suite run hung.
    monkeypatch.setattr(config, "PID_FILE", tmp_path / "data" / "daemon.pid")
    monkeypatch.setattr(triggers, "TRIGGERS_FILE", tmp_path / "data" / "triggers.json")
    monkeypatch.setattr(triggers, "EXIT_TRIGGERS_FILE",
                        tmp_path / "data" / "exit_triggers.json")
    monkeypatch.setattr(claudelink, "REQUESTS_FILE",
                        tmp_path / "data" / "claude-requests.jsonl")

    import vigil.monitor as monitor_mod

    monkeypatch.setattr(monitor_mod, "notify", lambda *a, **k: None)
    monkeypatch.setattr(monitor_mod, "alert_dialog", lambda *a, **k: None)
    # monitor.py's _auto_enqueue (SL_LOST/KILL_SWITCH/PHASE_CHANGE/TIME_ALERT triggers,
    # plus the timer) calls claudelink.enqueue() on a background thread from real code
    # paths several existing tests already drive incidentally — a position reaching phase
    # 3, or a replay that jumps past several time-alert thresholds at once — without
    # knowing or caring about auto-enqueue at all. Left running for real, that background
    # thread does a real file write and can easily outlive the test function that
    # triggered it; pytest deletes tmp_path right after the test returns, so the write
    # then fails against an already-deleted directory (an unhandled thread exception).
    # Muted here the same way notify/alert_dialog are above: monitor.py imports the
    # function directly (`from .claudelink import enqueue as _claudelink_enqueue`, not
    # `from . import claudelink`), so patching monitor_mod's own local name has no effect
    # on vigil.claudelink.enqueue itself — test_claudelink.py, which tests that function
    # directly, is untouched by this. A test that wants to assert an auto-enqueue
    # happened patches monitor_mod._claudelink_enqueue, which overrides this default.
    monkeypatch.setattr(monitor_mod, "_claudelink_enqueue",
                        lambda *a, **k: {"id": "test", "status": "pending"})
    # Belt-and-suspenders for claudelink's OTHER call site (webui.py's /api/ask, called
    # directly — not through monitor.py's local name above): keeps enqueue() in its safe,
    # request-only queue mode if any test exercises that path without its own CLI mock.
    monkeypatch.setattr(claudelink, "resolve_cli", lambda: None)
    yield tmp_path
