import { test } from "node:test";
import assert from "node:assert/strict";
import {
  createGuard, MemoryStore, BudgetExceededError, UnknownModelPriceError,
  computeCostUsd, DEFAULT_PRICES, isBudgetExceededError, UsageEvent, PriceTable,
} from "../src/index.js";

const PRICES: PriceTable = {
  big: { inputPerMTok: 10, outputPerMTok: 30, cachedInputPerMTok: 1 },
  small: { inputPerMTok: 1, outputPerMTok: 2 },
};
const fixedNow = () => new Date("2026-10-09T12:00:00Z");

// Fake OpenAI-compatible create(): echoes the model, fixed usage of 100k in / 100k out.
function fakeCreate(calls: any[]) {
  return async (params: any) => {
    calls.push(params);
    return { model: params.model, usage: { prompt_tokens: 100_000, completion_tokens: 100_000 }, choices: [] };
  };
}

test("computeCostUsd handles cached tokens and dated snapshots", () => {
  // 100k uncached*10 + 100k cached*1 + 50k out*30 = (1,000,000 + 100,000 + 1,500,000)/1e6 = 2.6
  const c = computeCostUsd("big-2026-01-01", { prompt_tokens: 200_000, completion_tokens: 50_000, prompt_tokens_details: { cached_tokens: 100_000 } }, PRICES);
  assert.equal(Number(c!.toFixed(6)), 2.6);
  assert.equal(computeCostUsd("nope", {}, PRICES), null);
});

test("default price table entries all cite a source", () => {
  for (const [m, p] of Object.entries(DEFAULT_PRICES)) assert.ok(p.source, `${m} missing source`);
});

test("records spend per customer and emits usage events", async () => {
  const events: UsageEvent[] = [];
  const calls: any[] = [];
  const g = createGuard({ budgetUsd: 100, prices: PRICES, now: fixedNow, onUsage: (e) => { events.push(e); } });
  const create = g.wrap(fakeCreate(calls));
  await create({ model: "small", messages: [] }, { customerId: "a", feature: "chat" });
  // 100k*1 + 100k*2 = 0.3
  assert.equal(events.length, 1);
  assert.equal(Number(events[0].costUsd!.toFixed(6)), 0.3);
  assert.equal(events[0].feature, "chat");
  assert.equal(events[0].period, "2026-10");
  const sa = await g.status({ customerId: "a" });
  const sb = await g.status({ customerId: "b" });
  assert.equal(Number(sa.spentUsd.toFixed(6)), 0.3);
  assert.equal(sb.spentUsd, 0);
});

test("downgrades to the cheaper model past the threshold", async () => {
  const calls: any[] = [];
  const events: UsageEvent[] = [];
  // big call costs 100k*10 + 100k*30 = $4.00 ; budget $5, downgrade at 50%
  const g = createGuard({ budgetUsd: 5, prices: PRICES, now: fixedNow, downgradeAt: 0.5, downgradeMap: { big: "small" }, onUsage: (e) => { events.push(e); } });
  const create = g.wrap(fakeCreate(calls));
  await create({ model: "big", messages: [] }, { customerId: "c" });
  await create({ model: "big", messages: [] }, { customerId: "c" });
  assert.equal(calls[0].model, "big");
  assert.equal(calls[1].model, "small");
  assert.equal(events[1].downgraded, true);
  assert.equal(events[1].requestedModel, "big");
});

test("blocks at the cap with a typed error", async () => {
  const calls: any[] = [];
  const g = createGuard({ budgetUsd: 4, prices: PRICES, now: fixedNow, downgradeAt: 1 });
  const create = g.wrap(fakeCreate(calls));
  await create({ model: "big", messages: [] }, { customerId: "d" }); // spends exactly $4
  await assert.rejects(create({ model: "big", messages: [] }, { customerId: "d" }), (e: unknown) => {
    assert.ok(e instanceof BudgetExceededError);
    assert.ok(isBudgetExceededError(e));
    assert.equal(e.customerId, "d");
    assert.equal(e.budgetUsd, 4);
    assert.equal(e.code, "LLMBRAKE_BUDGET_EXCEEDED");
    return true;
  });
  assert.equal(calls.length, 1, "blocked call must not reach the provider");
});

