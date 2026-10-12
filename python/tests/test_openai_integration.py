"""End-to-end with the real `openai` client and a mocked HTTP transport (no network).

Skipped if `openai`/`httpx` are not installed.
"""

import asyncio
import json

import pytest

openai = pytest.importorskip("openai")
httpx = pytest.importorskip("httpx")

from conftest import PRICES, fixed_now  # noqa: E402

from llmbrake import BudgetExceededError, create_guard  # noqa: E402

COMPLETION = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "created": 1,
    "model": "small-2026-01-01",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
    "usage": {
        "prompt_tokens": 1_000_000,
        "completion_tokens": 0,
        "total_tokens": 1_000_000,
        "prompt_tokens_details": {"cached_tokens": 0},
    },
}


def _sse(model):
    chunk = {"id": "c", "object": "chat.completion.chunk", "created": 1, "model": model}
    parts = [
        {**chunk, "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}]},
        {
            **chunk,
            "choices": [],
            "usage": {"prompt_tokens": 1_000_000, "completion_tokens": 0, "total_tokens": 1_000_000},
        },
    ]
    return "".join(f"data: {json.dumps(p)}\n\n" for p in parts) + "data: [DONE]\n\n"


def handler(seen):
    def handle(request):
        body = json.loads(request.content)
        seen.append(body)
        if body.get("stream"):
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=_sse(body["model"]))
        return httpx.Response(200, json={**COMPLETION, "model": body["model"] + "-2026-01-01"})

    return handle


def test_sync_openai_client():
    seen, events = [], []
    client = openai.OpenAI(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler(seen))))
    g = create_guard(
        2, prices=PRICES, now=fixed_now, on_usage=events.append, downgrade_at=0.4, downgrade_map={"big": "small"}
    )
    create = g.wrap(client.chat.completions.create)
    res = create(customer_id="u1", model="small", messages=[{"role": "user", "content": "hi"}])
    assert res.choices[0].message.content == "hi"
    assert "customer_id" not in seen[0]
    assert events[0].cost_usd == 1 and events[0].prompt_tokens == 1_000_000

    with create(customer_id="u1", model="big", messages=[], stream=True) as stream:
        chunks = list(stream)
    assert seen[1]["model"] == "small" and seen[1]["stream_options"] == {"include_usage": True}
    assert len(chunks) == 2 and events[1].downgraded and events[1].cost_usd == 1
    with pytest.raises(BudgetExceededError):
        create(customer_id="u1", model="small", messages=[])
    assert len(seen) == 2


def test_async_openai_client():
    async def main():
        seen, events = [], []
        client = openai.AsyncOpenAI(
            api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler(seen)))
        )
        g = create_guard(10, prices=PRICES, now=fixed_now, on_usage=events.append)
        create = g.wrap(client.chat.completions.create)  # detected as async through openai's decorator
        res = await create(customer_id="u2", model="small", messages=[])
        assert res.model == "small-2026-01-01"
        stream = await create(customer_id="u2", model="small", messages=[], stream=True)
        async with stream:
            n = 0
            async for _ in stream:
                n += 1
        assert n == 2
        assert [e.cost_usd for e in events] == [1, 1]
        assert (await g.astatus("u2")).spent_usd == 2

    asyncio.run(main())
