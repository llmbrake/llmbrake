import { test } from "node:test";
import assert from "node:assert/strict";
import { BaseChatModel } from "@langchain/core/language_models/chat_models";
import { AIMessage, type BaseMessage } from "@langchain/core/messages";
import type { ChatResult } from "@langchain/core/outputs";
import { isBudgetExceededError } from "llmbrake";
import { createLangChainBudget, toChatUsage } from "./budget.ts";

/** A real LangChain chat model subclass that returns a fixed reply and OpenAI-like usage_metadata. */
class FakeUsageChatModel extends BaseChatModel {
  constructor(public modelId: string, private calls: string[]) { super({}); }
  _llmType() { return "fake-usage"; }
  async _generate(_messages: BaseMessage[]): Promise<ChatResult> {
    this.calls.push(this.modelId);
    const message = new AIMessage({
      content: "ok",
      usage_metadata: { input_tokens: 3000, output_tokens: 600, total_tokens: 3600, input_token_details: { cache_read: 1000 } },
    });
    return { generations: [{ text: "ok", message }] };
  }
}

test("toChatUsage maps usage_metadata", () => {
  assert.deepEqual(toChatUsage({ input_tokens: 10, output_tokens: 5, total_tokens: 15, input_token_details: { cache_read: 4 } }),
    { prompt_tokens: 10, completion_tokens: 5, prompt_tokens_details: { cached_tokens: 4 } });
  assert.deepEqual(toChatUsage(undefined), { prompt_tokens: 0, completion_tokens: 0, prompt_tokens_details: { cached_tokens: 0 } });
});

test("LangChain invoke: charges, downgrades near the cap, blocks without calling the model", async () => {
  const calls: string[] = [];
  const b = createLangChainBudget({
    budgetUsd: 0.06, downgradeAt: 0.8,
    downgradeMap: { "gpt-5.5": "gpt-5.4-mini" },
    getModel: (id) => new FakeUsageChatModel(id, calls),
  });
  const ctx = { customerId: "lc_1", feature: "summarize" };

  const r1 = await b.budgetedInvoke(ctx, "gpt-5.5", "hello");
  assert.equal(r1.message.content, "ok");
  assert.ok(Math.abs(r1.costUsd! - 0.0285) < 1e-12);

  await b.budgetedInvoke(ctx, "gpt-5.5", "hello"); // spent 0.057 after this
  const r3 = await b.budgetedInvoke(ctx, "gpt-5.5", "hello"); // 0.057 >= 0.048 → mini
  assert.equal(r3.model, "gpt-5.4-mini");
  assert.equal(r3.downgraded, true);

  const r4 = b.budgetedInvoke(ctx, "gpt-5.5", "hello"); // 0.061275 >= 0.06 → blocked
  await assert.rejects(r4, (e) => isBudgetExceededError(e));
  assert.deepEqual(calls, ["gpt-5.5", "gpt-5.5", "gpt-5.4-mini"]);
});
