"""Pluggable spend storage."""

from __future__ import annotations

import threading
from typing import Dict, Protocol, Tuple, runtime_checkable

__all__ = ["UsageStore", "AsyncUsageStore", "MemoryStore"]


@runtime_checkable
class UsageStore(Protocol):
    """Synchronous spend store. Implement with Redis/Postgres/etc. for multi-instance apps.

    Works with both sync and async guarded calls.
    """

    def get_spend(self, customer_id: str, period: str) -> float:
        """Current spend in USD for a customer in a period (e.g. ``"2026-10"``)."""
        ...

    def add_spend(self, customer_id: str, period: str, usd: float) -> float:
        """Atomically add USD and return the new total."""
        ...


@runtime_checkable
class AsyncUsageStore(Protocol):
    """Asynchronous spend store (``async def`` methods). Only usable from async guarded calls."""

    async def get_spend(self, customer_id: str, period: str) -> float: ...

    async def add_spend(self, customer_id: str, period: str, usd: float) -> float: ...


class MemoryStore:
    """In-memory, thread-safe store. Fine for a single process, tests and demos; lost on restart."""

    def __init__(self) -> None:
        self._data: Dict[Tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def get_spend(self, customer_id: str, period: str) -> float:
        with self._lock:
            return self._data.get((period, customer_id), 0.0)

    def add_spend(self, customer_id: str, period: str, usd: float) -> float:
        with self._lock:
            k = (period, customer_id)
            nxt = self._data.get(k, 0.0) + usd
            self._data[k] = nxt
            return nxt