test("per-plan budgets via function", async () => {
  const g = createGuard({ budgetUsd: (ctx) => (ctx.plan === "pro" ? 50 : 0.1), prices: PRICES, now: fixedNow });
  const create = g.wrap(fakeCreate([]));
  await create({ model: "small", messages: [] }, { customerId: "free1", plan: "free" }); // 0.3 > 0.1
  await assert.rejects(create({ model: "small", messages: [] }, { customerId: "free1", plan: "free" }), BudgetExceededError);
  await create({ model: "small", messages: [] }, { customerId: "pro1", plan: "pro" });
  await create({ model: "small", messages: [] }, { customerId: "pro1", plan: "pro" });
});

test("budgets reset each calendar month (UTC)", async () => {
  let now = new Date("2026-10-31T23:00:00Z");
  const g = createGuard({ budgetUsd: 0.3, prices: PRICES, now: () => now, downgradeAt: 1 });
  const create = g.wrap(fakeCreate([]));
  await create({ model: "small", messages: [] }, { customerId: "e" });
  await assert.rejects(create({ model: "small", messages: [] }, { customerId: "e" }), BudgetExceededError);
  now = new Date("2026-11-01T00:00:01Z");
  await create({ model: "small", messages: [] }, { customerId: "e" });
});

test("unknown model throws by default, allowed with cost null when configured", async () => {
  const calls: any[] = [];
  const strict = createGuard({ budgetUsd: 10, prices: PRICES, now: fixedNow }).wrap(fakeCreate(calls));
  await assert.rejects(strict({ model: "mystery", messages: [] }, { customerId: "f" }), UnknownModelPriceError);
  assert.equal(calls.length, 0);
  const events: UsageEvent[] = [];
  const lax = createGuard({ budgetUsd: 10, prices: PRICES, now: fixedNow, unknownModel: "allow", onUsage: (e) => { events.push(e); } }).wrap(fakeCreate(calls));
  await lax({ model: "mystery", messages: [] }, { customerId: "f" });
  assert.equal(events[0].costUsd, null);
});

test("streaming: requests include_usage and records cost after the stream ends", async () => {
  const events: UsageEvent[] = [];
  let seenParams: any;
  const streamingCreate = async (params: any) => {
    seenParams = params;
    return (async function* () {
      yield { model: "small", choices: [{ delta: { content: "hi" } }] };
      yield { model: "small", choices: [], usage: { prompt_tokens: 1_000_000, completion_tokens: 0 } };
    })();
  };
  const g = createGuard({ budgetUsd: 10, prices: PRICES, now: fixedNow, onUsage: (e) => { events.push(e); } });
  const create = g.wrap(streamingCreate);
  const stream: any = await create({ model: "small", messages: [], stream: true }, { customerId: "s" });
  assert.equal(seenParams.stream_options.include_usage, true);
  const chunks = [];
  for await (const c of stream) chunks.push(c);
  assert.equal(chunks.length, 2);
  assert.equal(events.length, 1);
  assert.equal(events[0].costUsd, 1);
  assert.equal((await g.status({ customerId: "s" })).spentUsd, 1);
});

test("fails open when the store is down (default) and closed when configured", async () => {
  const broken = { getSpend: async () => { throw new Error("redis down"); }, addSpend: async () => { throw new Error("redis down"); } };
  const open = createGuard({ budgetUsd: 1, prices: PRICES, store: broken, now: fixedNow }).wrap(fakeCreate([]));
  await open({ model: "small", messages: [] }, { customerId: "g" });
  const closed = createGuard({ budgetUsd: 1, prices: PRICES, store: broken, now: fixedNow, failOpen: false }).wrap(fakeCreate([]));
  await assert.rejects(closed({ model: "small", messages: [] }, { customerId: "g" }), /redis down/);
});

