/**
 * Default price table, USD per 1M tokens, standard (non-batch, short-context) tier.
 *
 * Provider prices change often; check them before relying on a cap in production.
 * Values were last checked on 2026-10-17 against:
 *   - OpenAI:    https://developers.openai.com/api/docs/pricing
 *   - Anthropic: https://docs.anthropic.com/en/docs/about-claude/pricing
 * Anthropic IDs for the current generation were checked against the models overview page (2026-10-17).
 * Claude Haiku 5.5 is priced at its prompts-up-to-100K tier.
 * Long-context, batch, regional/data-residency uplifts and fast/priority tiers are NOT modeled.
 * Override or extend via `createGuard({ prices: { ...DEFAULT_PRICES, ...yours } })`.
 */
export interface ModelPrice {
  /** USD per 1M uncached input tokens */
  inputPerMTok: number;
  /** USD per 1M output tokens */
  outputPerMTok: number;
  /** USD per 1M cached input tokens (defaults to inputPerMTok if omitted) */
  cachedInputPerMTok?: number;
  /** Where the number came from */
  source?: string;
}

export type PriceTable = Record<string, ModelPrice>;

const OPENAI = "https://developers.openai.com/api/docs/pricing (checked 2026-10-17, verify for production)";
const ANTHROPIC = "https://docs.anthropic.com/en/docs/about-claude/pricing (checked 2026-10-17, verify for production)";

export const DEFAULT_PRICES: PriceTable = {
  "gpt-6-astra": { inputPerMTok: 10.0, cachedInputPerMTok: 1.0, outputPerMTok: 50.0, source: OPENAI },
  "gpt-6-sol": { inputPerMTok: 2.0, cachedInputPerMTok: 0.2, outputPerMTok: 10.0, source: OPENAI },
  "gpt-6.1-sol": { inputPerMTok: 2.0, cachedInputPerMTok: 0.1, outputPerMTok: 10.0, source: OPENAI },
  "gpt-6-luna": { inputPerMTok: 0.1, cachedInputPerMTok: 0.01, outputPerMTok: 0.5, source: OPENAI },
  "gpt-5.6-sol": { inputPerMTok: 4.0, cachedInputPerMTok: 0.4, outputPerMTok: 20.0, source: OPENAI },
  "gpt-5.6-terra": { inputPerMTok: 2.0, cachedInputPerMTok: 0.2, outputPerMTok: 12.0, source: OPENAI },
  "gpt-5.6-luna": { inputPerMTok: 0.2, cachedInputPerMTok: 0.02, outputPerMTok: 1.2, source: OPENAI },
  "gpt-5.5": { inputPerMTok: 5.0, cachedInputPerMTok: 0.5, outputPerMTok: 30.0, source: OPENAI },
  "gpt-5.4": { inputPerMTok: 2.5, cachedInputPerMTok: 0.25, outputPerMTok: 15.0, source: OPENAI },
  "gpt-5.4-mini": { inputPerMTok: 0.75, cachedInputPerMTok: 0.075, outputPerMTok: 4.5, source: OPENAI },
  "gpt-5.4-nano": { inputPerMTok: 0.2, cachedInputPerMTok: 0.02, outputPerMTok: 1.25, source: OPENAI },
  "gpt-4.1-mini": { inputPerMTok: 0.4, cachedInputPerMTok: 0.1, outputPerMTok: 1.6, source: OPENAI },
  "gpt-4o-mini": { inputPerMTok: 0.15, cachedInputPerMTok: 0.075, outputPerMTok: 0.6, source: OPENAI },
  "claude-fable-5-1": { inputPerMTok: 10.0, cachedInputPerMTok: 0.25, outputPerMTok: 50.0, source: ANTHROPIC },
  "claude-opus-5-5": { inputPerMTok: 4.0, cachedInputPerMTok: 0.2, outputPerMTok: 20.0, source: ANTHROPIC },
  "claude-sonnet-5-5": { inputPerMTok: 2.0, cachedInputPerMTok: 0.1, outputPerMTok: 10.0, source: ANTHROPIC },
  "claude-haiku-5-5": { inputPerMTok: 0.1, cachedInputPerMTok: 0.01, outputPerMTok: 0.5, source: ANTHROPIC },
  "claude-opus-4-8": { inputPerMTok: 5.0, cachedInputPerMTok: 0.5, outputPerMTok: 25.0, source: ANTHROPIC },
  "claude-sonnet-5": { inputPerMTok: 2.0, cachedInputPerMTok: 0.2, outputPerMTok: 10.0, source: ANTHROPIC },
  "claude-haiku-4-5": { inputPerMTok: 1.0, cachedInputPerMTok: 0.1, outputPerMTok: 5.0, source: ANTHROPIC },
};

/** OpenAI-compatible usage object (chat.completions). */
export interface ChatUsage {
  prompt_tokens?: number;
  completion_tokens?: number;
  prompt_tokens_details?: { cached_tokens?: number } | null;
}

/** Returns USD cost, or null if the model is not in the table. */
export function computeCostUsd(model: string, usage: ChatUsage, prices: PriceTable): number | null {
  const p = lookupPrice(model, prices);
  if (!p) return null;
  const prompt = usage.prompt_tokens ?? 0;
  const cached = Math.min(usage.prompt_tokens_details?.cached_tokens ?? 0, prompt);
  const uncached = prompt - cached;
  const out = usage.completion_tokens ?? 0;
  const cachedRate = p.cachedInputPerMTok ?? p.inputPerMTok;
  return (uncached * p.inputPerMTok + cached * cachedRate + out * p.outputPerMTok) / 1_000_000;
}

/** Exact match first, then strips a trailing date snapshot like "-2025-04-14". */
export function lookupPrice(model: string, prices: PriceTable): ModelPrice | undefined {
  if (prices[model]) return prices[model];
  const base = model.replace(/-\d{4}-\d{2}-\d{2}$/, "");
  return prices[base];
}
