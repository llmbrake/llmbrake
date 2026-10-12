"""HostedStore + reserve/settle path, against an in-memory fake of the LLMBrake Cloud API."""

import asyncio
import math
from typing import Any, Dict, List, Optional

import pytest
from conftest import PRICES, fake_acreate, fake_create, fixed_now

from llmbrake import BudgetExceededError, MemoryStore, create_guard, estimate_call_usd, is_reserving_store
from llmbrake.hosted import AsyncHostedStore, HostedStore, HostedStoreError


class FakeCloud(HostedStore):
    def __init__(self, override: Optional[float] = None, fail: Optional[int] = None) -> None:
        super().__init__("lbsk_test", base_url="http://cloud.test")
        self.override, self.fail = override, fail
        self.reqs: List[Dict[str, Any]] = []
        self.spent = 0.0
        self.holds: Dict[str, float] = {}

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.reqs.append({"path": path.split("?")[0], "body": body})
        if self.fail:
            raise HostedStoreError(f"llmbrake: hosted API {self.fail}", self.fail)
        budget = self.override if self.override is not None else (body or {}).get("budgetUsd")
        pending = sum(self.holds.values())
        p = path.split("?")[0]
        if p == "/v1/reserve":
            ok = budget is None or self.spent + pending < budget
            hid = f"hold_{len(self.reqs)}" if ok else None
            if hid:
                self.holds[hid] = body["holdUsd"]
            return {"allowed": ok, "holdId": hid, "spentUsd": self.spent, "budgetUsd": budget}
        if p == "/v1/settle":
            self.holds.pop(body["holdId"], None)
            self.spent += body["costUsd"]
            return {"spentUsd": self.spent}
        if p == "/v1/spend/add":
            self.spent += body["usd"]
            return {"spentUsd": self.spent}
        return {"spentUsd": self.spent, "pendingUsd": pending, "budgetUsd": budget}


def test_store_shape_and_infinity_mapping():
    s = FakeCloud()
    assert is_reserving_store(s) and not is_reserving_store(MemoryStore())
    assert s.add_spend("a", "2026-10", 1.5) == 1.5
    assert s.get_spend("a", "2026-10") == 1.5
    r = s.reserve("a", "2026-10", hold_usd=0.1, budget_usd=math.inf, downgrade_at=0.8, stream=True)
    assert r.budget_usd == math.inf and r.allowed
    assert s.reqs[-1]["body"]["budgetUsd"] is None and s.reqs[-1]["body"]["ttlSec"] == 900
    with pytest.raises(TypeError):
        HostedStore("")


def test_reserve_then_settle_real_cost():
    s, calls, events = FakeCloud(), [], []
    g = create_guard(10, store=s, prices=PRICES, now=fixed_now, on_usage=events.append)
    g.wrap(fake_create(calls))(
        customer_id="a", model="small", messages=[{"role": "user", "content": "hi"}], max_tokens=500
    )
    assert [r["path"] for r in s.reqs] == ["/v1/reserve", "/v1/settle"]
    assert 0.001 < s.reqs[0]["body"]["holdUsd"] < 0.0011
    assert s.reqs[1]["body"]["costUsd"] == pytest.approx(0.3)
    assert not s.holds and events[0].spent_usd == pytest.approx(0.3)


def test_blocked_by_server_override_never_calls_provider():
    s, calls = FakeCloud(override=0.2), []
    create = create_guard(100, store=s, prices=PRICES).wrap(fake_create(calls))
    create(customer_id="a", model="small", messages=[])
    with pytest.raises(BudgetExceededError) as ei:
        create(customer_id="a", model="small", messages=[])
    assert ei.value.budget_usd == 0.2 and len(calls) == 1


def test_server_budget_drives_downgrade():
    s, calls = FakeCloud(override=0.5), []

    def create(**p):
        calls.append(p["model"])
        return {"model": p["model"], "usage": {"prompt_tokens": 10_000, "completion_tokens": 0}}

    g = create_guard(100, store=s, prices=PRICES, downgrade_at=0.5, downgrade_map={"big": "small"})
    w = g.wrap(create)
    for _ in range(4):
        w(customer_id="a", model="big", messages=[])
    assert calls == ["big", "big", "big", "small"]


def test_provider_error_and_unknown_target_release_the_hold():
    s = FakeCloud()

    def boom(**p):
        raise RuntimeError("provider 500")

    with pytest.raises(RuntimeError):
        create_guard(10, store=s, prices=PRICES).wrap(boom)(customer_id="a", model="small", messages=[])
    assert s.reqs[-1]["path"] == "/v1/settle" and s.reqs[-1]["body"]["costUsd"] == 0 and not s.holds

    s2 = FakeCloud(override=1)
    s2.add_spend("a", "2026-10", 0.9)
    g = create_guard(1, store=s2, prices=PRICES, now=fixed_now, downgrade_map={"big": "mystery"})
    with pytest.raises(Exception, match="mystery"):
        g.wrap(fake_create([]))(customer_id="a", model="big", messages=[])
    assert not s2.holds


def test_stream_settles_on_end():
    s = FakeCloud()

    def create(**p):
        return iter(
            [
                {"model": p["model"]},
                {"model": p["model"], "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0}},
            ]
        )

    st = create_guard(10, store=s, prices=PRICES).wrap(create)(customer_id="s", model="small", messages=[], stream=True)
    assert len(s.reqs) == 1
    list(st)
    assert s.reqs[1]["path"] == "/v1/settle" and s.reqs[1]["body"]["costUsd"] == pytest.approx(1)


def test_fail_open_and_closed():
    calls = []
    create_guard(1, store=FakeCloud(fail=503), prices=PRICES).wrap(fake_create(calls))(
        customer_id="a", model="small", messages=[]
    )
    assert len(calls) == 1
    with pytest.raises(HostedStoreError):
        create_guard(1, store=FakeCloud(fail=503), prices=PRICES, fail_open=False).wrap(fake_create(calls))(
            customer_id="a", model="small", messages=[]
        )
    assert len(calls) == 1


def test_async_hosted_store_and_async_guard():
    fake = FakeCloud(override=0.5)
    s = AsyncHostedStore("k")
    s.sync = fake

    async def main():
        calls = []
        create = create_guard(100, store=s, prices=PRICES).wrap_async(fake_acreate(calls))
        await create(customer_id="a", model="small", messages=[])  # 0.3
        await create(customer_id="a", model="small", messages=[])  # 0.3 < 0.5 -> allowed, 0.6
        with pytest.raises(BudgetExceededError):
            await create(customer_id="a", model="small", messages=[])
        return calls

    assert len(asyncio.run(main())) == 2
    assert not fake.holds


def test_unreachable_server_raises_hosted_error():
    s = HostedStore("k", base_url="http://127.0.0.1:9", timeout=0.5)
    with pytest.raises(HostedStoreError, match="unreachable"):
        s.get_spend("a", "2026-10")


def test_estimate_and_custom_estimate():
    assert estimate_call_usd("small", {"messages": [], "max_tokens": 1_000_000}, PRICES) == pytest.approx(2.000001)
    assert estimate_call_usd("nope", {"messages": []}, PRICES) == 0
    s = FakeCloud()
    create_guard(10, store=s, prices=PRICES, estimate_usd=0.42).wrap(fake_create([]))(
        customer_id="a", model="small", messages=[]
    )
    assert s.reqs[0]["body"]["holdUsd"] == 0.42
