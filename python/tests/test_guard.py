"""Mirror of the TypeScript test suite (test/guard.test.ts), sync API."""

import math
from datetime import datetime, timezone

import pytest
from conftest import PRICES, fake_create, fixed_now

from llmbrake import (
    DEFAULT_PRICES,
    BudgetExceededError,
    MemoryStore,
    UnknownModelPriceError,
    compute_cost_usd,
    create_guard,
    is_budget_exceeded_error,
)


def test_compute_cost_handles_cached_tokens_and_dated_snapshots():
    # 100k uncached*10 + 100k cached*1 + 50k out*30 = (1,000,000 + 100,000 + 1,500,000)/1e6 = 2.6
    c = compute_cost_usd(
        "big-2026-01-01",
        {"prompt_tokens": 200_000, "completion_tokens": 50_000, "prompt_tokens_details": {"cached_tokens": 100_000}},
        PRICES,
    )
    assert c == pytest.approx(2.6)
    assert compute_cost_usd("nope", {}, PRICES) is None


def test_default_price_table_entries_all_cite_a_source():
    for m, p in DEFAULT_PRICES.items():
        assert p.source, f"{m} missing source"
        assert "verify" in p.source


def test_records_spend_per_customer_and_emits_usage_events():
    events, calls = [], []
    g = create_guard(100, prices=PRICES, now=fixed_now, on_usage=events.append)
    create = g.wrap(fake_create(calls))
    create(customer_id="a", feature="chat", model="small", messages=[])
    # 100k*1 + 100k*2 = 0.3
    assert len(events) == 1
    assert events[0].cost_usd == pytest.approx(0.3)
    assert events[0].feature == "chat"
    assert events[0].period == "2026-10"
    assert events[0].timestamp == "2026-10-09T12:00:00.000Z"
    assert "customer_id" not in calls[0] and "feature" not in calls[0], "ctx must not be forwarded"
    assert g.status("a").spent_usd == pytest.approx(0.3)
    assert g.status("b").spent_usd == 0


def test_downgrades_to_the_cheaper_model_past_the_threshold():
    calls, events = [], []
    # big call costs 100k*10 + 100k*30 = $4.00 ; budget $5, downgrade at 50%
    g = create_guard(
        5, prices=PRICES, now=fixed_now, downgrade_at=0.5, downgrade_map={"big": "small"}, on_usage=events.append
    )
    create = g.wrap(fake_create(calls))
    create(customer_id="c", model="big", messages=[])
    create(customer_id="c", model="big", messages=[])
    assert calls[0]["model"] == "big"
    assert calls[1]["model"] == "small"
    assert events[1].downgraded is True
    assert events[1].requested_model == "big"


def test_blocks_at_the_cap_with_a_typed_error():
    calls = []
    g = create_guard(4, prices=PRICES, now=fixed_now, downgrade_at=1)
    create = g.wrap(fake_create(calls))
    create(customer_id="d", model="big", messages=[])  # spends exactly $4
    with pytest.raises(BudgetExceededError) as ei:
        create(customer_id="d", model="big", messages=[])
    e = ei.value
    assert is_budget_exceeded_error(e)
    assert e.customer_id == "d"
    assert e.budget_usd == 4
    assert e.code == "LLMBRAKE_BUDGET_EXCEEDED"
    assert str(e) == "Customer d has used $4.0000 of a $4.00 AI budget for 2026-10."
    assert len(calls) == 1, "blocked call must not reach the provider"


def test_per_plan_budgets_via_function():
    g = create_guard(lambda ctx: 50 if ctx.plan == "pro" else 0.1, prices=PRICES, now=fixed_now)
    create = g.wrap(fake_create([]))
    create(customer_id="free1", plan="free", model="small", messages=[])  # 0.3 > 0.1
    with pytest.raises(BudgetExceededError):
        create(customer_id="free1", plan="free", model="small", messages=[])
    create(customer_id="pro1", plan="pro", model="small", messages=[])
    create(customer_id="pro1", plan="pro", model="small", messages=[])


def test_budgets_reset_each_calendar_month_utc():
    now = [datetime(2026, 10, 31, 23, 0, 0, tzinfo=timezone.utc)]
    g = create_guard(0.3, prices=PRICES, now=lambda: now[0], downgrade_at=1)
    create = g.wrap(fake_create([]))
    create(customer_id="e", model="small", messages=[])
    with pytest.raises(BudgetExceededError):
        create(customer_id="e", model="small", messages=[])
    now[0] = datetime(2026, 11, 1, 0, 0, 1, tzinfo=timezone.utc)
    create(customer_id="e", model="small", messages=[])


def test_unknown_model_throws_by_default_allowed_with_cost_none_when_configured():
    calls = []
    strict = create_guard(10, prices=PRICES, now=fixed_now).wrap(fake_create(calls))
    with pytest.raises(UnknownModelPriceError):
        strict(customer_id="f", model="mystery", messages=[])
    assert len(calls) == 0
    events = []
    lax = create_guard(10, prices=PRICES, now=fixed_now, unknown_model="allow", on_usage=events.append).wrap(
        fake_create(calls)
    )
    lax(customer_id="f", model="mystery", messages=[])
    assert events[0].cost_usd is None


