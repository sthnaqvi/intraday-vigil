"""MarketProfile: construction-time squareoff invariant + derived alert text."""
from datetime import time
from zoneinfo import ZoneInfo

import pytest

from vigil.market_profile import NSE, MarketProfile

IST = ZoneInfo("Asia/Kolkata")


def _profile(squareoff_at, venue_squareoff_at, min_lead=240):
    return MarketProfile(
        tz=IST, tick=0.05, market_open=time(9, 15), market_close=time(15, 30),
        no_new_entries_after=time(14, 30), squareoff_at=squareoff_at,
        venue_squareoff_at=venue_squareoff_at, min_squareoff_lead_s=min_lead,
    )


def test_default_nse_profile_has_a_real_head_start():
    assert NSE.squareoff_at == time(15, 5)
    assert NSE.venue_squareoff_at == time(15, 10)


def test_construction_rejects_too_little_head_start():
    with pytest.raises(ValueError, match="head start"):
        _profile(squareoff_at=time(15, 9), venue_squareoff_at=time(15, 10))


def test_construction_rejects_squareoff_at_or_after_venue():
    with pytest.raises(ValueError):
        _profile(squareoff_at=time(15, 10), venue_squareoff_at=time(15, 10))
    with pytest.raises(ValueError):
        _profile(squareoff_at=time(15, 12), venue_squareoff_at=time(15, 10))


def test_construction_accepts_exactly_the_minimum_lead():
    p = _profile(squareoff_at=time(15, 6), venue_squareoff_at=time(15, 10), min_lead=240)
    assert p.squareoff_at == time(15, 6)


def test_time_alerts_derive_from_squareoff_at_not_hardcoded():
    """Move squareoff 20 minutes earlier; every alert time and every minute count in the
    alert text must move with it, automatically."""
    p = _profile(squareoff_at=time(14, 45), venue_squareoff_at=time(14, 50))
    alerts = p.time_alerts()
    assert alerts["alert_1400"][0] == time(13, 40)
    assert "1h 5m" in alerts["alert_1400"][1]
    assert "14:45" in alerts["alert_1400"][1]
    assert alerts["alert_1430"][0] == time(14, 10)
    assert alerts["alert_1445"][0] == time(14, 25)
    assert "14:50" in alerts["alert_1445"][1]


def test_time_alerts_match_original_hardcoded_defaults():
    """The default NSE profile must reproduce exactly what used to be hand-typed."""
    alerts = NSE.time_alerts()
    assert alerts["alert_1400"] == (
        time(14, 0), "1h 5m to auto-squareoff (15:05). Review open positions.")
    assert alerts["alert_1430"] == (
        time(14, 30), "35 min left. No new entries from now. Consider exiting losers.")
    assert alerts["alert_1445"][0] == time(14, 45)
    assert "15:05" in alerts["alert_1445"][1] and "15:10" in alerts["alert_1445"][1]
