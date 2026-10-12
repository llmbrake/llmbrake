/**
 * Per-customer monthly budgets with the Vercel AI SDK (ai@7).
 *
 * The AI SDK doesn't call `chat.completions.create`, so `guard.wrap()` doesn't apply.
 * Instead: `guard.check()` before the call (it picks the downgraded model or throws
 * BudgetExceededError), then `charge()` from the call's `usage` afterwards.
 */
import { generateText, streamText, type LanguageModel, type LanguageModelUsage, type ModelMessage } from "ai";
import { openai } from "@ai-sdk/openai";
import {
  createGuard,
  computeCostUsd,
  DEFAULT_PRICES,
  MemoryStore,
  type CallContext,
  type PriceTable,
  type UsageStore,
} from "llmbrake";

export interface AiSdkBudgetOptions {
  /** USD per customer per calendar month (UTC): a number or a lookup by plan. */
  budgetUsd: number | ((ctx: CallContext) => number | Promise<number>);
  /** Use a shared store (e.g. Redis) in production; MemoryStore is per process. */
  store?: UsageStore;
  prices?: PriceTable;
  downgradeAt?: number;
  downgradeMap?: Record<string, string>;
  /** Turns a model id into an AI SDK model. Defaults to the OpenAI provider. */
  getModel?: (modelId: string) => LanguageModel;
}

/** Map AI SDK usage (total input incl. cached, cached reads, output) to the OpenAI-style shape llmbrake prices. */
export function toChatUsage(usage: LanguageModelUsage) {
  return {
    prompt_tokens: usage.inputTokens ?? 0,
    completion_tokens: usage.outputTokens ?? 0,
    prompt_tokens_details: { cached_tokens: usage.inputTokenDetails?.cacheReadTokens ?? 0 },
  };
}

export function createAiSdkBudget(opts: AiSdkBudgetOptions) {
  const store = opts.store ?? new MemoryStore();
  const prices = opts.prices ?? DEFAULT_PRICES;
  const getModel = opts.getModel ?? ((id: string) => openai(id));
  const guard = createGuard({
    budgetUsd: opts.budgetUsd,
    store,
    prices,
    downgradeAt: opts.downgradeAt,
    downgradeMap: opts.downgradeMap,
  });

  type Decision = Awaited<ReturnType<typeof guard.check>>;

  /** Record the cost of a finished call against the customer's monthly total. Returns the USD cost (null if unpriced). */
  async function charge(ctx: CallContext, d: Decision, usage: LanguageModelUsage) {
    const cost = computeCostUsd(d.model, toChatUsage(usage), prices);
    if (cost && cost > 0) await store.addSpend(ctx.customerId, d.period, cost);
    return cost;
  }

  /** generateText with a budget. Throws BudgetExceededError (never calls the model) at the cap. */
  async function budgetedGenerateText(ctx: CallContext, model: string, input: { prompt: string } | { messages: ModelMessage[] }) {
    const d = await guard.check(ctx, model);
    const result = await generateText({ model: getModel(d.model), ...input });
    const costUsd = await charge(ctx, d, result.usage); // in ai@7, `usage` covers all steps
    return { result, model: d.model, downgraded: d.downgraded, costUsd };
  }

  /**
   * streamText with a budget. The cost is recorded in onFinish, once usage is known.
   * Note: if the client aborts, onFinish may not run and the call goes uncharged.
   */
  async function budgetedStreamText(ctx: CallContext, model: string, input: { prompt: string } | { messages: ModelMessage[] }, onCharged?: (costUsd: number | null) => void) {
    const d = await guard.check(ctx, model);
    const result = streamText({
      model: getModel(d.model),
      ...input,
      onFinish: async ({ usage }) => {
        const cost = await charge(ctx, d, usage);
        onCharged?.(cost);
      },
    });
    return { result, model: d.model, downgraded: d.downgraded };
  }

  return { guard, charge, budgetedGenerateText, budgetedStreamText, status: guard.status };
}
