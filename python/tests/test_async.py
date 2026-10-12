"""Async API: mirrors the key TS tests for AsyncOpenAI-style providers."""

import asyncio

import pytest
from conftest import PRICES, fake_acreate, fixed_now

from llmbrake import BudgetExceededError, GuardedAsyncStream, MemoryStore, create_guard


def run(coro):
    return asyncio.run(coro)


def test_async_records_downgrades_and_blocks():
    async def main():
        calls, events = [], []

        async def on_usage(e):
            events.append(e)

        g = create_guard(
            5, prices=PRICES, now=fixed_now, downgrade_at=0.5, downgrade_map={"big": "small"}, on_usage=on_usage
        )
        create = g.wrap(fake_acreate(calls))  # auto-detected as async
        await create(customer_id="c", model="big", messages=[])
        await create(customer_id="c", model="big", messages=[])
        assert [c["model"] for c in calls] == ["big", "small"]
        assert events[1].downgraded is True
        assert (await g.astatus("c")).spent_usd == pytest.approx(4.3)
        g2 = create_guard(4, prices=PRICES, now=fixed_now, downgrade_at=1)
        c2 = g2.wrap_async(fake_acreate(calls))
        await c2(customer_id="d", model="big", messages=[])
        with pytest.raises(BudgetExceededError):
            await c2(customer_id="d", model="big", messages=[])
        assert len(calls) == 3

    run(main())


def test_async_store_and_async_budget_function():
    class AsyncMemory:
        def __init__(self):
            self.inner = MemoryStore()

        async def get_spend(self, c, p):
            return self.inner.get_spend(c, p)

        async def add_spend(self, c, p, usd):
            return self.inner.add_spend(c, p, usd)

    async def budget(ctx):
        return 0.3

    async def main():
        g = create_guard(budget, prices=PRICES, now=fixed_now, store=AsyncMemory(), downgrade_at=1)
        create = g.wrap_async(fake_acreate([]))
        await create(customer_id="e", model="small", messages=[])
        with pytest.raises(BudgetExceededError):
            await create(customer_id="e", model="small", messages=[])
        d = await g.acheck("other", "small")
        assert d.model == "small"

    run(main())


def test_async_fail_open_and_closed():
    class Broken:
        async def get_spend(self, c, p):
            raise RuntimeError("redis down")

        async def add_spend(self, c, p, usd):
            raise RuntimeError("redis down")

    async def main():
        await create_guard(1, prices=PRICES, store=Broken(), now=fixed_now).wrap_async(fake_acreate([]))(
            customer_id="g", model="small", messages=[]
        )
        closed = create_guard(1, prices=PRICES, store=Broken(), now=fixed_now, fail_open=False)
        with pytest.raises(RuntimeError, match="redis down"):
            await closed.wrap_async(fake_acreate([]))(customer_id="g", model="small", messages=[])

    run(main())


def _astream_factory(seen):
    async def create(**params):
        seen.update(params)

        async def gen():
            yield {"model": "small", "choices": [], "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}
            yield {"model": "small", "choices": [{"delta": {"content": "x"}}]}

        return gen()

    return create


def test_async_streaming_full_and_early_exit_with_async_with():
    async def main():
        events, seen = [], {}
        g = create_guard(10, prices=PRICES, now=fixed_now, on_usage=events.append)
        create = g.wrap_async(_astream_factory(seen))
        stream = await create(customer_id="s", model="small", messages=[], stream=True)
        assert isinstance(stream, GuardedAsyncStream)
        assert seen["stream_options"]["include_usage"] is True
        chunks = [c async for c in stream]
        assert len(chunks) == 2 and len(events) == 1 and events[0].cost_usd == 1

        stream = await create(customer_id="t", model="small", messages=[], stream=True)
        async with stream:
            async for _ in stream:
                break
        assert len(events) == 2 and events[1].cost_usd == 1
        assert (await g.astatus("t")).spent_usd == 1

    run(main())
