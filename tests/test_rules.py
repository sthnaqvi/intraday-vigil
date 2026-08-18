"""Pure rules-engine tests, including the two canonical incident regressions."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from algo import rules
from algo.rules import Direction

IST = ZoneInfo("Asia/Kolkata")


# ---------- phase boundaries ----------

@pytest.mark.parametrize("pr,phase", [(0.0, 1), (0.99, 1), (1.0, 2), (1.49, 2), (1.5, 3), (3.2, 3)])
def test_target_phase_boundaries(pr, phase):
    assert rules.target_phase(pr) == phase


def test_profit_r_both_directions():
    # long: entry 100, R=1 → ltp 101.5 = +1.5R
    assert rules.profit_r(101.5, 100.0, 1.0, Direction.LONG) == pytest.approx(1.5)
    # short: entry 100, R=1 → ltp 98 = +2R
    assert rules.profit_r(98.0, 100.0, 1.0, Direction.SHORT) == pytest.approx(2.0)
    assert rules.profit_r(101.0, 100.0, 1.0, Direction.SHORT) == pytest.approx(-1.0)


# ---------- tick rounding (always in the trade's favour) ----------

def test_tick_rounding_favor():
    assert rules.round_to_tick_favor(4082.715, Direction.LONG) == 4082.70   # floor
    assert rules.round_to_tick_favor(4082.715, Direction.SHORT) == 4082.75  # ceil
    assert rules.round_to_tick_favor(100.05, Direction.LONG) == 100.05      # exact tick unchanged
    assert rules.round_to_tick_favor(100.05, Direction.SHORT) == 100.05


# ---------- stop-hunt guard ----------

def test_guard_pushes_below_level_for_long():
    # sl-rules.md example: SL 4100 vs PDL 4095 → 4095*0.997 = 4082.715
    sl, applied = rules.apply_stop_hunt_guard(4100.0, [4095.0], Direction.LONG)
    assert applied
    assert sl == pytest.approx(4082.715)
    assert abs(sl - 4095.0) / 4095.0 >= 0.0029  # now clear of the level


def test_guard_pushes_above_level_for_short():
    sl, applied = rules.apply_stop_hunt_guard(1002.0, [1000.0], Direction.SHORT)
    assert applied
    assert sl == pytest.approx(1003.0)


def test_guard_iterates_across_stacked_levels():
    # pushing below 100 lands within 0.3% of 99.8 → must push again below 99.8
    sl, applied = rules.apply_stop_hunt_guard(100.1, [100.0, 99.8], Direction.LONG)
    assert applied
    assert all(abs(sl - lv) / lv >= 0.003 for lv in (100.0, 99.8))


def test_guard_noop_when_clear():
    sl, applied = rules.apply_stop_hunt_guard(4000.0, [4100.0, 3900.0], Direction.LONG)
    assert not applied
    assert sl == 4000.0


# ---------- canonical regression: INDIGO (never park an SL next to PDH) ----------

def test_indigo_regression_manual_sl_in_stop_hunt_zone_gets_pushed():
    # The mistake: SL 4200 with PDH 4205 (0.12% away) → hunted. Guard must move it.
    sl, applied = rules.apply_stop_hunt_guard(4200.0, [4205.0], Direction.LONG)
    assert applied
    rounded = rules.round_to_tick_favor(sl, Direction.LONG)
    assert rounded == 4192.35  # 4205*(1-0.003)=4192.385 → tick-floored
    assert abs(rounded - 4205.0) / 4205.0 >= 0.0029


# ---------- canonical regression: DRREDDY (2x sl_pct trail, never fixed 5%) ----------

def test_drreddy_regression_2x_trail_fires_fixed_5pct_does_not():
    entry, sl_pct = 1271.0, 0.0102
    trail_pct = 2 * sl_pct  # 2.04%
    ltp, be_sl = 1303.8, 1271.0  # peak +2.16R, SL at breakeven

    # Correct 2x sl_pct trail: 1303.8*0.9796 = 1277.20 > BE → must fire
    intent = rules.trail_decision(ltp, trail_pct, be_sl, Direction.LONG, [],
                                  "OID", 10, require_min_move=False)
    assert intent is not None
    assert intent.trigger_price == pytest.approx(1277.20, abs=0.05)
    assert intent.trigger_price > be_sl
    assert intent.quantity == 10

    # The old fixed 5% trail: 1238.61 < BE 1271 → must NOT fire
    assert rules.trail_decision(ltp, 0.05, be_sl, Direction.LONG, [],
                                "OID", 10, require_min_move=False) is None


def test_first_trail_exempt_from_min_move_but_later_trails_are_not():
    # DRREDDY numbers: BE→1277.20 is a 0.49% move; blocked if min-move enforced
    assert rules.trail_decision(1303.8, 0.0204, 1271.0, Direction.LONG, [],
                                "OID", 10, require_min_move=True) is None
    # once trailing, small improvements below 0.5% stay blocked (anti-spam)
    assert rules.trail_decision(1305.0, 0.0204, 1277.20, Direction.LONG, [],
                                "OID", 10, require_min_move=True) is None
    # a real move re-fires
    intent = rules.trail_decision(1320.0, 0.0204, 1277.20, Direction.LONG, [],
                                  "OID", 10, require_min_move=True)
    assert intent is not None and intent.trigger_price > 1277.20


def test_trail_ratchet_never_moves_backward():
    # price fell: candidate trail below current SL → no modify (long)
    assert rules.trail_decision(1280.0, 0.02, 1270.0, Direction.LONG, [], "O", 5) is None
    # short mirror: candidate above current → no modify
    assert rules.trail_decision(995.0, 0.02, 1010.0, Direction.SHORT, [], "O", 5) is None
    # short improving
    intent = rules.trail_decision(980.0, 0.02, 1010.0, Direction.SHORT, [], "O", 5)
    assert intent is not None and intent.trigger_price < 1010.0


# ---------- breakeven ----------

def test_breakeven_moves_to_entry_and_respects_guard():
    intent = rules.breakeven_decision(4150.0, 4108.5, Direction.LONG, [], "O", 100)
    assert intent is not None
    assert intent.trigger_price == 4150.0
    assert intent.quantity == 100
    # entry sits right on PDL → guarded below it
    intent = rules.breakeven_decision(4150.0, 4100.0, Direction.LONG, [4151.0], "O", 100)
    assert intent is not None
    assert intent.trigger_price < 4150.0
    assert abs(intent.trigger_price - 4151.0) / 4151.0 >= 0.0029


# ---------- cadence / misc ----------

def test_near_sl_and_cycle_interval():
    assert rules.near_sl(100.4, 100.0)
    assert not rules.near_sl(101.0, 100.0)
    assert rules.cycle_interval(True) == 90
    assert rules.cycle_interval(False) == 150


def test_realized_r_signs():
    assert rules.realized_r(100.0, 103.0, 1.0, Direction.LONG) == pytest.approx(3.0)
    assert rules.realized_r(100.0, 99.0, 1.0, Direction.LONG) == pytest.approx(-1.0)
    assert rules.realized_r(100.0, 97.0, 1.0, Direction.SHORT) == pytest.approx(3.0)


def test_time_actions_fire_once_each():
    dt = lambda h, m: datetime(2026, 8, 17, h, m, tzinfo=IST)  # noqa: E731
    assert rules.due_time_actions(dt(13, 59), set()) == []
    assert rules.due_time_actions(dt(14, 0), set()) == ["alert_1400"]
    assert rules.due_time_actions(dt(14, 46), {"alert_1400"}) == ["alert_1430", "alert_1445"]
    due = rules.due_time_actions(dt(15, 11), {"alert_1400", "alert_1430", "alert_1445"})
    assert due == ["squareoff"]
    assert rules.due_time_actions(dt(15, 11), {"alert_1400", "alert_1430", "alert_1445",
                                               "squareoff"}) == []


def test_no_new_entries_gates():
    dt = lambda h, m: datetime(2026, 8, 17, h, m, tzinfo=IST)  # noqa: E731
    assert rules.no_new_entries(dt(10, 0), False) == (False, None)
    blocked, why = rules.no_new_entries(dt(14, 30), False)
    assert blocked and "14:30" in why
    blocked, why = rules.no_new_entries(dt(10, 0), True)
    assert blocked and "kill_switch" in why
