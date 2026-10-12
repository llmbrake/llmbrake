/**
 * Per-customer budgets with LangChain.js chat models.
 *
 * Pattern: guard.check() → model.invoke() → charge from AIMessage.usage_metadata.
 * For OpenAI streaming in LangChain, set `streamUsage: true` (the default for ChatOpenAI
 * in recent versions) or usage may be missing and the call goes uncharged.
 */
import type { BaseChatModel } from "@langchain/core/language_models/chat_models";
import type { BaseLanguageModelInput } from "@langchain/core/language_models/base";
import type { AIMessage, UsageMetadata } from "@langchain/core/messages";
import { ChatOpenAI } from "@langchain/openai";
import {
  createGuard,
  computeCostUsd,
  DEFAULT_PRICES,
  MemoryStore,
  type CallContext,
  type PriceTable,
  type UsageStore,
} from "llmbrake";

/** Map LangChain usage_metadata (input includes cached reads) to the OpenAI-style usage llmbrake prices. */
export function toChatUsage(u: UsageMetadata | undefined) {
  return {
    prompt_tokens: u?.input_tokens ?? 0,
    completion_tokens: u?.output_tokens ?? 0,
    prompt_tokens_details: { cached_tokens: u?.input_token_details?.cache_read ?? 0 },
  };
}

export function createLangChainBudget(opts: {
  budgetUsd: number | ((ctx: CallContext) => number | Promise<number>);
  store?: UsageStore;
  prices?: PriceTable;
  downgradeAt?: number;
  downgradeMap?: Record<string, string>;
  /** Builds a chat model for a model id. Defaults to ChatOpenAI. */
  getModel?: (modelId: string) => BaseChatModel;
}) {
  const store = opts.store ?? new MemoryStore();
  const prices = opts.prices ?? DEFAULT_PRICES;
  const getModel = opts.getModel ?? ((model: string) => new ChatOpenAI({ model }));
  const guard = createGuard({ budgetUsd: opts.budgetUsd, store, prices, downgradeAt: opts.downgradeAt, downgradeMap: opts.downgradeMap });

  /** invoke() with a budget. Throws BudgetExceededError (no model call) at the cap. */
  async function budgetedInvoke(ctx: CallContext, model: string, input: BaseLanguageModelInput) {
    const d = await guard.check(ctx, model);
    const message = (await getModel(d.model).invoke(input)) as AIMessage;
    const costUsd = computeCostUsd(d.model, toChatUsage(message.usage_metadata), prices);
    if (costUsd && costUsd > 0) await store.addSpend(ctx.customerId, d.period, costUsd);
    return { message, model: d.model, downgraded: d.downgraded, costUsd };
  }

  return { guard, budgetedInvoke, status: guard.status };
}
