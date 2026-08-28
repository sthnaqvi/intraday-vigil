"""Regression: the day's realised R must not double-count a multi-leg exit.

Each closed record's `realized_r` is a per-share multiple, so it does not scale with
quantity. Summing it across the legs of one position reported roughly double the real
loss: a 720-share stop-out that filled as 130 + 590 was recorded as -2.03R for a
position that had lost -1.02R, and that figure tripped the kill switch (KILL_SWITCH_R
= -2.0) on a day that was barely 1R down, disabling new entries hours early.

Real numbers below are from the session that surfaced it — entry 1326.8878, stop
1313.40, so 1R = 13.4878/share and the full position's 1R is Rs 9,711.20 against a
total realised loss of Rs 9,929.36, i.e. -1.02R.
"""
from vigil import config
from vigil.state import SessionState


def _leg(symbol, entry, qty, realized_r, realized_pnl, reason):
    return {
        "symbol": symbol,
        "direction": "LONG",
        "entry": entry,
        "exit_price": 0.0,
        "qty": qty,
        "realized_r": realized_r,
        "realized_pnl": realized_pnl,
        "exit_reason": reason,
        "phase_at_exit": 1,
        "exit_time": "2026-08-28T13:52:01+05:30",
    }


def _two_leg_stopout():
    s = SessionState(date="2026-08-28")
    s.closed = [
        _leg("HCLTECH", 1326.887777777778, 130, -1.01, -1779.41, "PARTIAL_EXIT"),
        _leg("HCLTECH", 1326.887777777778, 590, -1.02, -8149.95, "SL_HIT"),
    ]
    return s


def test_multi_leg_exit_counts_once_not_once_per_leg():
    s = _two_leg_stopout()
    # Quantity-weighted: (130*-1.01 + 590*-1.02) / 720 = -1.0184
    assert s.realized_r_today == -1.018
    # The bug summed the legs to -2.03; anything near -2R is the regression.
    assert s.realized_r_today > -1.5


def test_multi_leg_exit_does_not_trip_the_kill_switch():
    s = _two_leg_stopout()
    assert s.realized_r_today > config.KILL_SWITCH_R, (
        "one position stopping at its stop must never reach the daily kill-switch "
        "threshold on its own"
    )


def test_day_r_matches_money_divided_by_the_positions_full_1r():
    s = _two_leg_stopout()
    r_per_share = 1326.887777777778 - 1313.40
    one_r_rupees = r_per_share * 720
    from_money = s.realized_pnl_today / one_r_rupees
    assert abs(s.realized_r_today - from_money) < 0.01


def test_separate_positions_still_add_up():
    s = SessionState(date="2026-08-28")
    s.closed = [
        _leg("HCLTECH", 1326.887777777778, 720, -1.0, -9711.20, "SL_HIT"),
        _leg("OBEROIRLTY", 1874.4888097660228, 983, -0.5, -11151.55, "SL_HIT"),
    ]
    assert s.realized_r_today == -1.5


def test_same_symbol_re_entered_stays_a_distinct_position():
    """A second position in the same symbol must add, not blend into the first."""
    s = SessionState(date="2026-08-28")
    s.closed = [
        _leg("HCLTECH", 1326.88, 720, -1.0, -9711.20, "SL_HIT"),
        _leg("HCLTECH", 1300.00, 500, -1.0, -6500.00, "SL_HIT"),
    ]
    assert s.realized_r_today == -2.0


def test_leg_with_no_quantity_is_not_dropped():
    s = SessionState(date="2026-08-28")
    s.closed = [_leg("HCLTECH", 1326.88, 0, -1.0, -9711.20, "SL_HIT")]
    assert s.realized_r_today == -1.0


def test_no_closed_positions_is_flat():
    assert SessionState(date="2026-08-28").realized_r_today == 0.0


def test_record_without_symbol_or_entry_is_counted_not_raised():
    """This property gates the kill switch — a legacy record must not make it throw."""
    s = SessionState(date="2026-08-28")
    s.closed = [
        {"symbol": "X0", "realized_r": -1.0, "realized_pnl": -1000.0},
        {"symbol": "X1", "realized_r": -1.1, "realized_pnl": -1100.0},
    ]
    assert s.realized_r_today == -2.1
