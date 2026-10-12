"""Hosted, shared spend counters (LLMBrake Cloud). No extra dependencies.

One counter per customer per month across every server using the same API key; budgets edited
in the dashboard override the guard's ``budget_usd``. Example::

    import os
    from llmbrake import create_guard
    from llmbrake.hosted import HostedStore

    guard = create_guard(5, store=HostedStore(os.environ["LLMBRAKE_API_KEY"]))

For ``asyncio`` apps use :class:`AsyncHostedStore` (same API, ``async`` methods; requests run in a
worker thread so the event loop never blocks).
"""

from __future__ import annotations

import asyncio
import json
import math
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

from ._store import Reservation
from ._version import __version__

__all__ = ["HostedStore", "AsyncHostedStore", "HostedStoreError", "DEFAULT_HOSTED_URL"]

DEFAULT_HOSTED_URL = "https://app.llmbrake.com"


class HostedStoreError(Exception):
    def __init__(self, message: str, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class HostedStore:
    """Sync hosted store. Implements ``get_spend``/``add_spend`` plus ``reserve``/``settle``."""

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_HOSTED_URL, timeout: float = 3.0) -> None:
        if not api_key:
            raise TypeError("llmbrake: HostedStore needs an api_key")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # -- transport (overridable in tests) -------------------------------------------------
    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "authorization": f"Bearer {self.api_key}",
                "content-type": "application/json",
                "user-agent": f"llmbrake-python/{__version__}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310 (https base URL)
                return json.loads(r.read() or b"{}")  # type: ignore[no-any-return]
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read() or b"{}").get("error", "")
            except Exception:
                err = ""
            raise HostedStoreError(f"llmbrake: hosted API {e.code} {err}".strip(), e.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise HostedStoreError(f"llmbrake: hosted API unreachable ({e})") from None

    # -- UsageStore ---------------------------------------------------------------------
    def get_spend(self, customer_id: str, period: str) -> float:
        q = urllib.parse.urlencode({"customerId": customer_id, "period": period})
        return float(self._request("GET", f"/v1/spend?{q}")["spentUsd"])

    def add_spend(self, customer_id: str, period: str, usd: float) -> float:
        r = self._request("POST", "/v1/spend/add", {"customerId": customer_id, "period": period, "usd": usd})
        return float(r["spentUsd"])

    # -- ReservingStore -----------------------------------------------------------------
    def reserve(
        self, customer_id: str, period: str, *, hold_usd: float, budget_usd: float, downgrade_at: float, stream: bool
    ) -> Reservation:
        r = self._request(
            "POST",
            "/v1/reserve",
            {
                "customerId": customer_id,
                "period": period,
                "holdUsd": hold_usd,
                "budgetUsd": None if math.isinf(budget_usd) else budget_usd,
                "downgradeAt": downgrade_at,
                "ttlSec": 900 if stream else 120,
            },
        )
        b = r.get("budgetUsd")
        return Reservation(
            allowed=bool(r.get("allowed")),
            spent_usd=float(r["spentUsd"]),
            budget_usd=math.inf if b is None else float(b),
            hold_id=r.get("holdId"),
        )

    def settle(
        self, hold_id: Optional[str], customer_id: str, period: str, *, cost_usd: float, downgraded: bool
    ) -> float:
        r = self._request(
            "POST",
            "/v1/settle",
            {
                "holdId": hold_id,
                "customerId": customer_id,
                "period": period,
                "costUsd": cost_usd,
                "downgraded": downgraded,
            },
        )
        return float(r["spentUsd"])

    def customer(self, customer_id: str, period: str) -> Dict[str, Any]:
        """Server view of a customer: spentUsd, pendingUsd, budgetUsd, budgetSource, status."""
        q = urllib.parse.urlencode({"customerId": customer_id, "period": period})
        return self._request("GET", f"/v1/spend?{q}")


class AsyncHostedStore:
    """Async hosted store for async guarded calls (``guard.wrap_async`` / AsyncOpenAI)."""

    def __init__(self, api_key: str, *, base_url: str = DEFAULT_HOSTED_URL, timeout: float = 3.0) -> None:
        self.sync = HostedStore(api_key, base_url=base_url, timeout=timeout)

    async def get_spend(self, customer_id: str, period: str) -> float:
        return await asyncio.to_thread(self.sync.get_spend, customer_id, period)

    async def add_spend(self, customer_id: str, period: str, usd: float) -> float:
        return await asyncio.to_thread(self.sync.add_spend, customer_id, period, usd)

    async def reserve(
        self, customer_id: str, period: str, *, hold_usd: float, budget_usd: float, downgrade_at: float, stream: bool
    ) -> Reservation:
        return await asyncio.to_thread(
            lambda: self.sync.reserve(
                customer_id, period, hold_usd=hold_usd, budget_usd=budget_usd, downgrade_at=downgrade_at, stream=stream
            )
        )

    async def settle(
        self, hold_id: Optional[str], customer_id: str, period: str, *, cost_usd: float, downgraded: bool
    ) -> float:
        return await asyncio.to_thread(
            lambda: self.sync.settle(hold_id, customer_id, period, cost_usd=cost_usd, downgraded=downgraded)
        )

    async def customer(self, customer_id: str, period: str) -> Dict[str, Any]:
        return await asyncio.to_thread(self.sync.customer, customer_id, period)
