"""The Claude bridge: queue semantics, and the CLI path when a binary exists."""
import json

import pytest

from algo import claudelink, config


@pytest.fixture(autouse=True)
def _no_cli(monkeypatch):
    """Default to queue-only so tests never shell out to a real binary."""
    monkeypatch.setattr(claudelink, "resolve_cli", lambda: None)
    yield


def test_enqueue_creates_a_pending_request():
    req = claudelink.enqueue("hold or exit?", context={"symbol": "HCLTECH"})
    assert req["status"] == claudelink.PENDING and req["answer"] is None
    pend = claudelink.pending()
    assert len(pend) == 1 and pend[0]["question"] == "hold or exit?"
    assert pend[0]["context"]["symbol"] == "HCLTECH"


def test_answer_marks_it_answered_and_clears_pending():
    req = claudelink.enqueue("q1")
    assert claudelink.answer(req["id"], "a1") is True
    assert claudelink.pending() == []
    assert claudelink.recent()[0]["answer"] == "a1"


def test_answer_unknown_id_is_a_no_op():
    claudelink.enqueue("q1")
    assert claudelink.answer("deadbeef", "nope") is False
    assert len(claudelink.pending()) == 1


def test_recent_is_newest_first_and_capped():
    for i in range(6):
        claudelink.enqueue(f"q{i}")
    r = claudelink.recent(3)
    assert len(r) == 3 and r[0]["question"] == "q5"


def test_corrupt_line_does_not_break_the_queue():
    claudelink.enqueue("good")
    with (config.DATA_DIR / "claude-requests.jsonl").open("a") as f:
        f.write("{ this is not json\n")
    assert len(claudelink.recent()) == 1


def test_context_carries_the_unprotected_flag():
    """The whole point: Claude must see a naked position without being told."""
    claudelink.enqueue("status?", context={"positions": [
        {"symbol": "HCLTECH", "qty": 914, "protected": False,
         "sl_order_status": "CANCELLED"}]})
    ctx = claudelink.pending()[0]["context"]
    assert ctx["positions"][0]["protected"] is False


def test_cli_path_answers_inline_when_a_binary_exists(monkeypatch, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho 'exit it'\n")
    fake.chmod(0o755)
    monkeypatch.setattr(claudelink, "resolve_cli", lambda: str(fake))

    req = claudelink.enqueue("hold or exit?")
    assert req["status"] == claudelink.ANSWERED
    assert "exit it" in req["answer"]
    assert claudelink.pending() == []


def test_cli_failure_is_recorded_not_swallowed(monkeypatch, tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setattr(claudelink, "resolve_cli", lambda: str(fake))

    req = claudelink.enqueue("q")
    assert req["status"] == claudelink.FAILED and "exited 3" in req["answer"]
