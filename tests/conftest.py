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
    yield tmp_path