test("onUsage hook errors never break the caller; customerId is required", async () => {
  const g = createGuard({ budgetUsd: 10, prices: PRICES, now: fixedNow, onUsage: () => { throw new Error("boom"); } });
  const create = g.wrap(fakeCreate([]));
  const res: any = await create({ model: "small", messages: [] }, { customerId: "h" });
  assert.equal(res.model, "small");
  await assert.rejects(create({ model: "small", messages: [] }, undefined as any), TypeError);
});

test("custom store is used", async () => {
  const store = new MemoryStore();
  await store.addSpend("i", "2026-10", 9.99);
  const g = createGuard({ budgetUsd: 10, prices: PRICES, store, now: fixedNow, downgradeAt: 1 });
  const create = g.wrap(fakeCreate([]));
  await create({ model: "small", messages: [] }, { customerId: "i" });
  await assert.rejects(create({ model: "small", messages: [] }, { customerId: "i" }), BudgetExceededError);
});

test("invalid budgets are rejected instead of silently disabling the cap", async () => {
  const calls: any[] = [];
  const nan = createGuard({ budgetUsd: () => Number("not a number"), prices: PRICES, now: fixedNow }).wrap(fakeCreate(calls));
  await assert.rejects(nan({ model: "small", messages: [] }, { customerId: "j" }), TypeError);
  const neg = createGuard({ budgetUsd: -1, prices: PRICES, now: fixedNow }).wrap(fakeCreate(calls));
  await assert.rejects(neg({ model: "small", messages: [] }, { customerId: "j" }), TypeError);
  assert.equal(calls.length, 0, "rejected calls must not reach the provider");
  // Infinity means unlimited
  const unlimited = createGuard({ budgetUsd: Infinity, prices: PRICES, now: fixedNow }).wrap(fakeCreate(calls));
  await unlimited({ model: "small", messages: [] }, { customerId: "j" });
  assert.equal(calls.length, 1);
});

test("downgradeAt outside 0..1 throws at construction", () => {
  assert.throws(() => createGuard({ budgetUsd: 1, downgradeAt: 1.5 }), RangeError);
  assert.throws(() => createGuard({ budgetUsd: 1, downgradeAt: Number.NaN }), RangeError);
  assert.doesNotThrow(() => createGuard({ budgetUsd: 1, downgradeAt: 0 }));
});

test("a store returning a non-numeric spend is treated as a store failure", async () => {
  const garbage = { getSpend: async () => Number("oops"), addSpend: async (_c: string, _p: string, usd: number) => usd };
  const closed = createGuard({ budgetUsd: 1, prices: PRICES, store: garbage, now: fixedNow, failOpen: false }).wrap(fakeCreate([]));
  await assert.rejects(closed({ model: "small", messages: [] }, { customerId: "k" }), /non-numeric/);
  const open = createGuard({ budgetUsd: 1, prices: PRICES, store: garbage, now: fixedNow }).wrap(fakeCreate([]));
  const res: any = await open({ model: "small", messages: [] }, { customerId: "k" });
  assert.equal(res.model, "small");
});

test("streaming: early break still records usage received so far", async () => {
  const events: UsageEvent[] = [];
  const streamingCreate = async () => (async function* () {
    yield { model: "small", choices: [], usage: { prompt_tokens: 1_000_000, completion_tokens: 0 } };
    yield { model: "small", choices: [{ delta: { content: "never read" } }] };
  })();
  const g = createGuard({ budgetUsd: 10, prices: PRICES, now: fixedNow, onUsage: (e) => { events.push(e); } });
  const stream: any = await g.wrap(streamingCreate)({ model: "small", messages: [], stream: true }, { customerId: "l" });
  for await (const _ of stream) break;
  assert.equal(events.length, 1);
  assert.equal(events[0].costUsd, 1);
});
