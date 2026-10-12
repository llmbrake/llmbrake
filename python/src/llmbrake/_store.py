"""Pluggable spend storage."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Dict, Optional, Protocol, Tuple, runtime_checkable

__all__ = ["UsageStore", "AsyncUsageStore", "MemoryStore", "Reservation", "ReservingStore", "is_reserving_store"]


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


@dataclass(frozen=True)
class Reservation:
    """What a reserving store answers before a call."""

    #: False = at/over budget (counting other in-flight calls); the call must not be made.
    allowed: bool
    #: Settled spend this period (USD).
    spent_usd: float
    #: Effective budget (USD); ``math.inf`` = unlimited. May be a dashboard override.
    budget_usd: float
    #: Pass back to ``settle``. None when not allowed.
    hold_id: Optional[str]


@runtime_checkable
class ReservingStore(Protocol):
    """Optional extension of :class:`UsageStore` for shared stores (e.g. ``HostedStore``).

    When the guard's store has ``reserve``/``settle``, wrapped calls check-and-reserve atomically
    before the provider call and settle after it, so parallel calls across servers can't all
    overshoot the cap together. Methods may be ``async def`` (async guarded calls only).
    """

    def reserve(
        self, customer_id: str, period: str, *, hold_usd: float, budget_usd: float, downgrade_at: float, stream: bool
    ) -> Reservation: ...

    def settle(
        self, hold_id: Optional[str], customer_id: str, period: str, *, cost_usd: float, downgraded: bool
    ) -> float: ...


def is_reserving_store(store: Any) -> bool:
    return callable(getattr(store, "reserve", None)) and callable(getattr(store, "settle", None))
