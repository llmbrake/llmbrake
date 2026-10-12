# llmbrake (Python)

**Per-customer cost caps for AI apps: put a monthly dollar budget on every customer, switch them to a cheaper model near the cap, and block them at the cap.**

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/llmbrake/llmbrake/blob/main/python/LICENSE)

`llmbrake` is a small Python library that wraps your OpenAI-compatible `chat.completions.create` call (sync or async). It prices every call in USD from the token usage the provider returns and keeps a running monthly total per customer. Near a customer's budget it swaps in a cheaper model; at the budget it raises a typed `BudgetExceededError` before the request ever reaches the provider.

- **Not a proxy.** It runs inside your app and calls your provider directly with your own keys.
- **Metadata only.** It never reads or stores prompts or completions, only customer ID, plan, feature tag, model, token counts, cost and timestamps.
- **Zero runtime dependencies.** Python 3.9+, fully typed (`py.typed`), MIT licensed. Redis support is an optional extra.

This is the Python port of the [TypeScript `llmbrake`](https://github.com/llmbrake/llmbrake#readme) package and behaves the same way. Status: `0.1.0`, early. Website: [llmbrake.com](https://llmbrake.com)

## Install

`pip install llmbrake` is coming soon to PyPI. For now, install from GitHub:

```bash
pip install "git+https://github.com/llmbrake/llmbrake#subdirectory=python"            # core, no dependencies
pip install "llmbrake[redis] @ git+https://github.com/llmbrake/llmbrake#subdirectory=python"   # + redis client
```

## 30-second quickstart

```python
from openai import OpenAI
from llmbrake import create_guard, BudgetExceededError

client = OpenAI()

guard = create_guard(
    budget_usd=lambda ctx: 20 if ctx.plan == "pro" else 2,  # USD per customer per calendar month (UTC)
    downgrade_at=0.8,                                      # at 80% of the budget...
    downgrade_map={"gpt-5.5": "gpt-5.4-mini"},             # ...send gpt-5.5 requests to gpt-5.4-mini
)

create = guard.wrap(client.chat.completions.create)

try:
    res = create(
        customer_id=user.id, plan=user.plan, feature="chat",   # llmbrake's args, not sent to OpenAI
        model="gpt-5.5",
        messages=[{"role": "user", "content": "Hello"}],
    )
except BudgetExceededError as e:
    # e.customer_id, e.spent_usd, e.budget_usd, e.period
    return show_upgrade_prompt()

guard.status(user.id, plan=user.plan)
# BudgetStatus(period='2026-10', spent_usd=..., budget_usd=20.0, remaining_usd=...)
```

### Async

```python
from openai import AsyncOpenAI

client = AsyncOpenAI()
create = guard.wrap(client.chat.completions.create)   # async clients are detected; or use guard.wrap_async(...)
res = await create(customer_id="cus_123", model="gpt-5.5", messages=[...])
await guard.astatus("cus_123")
```

The async path also accepts `async def` budget functions, `on_usage` hooks and stores.

## How it works

Every wrapped call goes through three steps:

1. **Check (before the call).** `llmbrake` resolves the customer's budget (a number, or your `ctx -> number` function, e.g. by plan) and reads their spend for the current period, a calendar month in UTC such as `2026-10`.
2. **Decide.**
   - **Block:** if `spent >= budget`, it raises `BudgetExceededError` (`code == "LLMBRAKE_BUDGET_EXCEEDED"`) and **the provider is never called**. Catch it to show an upgrade prompt.
   - **Downgrade:** if `spent >= budget × downgrade_at` and the requested model has an entry in `downgrade_map`, the request is sent with the cheaper model instead. The usage event reports `downgraded=True` and the original `requested_model`.
   - Otherwise the request goes through unchanged.
3. **Record (after the call).** The cost is computed from the response's `usage` (uncached input, cached input and output tokens) and your price table, then added to the customer's total. The `on_usage` hook receives a `UsageEvent`:

```python
UsageEvent(
    customer_id="cus_123", plan="pro", feature="chat", period="2026-10",
    requested_model="gpt-5.5", model="gpt-5.4-mini", downgraded=True,
    prompt_tokens=1102, completion_tokens=182, cached_tokens=640,
    cost_usd=0.0012, spent_usd=16.41, budget_usd=20.0, timestamp="2026-10-11T20:00:00.000Z",
)
# event.to_dict() for logging / analytics
```

`guard.check(customer_id, model, plan=...)` (or `await guard.acheck(...)`) runs steps 1–2 without making a call and returns a `Decision(period, budget_usd, spent_usd, model, downgraded)`, handy for disabling a button before the user clicks it.

### Streaming

With `stream=True`, `llmbrake` sets `stream_options={"include_usage": True}` (merged with yours) and returns a proxy around the provider's stream that yields the same chunks. The cost is recorded once when the stream ends: when it's exhausted, on an error, on `close()` / leaving a `with` block, or when you `break` out of a `for` loop early (whatever usage was received by then is recorded). Other attributes (e.g. `stream.response`) pass through.

```python
with create(customer_id="cus_123", model="gpt-5.5", messages=msgs, stream=True) as stream:
    for chunk in stream:
        ...
```

For async streams, use `async with` (or `await stream.close()`) if you may stop reading early; after a bare `break` out of `async for`, recording happens when the event loop finalizes the iterator.

### Other OpenAI-compatible endpoints

Any `create(**params)` callable that accepts `model=...` and returns something with `model` and `usage.prompt_tokens` / `usage.completion_tokens` (attributes or dict keys) works, for example OpenRouter, Anthropic's OpenAI-compatible endpoint and local gateways. Add their model IDs to the price table.

## Storage (Redis)

The default `MemoryStore` keeps totals in process memory (thread-safe): fine for one server, tests and demos, but lost on restart and not shared between processes. For multiple workers or servers, use Redis or implement the two-method `UsageStore` protocol yourself.

```python
import os, redis
from llmbrake import create_guard
from llmbrake.redis_store import RedisStore

store = RedisStore(redis.Redis.from_url(os.environ["REDIS_URL"]))   # keys: llmbrake:<period>:<customer>, ~40-day TTL
guard = create_guard(5, store=store)
```

With asyncio, use `AsyncRedisStore(redis.asyncio.Redis.from_url(...))` together with async guarded calls. Writing your own:

```python
class MyStore:
    def get_spend(self, customer_id: str, period: str) -> float: ...
    def add_spend(self, customer_id: str, period: str, usd: float) -> float: ...  # atomic, return new total
```

(or the same with `async def`, for async calls only). Redis `INCRBYFLOAT` is atomic, so totals stay correct across instances. If the store raises (or returns a non-numeric value), calls **fail open** by default so your product keeps working; set `fail_open=False` to fail closed instead.

## Shared budgets across servers (`HostedStore`)

`llmbrake.hosted.HostedStore` keeps counters in LLMBrake Cloud: every process using your project's API key shares one counter per customer, and budgets edited in the dashboard apply on the next call. No extra dependencies.

```python
import os
from llmbrake import create_guard
from llmbrake.hosted import HostedStore  # AsyncHostedStore for asyncio apps

guard = create_guard(5, store=HostedStore(os.environ["LLMBRAKE_API_KEY"]))
```

Before each call the guard reserves an estimated cost (`estimate_usd`, default from prompt size plus `max_tokens`) and the server admits it only if spend plus other in-flight reservations is under budget, atomically; after the call it settles the real cost. Concurrent overshoot is bounded to about one call. Any store with `reserve`/`settle` methods (`ReservingStore`) gets this behavior.

## Options

`create_guard(budget_usd, **options)` returns a `Guard` with `wrap`, `wrap_async`, `check`, `acheck`, `status`, `astatus`.

| Option | Default | Description |
|---|---|---|
| `budget_usd` | required | Monthly USD budget: a number, or `(ctx: CallContext) -> float` (may be `async` for async calls). Must be `>= 0`; use `math.inf` for unlimited. |
| `store` | `MemoryStore()` | Where spend is kept. Any `UsageStore` (`get_spend`, `add_spend`). |
| `prices` | `DEFAULT_PRICES` | Model → `ModelPrice(input_per_mtok, output_per_mtok, cached_input_per_mtok=None, source=None)`, USD per 1M tokens. |
| `downgrade_at` | `0.8` | Fraction of the budget (0–1) at which `downgrade_map` applies. `1` disables downgrading. |
| `downgrade_map` | `{}` | `{"expensive-model": "cheaper-model"}`. |
| `on_usage` | `None` | `(event: UsageEvent) -> None` (or `async`), called after each metered call. Exceptions in it are swallowed. |
| `unknown_model` | `"throw"` | Model missing from `prices`: `"throw"` an `UnknownModelPriceError` before the call, or `"allow"` it and record `cost_usd=None`. |
| `fail_open` | `True` | If the store errors: `True` lets calls through, `False` re-raises. |
| `now` | `datetime.now(timezone.utc)` | Clock override, mainly for tests. Naive datetimes are treated as UTC. |

The wrapped function takes the provider's normal arguments plus keyword-only `customer_id` (required), `plan` and `feature`, which are removed before the provider is called.

Invalid configuration fails loudly instead of silently disabling the cap: a NaN, negative or non-numeric budget raises `TypeError` before the provider is called, `downgrade_at` outside 0–1 raises `ValueError` at construction, and using an async store/budget function/provider from a sync call raises `TypeError` (even with `fail_open=True`).

## Model prices: verify before relying on them

`DEFAULT_PRICES` (read-only) covers a few common OpenAI and Anthropic models in USD per 1M tokens (standard tier), read on 2026-10-09 from:

- OpenAI: https://developers.openai.com/api/docs/pricing
- Anthropic: https://docs.anthropic.com/en/docs/about-claude/pricing

**Provider prices change often, and these values are not guaranteed current.** Check them against the pages above before using them for billing decisions. Each entry carries a `source` field. Override or extend the table:

```python
from llmbrake import create_guard, DEFAULT_PRICES, ModelPrice

create_guard(5, prices={**DEFAULT_PRICES, "my-model": ModelPrice(input_per_mtok=1, output_per_mtok=3)})
```

Dated snapshot IDs such as `gpt-5.4-mini-2026-03-17` fall back to the undated entry. `compute_cost_usd(model, usage, prices)` and `lookup_price(model, prices)` are exported too.

## Known limitations

- **Concurrent overshoot.** The budget is checked before a call and the cost is recorded after it. Several in-flight calls for the same customer can each pass the check, so the cap can be overshot by roughly the cost of those concurrent calls. Keep a margin in your budgets if this matters.
- **Prices need verification.** See above. Long-context tiers, batch discounts, regional or data-residency uplifts and priority tiers are not modeled, and the Anthropic IDs in the default table may differ from what your endpoint accepts.
- **Chat completions only.** Only chat-completions-style token usage is metered: no images, audio, embeddings, the Responses API or tool fees yet.
- **Streaming needs usage.** If a provider ignores `include_usage`, or a stream is abandoned before the usage chunk arrives, that call is recorded with zero cost.
- **UTC calendar months.** Budget periods reset at 00:00 UTC on the 1st.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e . pytest fakeredis openai httpx ruff mypy
pytest && ruff check . && mypy
python -m build   # wheel + sdist in dist/
```

The tests use fake providers and a mocked HTTP transport; they never touch the network.

## License

[MIT](https://github.com/llmbrake/llmbrake/blob/main/python/LICENSE)
