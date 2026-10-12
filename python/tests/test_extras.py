"""Python-specific behavior: check/status, stream proxy, async misuse guards, pickling."""

import pickle
import warnings

import pytest
from conftest import PRICES, fake_acreate, fake_create, fixed_now

from llmbrake import (
    BudgetExceededError,
    GuardedStream,
    LLMBrakeError,
    UnknownModelPriceError,
    create_guard,
    lookup_price,
    period_of,
)


def test_check_and_status():
    g = create_guard(5, prices=PRICES, now=fixed_now, downgrade_at=0.5, downgrade_map={"big": "small"})
    d = g.check("c", "big")
    assert (d.model, d.downgraded, d.period, d.budget_usd, d.spent_usd) == ("big", False, "2026-10", 5, 0)
    g.wrap(fake_create([]))(customer_id="c", model="big", messages=[])
    d = g.check("c", "big")
    assert (d.model, d.downgraded) == ("small", True)
    s = g.status("c")
    assert s.remaining_usd == pytest.approx(1.0)


def test_no_downgrade_when_unlimited_and_downgrade_target_must_be_priced():
    import math

    g = create_guard(math.inf, prices=PRICES, now=fixed_now, downgrade_at=0, downgrade_map={"big": "small"})
    assert g.check("x", "big").model == "big"
    g2 = create_guard(5, prices=PRICES, now=fixed_now, downgrade_at=0, downgrade_map={"big": "unpriced"})
    with pytest.raises(UnknownModelPriceError) as ei:
        g2.check("x", "big")
    assert ei.value.model == "unpriced"


def test_prices_by_the_model_the_provider_reports():
    events = []

    def create(**params):
        return {"model": "big-2026-01-01", "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}

    g = create_guard(100, prices=PRICES, now=fixed_now, on_usage=events.append)
    g.wrap(create)(customer_id="m", model="small", messages=[])
    assert events[0].cost_usd == 10  # priced as "big", not "small"
    assert lookup_price("big-2026-01-01", PRICES) is PRICES["big"]


def test_stream_options_are_merged_and_other_params_forwarded():
    seen = {}

    def create(*args, **params):
        seen.update(params, args=args)
        return iter([])

    g = create_guard(10, prices=PRICES, now=fixed_now)
    g.wrap(create)(
        "positional", customer_id="n", model="small", messages=[], stream=True, stream_options={"x": 1}, temperature=0
    )
    assert seen["stream_options"] == {"x": 1, "include_usage": True}
    assert seen["temperature"] == 0 and seen["args"] == ("positional",)


def test_stream_proxy_delegates_attributes_and_supports_with_and_next():
    events = []

    class FakeStream:
        response = "raw-response"
        closed = False

        def __init__(self):
            self._it = iter(
                [{"model": "small", "usage": None}, {"model": "small", "usage": {"prompt_tokens": 1_000_000}}]
            )

        def __iter__(self):
            return self._it

        def close(self):
            self.closed = True

    fs = FakeStream()
    g = create_guard(10, prices=PRICES, now=fixed_now, on_usage=events.append)
    stream = g.wrap(lambda **p: fs)(customer_id="o", model="small", messages=[], stream=True)
    assert isinstance(stream, GuardedStream)
    assert stream.response == "raw-response"
    with stream as s:
        next(s)
        next(s)
        assert events == []
    assert fs.closed is True
    assert len(events) == 1 and events[0].cost_usd == 1
    stream.close()  # idempotent
    assert len(events) == 1


def test_sync_wrapper_refuses_an_async_provider_instead_of_metering_zero():
    calls = []
    g = create_guard(10, prices=PRICES, now=fixed_now)

    # a plain lambda returning a coroutine hides that it is async
    acreate = fake_acreate(calls)
    guarded = g.wrap(lambda **p: acreate(**p))
    with pytest.raises(TypeError, match="wrap_async"):
        guarded(customer_id="p", model="small", messages=[])
    assert calls == []


def test_sync_call_with_async_store_or_budget_is_a_config_error_even_when_failing_open():
    class AsyncStore:
        async def get_spend(self, c, p):
            return 0.0

        async def add_spend(self, c, p, usd):
            return usd

    g = create_guard(10, prices=PRICES, now=fixed_now, store=AsyncStore())
    with pytest.raises(TypeError, match="awaitable"):
        g.wrap(fake_create([]))(customer_id="q", model="small", messages=[])

    async def budget(ctx):
        return 10

    g2 = create_guard(budget, prices=PRICES, now=fixed_now)
    with pytest.raises(TypeError, match="awaitable"):
        g2.status("q")


def test_async_on_usage_in_sync_call_warns():
    async def hook(e):
        pass

    g = create_guard(10, prices=PRICES, now=fixed_now, on_usage=hook)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        g.wrap(fake_create([]))(customer_id="r", model="small", messages=[])
    assert any(issubclass(x.category, RuntimeWarning) for x in w)


def test_errors_are_picklable_and_share_a_base_class():
    e = pickle.loads(pickle.dumps(BudgetExceededError("a", 1.5, 2, "2026-10")))
    assert (e.customer_id, e.spent_usd, e.budget_usd, e.period) == ("a", 1.5, 2, "2026-10")
    assert isinstance(e, LLMBrakeError)
    u = pickle.loads(pickle.dumps(UnknownModelPriceError("m")))
    assert u.model == "m" and u.code == "LLMBRAKE_UNKNOWN_MODEL"


def test_period_of_naive_and_aware():
    from datetime import datetime, timedelta, timezone

    assert period_of(datetime(2026, 1, 31, 23, 30)) == "2026-01"
    # 2026-01-31 20:00 at UTC-6 is 2026-02-01 02:00 UTC
    assert period_of(datetime(2026, 1, 31, 20, 0, tzinfo=timezone(timedelta(hours=-6)))) == "2026-02"
