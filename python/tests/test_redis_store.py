import asyncio

import pytest

fakeredis = pytest.importorskip("fakeredis")

from conftest import PRICES, fake_acreate, fake_create, fixed_now  # noqa: E402

from llmbrake import BudgetExceededError, create_guard  # noqa: E402
from llmbrake.redis_store import AsyncRedisStore, RedisStore  # noqa: E402


def test_sync_redis_store():
    r = fakeredis.FakeRedis()
    store = RedisStore(r)
    g = create_guard(0.5, prices=PRICES, store=store, now=fixed_now, downgrade_at=1)
    create = g.wrap(fake_create([]))
    create(customer_id="a", model="small", messages=[])
    create(customer_id="a", model="small", messages=[])
    with pytest.raises(BudgetExceededError):
        create(customer_id="a", model="small", messages=[])
    assert float(r.get("llmbrake:2026-10:a")) == pytest.approx(0.6)
    assert 0 < r.ttl("llmbrake:2026-10:a") <= 60 * 60 * 24 * 40


def test_async_redis_store():
    async def main():
        r = fakeredis.FakeAsyncRedis()
        g = create_guard(0.5, prices=PRICES, store=AsyncRedisStore(r, prefix="t"), now=fixed_now, downgrade_at=1)
        create = g.wrap_async(fake_acreate([]))
        await create(customer_id="b", model="small", messages=[])
        assert (await g.astatus("b")).spent_usd == pytest.approx(0.3)
        assert await r.ttl("t:2026-10:b") > 0

    asyncio.run(main())


def test_garbage_in_redis_is_a_store_failure():
    r = fakeredis.FakeRedis()
    r.set("llmbrake:2026-10:c", "oops")
    g = create_guard(1, prices=PRICES, store=RedisStore(r), now=fixed_now, fail_open=False)
    with pytest.raises(TypeError, match="non-numeric"):
        g.status("c")
