"""Armed-trigger tests: crossing logic, the entry gate, and the execute path.

The WebSocket transport itself is proven separately against the live feed; these cover the
decision logic that fires an order, which is where money is at risk.
"""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from tests.mock_kite import MockKite
from vigil import config
from vigil import triggers as T
from vigil.broker import Broker
from vigil.events import EventLog

IST = ZoneInfo("Asia/Kolkata")


class FakeSession:
    def __init__(self, kill_switch=False, realized_r_today=0.0):
        self.kill_switch = kill_switch
        self.realized_r_today = realized_r_today


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "TRIGGERS_FILE", tmp_path / "triggers.json")
    monkeypatch.setattr(T, "notify", lambda *a, **k: None)
    monkeypatch.setattr(T, "alert_dialog", lambda *a, **k: None)
    yield


def _bits(tmp_path):
    kite = MockKite()
    events = EventLog(tmp_path / "data")
    return kite, Broker(kite, events), events


def _events(tmp_path, type_):
    p = next((tmp_path / "data").glob("events-*.jsonl"), None)
    if p is None:
        return []
    return [json.loads(line) for line in p.read_text().splitlines()
            if json.loads(line)["type"] == type_]


# ---------- crossing ----------

def test_crossed_above_and_below():
    up = T.Trigger("RELIANCE", "LONG", 1328.60, "above", 10, 0.01)
    assert not up.crossed(1328.60), "touching the level is not a break"
    assert up.crossed(1328.65)
    assert not up.crossed(1300.0)

    down = T.Trigger("HCLTECH", "SHORT", 1296.0, "below", 10, 0.01)
    assert not down.crossed(1296.0)
    assert down.crossed(1295.95)
    assert not down.crossed(1300.0)


def test_persistence_round_trip(tmp_path):
    T.save([T.Trigger("RELIANCE", "LONG", 1328.6, "above", 590, 0.0091, auto=True,
                      pdh=1320.8, pdl=1298.1, note="x")])
    back = T.load()
    assert len(back) == 1 and back[0].symbol == "RELIANCE"
    assert back[0].auto is True and back[0].qty == 590
    assert T.armed(back) == back


def test_unknown_fields_in_file_do_not_crash_load(tmp_path):
    (tmp_path / "triggers.json").write_text(json.dumps([{
        "symbol": "X", "direction": "LONG", "level": 1.0, "side": "above",
        "qty": 1, "sl_pct": 0.01, "some_future_field": "ignored",
    }]))
    assert T.load()[0].symbol == "X"


def test_corrupt_file_returns_empty_not_exception(tmp_path):
    (tmp_path / "triggers.json").write_text("{not json")
    assert T.load() == []


# ---------- entry gate ----------

def test_gate_blocks_after_1430(monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 14, 31, tzinfo=IST))
    assert "cutoff" in T.gate_block_reason(FakeSession())


