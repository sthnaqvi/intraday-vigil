"""`vigil stop` refuses (absent --i-know) inside the pre-squareoff window while positions
are open — the direct fix for a decision that has now cost real money once (2026-08-20:
₹1,046) and been saved by luck once (2026-08-21: the same decision, opposite price drift).
A documented rule that only survives being remembered mid-session failed twice; this makes
the refusal itself the safety net instead of memory. See
docs/incidents/discipline-and-process.md.
"""
from __future__ import annotations

import types
from datetime import datetime
from zoneinfo import ZoneInfo

from vigil import clock
from vigil.commands import daemon
from vigil.commands.daemon import _squareoff_window_block
from vigil.state import SessionState, TrackedPosition

IST = ZoneInfo("Asia/Kolkata")


class _Args(types.SimpleNamespace):
    def __init__(self, **kwargs):
        kwargs.setdefault("i_know", False)
        super().__init__(**kwargs)


def _at(hhmm: str) -> datetime:
    h, m = int(hhmm[:2]), int(hhmm[2:])
    return datetime(2026, 8, 21, h, m, 0, tzinfo=IST)


def test_no_block_when_no_positions_open():
    SessionState(date="2026-08-21").save()
    assert _squareoff_window_block(_at("1503")) is None


def test_blocks_inside_window_with_open_position():
    s = SessionState(date="2026-08-21")
    s.positions["KOTAKBANK"] = TrackedPosition(
        symbol="KOTAKBANK", direction="LONG", entry=402.99, qty=3674,
        sl_order_id="O1", sl_price=397.85, sl_pct=0.0128,
    )
    s.save()
    # SQUAREOFF_AT is 15:05 by default; 15:02 is inside the 15-minute lead window and
    # matches almost exactly when both real incidents stopped the daemon.
    msg = _squareoff_window_block(_at("1502"))
    assert msg is not None
    assert "KOTAKBANK" in msg
    assert "--i-know" in msg


def test_does_not_block_well_before_the_window():
    s = SessionState(date="2026-08-21")
    s.positions["KOTAKBANK"] = TrackedPosition(
        symbol="KOTAKBANK", direction="LONG", entry=402.99, qty=3674,
        sl_order_id="O1", sl_price=397.85, sl_pct=0.0128,
    )
    s.save()
    assert _squareoff_window_block(_at("1200")) is None


def test_does_not_block_after_broker_squareoff_has_passed():
    s = SessionState(date="2026-08-21")
    s.positions["KOTAKBANK"] = TrackedPosition(
        symbol="KOTAKBANK", direction="LONG", entry=402.99, qty=3674,
        sl_order_id="O1", sl_price=397.85, sl_pct=0.0128,
    )
    s.save()
    assert _squareoff_window_block(_at("1530")) is None


def _seed_open_position():
    s = SessionState(date="2026-08-21")
    s.positions["KOTAKBANK"] = TrackedPosition(
        symbol="KOTAKBANK", direction="LONG", entry=402.99, qty=3674,
        sl_order_id="O1", sl_price=397.85, sl_pct=0.0128,
    )
    s.save()


def test_cmd_stop_refuses_inside_window_without_i_know(monkeypatch, capsys):
    _seed_open_position()
    monkeypatch.setattr(daemon, "_daemon_pid", lambda: 12345)
    monkeypatch.setattr(clock, "now_ist", lambda: _at("1502"))
    killed = []
    monkeypatch.setattr(daemon.os, "kill", lambda *a: killed.append(a))

    code = daemon.cmd_stop(_Args())

    assert code == 3
    assert killed == []  # never touched the process
    assert "REFUSED" in capsys.readouterr().err


def test_cmd_stop_proceeds_with_i_know_override(monkeypatch):
    _seed_open_position()
    monkeypatch.setattr(daemon, "_daemon_pid", lambda: 12345)
    monkeypatch.setattr(clock, "now_ist", lambda: _at("1502"))
    killed = []
    monkeypatch.setattr(daemon.os, "kill", lambda *a: killed.append(a))
    # cmd_stop polls _daemon_pid() again after signalling to confirm the process exited;
    # returning None the second call simulates a clean, prompt shutdown.
    calls = {"n": 0}

    def _pid():
        calls["n"] += 1
        return 12345 if calls["n"] == 1 else None
    monkeypatch.setattr(daemon, "_daemon_pid", _pid)

    code = daemon.cmd_stop(_Args(i_know=True))

    assert code == 0
    assert killed and killed[0][0] == 12345
