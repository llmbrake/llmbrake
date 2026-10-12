import { test } from "node:test";
import assert from "node:assert/strict";
import { createGuard, HostedStore, HostedStoreError, BudgetExceededError, estimateCallUsd, isReservingStore, MemoryStore, PriceTable, UsageEvent } from "../src/index.js";

const PRICES: PriceTable = { big: { inputPerMTok: 10, outputPerMTok: 30 }, small: { inputPerMTok: 1, outputPerMTok: 2 } };
const fixedNow = () => new Date("2026-10-09T12:00:00Z");

/** Tiny in-memory imitation of the LLMBrake Cloud API, behind a fake fetch. */
function fakeCloud(opts: { override?: number | null; failWith?: number; hang?: boolean } = {}) {
  const reqs: { path: string; body: any; auth: string | null }[] = [];
  let spent = 0; const holds = new Map<string, number>(); let n = 0;
  const f = (async (url: string, init: any) => {
    if (opts.hang) return new Promise((_, rej) => init.signal.addEventListener("abort", () => rej(Object.assign(new Error("aborted"), { name: "AbortError" }))));
    const u = new URL(url); const body = init.body ? JSON.parse(init.body) : undefined;
    reqs.push({ path: u.pathname, body, auth: init.headers.authorization });
    if (opts.failWith) return new Response(JSON.stringify({ error: "boom" }), { status: opts.failWith });
    const budget = opts.override ?? body?.budgetUsd ?? null;
    const pending = () => [...holds.values()].reduce((a, b) => a + b, 0);
    let out: any;
    if (u.pathname === "/v1/reserve") {
      const allowed = budget === null || spent + pending() < budget;
      const holdId = allowed ? `hold_${++n}` : null;
      if (holdId) holds.set(holdId, body.holdUsd);
      out = { allowed, holdId, spentUsd: spent, pendingUsd: pending(), budgetUsd: budget };
    } else if (u.pathname === "/v1/settle") { holds.delete(body.holdId); spent += body.costUsd; out = { spentUsd: spent }; }
    else if (u.pathname === "/v1/spend/add") { spent += body.usd; out = { spentUsd: spent }; }
    else if (u.pathname === "/v1/spend") out = { spentUsd: spent, pendingUsd: pending(), budgetUsd: budget, status: "normal" };
    return new Response(JSON.stringify(out), { status: 200 });
  }) as unknown as typeof fetch;
  return { f, reqs, holds, get spent() { return spent; } };
}

const provider = (calls: any[], usage = { prompt_tokens: 100_000, completion_tokens: 100_000 }) => async (p: any) => { calls.push(p); return { model: p.model, usage, choices: [] }; };

test("HostedStore implements the reserving store API and talks to the configured base URL", async () => {
  const c = fakeCloud();
  const s = new HostedStore({ apiKey: "lbsk_x", baseUrl: "http://cloud.test/", fetch: c.f });
  assert.ok(isReservingStore(s)); assert.ok(!isReservingStore(new MemoryStore()));
  assert.equal(await s.addSpend("a", "2026-10", 1.5), 1.5);
  assert.equal(await s.getSpend("a", "2026-10"), 1.5);
  const r = await s.reserve({ customerId: "a", period: "2026-10", holdUsd: 0.1, budgetUsd: Infinity, downgradeAt: 0.8, stream: true });
  assert.equal(r.budgetUsd, Infinity);
  assert.equal(c.reqs[2].body.budgetUsd, null, "Infinity is sent as null");
  assert.equal(c.reqs[2].body.ttlSec, 900, "streams get a long hold");
  assert.equal(c.reqs[0].auth, "Bearer lbsk_x");
  assert.throws(() => new HostedStore({ apiKey: "" }), /apiKey/);
});

test("wrapped calls reserve an estimate first, then settle the real cost", async () => {
  const c = fakeCloud(); const calls: any[] = []; const events: UsageEvent[] = [];
  const g = createGuard({ budgetUsd: 10, prices: PRICES, now: fixedNow, store: new HostedStore({ apiKey: "k", fetch: c.f }), onUsage: (e) => { events.push(e); } });
  const create = g.wrap(provider(calls));
  await create({ model: "small", messages: [{ role: "user", content: "hi" }], max_tokens: 500 }, { customerId: "a" });
  assert.deepEqual(c.reqs.map((r) => r.path), ["/v1/reserve", "/v1/settle"]);
  // estimate: ceil(len/4) prompt tokens * $1/M + 500 * $2/M
  const est = c.reqs[0].body.holdUsd;
  assert.ok(est > 0.001 && est < 0.0011, String(est));
  assert.equal(c.reqs[0].body.budgetUsd, 10); assert.equal(c.reqs[0].body.downgradeAt, 0.8);
  assert.equal(c.reqs[1].body.holdId, "hold_1");
  assert.equal(Number(c.reqs[1].body.costUsd.toFixed(6)), 0.3);
  assert.equal(c.holds.size, 0);
  assert.equal(Number(events[0].spentUsd.toFixed(6)), 0.3);
});

