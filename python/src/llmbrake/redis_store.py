"""Redis-backed stores (optional; ``pip install "llmbrake[redis]"``).

This module never imports ``redis`` itself: pass in a client you created. Any client with
redis-py's ``get`` and ``pipeline`` API works (``redis.Redis``, ``redis.asyncio.Redis``,
``fakeredis`` ...).

``INCRBYFLOAT`` is atomic, so totals stay correct across processes and servers.

Example::

    import redis
    from llmbrake import create_guard
    from llmbrake.redis_store import RedisStore

    guard = create_guard(5, store=RedisStore(redis.Redis.from_url(os.environ["REDIS_URL"])))

    # asyncio
    import redis.asyncio as aioredis
    from llmbrake.redis_store import AsyncRedisStore

    guard = create_guard(5, store=AsyncRedisStore(aioredis.Redis.from_url(os.environ["REDIS_URL"])))
"""

from __future__ import annotations

from typing import Any

__all__ = ["RedisStore", "AsyncRedisStore", "DEFAULT_TTL_SECONDS"]

#: Keep each month's counter ~40 days, then let it expire.
DEFAULT_TTL_SECONDS = 60 * 60 * 24 * 40


def _to_float(raw: Any) -> Any:
    if raw is None:
        return 0.0
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return raw  # let the guard treat it as a store failure


class _Base:
    def __init__(self, client: Any, *, prefix: str = "llmbrake", ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self.client = client
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds

    def key(self, customer_id: str, period: str) -> str:
        return f"{self.prefix}:{period}:{customer_id}"


class RedisStore(_Base):
    """Sync store for ``redis.Redis``. Usable from both sync and async guarded calls
    (from async calls it blocks the event loop briefly; prefer :class:`AsyncRedisStore`)."""

    def get_spend(self, customer_id: str, period: str) -> float:
        return _to_float(self.client.get(self.key(customer_id, period)))  # type: ignore[no-any-return]

    def add_spend(self, customer_id: str, period: str, usd: float) -> float:
        k = self.key(customer_id, period)
        pipe = self.client.pipeline(transaction=True)
        pipe.incrbyfloat(k, usd)
        pipe.expire(k, self.ttl_seconds)
        total, _ = pipe.execute()
        return _to_float(total)  # type: ignore[no-any-return]


class AsyncRedisStore(_Base):
    """Async store for ``redis.asyncio.Redis``. Only usable from async guarded calls."""

    async def get_spend(self, customer_id: str, period: str) -> float:
        return _to_float(await self.client.get(self.key(customer_id, period)))  # type: ignore[no-any-return]

    async def add_spend(self, customer_id: str, period: str, usd: float) -> float:
        k = self.key(customer_id, period)
        pipe = self.client.pipeline(transaction=True)
        pipe.incrbyfloat(k, usd)
        pipe.expire(k, self.ttl_seconds)
        total, _ = await pipe.execute()
        return _to_float(total)  # type: ignore[no-any-return]
