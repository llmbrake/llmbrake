/**
 * Per-customer budgets for OpenRouter. OpenRouter is OpenAI-compatible, so
 * llmbrake's `guard.wrap()` works directly on the OpenAI SDK pointed at it.
 *
 * OpenRouter model IDs are namespaced (e.g. "openai/gpt-5.5"), so add them to
 * the price table. The prices below mirror OpenAI's list prices read on
 * 2026-10-09; check https://openrouter.ai/models before relying on them.
 */
import OpenAI from "openai";
import { createGuard, DEFAULT_PRICES, type PriceTable, type UsageStore, type UsageEvent } from "llmbrake";

export const OPENROUTER_PRICES: PriceTable = {
  ...DEFAULT_PRICES,
  "openai/gpt-5.5": { inputPerMTok: 5, cachedInputPerMTok: 0.5, outputPerMTok: 30, source: "openrouter.ai/models (verify)" },
  "openai/gpt-5.4-mini": { inputPerMTok: 0.75, cachedInputPerMTok: 0.075, outputPerMTok: 4.5, source: "openrouter.ai/models (verify)" },
};

/** The one method llmbrake needs; OpenAI's `client.chat.completions` satisfies it. */
export interface ChatCompletions {
  create(params: any, options?: any): Promise<any>;
}

export function openRouterClient(apiKey = process.env.OPENROUTER_API_KEY) {
  return new OpenAI({ baseURL: "https://openrouter.ai/api/v1", apiKey });
}

export function createOpenRouterBudget(opts: {
  completions: ChatCompletions;
  budgetUsd: number | ((ctx: { customerId: string; plan?: string }) => number);
  store?: UsageStore;
  onUsage?: (e: UsageEvent) => void;
}) {
  const guard = createGuard({
    budgetUsd: opts.budgetUsd,
    store: opts.store,
    prices: OPENROUTER_PRICES,
    downgradeAt: 0.8,
    downgradeMap: { "openai/gpt-5.5": "openai/gpt-5.4-mini" },
    onUsage: opts.onUsage,
  });
  const create = guard.wrap(opts.completions.create.bind(opts.completions));
  return { create, guard };
}

// Usage:
//   const client = openRouterClient();
//   const { create } = createOpenRouterBudget({ completions: client.chat.completions, budgetUsd: 5 });
//   const res = await create({ model: "openai/gpt-5.5", messages }, { customerId: user.id });
//
// OpenRouter also returns `usage.cost` (the USD it actually charged). llmbrake 0.1.0
// prices from tokens and the table above; it doesn't read `usage.cost` yet.
