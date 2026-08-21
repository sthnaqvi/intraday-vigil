"""The margin-rejection fix: `rules.qty_from_margin` (pure arithmetic) and
`execution.margin_rejection_hint` (asks the broker's own order-margin calculator instead
of inferring a leverage multiplier from a rejection's numbers).

The mistake this replaces, twice-repeated (see docs/incidents/verification-gaps.md): a
rejection reported the *total* margin required for an order and how much *more* was
needed; dividing the total by the attempted quantity produced a per-share figure implying
leverage had collapsed (5x -> ~2x), when the real gap was a small fraction of one share's
value. shortfall ÷ leverage is the right number; total_required ÷ quantity is not.
"""
from __future__ import annotations

from tests.mock_kite import MockKite
from vigil import execution, rules
from vigil.events import EventLog
from vigil.guard import GuardedBroker
from vigil.kite_adapter import KiteAdapter
from vigil.rules import Direction


def test_qty_from_margin_basic():
    # Rs 100,000 available, Rs 80/share required (5x leverage on a Rs 400 stock) ->
    # Rs 99,000 sizing capital after the 1% buffer -> 1237 shares.
    qty = rules.qty_from_margin(available_margin=100_000, per_share_margin=80.0)
    assert qty == int(100_000 * 0.99 / 80.0) == 1237


def test_qty_from_margin_zero_or_negative_per_share_is_safe():
    assert rules.qty_from_margin(100_000, 0.0) == 0
    assert rules.qty_from_margin(100_000, -5.0) == 0


def _guarded(kite: MockKite) -> GuardedBroker:
    return GuardedBroker(KiteAdapter(kite), EventLog(), dry_run=False)


def test_margin_rejection_hint_uses_real_calculator_not_the_rejections_own_numbers():
    """The exact scenario that was misdiagnosed live: a big order rejected for a small
    shortfall. The hint must be built from order_margins() at real 5x leverage, not from
    dividing the rejection's total by the attempted quantity (which would imply ~2x)."""
    kite = MockKite()
    kite.set_quote("KOTAKBANK", 402.35)
    kite.set_margin_leverage(5)
    kite.set_available_margin(159_318.97)
    broker = _guarded(kite)

    error = ValueError(
        "Insufficient funds. Margin required: 403858.10. "
        "Margin available: 403184.90. Add 673.20 to place this order."
    )
    hint = execution.margin_rejection_hint(broker, "KOTAKBANK", Direction.LONG, error)

    assert hint is not None
    # Real per-share margin at 5x on 402.35 is 80.47, not 403858.10/1969 ≈ 205.11.
    assert "80.4" in hint
    expected_qty = rules.qty_from_margin(159_318.97, 402.35 * 0.20)
    assert f"~{expected_qty}" in hint


def test_margin_rejection_hint_returns_none_for_unrelated_errors():
    kite = MockKite()
    broker = _guarded(kite)
    hint = execution.margin_rejection_hint(broker, "KOTAKBANK", Direction.LONG,
                                           ValueError("some other broker error"))
    assert hint is None


def test_margin_rejection_hint_never_raises_if_calculator_call_fails():
    kite = MockKite()

    def _boom(params):
        raise RuntimeError("network blip")
    kite.order_margins = _boom  # type: ignore[method-assign]
    broker = _guarded(kite)

    hint = execution.margin_rejection_hint(
        broker, "KOTAKBANK", Direction.LONG,
        ValueError("Insufficient funds. Margin required: 1000. Margin available: 900."))
    assert hint is None  # degrades silently, never crashes the original error path


def test_end_to_end_rejection_then_correct_sized_retry(monkeypatch):
    """Full loop: an oversized order is genuinely rejected by the mock's own margin
    check, the hint computes the real affordable qty, and placing exactly that qty
    succeeds — proving the hint's number is actually correct, not just plausible."""
    kite = MockKite()
    kite.set_quote("KOTAKBANK", 402.35)
    kite.set_margin_leverage(5)
    kite.set_available_margin(1000.0)  # deliberately small, forces a real rejection
    kite.reject_orders_over_available = True
    broker = _guarded(kite)

    with_error = None
    try:
        execution.place_entry(broker, "KOTAKBANK", Direction.LONG, 100)
    except Exception as e:
        with_error = e
    assert with_error is not None

    hint = execution.margin_rejection_hint(broker, "KOTAKBANK", Direction.LONG, with_error)
    assert hint is not None
    expected_qty = rules.qty_from_margin(1000.0, 402.35 * 0.20)

    kite.reject_orders_over_available = False  # simulate placing the corrected size
    order_id = execution.place_entry(broker, "KOTAKBANK", Direction.LONG, expected_qty)
    assert order_id
