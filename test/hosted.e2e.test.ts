// End-to-end against a running LLMBrake Cloud (`wrangler dev`). Skipped unless LB_BASE is set.
import { test } from "node:test";
import assert from "node:assert/strict";
import { createGuard, HostedStore, BudgetExceededError, MemoryStore, PriceTable } from "../src/index.js";

const BASE = process.env.LB_BASE;
const PRICES: PriceTable = { m: { inputPerMTok: 0, outputPerMTok: 1 }, cheap: { inputPerMTok: 0, outputPerMTok: 0.1 } };

async function signup(): Promise<{ sdkKey: string; cookie: string }> {
  const r = await fetch(`${BASE}/signup`, { method: "POST", redirect: "manual", headers: { origin: BASE!, "content-type": "application/x-www-form-urlencoded" }, body: "name=SDK+e2e" });
  const html = await r.text();
  return { sdkKey: html.match(/lbsk_[A-Za-z0-9_-]{20,}/)![0], cookie: r.headers.get("set-cookie")!.split(";")[0] };
}
// Provider whose every call costs exactly $0.30 (300k output tokens at $1/M), with a small delay.
const provider = (log: string[]) => async (p: any) => { await new Promise((r) => setTimeout(r, 30)); log.push(p.model); return { model: p.model, usage: { prompt_tokens: 0, completion_tokens: 300_000 } }; };
const PARAMS = { model: "m", messages: [], max_tokens: 300_000 };

test("two servers share one budget; parallel calls can't overshoot by more than one call", { skip: !BASE }, async () => {
  const { sdkKey } = await signup();
  const mk = () => createGuard({ budgetUsd: 1, prices: PRICES, store: new HostedStore({ apiKey: sdkKey, baseUrl: BASE }) });
  const log: string[] = [];
  const a = mk().wrap(provider(log)), b = mk().wrap(provider(log));
  const results = await Promise.allSettled(Array.from({ length: 12 }, (_, i) => (i % 2 ? a : b)(PARAMS, { customerId: "shared" })));
  const ok = results.filter((r) => r.status === "fulfilled").length;
  const blocked = results.filter((r) => r.status === "rejected" && r.reason instanceof BudgetExceededError).length;
  assert.equal(ok + blocked, 12);
  assert.equal(ok, 4, "holds of $0.30 admit 0, .3, .6, .9 -> 4 calls, $1.20 total (overshoot <= one call)");
  const store = new HostedStore({ apiKey: sdkKey, baseUrl: BASE });
  const v = await store.customer("shared", new Date().toISOString().slice(0, 7));
  assert.ok(Math.abs(v.spentUsd - 1.2) < 1e-9); assert.equal(v.pendingUsd, 0); assert.equal(v.status, "blocked");

  // Contrast: per-process MemoryStores (the 0.1.0 default) let all 12 through ($3.60 on a $1 budget).
  const mem = () => createGuard({ budgetUsd: 1, prices: PRICES, store: new MemoryStore() }).wrap(provider([]));
  const servers = [mem(), mem(), mem(), mem(), mem(), mem()];
  const memRes = await Promise.allSettled(Array.from({ length: 12 }, (_, i) => servers[i % 6](PARAMS, { customerId: "x" })));
  assert.equal(memRes.filter((r) => r.status === "fulfilled").length, 12);
});

test("budget edited in the dashboard applies to the next call; downgrade kicks in", { skip: !BASE }, async () => {
  const { sdkKey, cookie } = await signup();
  const log: string[] = [];
  const g = createGuard({ budgetUsd: 100, prices: PRICES, downgradeAt: 0.5, downgradeMap: { m: "cheap" }, store: new HostedStore({ apiKey: sdkKey, baseUrl: BASE }) });
  const create = g.wrap(provider(log));
  await create(PARAMS, { customerId: "cus_1" }); // $0.30
  const period = new Date().toISOString().slice(0, 7);
  const r = await fetch(`${BASE}/app/budget`, { method: "POST", redirect: "manual", headers: { origin: BASE!, cookie, "content-type": "application/x-www-form-urlencoded" }, body: `customerId=cus_1&period=${period}&budget=0.5` });
  assert.equal(r.status, 303);
  await create(PARAMS, { customerId: "cus_1" }); // spent .30 >= .25 -> downgraded to cheap ($0.03)
  assert.deepEqual(log, ["m", "cheap"]);
  await create(PARAMS, { customerId: "cus_1" }); // .33 < .5 still allowed
  for (let i = 0; i < 10; i++) await create(PARAMS, { customerId: "cus_1" }).catch(() => {});
  await assert.rejects(create(PARAMS, { customerId: "cus_1" }), (e: any) => e instanceof BudgetExceededError && e.budgetUsd === 0.5);
});

test("a bad API key fails closed when failOpen is false", { skip: !BASE }, async () => {
  const g = createGuard({ budgetUsd: 1, prices: PRICES, failOpen: false, store: new HostedStore({ apiKey: "lbsk_wrong", baseUrl: BASE }) });
  await assert.rejects(g.wrap(provider([]))(PARAMS, { customerId: "z" }), /hosted API 401/);
});
