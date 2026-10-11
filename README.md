# llmbrake

**Per-customer cost caps for AI apps: put a monthly dollar budget on every customer, switch them to a cheaper model near the cap, and block them at the cap.**

[![CI](https://github.com/llmbrake/llmbrake/actions/workflows/ci.yml/badge.svg)](https://github.com/llmbrake/llmbrake/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

`llmbrake` is a small TypeScript library that wraps your OpenAI-compatible `chat.completions.create` call. It prices every call in USD from the token usage the provider returns and keeps a running monthly total per customer. Near a customer's budget it swaps in a cheaper model; at the budget it throws a typed `BudgetExceededError` before the request ever reaches the provider.

- **Not a proxy.** It runs inside your app and calls your provider directly with your own keys.
- **Metadata only.** It never reads or stores prompts or completions, only customer ID, plan, feature tag, model, token counts, cost and timestamps.
- **Zero runtime dependencies.** ESM, Node 18+, MIT licensed.

Status: `0.1.0`, early. Website: [llmbrake.com](https://llmbrake.com)

## Install

```bash
npm install llmbrake
```

`llmbrake` is ESM-only (`import`, not `require`).

## 30-second quickstart

```ts
import OpenAI from "openai";
import { createGuard, isBudgetExceededError } from "llmbrake";

const openai = new OpenAI();

const guard = createGuard({
  budgetUsd: (ctx) => (ctx.plan === "pro" ? 20 : 2), // USD per customer per calendar month (UTC)
  downgradeAt: 0.8,                                   // at 80% of the budget...
  downgradeMap: { "gpt-5.5": "gpt-5.4-mini" },        // ...send gpt-5.5 requests to gpt-5.4-mini
});

const chat = openai.chat.completions;
const create = guard.wrap(chat.create.bind(chat));

try {
  const res = await create(
    { model: "gpt-5.5", messages: [{ role: "user", content: "Hello" }] },
    { customerId: user.id, plan: user.plan, feature: "chat" },
  );
} catch (e) {
  if (isBudgetExceededError(e)) {
    // e.customerId, e.spentUsd, e.budgetUsd, e.period
    return showUpgradePrompt();
  }
  throw e;
}

await guard.status({ customerId: user.id, plan: user.plan });
// → { period: "2026-10", spentUsd: …, budgetUsd: 20, remainingUsd: … }
```

## How it works

Every wrapped call goes through three steps:

1. **Check (before the call).** `llmbrake` resolves the customer's budget (a number, or your `(ctx) => number` function, e.g. by plan) and reads their spend for the current period, a calendar month in UTC such as `2026-10`.
2. **Decide.**
   - **Block:** if `spent >= budget`, it throws `BudgetExceededError` (`code: "LLMBRAKE_BUDGET_EXCEEDED"`) and **the provider is never called**. Catch it to show an upgrade prompt.
   - **Downgrade:** if `spent >= budget × downgradeAt` and the requested model has an entry in `downgradeMap`, the request is sent with the cheaper model instead. The usage event reports `downgraded: true` and the original `requestedModel`.
   - Otherwise the request goes through unchanged.
3. **Record (after the call).** The cost is computed from the response's `usage` (uncached input, cached input and output tokens) and your price table, then added to the customer's total. The `onUsage` hook receives a usage event:

```ts
{
  customerId: "cus_123", plan: "pro", feature: "chat", period: "2026-10",
  requestedModel: "gpt-5.5", model: "gpt-5.4-mini", downgraded: true,
  promptTokens: 1102, completionTokens: 182, cachedTokens: 640,
  costUsd: 0.0012, spentUsd: 16.41, budgetUsd: 20, timestamp: "2026-10-11T20:00:00.000Z"
}
```

`guard.check(ctx, model)` runs steps 1–2 without making a call, which is handy for disabling a button before the user clicks it.

### Streaming

With `stream: true`, `llmbrake` sets `stream_options.include_usage = true` and records the cost when the stream ends, including when your code stops reading early (whatever usage was received by then is recorded).

### Other OpenAI-compatible endpoints

Any `create(params)` function that accepts `{ model, ... }` and returns `{ model, usage: { prompt_tokens, completion_tokens } }` works, for example OpenRouter, Anthropic's OpenAI-compatible endpoint and local gateways. Add their model IDs to the price table.

## Storage (Redis example)

The default `MemoryStore` keeps totals in process memory: fine for one server, tests and demos, but lost on restart and not shared between instances. For multiple instances or serverless, implement the two-method `UsageStore` interface. With [ioredis](https://github.com/redis/ioredis):

```ts
import Redis from "ioredis";
import { createGuard, type UsageStore } from "llmbrake";

const redis = new Redis(process.env.REDIS_URL!);
const key = (customerId: string, period: string) => `llmbrake:${period}:${customerId}`;

const redisStore: UsageStore = {
  async getSpend(customerId, period) {
    return Number((await redis.get(key(customerId, period))) ?? 0);
  },
  async addSpend(customerId, period, usd) {
    const k = key(customerId, period);
    const res = await redis
      .multi()
      .incrbyfloat(k, usd)
      .expire(k, 60 * 60 * 24 * 40) // keep ~40 days, then let old months expire
      .exec();
    const [err, total] = res?.[0] ?? [new Error("Redis transaction failed"), null];
    if (err) throw err;
    return Number(total);
  },
};

const guard = createGuard({ budgetUsd: 5, store: redisStore });
```

`INCRBYFLOAT` is atomic, so totals stay correct across instances. If the store throws (or returns a non-numeric value), calls **fail open** by default so your product keeps working; set `failOpen: false` to fail closed instead.

## Options

`createGuard(options)` returns `{ wrap, check, status }`.

| Option | Default | Description |
|---|---|---|
| `budgetUsd` | required | Monthly USD budget: a number, or `(ctx) => number \| Promise<number>`. Must be `>= 0`; use `Infinity` for unlimited. |
| `store` | `new MemoryStore()` | Where spend is kept. Any `UsageStore` (`getSpend`, `addSpend`). |
| `prices` | `DEFAULT_PRICES` | Model → `{ inputPerMTok, outputPerMTok, cachedInputPerMTok?, source? }`, USD per 1M tokens. |
| `downgradeAt` | `0.8` | Fraction of the budget (0–1) at which `downgradeMap` applies. `1` disables downgrading. |
| `downgradeMap` | `{}` | `{ "expensive-model": "cheaper-model" }`. |
| `onUsage` | none | `(event) => void \| Promise<void>`, called after each metered call. Errors in it are swallowed. |
| `unknownModel` | `"throw"` | Model missing from `prices`: `"throw"` an `UnknownModelPriceError` before the call, or `"allow"` it and record `costUsd: null`. |
| `failOpen` | `true` | If the store errors: `true` lets calls through, `false` rethrows. |
| `now` | `() => new Date()` | Clock override, mainly for tests. |

The call context passed as the second argument to the wrapped function is `{ customerId: string; plan?: string; feature?: string }`.

## Model prices: verify before relying on them

`DEFAULT_PRICES` covers a few common OpenAI and Anthropic models in USD per 1M tokens (standard tier), read on 2026-10-09 from:

- OpenAI: https://developers.openai.com/api/docs/pricing
- Anthropic: https://docs.anthropic.com/en/docs/about-claude/pricing

**Provider prices change often, and these values are not guaranteed current.** Check them against the pages above before using them for billing decisions. Each entry carries a `source` field. Override or extend the table:

```ts
import { createGuard, DEFAULT_PRICES } from "llmbrake";

createGuard({
  budgetUsd: 5,
  prices: { ...DEFAULT_PRICES, "my-model": { inputPerMTok: 1, outputPerMTok: 3 } },
});
```

Dated snapshot IDs such as `gpt-5.4-mini-2026-03-17` fall back to the undated entry.

## Known limitations

- **Concurrent overshoot.** The budget is checked before a call and the cost is recorded after it. Several in-flight calls for the same customer can each pass the check, so the cap can be overshot by roughly the cost of those concurrent calls. Keep a margin in your budgets if this matters.
- **Prices need verification.** See above. Long-context tiers, batch discounts, regional or data-residency uplifts and priority tiers are not modeled, and the Anthropic IDs in the default table may differ from what your endpoint accepts.
- **Chat completions only.** Only chat-completions-style token usage is metered: no images, audio, embeddings or tool fees yet.
- **Streaming needs usage.** If a provider ignores `include_usage`, or a stream is abandoned before the usage chunk arrives, that call is recorded with zero cost.
- **UTC calendar months.** Budget periods reset at 00:00 UTC on the 1st.

## Hosted dashboard (coming)

The library is free and stays MIT licensed. We're building an optional hosted dashboard on top of it: cost per customer, feature and model, shared counters, alerts near and at caps, and AI cost vs. Stripe revenue per customer. It is **not available yet**; join the waitlist at [llmbrake.com](https://llmbrake.com).

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md). Security issues: see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
