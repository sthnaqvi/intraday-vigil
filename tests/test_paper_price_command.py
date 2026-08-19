"""`vigil paper-price` — the only CLI-level way to move price in a paper session, since
there's no real market feeding it."""
import types

import pytest

from vigil import config, paper_mode
from vigil.commands.orders import cmd_paper_price
from vigil.paper_adapter import PaperAdapter


def test_refuses_outside_paper_mode(capsys):
    assert not paper_mode.is_paper_mode()
    code = cmd_paper_price(types.SimpleNamespace(symbol="DEMO", price=100.0))
    assert code == 3
    assert "REFUSED" in capsys.readouterr().err


def test_sets_price_visible_to_a_fresh_adapter_load(capsys):
    paper_mode.set_paper_mode(True)
    code = cmd_paper_price(types.SimpleNamespace(symbol="demo", price=101.5))
    assert code == 0
    assert "DEMO" in capsys.readouterr().out

    adapter = PaperAdapter.load_or_create(config.DATA_DIR / "paper_book.json")
    quotes = adapter.quotes(["NSE:DEMO"])
    assert quotes["NSE:DEMO"].last_price == pytest.approx(101.5)