test("blocked by the server: provider is never called", async () => {
  const c = fakeCloud({ override: 0.2 }); const calls: any[] = [];
  const g = createGuard({ budgetUsd: 100, prices: PRICES, store: new HostedStore({ apiKey: "k", fetch: c.f }) });
  const create = g.wrap(provider(calls));
  await create({ model: "small", messages: [] }, { customerId: "a" }); // 0 < 0.2 -> allowed, costs 0.3
  await assert.rejects(create({ model: "small", messages: [] }, { customerId: "a" }), (e: any) => e instanceof BudgetExceededError && e.budgetUsd === 0.2);
  assert.equal(calls.length, 1);
});

test("dashboard budget (returned by the server) drives the downgrade", async () => {
  const c = fakeCloud({ override: 0.5 }); const calls: any[] = [];
  const g = createGuard({ budgetUsd: 100, prices: PRICES, downgradeAt: 0.5, downgradeMap: { big: "small" }, store: new HostedStore({ apiKey: "k", fetch: c.f }) });
  const create = g.wrap(provider(calls, { prompt_tokens: 10_000, completion_tokens: 0 }));
  await create({ model: "big", messages: [] }, { customerId: "a" }); // costs 0.1 at big price
  await create({ model: "big", messages: [] }, { customerId: "a" });
  await create({ model: "big", messages: [] }, { customerId: "a" }); // spent 0.2 < 0.25 (50% of the $0.50 override)
  assert.deepEqual(calls.map((x) => x.model), ["big", "big", "big"]);
  await create({ model: "big", messages: [] }, { customerId: "a" }); // spent 0.3 >= 0.25 -> small
  assert.equal(calls[3].model, "small");
});

test("provider errors release the hold (settle with $0)", async () => {
  const c = fakeCloud();
  const g = createGuard({ budgetUsd: 10, prices: PRICES, store: new HostedStore({ apiKey: "k", fetch: c.f }) });
  const create = g.wrap(async () => { throw new Error("provider 500"); });
  await assert.rejects(create({ model: "small", messages: [] }, { customerId: "a" }), /provider 500/);
  assert.equal(c.reqs[1].path, "/v1/settle"); assert.equal(c.reqs[1].body.costUsd, 0);
  assert.equal(c.holds.size, 0);
});

test("unknown downgrade target releases the hold", async () => {
  const c = fakeCloud({ override: 1 });
  const s = new HostedStore({ apiKey: "k", fetch: c.f });
  await s.addSpend("a", "2026-10", 0.9);
  const g = createGuard({ budgetUsd: 1, prices: PRICES, now: fixedNow, downgradeMap: { big: "mystery" }, store: s });
  await assert.rejects(g.wrap(provider([]))({ model: "big", messages: [] }, { customerId: "a" }), /mystery/);
  assert.equal(c.holds.size, 0);
});

test("streams settle when the stream ends", async () => {
  const c = fakeCloud();
  const g = createGuard({ budgetUsd: 10, prices: PRICES, store: new HostedStore({ apiKey: "k", fetch: c.f }) });
  const create = g.wrap(async (p: any) => (async function* () { yield { model: p.model }; yield { model: p.model, usage: { prompt_tokens: 1_000_000, completion_tokens: 0 } }; })());
  const stream: any = await create({ model: "small", messages: [], stream: true }, { customerId: "s" });
  assert.equal(c.reqs.length, 1);
  for await (const _ of stream) { /* drain */ }
  assert.equal(c.reqs[1].path, "/v1/settle"); assert.equal(c.reqs[1].body.costUsd, 1);
});

test("unreachable API: fails open by default, fails closed with failOpen:false", async () => {
  const bad = fakeCloud({ failWith: 503 }); const calls: any[] = [];
  const open = createGuard({ budgetUsd: 1, prices: PRICES, store: new HostedStore({ apiKey: "k", fetch: bad.f }) });
  await open.wrap(provider(calls))({ model: "small", messages: [] }, { customerId: "a" });
  assert.equal(calls.length, 1);
  const closed = createGuard({ budgetUsd: 1, prices: PRICES, failOpen: false, store: new HostedStore({ apiKey: "k", fetch: bad.f }) });
  await assert.rejects(closed.wrap(provider(calls))({ model: "small", messages: [] }, { customerId: "a" }), (e: any) => e instanceof HostedStoreError && e.status === 503);
  assert.equal(calls.length, 1);
});

test("timeouts surface as HostedStoreError", async () => {
  const c = fakeCloud({ hang: true });
  const s = new HostedStore({ apiKey: "k", fetch: c.f, timeoutMs: 50 });
  await assert.rejects(s.getSpend("a", "2026-10"), /timeout after 50ms/);
});

test("estimateCallUsd", () => {
  assert.equal(estimateCallUsd("small", { messages: [], max_tokens: 1_000_000 }, PRICES).toFixed(6), "2.000001");
  assert.equal(estimateCallUsd("nope", { messages: [] }, PRICES), 0);
  assert.ok(estimateCallUsd("small", { messages: [] }, PRICES) > 0, "defaults to 1000 output tokens");
});

test("a custom estimateUsd is used for the hold", async () => {
  const c = fakeCloud();
  const g = createGuard({ budgetUsd: 10, prices: PRICES, estimateUsd: () => 0.42, store: new HostedStore({ apiKey: "k", fetch: c.f }) });
  await g.wrap(provider([]))({ model: "small", messages: [] }, { customerId: "a" });
  assert.equal(c.reqs[0].body.holdUsd, 0.42);
});