def test_gate_blocks_on_kill_switch(monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    session = FakeSession(kill_switch=True, realized_r_today=-2.1)
    assert "kill switch" in T.gate_block_reason(session)


def test_gate_allows_in_window(monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    assert T.gate_block_reason(FakeSession()) is None


def test_execute_refuses_when_gate_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 14, 45, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_quote("RELIANCE", 1329.0)
    t = T.Trigger("RELIANCE", "LONG", 1328.6, "above", 100, 0.01, auto=True)

    assert T.execute(t, broker, events, FakeSession(), 1329.0) is False
    assert kite.place_calls == [], "no order may be placed once the gate is shut"
    assert t.status == T.CANCELLED
    assert _events(tmp_path, "TRIGGER_BLOCKED")


# ---------- execution ----------

def test_execute_places_entry_then_guarded_sl_and_writes_seed(tmp_path, monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_quote("RELIANCE", 1329.0)
    t = T.Trigger("RELIANCE", "LONG", 1328.6, "above", 100, 0.008,
                  auto=True, pdh=1320.8, pdl=1298.1)

    assert T.execute(t, broker, events, FakeSession(), 1329.0) is True

    kinds = [c["order_type"] for c in kite.place_calls]
    assert kinds == ["MARKET", "SL-M"], "entry must precede the stop"
    assert kite.place_calls[0]["transaction_type"] == "BUY"
    assert kite.place_calls[1]["transaction_type"] == "SELL"
    assert kite.place_calls[1]["quantity"] == 100, "SL must cover the whole position"

    # raw SL 1329*0.992 = 1318.37 sits 0.18% under PDH 1320.80 -> guard pushes it below
    sl = kite.place_calls[1]["trigger_price"]
    assert sl < 1320.8 * (1 - 0.0029), f"stop-hunt guard not applied, sl={sl}"

    seeds = json.loads(config.RISK_FILE.read_text())
    assert "RELIANCE" in seeds and seeds["RELIANCE"]["pdh"] == 1320.8
    assert 0.005 < seeds["RELIANCE"]["sl_pct"] < 0.015

    assert t.status == T.FIRED and t.entry_order_id and t.sl_order_id
    assert _events(tmp_path, "TRIGGER_FIRED")


def test_execute_derives_sl_from_actual_fill_not_the_trigger_level(tmp_path, monkeypatch):
    """A gap through the level must not anchor the stop to a price we never got."""
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_quote("RELIANCE", 1350.0)          # filled well above the 1328.60 trigger
    t = T.Trigger("RELIANCE", "LONG", 1328.6, "above", 100, 0.01, auto=True)

    assert T.execute(t, broker, events, FakeSession(), 1350.0) is True
    sl = kite.place_calls[1]["trigger_price"]
    assert sl > 1328.6, "SL was anchored to the trigger level, not the fill"
    assert 1330 < sl < 1340


def test_execute_warns_but_still_places_when_guard_pushes_sl_past_the_cap(tmp_path, monkeypatch):
    """A real live incident (2026-08-24): a 1.1% input sl_pct passed the pre-trade cap
    check cleanly, but its raw SL (1042.55) landed within the guard's 0.3% buffer of PDH
    (1040.45, distance 0.20%) — a real, correct guard decision since the position had just
    broken out above it. The guard pushed the SL to 1037.33 to stay clear, which by itself
    implies an effective width of ~1.60%, over the 1.5% cap the pre-trade check exists to
    enforce. The position is already open by this point (entry already filled) — refusing
    the SL leg would leave it naked, and clamping back to exactly the cap would put the
    stop right back inside the level the guard just avoided. So this must not refuse:
    it must place the (wider) guarded SL anyway and make the breach loud instead of only
    discoverable later by hand-computing sl_pct from `vigil status --json`, as happened
    live."""
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 24, 11, 42, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_quote("HINDALCO", 1054.145)
    t = T.Trigger("HINDALCO", "LONG", 1050.0, "above", 628, 0.011,
                  auto=True, pdh=1040.45, pdl=1024.6)

    assert T.execute(t, broker, events, FakeSession(), 1054.145) is True, \
        "a post-guard cap breach must not block placing the SL"

    sl = kite.place_calls[1]["trigger_price"]
    effective = abs(1054.145 - sl) / 1054.145
    assert effective > 0.015, f"test setup didn't actually breach the cap, sl={sl}"

    warnings = _events(tmp_path, "SL_CAP_EXCEEDED_POST_GUARD")
    assert len(warnings) == 1
    assert warnings[0]["symbol"] == "HINDALCO"
    assert warnings[0]["data"]["input_sl_pct"] == 0.011
    assert warnings[0]["data"]["effective_sl_pct"] == round(effective, 4)


def test_execute_rounds_sl_to_the_symbols_own_tick_not_the_nse_default(tmp_path, monkeypatch):
    """DRREDDY (and other 0.10-tick scrips) must not get an SL rounded to the 0.05
    default: the exchange rejects a trigger price that isn't a multiple of the script's
    own tick ('Kindly enter trigger price in the multiple of tick size for this script'),
    which is exactly what a live DRREDDY entry hit — filled, then the SL leg failed."""
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 20, 12, 43, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_tick_size("DRREDDY", 0.10)
    kite.set_quote("DRREDDY", 1179.65)
    # raw SL 1179.65 * 0.99 = 1167.8535 -> rounds to 1167.85 under the 0.05 default
    # (not a multiple of 0.10) but must round to 1167.80 under DRREDDY's real tick.
    t = T.Trigger("DRREDDY", "LONG", 1179.0, "above", 60, 0.01, auto=True)

    assert T.execute(t, broker, events, FakeSession(), 1179.65) is True
    sl = kite.place_calls[1]["trigger_price"]
    assert round(sl / 0.10, 6) % 1 == 0, f"sl={sl} is not a multiple of DRREDDY's 0.10 tick"
    assert sl == 1167.80


def test_sl_failure_leaves_a_seed_so_the_daemon_can_recover(tmp_path, monkeypatch):
    """Entry filled but the SL leg failed: the position is open and unprotected."""
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_quote("RELIANCE", 1329.0)

    real_place_stop_order = Broker.place_stop_order
    monkeypatch.setattr(Broker, "place_stop_order",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("rejected")))
    t = T.Trigger("RELIANCE", "LONG", 1328.6, "above", 100, 0.01, auto=True)

    assert T.execute(t, broker, events, FakeSession(), 1329.0) is False
    assert t.status == T.FAILED and "WITHOUT SL" in t.detail
    assert _events(tmp_path, "TRIGGER_SL_FAILED")
    seeds = json.loads(config.RISK_FILE.read_text())
    assert "RELIANCE" in seeds, "seed must exist so the monitor can place the missing SL"
    monkeypatch.setattr(Broker, "place_stop_order", real_place_stop_order)


def test_entry_failure_places_no_stop_and_reports(tmp_path, monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_quote("RELIANCE", 1329.0)
    monkeypatch.setattr(Broker, "place_market_order",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("margin")))
    t = T.Trigger("RELIANCE", "LONG", 1328.6, "above", 100, 0.01, auto=True)

    assert T.execute(t, broker, events, FakeSession(), 1329.0) is False
    assert t.status == T.FAILED
    assert kite.place_calls == [], "a failed entry must not leave a naked stop"
    assert _events(tmp_path, "TRIGGER_ENTRY_FAILED")


def test_short_trigger_places_buy_stop_above(tmp_path, monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_quote("HCLTECH", 1295.0)
    t = T.Trigger("HCLTECH", "SHORT", 1296.0, "below", 200, 0.01, auto=True)

    assert T.execute(t, broker, events, FakeSession(), 1295.0) is True
    assert kite.place_calls[0]["transaction_type"] == "SELL"
    assert kite.place_calls[1]["transaction_type"] == "BUY"
    assert kite.place_calls[1]["trigger_price"] > 1295.0, "short stop must sit above entry"


# ---------- TriggerEngine: transport-free, fed by a FakeFeed ----------

class FakeFeed:
    """A PriceFeed that fires prices on command instead of from a real transport — stands
    in for both KiteTickerFeed and PollingFeed, since TriggerEngine can't tell them apart."""

    def __init__(self):
        self.started_with: list[str] | None = None
        self.on_price = None
        self.stopped = False

    def start(self, symbols, on_price):
        self.started_with = symbols
        self.on_price = on_price
        return True

    def stop(self):
        self.stopped = True

    def push(self, symbol: str, ltp: float, source: str = "ws") -> None:
        """Simulate one price update arriving from whatever transport this stands in for."""
        self.on_price(symbol, ltp, source)


def test_trigger_engine_fires_auto_trigger_on_price(tmp_path, monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_quote("RELIANCE", 1329.0)
    T.save([T.Trigger("RELIANCE", "LONG", 1328.6, "above", 100, 0.01, auto=True)])
    engine = T.TriggerEngine(broker, events, FakeSession())
    feed = FakeFeed()
    feed.start(["RELIANCE"], engine.on_price)

    feed.push("RELIANCE", 1329.0, source="ws")

    kinds = [c["order_type"] for c in kite.place_calls]
    assert kinds == ["MARKET", "SL-M"]
    fired = T.load()[0]
    assert fired.status == T.FIRED
    assert _events(tmp_path, "TRIGGER_HIT")[0]["data"]["source"] == "ws"


def test_trigger_engine_below_level_does_not_fire(tmp_path, monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    T.save([T.Trigger("RELIANCE", "LONG", 1328.6, "above", 100, 0.01, auto=True)])
    engine = T.TriggerEngine(broker, events, FakeSession())

    engine.on_price("RELIANCE", 1300.0, "poll")

    assert kite.place_calls == []
    assert T.load()[0].status == T.ARMED


def test_trigger_engine_manual_trigger_alerts_but_does_not_place(tmp_path, monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    T.save([T.Trigger("RELIANCE", "LONG", 1328.6, "above", 100, 0.01, auto=False)])
    engine = T.TriggerEngine(broker, events, FakeSession())

    engine.on_price("RELIANCE", 1329.0, "poll")

    assert kite.place_calls == [], "auto=False must never place an order"
    t = T.load()[0]
    assert t.status == T.CANCELLED and "auto disabled" in t.detail


def test_trigger_engine_ignores_prices_for_other_symbols(tmp_path, monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    T.save([T.Trigger("RELIANCE", "LONG", 1328.6, "above", 100, 0.01, auto=True)])
    engine = T.TriggerEngine(broker, events, FakeSession())

    engine.on_price("HCLTECH", 5000.0, "poll")

    assert kite.place_calls == []
    assert T.load()[0].status == T.ARMED


# ---------- exit triggers ----------

@pytest.fixture(autouse=True)
def _isolate_exit_triggers(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "EXIT_TRIGGERS_FILE", tmp_path / "exit_triggers.json")
    yield


def test_exit_crossed_above_and_below():
    up = T.ExitTrigger("RELIANCE", 1340.0, "above")
    assert not up.crossed(1340.0)
    assert up.crossed(1340.05)

    down = T.ExitTrigger("HCLTECH", 1290.0, "below")
    assert not down.crossed(1290.0)
    assert down.crossed(1289.95)


def test_exit_trigger_persistence_round_trip():
    T.save_exit_triggers([T.ExitTrigger("RELIANCE", 1340.0, "above", note="take profit")])
    back = T.load_exit_triggers()
    assert len(back) == 1 and back[0].symbol == "RELIANCE" and back[0].level == 1340.0
    assert T.armed_exits(back) == back


def test_all_armed_symbols_unions_entry_and_exit_triggers():
    T.save([T.Trigger("RELIANCE", "LONG", 1328.6, "above", 100, 0.01, auto=True)])
    T.save_exit_triggers([T.ExitTrigger("HCLTECH", 1290.0, "below")])
    assert T.all_armed_symbols() == {"RELIANCE", "HCLTECH"}


def test_trigger_engine_fires_exit_trigger_and_closes_the_position(tmp_path, monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_position("RELIANCE", 100, buy_price=1300.0)
    kite.add_sl_order("RELIANCE", "SELL", 1290.0, 100)
    T.save_exit_triggers([T.ExitTrigger("RELIANCE", 1340.0, "above")])
    engine = T.TriggerEngine(broker, events, FakeSession())

    engine.on_price("RELIANCE", 1340.5, "ws")

    assert kite.cancel_calls, "resting SL must be cancelled before the market exit"
    kinds = [c["order_type"] for c in kite.place_calls]
    assert kinds == ["MARKET"]
    assert kite.place_calls[0]["transaction_type"] == "SELL"
    fired = T.load_exit_triggers()[0]
    assert fired.status == T.FIRED
    assert _events(tmp_path, "EXIT_TRIGGER_HIT")
    assert _events(tmp_path, "EXIT_TRIGGER_FIRED")


def test_trigger_engine_exit_trigger_with_no_position_cancels_not_fires(tmp_path, monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    T.save_exit_triggers([T.ExitTrigger("RELIANCE", 1340.0, "above")])
    engine = T.TriggerEngine(broker, events, FakeSession())

    engine.on_price("RELIANCE", 1340.5, "ws")

    assert kite.place_calls == []
    t = T.load_exit_triggers()[0]
    assert t.status == T.CANCELLED and "no open position" in t.detail


def test_trigger_engine_exit_trigger_below_level_does_not_fire(tmp_path, monkeypatch):
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_position("RELIANCE", 100, buy_price=1300.0)
    T.save_exit_triggers([T.ExitTrigger("RELIANCE", 1340.0, "above")])
    engine = T.TriggerEngine(broker, events, FakeSession())

    engine.on_price("RELIANCE", 1320.0, "ws")

    assert kite.place_calls == []
    assert T.load_exit_triggers()[0].status == T.ARMED


def test_trigger_engine_fires_entry_and_exit_triggers_independently(tmp_path, monkeypatch):
    """A symbol can carry both kinds at once — an armed entry and an armed exit on some
    other already-open symbol — and each fires on its own price without touching the
    other's state."""
    monkeypatch.setattr(T.clock, "now_ist",
                        lambda: datetime(2026, 8, 18, 11, 0, tzinfo=IST))
    kite, broker, events = _bits(tmp_path)
    kite.set_quote("RELIANCE", 1329.0)
    kite.set_position("HCLTECH", 200, sell_price=1296.0)
    kite.add_sl_order("HCLTECH", "BUY", 1310.0, 200)
    T.save([T.Trigger("RELIANCE", "LONG", 1328.6, "above", 100, 0.01, auto=True)])
    T.save_exit_triggers([T.ExitTrigger("HCLTECH", 1280.0, "below")])
    engine = T.TriggerEngine(broker, events, FakeSession())

    engine.on_price("RELIANCE", 1329.0, "ws")
    engine.on_price("HCLTECH", 1279.5, "ws")

    assert T.load()[0].status == T.FIRED
    assert T.load_exit_triggers()[0].status == T.FIRED
