import { test } from "node:test";
import assert from "node:assert/strict";
import { isBudgetExceededError, type UsageEvent } from "llmbrake";
import { createOpenRouterBudget, openRouterClient, type ChatCompletions } from "./openrouter.ts";

/** Fake OpenRouter chat.completions: 1,500 input / 500 output tokens per call, OpenRouter-style response. */
function fakeCompletions(calls: any[]): ChatCompletions {
  return {
    async create(params: any) {
      calls.push(params);
      if (params.stream) {
        return (async function* () {
          yield { model: params.model, choices: [{ delta: { content: "ok" } }] };
          yield { model: params.model, choices: [], usage: { prompt_tokens: 1500, completion_tokens: 500, cost: 0.02 } };
        })();
      }
      return { model: params.model, choices: [{ message: { content: "ok" } }], usage: { prompt_tokens: 1500, completion_tokens: 500, cost: 0.02 } };
    },
  };
}

test("openRouterClient points the OpenAI SDK at OpenRouter", () => {
  const c = openRouterClient("sk-or-test");
  assert.equal(c.baseURL, "https://openrouter.ai/api/v1");
});

test("wraps OpenRouter: prices namespaced models, downgrades at 80%, blocks at the cap", async () => {
  const calls: any[] = [];
  const events: UsageEvent[] = [];
  const { create } = createOpenRouterBudget({ completions: fakeCompletions(calls), budgetUsd: 0.05, onUsage: (e) => events.push(e) });
  const ctx = { customerId: "u1" };
  const outcome: string[] = [];
  for (let i = 0; i < 5; i++) {
    try { const r = await create({ model: "openai/gpt-5.5", messages: [{ role: "user", content: "hi" }] }, ctx); outcome.push(r.model); }
    catch (e) { assert.ok(isBudgetExceededError(e)); outcome.push("blocked"); }
  }
  // $0.0225 per gpt-5.5 call; at $0.045 (>= $0.04) switch to mini ($0.003375); cap reached after the 4th call.
  assert.deepEqual(outcome, ["openai/gpt-5.5", "openai/gpt-5.5", "openai/gpt-5.4-mini", "openai/gpt-5.4-mini", "blocked"]);
  assert.equal(calls.length, 4, "blocked request never reaches OpenRouter");
  assert.ok(Math.abs(events[0].costUsd! - 0.0225) < 1e-12);
  assert.equal(events[2].downgraded, true);
  assert.equal(events[2].requestedModel, "openai/gpt-5.5");
});

test("streaming: requests include_usage and records cost when the stream ends", async () => {
  const calls: any[] = [];
  const events: UsageEvent[] = [];
  const { create } = createOpenRouterBudget({ completions: fakeCompletions(calls), budgetUsd: 1, onUsage: (e) => events.push(e) });
  const stream: any = await create({ model: "openai/gpt-5.5", messages: [], stream: true }, { customerId: "u2" });
  for await (const _ of stream) { /* consume */ }
  assert.equal(calls[0].stream_options.include_usage, true);
  assert.equal(events.length, 1);
  assert.ok(Math.abs(events[0].costUsd! - 0.0225) < 1e-12);
});
