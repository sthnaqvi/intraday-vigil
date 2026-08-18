"""Broker: the Kite-flavored convenience wrapper — GuardedBroker pre-wired with
KiteAdapter, so existing callers keep writing `Broker(kite, events, dry_run=...)`.

For a broker that isn't Kite, compose `GuardedBroker(SomeAdapter(...), events, dry_run=...)`
directly instead — GuardedBroker owns every safety concern (dry-run, spacing, retry,
audit) and works with any BrokerClient adapter, not just this one.
"""
from __future__ import annotations

from typing import Any

from .events import EventLog
from .guard import GuardedBroker, NetworkException, TokenException
from .kite_adapter import KiteAdapter

__all__ = ["Broker", "NetworkException", "TokenException"]


class Broker(GuardedBroker):
    def __init__(self, kite: Any, events: EventLog, dry_run: bool = False):
        super().__init__(KiteAdapter(kite), events, dry_run=dry_run)