def test_streaming_requests_include_usage_and_records_cost_after_the_stream_ends():
    events, seen = [], {}

    def streaming_create(**params):
        seen.update(params)

        def gen():
            yield {"model": "small", "choices": [{"delta": {"content": "hi"}}]}
            yield {"model": "small", "choices": [], "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}

        return gen()

    g = create_guard(10, prices=PRICES, now=fixed_now, on_usage=events.append)
    stream = g.wrap(streaming_create)(customer_id="s", model="small", messages=[], stream=True)
    assert seen["stream_options"]["include_usage"] is True
    chunks = list(stream)
    assert len(chunks) == 2
    assert len(events) == 1
    assert events[0].cost_usd == 1
    assert g.status("s").spent_usd == 1


def test_fails_open_when_the_store_is_down_default_and_closed_when_configured():
    class Broken:
        def get_spend(self, c, p):
            raise RuntimeError("redis down")

        def add_spend(self, c, p, usd):
            raise RuntimeError("redis down")

    opened = create_guard(1, prices=PRICES, store=Broken(), now=fixed_now).wrap(fake_create([]))
    opened(customer_id="g", model="small", messages=[])
    closed = create_guard(1, prices=PRICES, store=Broken(), now=fixed_now, fail_open=False).wrap(fake_create([]))
    with pytest.raises(RuntimeError, match="redis down"):
        closed(customer_id="g", model="small", messages=[])


def test_on_usage_hook_errors_never_break_the_caller_customer_id_is_required():
    def boom(e):
        raise RuntimeError("boom")

    create = create_guard(10, prices=PRICES, now=fixed_now, on_usage=boom).wrap(fake_create([]))
    res = create(customer_id="h", model="small", messages=[])
    assert res["model"] == "small"
    with pytest.raises(TypeError):
        create(model="small", messages=[])
    with pytest.raises(TypeError):
        create(customer_id="", model="small", messages=[])


def test_custom_store_is_used():
    store = MemoryStore()
    store.add_spend("i", "2026-10", 9.99)
    create = create_guard(10, prices=PRICES, store=store, now=fixed_now, downgrade_at=1).wrap(fake_create([]))
    create(customer_id="i", model="small", messages=[])
    with pytest.raises(BudgetExceededError):
        create(customer_id="i", model="small", messages=[])


def test_invalid_budgets_are_rejected_instead_of_silently_disabling_the_cap():
    calls = []
    nan = create_guard(lambda ctx: float("nan"), prices=PRICES, now=fixed_now).wrap(fake_create(calls))
    with pytest.raises(TypeError):
        nan(customer_id="j", model="small", messages=[])
    neg = create_guard(-1, prices=PRICES, now=fixed_now).wrap(fake_create(calls))
    with pytest.raises(TypeError):
        neg(customer_id="j", model="small", messages=[])
    for bad in ("5", None, True):
        with pytest.raises(TypeError):
            create_guard(bad, prices=PRICES, now=fixed_now).wrap(fake_create(calls))(  # type: ignore[arg-type]
                customer_id="j", model="small", messages=[]
            )
    assert len(calls) == 0, "rejected calls must not reach the provider"
    # math.inf means unlimited
    unlimited = create_guard(math.inf, prices=PRICES, now=fixed_now).wrap(fake_create(calls))
    unlimited(customer_id="j", model="small", messages=[])
    assert len(calls) == 1


def test_downgrade_at_outside_0_1_raises_at_construction():
    with pytest.raises(ValueError):
        create_guard(1, downgrade_at=1.5)
    with pytest.raises(ValueError):
        create_guard(1, downgrade_at=float("nan"))
    with pytest.raises(ValueError):
        create_guard(1, unknown_model="ignore")  # type: ignore[arg-type]
    create_guard(1, downgrade_at=0)


def test_a_store_returning_a_non_numeric_spend_is_treated_as_a_store_failure():
    class Garbage:
        def get_spend(self, c, p):
            return float("nan")

        def add_spend(self, c, p, usd):
            return usd

    closed = create_guard(1, prices=PRICES, store=Garbage(), now=fixed_now, fail_open=False).wrap(fake_create([]))
    with pytest.raises(TypeError, match="non-numeric"):
        closed(customer_id="k", model="small", messages=[])
    opened = create_guard(1, prices=PRICES, store=Garbage(), now=fixed_now).wrap(fake_create([]))
    assert opened(customer_id="k", model="small", messages=[])["model"] == "small"


def test_streaming_early_break_still_records_usage_received_so_far():
    events = []

    def streaming_create(**params):
        def gen():
            yield {"model": "small", "choices": [], "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}}
            yield {"model": "small", "choices": [{"delta": {"content": "never read"}}]}

        return gen()

    g = create_guard(10, prices=PRICES, now=fixed_now, on_usage=events.append)
    stream = g.wrap(streaming_create)(customer_id="l", model="small", messages=[], stream=True)
    for _ in stream:
        break
    assert len(events) == 1
    assert events[0].cost_usd == 1
