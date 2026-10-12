import { test } from "node:test";
import assert from "node:assert/strict";
import { MockLanguageModelV4, convertArrayToReadableStream } from "ai/test";
import { isBudgetExceededError, MemoryStore } from "llmbrake";
import { createAiSdkBudget, toChatUsage } from "./budget.ts";

// 2,000 uncached + 1,000 cached input tokens, 600 output tokens per call.
const usage = {
  inputTokens: { total: 3000, noCache: 2000, cacheRead: 1000, cacheWrite: 0 },
  outputTokens: { total: 600, text: 600, reasoning: 0 },
};
const finish = { unified: "stop" as const, raw: "stop" };

function mockModel(id: string, calls: string[]) {
  return new MockLanguageModelV4({
    modelId: id,
    doGenerate: async () => {
      calls.push(id);
      return { content: [{ type: "text", text: "ok" }], finishReason: finish, usage, warnings: [] };
    },
    doStream: async () => {
      calls.push(id);
      return {
        stream: convertArrayToReadableStream([
          { type: "stream-start", warnings: [] },
          { type: "text-start", id: "1" },
          { type: "text-delta", id: "1", delta: "ok" },
          { type: "text-end", id: "1" },
          { type: "finish", finishReason: finish, usage },
        ]),
      };
    },
  });
}

// gpt-5.5 at $5 / $0.50 cached / $30 per 1M: 2000*5 + 1000*0.5 + 600*30 = 28,500 / 1M = $0.0285
const GPT55_COST = 0.0285;
// gpt-5.4-mini at $0.75 / $0.075 / $4.50: 1500 + 75 + 2700 = 4,275 / 1M = $0.004275
const MINI_COST = 0.004275;

test("toChatUsage maps AI SDK usage to OpenAI-style usage", () => {
  const u = toChatUsage({
    inputTokens: 3000, outputTokens: 600, totalTokens: 3600,
    inputTokenDetails: { noCacheTokens: 2000, cacheReadTokens: 1000, cacheWriteTokens: 0 },
    outputTokenDetails: { textTokens: 600, reasoningTokens: 0 },
  });
  assert.deepEqual(u, { prompt_tokens: 3000, completion_tokens: 600, prompt_tokens_details: { cached_tokens: 1000 } });
});

test("generateText: charges cached-token-aware cost, downgrades near the cap, blocks at the cap", async () => {
  const calls: string[] = [];
  const store = new MemoryStore();
  const b = createAiSdkBudget({
    budgetUsd: 0.07, store, downgradeAt: 0.8,
    downgradeMap: { "gpt-5.5": "gpt-5.4-mini" },
    getModel: (id) => mockModel(id, calls),
  });
  const ctx = { customerId: "cus_1" };

  const r1 = await b.budgetedGenerateText(ctx, "gpt-5.5", { prompt: "hi" });
  assert.equal(r1.result.text, "ok");
  assert.equal(r1.model, "gpt-5.5");
  assert.ok(Math.abs(r1.costUsd! - GPT55_COST) < 1e-12);

  const r2 = await b.budgetedGenerateText(ctx, "gpt-5.5", { prompt: "hi" }); // spent 0.0285 < 0.056
  assert.equal(r2.downgraded, false);

  const r3 = await b.budgetedGenerateText(ctx, "gpt-5.5", { prompt: "hi" }); // spent 0.057 >= 0.056 → mini
  assert.equal(r3.model, "gpt-5.4-mini");
  assert.equal(r3.downgraded, true);
  assert.ok(Math.abs(r3.costUsd! - MINI_COST) < 1e-12);

  // keep going until the cap; the provider must not be called once blocked
  let blocked = 0;
  for (let i = 0; i < 10; i++) {
    try { await b.budgetedGenerateText(ctx, "gpt-5.5", { prompt: "hi" }); }
    catch (e) { assert.ok(isBudgetExceededError(e)); blocked++; }
  }
  assert.ok(blocked > 0);
  const callsBefore = calls.length;
  await assert.rejects(b.budgetedGenerateText(ctx, "gpt-5.5", { prompt: "hi" }), (e) => isBudgetExceededError(e));
  assert.equal(calls.length, callsBefore, "blocked call must not reach the model");

  const s = await b.status(ctx);
  assert.equal(s.remainingUsd, 0);
});

test("streamText: charges in onFinish after the stream completes", async () => {
  const calls: string[] = [];
  const b = createAiSdkBudget({ budgetUsd: 1, getModel: (id) => mockModel(id, calls) });
  const ctx = { customerId: "cus_stream" };
  let charged: number | null | undefined;
  const { result } = await b.budgetedStreamText(ctx, "gpt-5.5", { prompt: "hi" }, (c) => { charged = c; });
  let text = "";
  for await (const t of result.textStream) text += t;
  await result.usage; // wait for completion (onFinish has run)
  assert.equal(text, "ok");
  assert.ok(Math.abs((charged ?? 0) - GPT55_COST) < 1e-12);
  const s = await b.status(ctx);
  assert.ok(Math.abs(s.spentUsd - GPT55_COST) < 1e-12);
});

test("unknown models are rejected before the call (default unknownModel: throw)", async () => {
  const calls: string[] = [];
  const b = createAiSdkBudget({ budgetUsd: 1, getModel: (id) => mockModel(id, calls) });
  await assert.rejects(b.budgetedGenerateText({ customerId: "c" }, "not-a-real-model", { prompt: "hi" }), /not-a-real-model/);
  assert.equal(calls.length, 0);
});
